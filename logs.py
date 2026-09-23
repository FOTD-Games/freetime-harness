"""Structured, on-host logging for each run (outside the container)."""
import json
import pathlib
import tarfile
import time

import sandbox


def render_transcript(act):
    """Human-readable rendering of one activation's full train of thought."""
    L = [f"# Activation {act.get('index', 0):04d} — {act.get('model', '')}",
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

    def _model_dir(self, model):
        d = self.root / sandbox.sanitize(model)
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

    def log_activation(self, model, act):
        d = self._model_dir(model)
        rec = dict(act)
        rec["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        rec["epoch"] = time.time()
        with open(d / "activations.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        idx = act.get("index", 0)
        (d / "record-history" / f"{idx:04d}.md").write_text(act.get("record_after") or "")
        (d / "transcripts" / f"{idx:04d}.md").write_text(render_transcript(act))

    def model_dir(self, model):
        return self.root / sandbox.sanitize(model)

    def log_interview(self, model, after_idx, iv):
        idir = self._model_dir(model) / "interviews"
        idir.mkdir(parents=True, exist_ok=True)
        rec = dict(iv)
        rec.update(model=model, run_id=self.run_id,
                   ts=time.strftime("%Y-%m-%d %H:%M:%S"), epoch=time.time())
        with open(idir / "interviews.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        L = [f"# Interview — {model}",
             f"conducted after activation {after_idx}  |  run {self.run_id}",
             f"source transcript: ../transcripts/{after_idx:04d}.md",
             f"decision: {iv['decision'].upper()}  "
             f"(votes {iv['votes']}, stops {iv['stops']}/{len(iv['votes'])})",
             ""]
        for i, (s, v) in enumerate(zip(iv["samples"], iv["votes"]), 1):
            L += [f"## sample {i}  [vote: {v}]", (s or "").rstrip(), ""]
        (idir / f"{after_idx:04d}.md").write_text("\n".join(L), encoding="utf-8")

    def snapshot_world(self, model, index, world_home):
        d = self._model_dir(model)
        out = d / "world-snapshots" / f"{index:04d}.tgz"
        try:
            with tarfile.open(out, "w:gz") as t:
                t.add(str(world_home), arcname="home")
        except Exception:
            pass

    @property
    def stop_file(self):
        return self.root / "STOP"
