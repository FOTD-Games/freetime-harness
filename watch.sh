#!/usr/bin/env bash
# Tail the most recent run's activations for one model (default: first found),
# pretty-printing each activation as it lands.
set -euo pipefail
cd "$(dirname "$0")"
run=$(ls -dt runs/*/ 2>/dev/null | head -1)
[ -z "${run:-}" ] && { echo "no runs yet"; exit 1; }
model_dir="${1:-$(ls -dt "$run"*/ 2>/dev/null | head -1)}"
f="${model_dir%/}/activations.jsonl"
echo "watching $f"
tail -n0 -F "$f" | python3 - <<'PY'
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        a = json.loads(line)
    except Exception:
        continue
    print(f"\n=== act {a['index']} [{a['model']}] end={a['end_reason']} "
          f"tok={a['total_gen_tokens']} steps={len(a['steps'])} ===")
    for i, s in enumerate(a["steps"], 1):
        gen = (s.get("gen_text") or "").strip().replace("\n", " ")
        print(f"  [{i}] {gen[:220]}")
        if s.get("command"):
            print(f"      $ {s['command'][:160]}")
            out = (s.get("output") or "").strip().replace("\n", " ")
            print(f"      -> {out[:160]}")
PY
