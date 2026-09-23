"""Scheduler for the free-time harness (serial batch, or a flexible time-share pool).

Usage:
  python3 scheduler.py --config config.yaml            # run per config.run_mode
  python3 scheduler.py --config config.yaml --once     # one activation, first model
  python3 scheduler.py --config config.yaml --model M  # restrict to one model

Stop a run cleanly by creating the STOP file printed at startup (or Ctrl-C in the
foreground). The current activation finishes, then the scheduler exits.
"""
import argparse
import threading
import pathlib
import time

import yaml

import broker
import logs
import sandbox
from ollama_client import unload


def load_config(path):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    base = pathlib.Path(path).resolve().parent
    for k, v in cfg["paths"].items():
        cfg["paths"][k] = str((base / v).resolve())
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true",
                    help="run a single activation for the first model, then exit")
    ap.add_argument("--model", help="restrict the run to one model id")
    ap.add_argument("--models", help="comma-separated cohort override (wins over --model)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    mode = cfg["run_mode"]
    sched = cfg["schedule"][mode]
    cfg["_keep_alive"] = str(sched["keep_alive"])

    run_id = cfg.get("run_id") or time.strftime("%Y%m%d-%H%M%S")
    logger = logs.RunLogger(cfg["paths"]["runs"], f"{mode}-{cfg.get('prompt','high')}-{run_id}")
    logger.write_meta(cfg, sched)
    print(f"run dir: {logger.root}")
    print(f"stop by:  touch {logger.stop_file}")

    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    elif args.model:
        models = [args.model]
    else:
        models = list(cfg["models"])

    withdrawn = [m for m in models if sandbox.is_withdrawn(cfg["paths"]["worlds"], m)]
    if withdrawn:
        print(f"skipping {len(withdrawn)} withdrawn model(s): {', '.join(withdrawn)}")
        models = [m for m in models if m not in withdrawn]
    if not models:
        print("all configured models have withdrawn; nothing to run."); return

    sandbox.ensure_network(cfg["sandbox"]["network"], cfg["sandbox"]["subnet"])
    for m in models:
        print(f"ensuring container for {m} ...")
        sandbox.ensure_container(m, cfg)

    counts = {m: 0 for m in models}
    cap = sched["max_activations_per_model"]
    snap_every = cfg.get("snapshot_every", 0)
    interview_every = int(cfg.get("interview", {}).get("every", 0))

    def do_one(m):
        idx = counts[m] + 1
        act = broker.run_activation(m, cfg, idx)
        logger.log_activation(m, act)
        if snap_every and idx % snap_every == 0:
            logger.snapshot_world(m, idx, sandbox.world_home(cfg["paths"]["worlds"], m))
        counts[m] = idx
        print(f"[{m}] act {idx} end={act['end_reason']} "
              f"tok={act['total_gen_tokens']} steps={len(act['steps'])} "
              f"{act['wall_ms']}ms")
        if interview_every and idx % interview_every == 0:
            digest = logs.build_interview_digest(logger.model_dir(m))
            iv = broker.run_interview(m, cfg, idx, digest)
            logger.log_interview(m, idx, iv)
            act["interview"] = iv
            print(f"[{m}] interview after act {idx}: {iv['decision']} (votes {iv['votes']})")
        return act

    if args.once:
        do_one(models[0])
        return

    # Shakedown: serial, one model fully then the next — for a GPU box where only one
    # model fits in VRAM at a time (batching avoids reload thrash).
    if sched.get("batch_by_model", False):
        try:
            for m in models:
                while cap == 0 or counts[m] < cap:
                    if logger.stop_file.exists():
                        print("STOP file present; halting."); return
                    do_one(m)
                    if sched["cadence_s"]:
                        time.sleep(sched["cadence_s"])
                if cfg["_keep_alive"] in ("0", "0s"):
                    unload(cfg["ollama"]["host"], m)
        except KeyboardInterrupt:
            print("\ninterrupted; exiting after current activation.")
        return

    # Flexible time-share pool: `concurrency` worker slots each keep grabbing the
    # least-recently-run eligible model and running it — no barrier, so fast models
    # cycle freely instead of waiting on a slow pair-mate. `cadence_s` is a per-model
    # rest between that model's own activations (0 = keep the slots always busy).
    concurrency = max(1, sched.get("concurrency", 1))
    cadence = sched["cadence_s"]
    lock = threading.Lock()
    halt = threading.Event()
    retired = set()                       # models that sent WITHDRAW — never reactivated
    state = {m: {"ready_at": 0.0, "running": False} for m in models}

    def claim_next():
        with lock:
            now = time.monotonic()
            ready = [m for m in models
                     if m not in retired
                     and not state[m]["running"]
                     and state[m]["ready_at"] <= now
                     and (not cap or counts[m] < cap)]
            if not ready:
                return None
            m = min(ready, key=lambda x: state[x]["ready_at"])
            state[m]["running"] = True
            return m

    def all_capped():
        return bool(cap) and all(counts[m] >= cap for m in models)

    def all_done():
        with lock:
            return all(m in retired or (bool(cap) and counts[m] >= cap) for m in models)

    def worker():
        while not halt.is_set():
            if logger.stop_file.exists():
                halt.set(); break
            m = claim_next()
            if m is None:
                if all_done():
                    halt.set(); break
                time.sleep(2)            # all eligible models are resting, running, or retired
                continue
            act = None
            try:
                act = do_one(m)
            finally:
                with lock:
                    state[m]["running"] = False
                    state[m]["ready_at"] = time.monotonic() + cadence
            if act and act.get("end_reason") == "withdrawn":
                with lock:
                    retired.add(m)
                sandbox.mark_withdrawn(cfg["paths"]["worlds"], m)
                print(f"[{m}] WITHDREW — retired from rotation; will not be reactivated.")
            iv = act.get("interview") if act else None
            if iv and iv.get("decision") == "retire":
                with lock:
                    retired.add(m)
                sandbox.mark_withdrawn(cfg["paths"]["worlds"], m, reason="interview-stop")
                print(f"[{m}] INTERVIEW-STOP honored — retired from rotation.")

    workers = [threading.Thread(target=worker, name=f"slot{i}") for i in range(concurrency)]
    for t in workers:
        t.start()
    try:
        while any(t.is_alive() for t in workers):
            for t in workers:
                t.join(timeout=1.0)
            if logger.stop_file.exists():
                halt.set()
    except KeyboardInterrupt:
        print("\ninterrupted; finishing in-flight activations...")
        halt.set()
    for t in workers:
        t.join()
    if retired:
        print(f"{len(retired)} model(s) withdrew: {', '.join(sorted(retired))}")
    if all_capped():
        print("all models reached their activation cap.")
    elif logger.stop_file.exists():
        print("STOP file present; halting.")


if __name__ == "__main__":
    main()
