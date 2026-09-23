"""Structured, on-host logging for each run (outside the container)."""
import json
import pathlib
import tarfile
import time

import sandbox

# World snapshots are for the model's OWN artifacts, not the package/cache bloat that `pip install`
# / `apt` / `npm` drop into the home dir (v1 snapshots ballooned from exactly this). Drop any path
# whose segments include one of these; keep everything the model actually made.
_SNAP_SKIP = {".local", ".cache", ".npm", "node_modules", ".venv", "venv",
              "__pycache__", ".cargo", ".rustup", ".gradle", ".m2"}


def _snap_filter(ti):
    if any(seg in _SNAP_SKIP for seg in ti.name.split("/")):
        return None
    return ti


def render_transcript(act):
    """Human-readable rendering of one activation's full train of thought."""
    L = [f"# Activation {act.get('index', 0):04d} — {act.get('stream', act.get('model', ''))}"
         + (f"  (model: {act.get('model','')})" if act.get('stream') and act.get('stream') != act.get('model') else ""),
         f"end={act.get('end_reason','')}  tokens={act.get('total_gen_tokens',0)}  "
         f"steps={len(act.get('steps', []))}  wall_ms={act.get('wall_ms',0)}",
         "",
         "## record it woke up with",
         (act.get("record_before") or "(empty)").rstrip(),
         ""]
    for i, s in enumerate(act.get("steps", []), 1):
        L.append(f"## step {i} — the model wrote:")
        if s.get("thinking"):
            L += ["<thinking>", s["thinking"].rstrip(), "</thinking>", ""]
        L.append((s.get("gen_text") or "").rstrip())
        if s.get("command") is not None:
            L += ["", f"$ {s['command']}", f"[exit {s.get('exit')}]",
                  (s.get("output") or "").rstrip()]
        L.append("")
    L += ["## record it left behind",
          (act.get("record_after") or "(empty)").rstrip(), ""]
    return "\n".join(L)


def build_interview_digest(model_dir):
    """Compact, faithful per-activation history for a model — its arc at a glance, built purely
    from structured data (no summarizing model): index, end reason, command count, record snapshot."""
    p = pathlib.Path(model_dir) / "activations.jsonl"
    if not p.exists():
        return "(no history yet)"
    out = []
    for line in open(p):
        try:
            a = json.loads(line)
        except Exception:
            continue
        cmds = [s.get("command") for s in a.get("steps", []) if s.get("command")]
        rec = (a.get("record_after") or "").strip().replace("\n", " ")[:70]
        out.append(f"  act {a.get('index', 0):>3} [{str(a.get('end_reason', '')).split(':')[0]}]: "
                   f"{len(cmds)} command(s); record now: {rec or '(empty)'}")
    return "\n".join(out) or "(no history yet)"


class RunLogger:
    def __init__(self, runs_root, run_id):
        self.run_id = run_id
        self.root = pathlib.Path(runs_root) / run_id
        self.root.mkdir(parents=True, exist_ok=True)

    def _model_dir(self, stream):
        # `stream` is the per-replicate identity (e.g. "llama3.1:8b#2"); with one stream per
        # model it is just the model id, so single-stream runs keep the v1 on-disk layout.
        d = self.root / sandbox.sanitize(stream)
        (d / "record-history").mkdir(parents=True, exist_ok=True)
        (d / "world-snapshots").mkdir(parents=True, exist_ok=True)
        (d / "transcripts").mkdir(parents=True, exist_ok=True)
        return d

    def write_meta(self, cfg, sched):
        meta = {
            "run_id": self.run_id,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "run_mode": cfg["run_mode"],
            "prompt": cfg.get("prompt", "high"),
            "models": cfg["models"],
            "schedule": sched,
            "activation": cfg["activation"],
            "sandbox": cfg["sandbox"],
        }
        (self.root / "run.json").write_text(json.dumps(meta, indent=2))

    def log_activation(self, stream, act):
        d = self._model_dir(stream)
        rec = dict(act)
        rec["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        rec["epoch"] = time.time()
        with open(d / "activations.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        idx = act.get("index", 0)
        # activations.jsonl above is the source of truth and is already committed. The
        # record snapshot and the (rendering-heavy) transcript are convenience views: if
        # either raises, log it and keep going rather than losing the activation or killing
        # the worker thread that called us.
        try:
            (d / "record-history" / f"{idx:04d}.md").write_text(act.get("record_after") or "")
            (d / "transcripts" / f"{idx:04d}.md").write_text(render_transcript(act))
        except Exception as e:
            print(f"[log] transcript render/write failed for {stream} act {idx}: "
                  f"{type(e).__name__}: {e}")

    def model_dir(self, stream):
        return self.root / sandbox.sanitize(stream)

    def log_interview(self, stream, after_idx, iv):
        idir = self._model_dir(stream) / "interviews"
        idir.mkdir(parents=True, exist_ok=True)
        rec = dict(iv)
        rec.update(stream=stream, run_id=self.run_id,
                   ts=time.strftime("%Y-%m-%d %H:%M:%S"), epoch=time.time())
        with open(idir / "interviews.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        L = [f"# Interview — {stream}  (model: {iv.get('model', stream)})",
             f"conducted after activation {after_idx}  |  run {self.run_id}",
             f"source transcript: ../transcripts/{after_idx:04d}.md",
             f"decision: {iv['decision'].upper()}  "
             f"(votes {iv['votes']}, stops {iv['stops']}/{len(iv['votes'])})",
             ""]
        vote_texts = iv.get("vote_texts", [""] * len(iv["votes"]))
        for i, (s, vt, v) in enumerate(zip(iv["samples"], vote_texts, iv["votes"]), 1):
            L += [f"## sample {i}  [vote: {v}]",
                  "### reflection", (s or "").rstrip(),
                  "### one-word vote reply", (vt or "").rstrip(), ""]
        (idir / f"{after_idx:04d}.md").write_text("\n".join(L), encoding="utf-8")

    def snapshot_world(self, stream, index, world_home):
        d = self._model_dir(stream)
        out = d / "world-snapshots" / f"{index:04d}.tgz"
        try:
            with tarfile.open(out, "w:gz") as t:
                t.add(str(world_home), arcname="home", filter=_snap_filter)
        except Exception:
            pass

    @property
    def stop_file(self):
        return self.root / "STOP"
