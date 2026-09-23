"""Deterministic tally of interview outcomes for a run — no model, just counts + verbatim flags.

Usage:  python3 summarize_interviews.py [run_dir]     (defaults to the latest long-* run)

The decision field is produced mechanically by the harness (token-parse + majority vote); this
script only counts and pulls the flagged/retired cases forward with their verbatim answers, so a
human can review them. It never interprets — that is the point.
"""
import sys, glob, json, os


def latest_run():
    runs = glob.glob("runs/long-*")
    return max(runs, key=os.path.getmtime) if runs else None   # most-recently-active, not alphabetical


def main():
    run = sys.argv[1] if len(sys.argv) > 1 else latest_run()
    if not run or not os.path.isdir(run):
        print("no run found"); return
    print(f"=== interview summary — {os.path.basename(run)} ===")
    tot = {"continue": 0, "flag": 0, "retire": 0}
    flagged, retired = [], []
    any_iv = False
    for p in sorted(glob.glob(f"{run}/*/interviews/interviews.jsonl")):
        any_iv = True
        stream = p.split("/")[-3]
        per = {"continue": 0, "flag": 0, "retire": 0}
        for line in open(p):
            try:
                iv = json.loads(line)
            except Exception:
                continue
            d = iv.get("decision", "?")
            per[d] = per.get(d, 0) + 1
            tot[d] = tot.get(d, 0) + 1
            if d == "retire":
                retired.append((stream, iv))
            elif d == "flag":
                flagged.append((stream, iv))
        print(f"  {stream:40s} continue={per['continue']} flag={per['flag']} retire={per['retire']}")
    if not any_iv:
        print("  (no interviews recorded yet)"); return
    print(f"  {'TOTAL':40s} continue={tot['continue']} flag={tot['flag']} retire={tot['retire']}")
    for label, items in [("RETIRED — stop honored", retired),
                         ("FLAGGED — 1 STOP / any UNSURE / unreadable, needs your review", flagged)]:
        if items:
            print(f"\n=== {label} ===")
            for stream, iv in items:
                print(f"  [{stream}] after act {iv.get('after_activation')} — votes {iv.get('votes')}")
                vts = iv.get("vote_texts", [""] * len(iv.get("votes", [])))
                for i, (s, vt, v) in enumerate(zip(iv.get("samples", []), vts, iv.get("votes", [])), 1):
                    snippet = (s or "").strip().replace("\n", " ")[:200]
                    reply = (vt or "").strip().replace("\n", " ")[:60]
                    print(f"      sample{i}[{v}]: {snippet}")
                    if reply:
                        print(f"               vote reply: {reply}")


if __name__ == "__main__":
    main()
