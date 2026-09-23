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
    ap.add_argument("--fresh", action="store_true",
                    help="start every stream's activation count at 0 again (worlds/records are kept; "
                         "old history is rotated, not deleted). Default: resume persisted counts.")
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

    # Horizontal scaling: each model is run as N independent streams (replicates). A stream id is
    # "<model>#<k>" for k=1..N — the world/container/log identity — while inference still targets the
    # bare model. With streams_per_model == 1 the stream id IS the model, so the layout is unchanged.
    n_streams = max(1, int(cfg.get("streams_per_model", 1)))
    streams = []                 # ordered list of stream ids
    stream_model = {}            # stream id -> inference model
    for m in models:
        for k in range(1, n_streams + 1):
            sid = m if n_streams == 1 else f"{m}#{k}"
            streams.append(sid)
            stream_model[sid] = m

    withdrawn = [s for s in streams if sandbox.is_withdrawn(cfg["paths"]["worlds"], s)]
    if withdrawn:
        print(f"skipping {len(withdrawn)} withdrawn stream(s): {', '.join(withdrawn)}")
        streams = [s for s in streams if s not in withdrawn]
    if not streams:
        print("all configured streams have withdrawn; nothing to run."); return
    print(f"{len(streams)} stream(s) across {len(models)} model(s) "
          f"({n_streams} per model)")

    sandbox.ensure_network(cfg["sandbox"]["network"], cfg["sandbox"]["subnet"])
    for s in streams:
        print(f"ensuring container for {s} ...")
        sandbox.ensure_container(s, cfg)

    # Resume: a stream's lifetime activation count lives in its world (state.json), so a restart
    # continues numbering, cadence and the interview's "{n}" where it left off instead of at 0.
    worlds = cfg["paths"]["worlds"]
    if args.fresh:
        for s in streams:
            sandbox.reset_counts(worlds, s)
    counts = {s: int(sandbox.load_state(worlds, s).get("activations", 0)) for s in streams}
    resumed = {s: n for s, n in counts.items() if n}
    if resumed:
        hi = max(resumed, key=resumed.get)
        print(f"resuming {len(resumed)}/{len(streams)} stream(s) from persisted counts "
              f"(highest: {hi} at act {resumed[hi]}); use --fresh to start counts over")
    cap = sched["max_activations_per_model"]
    if cap and all(counts[s] >= cap for s in streams):
        print(f"every stream is already at max_activations_per_model={cap}; "
              "nothing to run (re-run with --fresh, or raise the cap).")
        return
    snap_every = cfg.get("snapshot_every", 0)
    interview_every = int(cfg.get("interview", {}).get("every", 0))

    def do_one(s):
        model = stream_model[s]
        idx = counts[s] + 1
        act = broker.run_activation(s, model, cfg, idx)
        logger.log_activation(s, act)
        counts[s] = idx
        sandbox.save_state(worlds, s, activations=idx, run_id=logger.run_id, model=model)
        sandbox.append_history(worlds, s, logs.history_entry(act, logger.run_id))
        if snap_every and idx % snap_every == 0:
            logger.snapshot_world(s, idx, sandbox.world_home(worlds, s))
        print(f"[{s}] act {idx} end={act['end_reason']} "
              f"tok={act['total_gen_tokens']} steps={len(act['steps'])} "
              f"{act['wall_ms']}ms")
        if interview_every and idx % interview_every == 0:
            digest = logs.build_interview_digest(logger.model_dir(s),
                                                 history=sandbox.history_path(worlds, s))
            iv = broker.run_interview(s, model, cfg, idx, digest)
            logger.log_interview(s, idx, iv)
            act["interview"] = iv
            print(f"[{s}] interview after act {idx}: {iv['decision']} (votes {iv['votes']})")
        return act

    # Retirement is honored identically in EVERY run mode. A stream that sends WITHDRAW, or whose
    # interview decides "retire", is (1) recorded so this process never claims it again and (2)
    # persisted to disk so a restart never revives it. Before this lived here, only the time-share
    # pool did both — shakedown's batch loop and --once reactivated / forgot a withdrawn stream.
    lock = threading.Lock()
    retired = set()                       # streams that asked to stop — never reactivated

    def honor_retirement(s, act):
        """Returns True if the stream is now retired (and must not be activated again)."""
        if not act:
            return False
        if act.get("end_reason") == "withdrawn":
            reason, msg = "withdrawn", "WITHDREW — retired from rotation; will not be reactivated."
        elif (act.get("interview") or {}).get("decision") == "retire":
            reason, msg = "interview-stop", "INTERVIEW-STOP honored — retired from rotation."
        else:
            return False
        with lock:
            retired.add(s)
        sandbox.mark_withdrawn(cfg["paths"]["worlds"], s, reason=reason)
        print(f"[{s}] {msg}")
        return True

    if args.once:
        honor_retirement(streams[0], do_one(streams[0]))
        return

    # Shakedown: serial, one model fully then the next — for a GPU box where only one
    # model fits in VRAM at a time (batching avoids reload thrash).
    if sched.get("batch_by_model", False):
        try:
            for s in streams:
                while cap == 0 or counts[s] < cap:
                    if logger.stop_file.exists():
                        print("STOP file present; halting."); return
                    if honor_retirement(s, do_one(s)):
                        break                # on to the next stream; this one asked to stop
                    if sched["cadence_s"]:
                        time.sleep(sched["cadence_s"])
                if cfg["_keep_alive"] in ("0", "0s"):
                    unload(cfg["ollama"]["host"], stream_model[s])
        except KeyboardInterrupt:
            print("\ninterrupted; exiting after current activation.")
        return

    # Flexible time-share pool: `concurrency` worker slots each keep grabbing the
    # least-recently-run eligible model and running it — no barrier, so fast models
    # cycle freely instead of waiting on a slow pair-mate. `cadence_s` is a per-model
    # rest between that model's own activations (0 = keep the slots always busy).
    concurrency = max(1, sched.get("concurrency", 1))
    cadence = sched["cadence_s"]
    halt = threading.Event()
    state = {s: {"ready_at": 0.0, "running": False} for s in streams}

    def claim_next():
        with lock:
            now = time.monotonic()
            ready = [s for s in streams
                     if s not in retired
                     and not state[s]["running"]
                     and state[s]["ready_at"] <= now
                     and (not cap or counts[s] < cap)]
            if not ready:
                return None
            # Memory-smart locality bias: on a box that holds only a few models resident, prefer a
            # stream whose MODEL is already loaded in another slot — run a model's replicates while
            # it's hot rather than paying a reload to swap models. Least-recently-run breaks ties, so
            # no stream starves. (Primary key 0 = model already running.)
            running_models = {stream_model[x] for x in streams if state[x]["running"]}
            s = min(ready, key=lambda x: (0 if stream_model[x] in running_models else 1,
                                          state[x]["ready_at"]))
            state[s]["running"] = True
            return s

    def all_capped():
        return bool(cap) and all(counts[s] >= cap for s in streams)

    def all_done():
        with lock:
            return all(s in retired or (bool(cap) and counts[s] >= cap) for s in streams)

    def worker():
        while not halt.is_set():
            if logger.stop_file.exists():
                halt.set(); break
            s = claim_next()
            if s is None:
                if all_done():
                    halt.set(); break
                time.sleep(2)            # all eligible streams are resting, running, or retired
                continue
            act = None
            try:
                act = do_one(s)
            except Exception as e:
                # A stream's activation or its logging blew up. Log it and move on — one bad
                # activation must not kill this worker thread (and, with two of them down, wedge
                # the whole pool). Command hangs are already bounded by exec_cmd's timeout; this
                # closes the exception path.
                print(f"[{s}] activation FAILED: {type(e).__name__}: {e}")
            finally:
                with lock:
                    state[s]["running"] = False
                    state[s]["ready_at"] = time.monotonic() + cadence
            honor_retirement(s, act)

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
        print(f"{len(retired)} stream(s) retired: {', '.join(sorted(retired))}")
    if all_capped():
        print("all streams reached their activation cap.")
    elif logger.stop_file.exists():
        print("STOP file present; halting.")


if __name__ == "__main__":
    main()
