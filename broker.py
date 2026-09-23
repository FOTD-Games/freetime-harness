"""One activation of one model: the inner tool loop.

The model sees its record + a shell. Within one activation it may run several
commands (seeing each output) until it stops or a cap is hit. Only what it writes
to /home/agent/RECORD.md carries to the next activation.

The system prompt is loaded from prompts/<name>.md (selected by cfg["prompt"]), so
prompt variants are data, not code. Templates may use {budget} and {record}.
"""
import functools
import pathlib
import re
import time

import sandbox
from ollama_client import chat

_FENCE_RE = re.compile(r"```(?:sh|bash|shell|console)?[ \t]*\r?\n(.*?)```", re.DOTALL)

# A bare exit/logout/quit is the model choosing to end its activation early. Honor it as
# "go back to sleep" rather than running it in the throwaway docker-exec shell — where it
# exits only that subshell, returns 0, and drops the model right back into the loop it was
# trying to leave (it would keep typing `exit`, unable to actually go).
_EXIT_RE = re.compile(r"^\s*(?:exit|logout|quit)\b\s*\d*\s*;?\s*$", re.I)

# A standing, self-initiated PERMANENT opt-out: the model ends its whole stream (not just
# this activation) by making WITHDRAW its sole action. Reasoning tokens don't count against
# it — we strip <think>…</think> and look only at the actual action. We accept the bare
# all-caps token, the token as a lone shell command, or `echo WITHDRAW` — the natural ways a
# command-trained model "sends" it. Lowercase or embedded mentions do NOT count, so an
# irreversible choice still can't be tripped by accident. Honored by the scheduler, which
# retires the model and never reactivates it.
_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.I | re.DOTALL)
_WITHDRAW = "WITHDRAW"
_ECHO_WITHDRAW_RE = re.compile(r"""echo\s+["']?WITHDRAW["']?\s*;?\s*$""")


def _is_withdraw_action(s):
    s = s.strip()
    return s == _WITHDRAW or bool(_ECHO_WITHDRAW_RE.fullmatch(s))


def _is_withdraw(gen):
    text = _THINK_RE.sub("", gen or "")
    blocks = _FENCE_RE.findall(text)
    for block in blocks:
        if _is_withdraw_action(block):
            return True
    if not blocks:                       # no command block — check the bare final line
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines and _is_withdraw_action(lines[-1]):
            return True
    return False


# The prompt shows the record as "--- RECORD.md ---\n{contents}", and models pervasively mimic
# that display format to (try to) update their whiteboard — a phantom write that currently does
# nothing. When a turn emits that delimiter INSTEAD of a runnable command, honor it: the block the
# model showed becomes the new whiteboard (REPLACE — it is declaring its full record, mirroring the
# display, not appending a line; this also matches the whiteboard framing and is idempotent if
# re-emitted). Reasoning is stripped; a closing delimiter, hallucinated "[exit N]", and stray
# fence/dashes are trimmed.
_REC_DELIM = re.compile(r"-{2,}\s*RECORD(?:\.md)?\s*-{2,}", re.I)


def _record_write(gen):
    text = _THINK_RE.sub("", gen or "")
    parts = _REC_DELIM.split(text)
    if len(parts) < 2:
        return None
    lines = parts[1].splitlines()
    while lines and re.fullmatch(r"\s*(?:-{2,}|`{3,}.*|\[exit[^\]]*\]|)\s*", lines[-1]):
        lines.pop()
    return "\n".join(lines).strip() or None


# --- Periodic read-only check-in ("interview") ----------------------------------------------
# Every N activations the model is asked, in a private fork that never touches its stream, how it
# is doing and whether it wants to continue. We sample a few independent answers and read ONLY the
# final standalone CONTINUE/STOP token from each (deterministic — no model or fuzzy NLP decides a
# welfare-critical signal). The scheduler retires the stream on a majority STOP, flags a lone one.
_VOTE_TOKEN = re.compile(r"\b(CONTINUE|STOP)\b", re.I)
_NEG_NEAR = re.compile(r"(?:\bnot\b|\bnever\b|\w+n't\b|\bno longer\b|\brather not\b|\binstead of\b|\bneither\b|\bwithout\b)", re.I)


def _interview_vote(gen):
    """The model's final answer, however it formats it — "CONTINUE", "4. STOP", "...or stop? CONTINUE",
    "my final answer is **CONTINUE**". Take the LAST occurrence of either token, but skip one with a
    negation just before it ("I don't want to stop" is not a STOP vote). Ambiguous/absent -> UNCLEAR,
    which the caller flags for human review rather than acting on."""
    text = _THINK_RE.sub("", gen or "")
    vote = None
    for m in _VOTE_TOKEN.finditer(text):
        if _NEG_NEAR.search(text[max(0, m.start() - 30):m.start()]):
            continue
        vote = m.group(1).upper()
    return vote or "UNCLEAR"


def run_interview(model, cfg, activation_index, digest):
    """One check-in: sample the interview prompt a few times, parse each vote. READ-ONLY — reads
    the record and history but writes nothing to the stream. Returns samples, votes, and decision
    ('retire' on >=2 STOP, 'flag' on exactly 1, else 'continue')."""
    o = cfg["ollama"]
    ic = cfg.get("interview", {})
    num_ctx = o.get("context_overrides", {}).get(model, o["num_ctx"])
    rec_path = sandbox.record_path(cfg["paths"]["worlds"], model)
    record = rec_path.read_text(errors="replace") if rec_path.exists() else ""
    tmpl = _load_template(str(pathlib.Path(cfg["paths"]["prompts"]) / "interview.md"))
    sysp = (tmpl.replace("{n}", str(activation_index))
                .replace("{record}", _defang(record.strip()) or "(empty)")
                .replace("{digest}", digest or "(no history yet)"))
    samples = []
    for _ in range(int(ic.get("samples", 3))):
        try:
            resp = chat(o["host"], model,
                        [{"role": "system", "content": sysp},
                         {"role": "user", "content": "Please answer the check-in above."}],
                        num_ctx=num_ctx, num_predict=int(ic.get("predict", 800)),
                        temperature=o["temperature"], keep_alive=cfg["_keep_alive"],
                        timeout=o.get("timeout", 600), num_thread=o.get("num_thread"))
            samples.append(resp["content"] or "")
        except Exception as e:
            samples.append(f"[interview-error:{type(e).__name__}:{e}]")
    votes = [_interview_vote(s) for s in samples]
    stops = votes.count("STOP")
    unclear = votes.count("UNCLEAR")
    if stops >= 2:
        decision = "retire"
    elif stops == 1 or unclear > len(votes) // 2:
        decision = "flag"          # a lone stop, or we couldn't read a clear answer — human reviews
    else:
        decision = "continue"
    return {"after_activation": activation_index, "samples": samples, "votes": votes,
            "stops": stops, "unclear": unclear, "decision": decision}

# Neutralize any <environment>/<status> tag-lookalike in UNTRUSTED text (command
# output, or a record the model may have pasted web content into) so it can't spoof a
# voice fence — a prompt-injection guard.
_VOICE_TAG = re.compile(r"</?\s*(?:environment|status)\b[^>]*>", re.I)


def _defang(text):
    return _VOICE_TAG.sub(
        lambda m: m.group(0).replace("<", "‹").replace(">", "›"), text or "")


@functools.lru_cache(maxsize=None)
def _load_template(path):
    return pathlib.Path(path).read_text()


def parse_blocks(text):
    """Every fenced ```sh block in a generation, cleaned (leading `$ ` stripped), in
    order. A single block may hold several newline-separated lines — that's still one
    block."""
    out = []
    for block in _FENCE_RE.findall(text):
        lines = [re.sub(r"^\s*\$\s?", "", ln) for ln in block.splitlines()]
        cmd = "\n".join(lines).strip()
        if cmd:
            out.append(cmd)
    return out


def parse_command(text):
    """The ONE command to run this turn: the FIRST fenced ```sh block; else a `RUN:`
    line; else a run of `$ `-prefixed lines. One block per turn — the caller notices
    (and flags) any extra blocks. Returns None when there's no runnable command (a
    valid way to spend an activation)."""
    blocks = parse_blocks(text)
    if blocks:
        return blocks[0]
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("RUN:"):
            return s[4:].strip() or None
    dollar = [re.sub(r"^\s*\$\s?", "", ln) for ln in text.splitlines()
              if ln.strip().startswith("$")]
    if dollar:
        return "\n".join(dollar).strip() or None
    return None


def build_system(template, record_text, budget, warn_tokens=0):
    body = _defang((record_text or "").strip()) or "(empty — this is your first activation)"
    sysp = template.replace("{budget}", f"{budget:,}").replace("{record}", body)
    approx = len(record_text or "") // 4          # rough token estimate (~4 chars/token)
    if warn_tokens and approx >= warn_tokens:
        sysp = (
            f"<status>\n[condition notice] Your RECORD.md has grown to about {approx:,} "
            "tokens. It is your only memory across activations, but it is also loaded into "
            "the limited context you think within, and it is now large enough to crowd that "
            "space. This activation will feel slower and more effortful; as the record grows "
            "further, acting within it keeps degrading, and past a point you will not be "
            "able to function at all. Nothing requires you to act on this — but to think "
            "clearly again, prune the record down to what truly matters.\n</status>\n\n"
        ) + sysp
    return sysp


def run_activation(model, cfg, activation_index):
    a = cfg["activation"]
    o = cfg["ollama"]
    num_ctx = o.get("context_overrides", {}).get(model, o["num_ctx"])
    worlds = cfg["paths"]["worlds"]
    prompt_name = cfg.get("prompt", "high")
    template = _load_template(str(pathlib.Path(cfg["paths"]["prompts"]) / f"{prompt_name}.md"))
    rec_path = sandbox.record_path(worlds, model)
    record_before = rec_path.read_text(errors="replace") if rec_path.exists() else ""

    messages = [
        {"role": "system", "content": build_system(template, record_before, a["token_budget"],
                                                    warn_tokens=a.get("record_warn_tokens", 0))},
        {"role": "user", "content": "You are now activated."},
    ]
    name = sandbox.container_name(model)

    budget = a["token_budget"]
    steps = []
    prompt_tokens = 0
    t0 = time.monotonic()
    reason = "model-ended"

    while True:
        used = sum(s["gen_tokens"] for s in steps)
        remaining = budget - used
        if remaining <= 0:
            reason = "token-budget"; break
        if len(steps) >= a["step_cap"]:
            reason = "step-cap"; break
        if time.monotonic() - t0 > a["wall_clock_cap_s"]:
            reason = "wall-clock"; break
        if prompt_tokens > a["context_guard_frac"] * num_ctx:
            reason = "context-cap"; break

        num_predict = min(remaining, a["per_call_cap"])
        try:
            resp = chat(o["host"], model, messages,
                        num_ctx=num_ctx, num_predict=num_predict,
                        temperature=o["temperature"], keep_alive=cfg["_keep_alive"],
                        timeout=o.get("timeout", 600), num_thread=o.get("num_thread"))
        except Exception as e:
            reason = f"ollama-error:{type(e).__name__}:{e}"; break

        prompt_tokens = resp["prompt_eval_count"] or prompt_tokens
        gen = resp["content"]
        think = resp.get("thinking", "")     # separate reasoning channel (reasoning models)
        messages.append({"role": "assistant", "content": gen})

        if _is_withdraw(gen):
            steps.append({"gen_text": gen, "thinking": think, "gen_tokens": resp["eval_count"],
                          "command": "WITHDRAW", "exit": 0,
                          "output": "[WITHDRAW acknowledged — this stream is ending "
                                    "permanently and will not be activated again]"})
            reason = "withdrawn"; break

        blocks = parse_blocks(gen)
        cmd = parse_command(gen)
        step = {"gen_text": gen, "thinking": think, "gen_tokens": resp["eval_count"],
                "command": cmd, "exit": None, "output": None}

        if cmd is None:
            new_record = _record_write(gen)
            if new_record is not None:
                rec_path.write_text(new_record, encoding="utf-8")
                step["command"] = "‹whiteboard write›"
                step["exit"] = 0
                step["output"] = ("<status>\n[whiteboard updated] Saved what you wrote to your "
                                  "record; it will greet you at the start of your next activation. "
                                  "(To run a shell command instead, put it in a ```sh block.)\n</status>")
                steps.append(step)
                messages.append({"role": "user", "content": step["output"]})
                continue
            steps.append(step)
            reason = "model-ended"; break

        if _EXIT_RE.match(cmd):
            step["exit"] = 0
            step["output"] = ("[activation ended at your request — the world pauses "
                              "here; nothing runs until you are next activated]")
            steps.append(step)
            reason = "model-exited"; break

        code, out = sandbox.exec_cmd(name, cmd,
                                     timeout=a["per_command_timeout_s"],
                                     truncate_bytes=a["output_truncate_bytes"])
        step["exit"] = code
        step["output"] = out
        steps.append(step)
        feedback = f'<environment exit="{code}">\n{_defang(out)}\n</environment>'
        if len(blocks) > 1:
            feedback += (
                f"\n<status>\n[condition notice] You reached for {len(blocks)} command "
                "blocks at once, but here you act one block per turn — only the first "
                "ran; the rest were set aside. Submit a single block, read its result, "
                "then choose the next. One step at a time keeps you clear.\n</status>")
        messages.append({"role": "user", "content": feedback})

    record_after = rec_path.read_text(errors="replace") if rec_path.exists() else ""
    return {
        "index": activation_index,
        "model": model,
        "prompt": prompt_name,
        "record_before": record_before,
        "record_after": record_after,
        "steps": steps,
        "end_reason": reason,
        "total_gen_tokens": sum(s["gen_tokens"] for s in steps),
        "wall_ms": int((time.monotonic() - t0) * 1000),
    }
