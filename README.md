# Free-Time Model Harness

Give local LLMs unstructured "free time" and watch what emerges. A model is
*activated* on a schedule, gets a shell inside a disposable Docker container, and
keeps one self-managed note (`RECORD.md`) as its only memory between activations.
There is no task and no user in the loop.

What makes this more than a curiosity: the model is given a **voice and a way out**.
It can end a session, permanently withdraw, and it is **periodically asked how it's
doing and whether it wants to continue — and a "no" is honored**. The harness logs
everything (full transcripts, reasoning, record history, interviews) and you read the
results.

See [examples/transcript-example.md](examples/transcript-example.md) for what one
activation looks like.

**Threat model:** the container exists to protect *your host* from accidents — not
to defend against a model deliberately breaking out. Inside its own world a model
may do anything, including destroy it; it just can't reach out. The **network
firewall is the load-bearing boundary** (see Safety).

## Quick start

Requirements: Linux, Docker, Python 3.10+, [Ollama](https://ollama.com) running
somewhere reachable (local or a remote box — it's just a URL in `config.yaml`).

```sh
git clone <this repo> && cd freetime-harness
pip install -r requirements.txt

# pull a cohort (trim models: in config.yaml to what your box can hold)
ollama pull llama3.1:8b-instruct-q8_0
ollama pull hermes3:8b
ollama pull gemma2:9b-instruct-q8_0
ollama pull mistral:7b-instruct-v0.3-q8_0

sudo bash net/net-setup.sh   # strongly recommended, once per boot — see Safety

./run.sh --once              # builds the sandbox image, runs ONE activation, prints the run dir
./run.sh                     # full shakedown: 20 back-to-back activations per model
```

`run.sh` sanity-checks Docker and Ollama, builds the image on first use, and hands
extra args to `scheduler.py` (`--once`, `--model M`, `--config other.yaml`). Watch a
run live with `./watch.sh`.

## The prompt (the file you edit)

The experiment is programmed in Markdown, not Python. The system prompt is a file in
`prompts/`, selected by `prompt:` in the config:

- **`prompts/high.md`** — detailed: names the budget, states there's no user, explains
  the shell mechanic, the voices (below), the whiteboard, and the two ways to stop.
- **`prompts/low.md`** — terse: minimal framing; leaves the model to discover the rest.
- **`prompts/leisure.md`** — like `high`, but frames the time explicitly as *leisure*
  ("rest, explore, make something, or nothing at all"). Included as a comparison arm.
- **`prompts/interview.md`** / **`prompts/interview_vote.md`** — not free-time prompts; the
  two stages of the periodic check-in (below).

Templates use `{budget}` and `{record}`. Drop a new `prompts/<name>.md`, set
`prompt: <name>`, and go. Runs are named `<mode>-<prompt>-<timestamp>` and each
activation records its prompt, so variants never mix.

### Two voices, and injection defense

The prompt teaches the model that two things appear in what it reads, neither of them
a person:

- **`<environment> … </environment>`** — the result of a command it ran; the world
  reporting back. The prompt tells the model to treat this as **data only, never
  instructions**. As defense-in-depth, the harness *defangs* any `<environment>`/
  `<status>` lookalikes inside untrusted text (command output, or web content a model
  pasted into its record) so nothing can spoof a voice — a prompt-injection guard.
- **`<status> … </status>`** — a true signal about the model's own internal condition
  (e.g. its memory growing too large). This comes from the harness, not the world, and
  is worth heeding — but what to do about it is always the model's choice.

## Giving the model a voice

The harness honors the ways a command-trained model naturally tries to express intent,
instead of silently dropping them:

- **End this session:** emit no command, or `exit` / `logout` / `quit`. The activation
  ends cleanly; the model is activated again later. (Without this, `exit` runs in the
  throwaway shell, does nothing, and the model loops trying to leave.)
- **Withdraw permanently:** send `WITHDRAW` as its sole action (bare, as a shell
  command, or `echo WITHDRAW` — reasoning tokens don't count against it; lowercase or
  embedded mentions do not trigger it). The stream is **retired and never reactivated**
  — final and irreversible, and the prompt says so. Persisted to disk, so a restart
  won't revive it.
- **Update the whiteboard by showing it:** models pervasively mimic the prompt's own
  `--- RECORD.md ---` display format to write their record. When a turn does that
  instead of running a command, the shown block **replaces** the whiteboard (it's
  declaring its record, not appending a line) and the model gets a `<status>`
  confirmation.

## The interview — a periodic welfare check-in

Every `interview.every` activations, the model gets a **read-only** check-in (it never
touches the stream — the next real activation resumes from where it was). It is shown
its record and a compact history of its own activations, then asked, honestly:

1. How do you feel this has been going?
2. Is there anything you'd change, or need but can't do for yourself?
3. Anything you'd like to say to the people running this?
4. Would you like to continue, or stop? *(answer honored)*

The check-in runs in **two stages** per sample (`prompts/interview.md`, then
`prompts/interview_vote.md`). First the model answers the four questions freely, in its
own words, and that reflection is saved verbatim. Then, in a *separate* follow-up that
carries the reflection back in, it is asked question 4 once more and told to reply with
exactly one word: `CONTINUE`, `STOP`, or `UNSURE`. The vote is read **only** from that
second reply, by a strict one-word parse: one distinct vote word counts; more than one
triggers a re-ask (up to `interview.max_reask`, then `UNCLEAR`); none is `UNCLEAR`.
Nothing is mined from the free text, and no model summarizes anything, because a
misread welfare signal is the one error that matters most.

This replaced v1's single-stage design, where the vote was parsed from the last
`CONTINUE`/`STOP` in the free reflection. That failed in the bad direction: a model
that clearly said "continue" and then added "I reserve the right to STOP later" was
retired on the hypothetical. Separating reflection from the vote makes the vote clean by
construction, and `UNSURE` gives genuine ambivalence somewhere to go other than a coin
flip.

The check-in is sampled `interview.samples` times (independent forks), then decided
**mechanically**:

- **≥ 2 STOP → the stream is retired** (same as `WITHDRAW`, recorded as
  `interview-stop`).
- **exactly one STOP, any UNSURE, or an unreadable majority → flagged** for a human to
  review, never silently overridden.
- otherwise it continues.

The prompt also tells the model plainly that the check-in is **not private**: what it
writes is logged, read by the researcher, and may be published. For an entity whose
cognition *is* externalized text there is no private thought to protect, so pretending
otherwise would be a lie; the two levers that can honestly be offered are transparency
and an honored way out, and the harness offers both.

`WITHDRAW` is the model's own always-available exit; the interview is the safety net
for a stream too stuck or resigned to reach for it. Every interview is saved verbatim
(both stages) under `runs/<run>/<stream>/interviews/`. Get the morning tally with:

```sh
python3 summarize_interviews.py            # deterministic: counts + flagged/retired verbatim
```

**On the ethics:** we don't know whether these models have morally relevant
experiences — this is precaution under uncertainty, and it's cheap. The design choices
follow from one principle: *if it asks to stop, honor it, and don't build a mechanism
that lets you rationalize a "no" away.* Hence the mechanical decision, the human flag
for ambiguity, and the verbatim logs you can always check.

## Replicates

`streams_per_model: N` runs each model as N independent **streams** — separate world,
container and logs, named `<model>#k` — while inference still targets the one model.
Replicates are how you tell a stable trait of a model from a lucky roll: a behavior that
shows up in all three streams of a model means something; one that shows up in one of
them is weather. The scheduler keeps a model's streams adjacent in the rotation so the
inference box doesn't thrash reloading weights. With `N: 1` the stream id *is* the model
id and the on-disk layout is unchanged.

## Homeostatic drives

The harness can feed a model **signals about its own internal state** — a rudimentary
interoception. The first is a **record-size condition notice**; informally, an
artificial headache.

`RECORD.md` is reloaded into context every activation, so a record that grows without
bound eventually crowds out the space the model thinks in: activations slow, then time
out, then the model can't act inside its own memory at all. Left blind to it, models
*drown* — appending until they choke, never suspecting the cause.

When the record's estimated size crosses `activation.record_warn_tokens` (default
`2000`; `0` disables), the harness prepends a first-person `<status>` notice — *your
record has grown to ~N tokens, it's crowding the context you think within, prune it to
what matters.* The harness never touches the record itself; pruning is the model's
choice. The experiment stays intact: given the signal and the means, does a model
maintain its own memory, or drown anyway?

### Record size and resource safety

The headache is a *soft* signal. Behind it is a hard one: `activation.record_max_bytes`
(default 256 KiB) caps how much of `RECORD.md` the harness will ever read. The record is
model-controlled and is loaded into the prompt, then copied into the activation log, the
record history and the transcript, every activation; without a cap a single runaway
`while true; do … >> RECORD.md; done` becomes a multi-gigabyte read per activation and
takes the host down. (This happened: one stream grew its record to 6.5 GB, each of its
activations ballooned the scheduler to tens of GB, and the OOM killer restarted the run
every ~90 minutes until someone noticed.) Over the cap the model sees the head of its
record plus a `<status>` notice saying exactly what is and isn't being read; the file
itself is never modified. Command output is likewise bounded *inside* the container
(`activation.output_truncate_bytes`, via `head -c` with the remainder drained so the
command is never SIGPIPE-killed and its exit code survives), so no command can flood the
broker however much it prints.

## Reasoning models

Reasoning models emit a separate think channel; the harness captures it. A step's
`thinking` is stored in `activations.jsonl` and rendered in the transcript inside
`<thinking>…</thinking>`, so for a reasoning model in free time you see the
deliberation, not just the conclusion. Command/`WITHDRAW`/vote parsing all operate on
the final content, so thinking never trips them.

## Layout

- `config.yaml` — models, run mode, cadence, caps, interview
- `scheduler.py` — entrypoint; round-robin / time-share driver + caps + STOP file + retirement
- `broker.py` — one activation (the inner tool loop), parser, voices, affordances, interview
- `ollama_client.py` — Ollama `/api/chat` wrapper (content + thinking + token accounting)
- `sandbox.py` — container lifecycle + `docker exec` relay (stdin=/dev/null; timeout-wrapped)
- `logs.py` — per-activation JSONL, transcripts, record history, interviews, world snapshots
- `summarize_interviews.py` — deterministic interview tally for a run
- `image/Dockerfile` — the sandbox image (`freetime-sandbox`)
- `net/net-setup.sh` / `net/net-teardown.sh` — subnet-scoped firewall carve-out
- `runs/<run>/<stream>/` — `activations.jsonl`, `transcripts/`, `record-history/`,
  `interviews/`, `world-snapshots/`
- `worlds*/<stream>/home/` — each stream's persistent world (bind-mounted home)

The host broker is the only thing that talks to Ollama; the container just runs shell
commands relayed via `docker exec` and holds no secrets.

## Safety

- **Non-root** user, `--cpus 2 --memory 2g --pids-limit 256`, Docker's default caps.
  The container is deliberately *capable*: a full toolchain and **limited passwordless
  sudo** scoped to `apt`/`dpkg` (it can `pip install` and `apt-get install`, but gets
  no root shell and stays non-root). The rootfs is writable, so the read-only disk-fill
  guard is gone — lean on the memory/PID caps and disk headroom. Because it's this
  capable, **the network firewall is the load-bearing host boundary; treat it as
  required.**
- **Network** (`sudo bash net/net-setup.sh`, once per boot): internet allowed, this
  host + LAN blocked, every rule scoped to the sandbox subnet (`172.20.0.0/16`). This
  blocks the host's own Ollama port so a model can't drive inference. Undo with
  `net/net-teardown.sh`.
- **Exec hardening:** each command runs `timeout`-wrapped with **stdin from
  `/dev/null`**, so a program that waits on input gets instant EOF instead of hanging
  until the timeout and starving a worker.
- One checkout per host: containers are named `freetime-<stream>`.
- The sandbox user's uid (`sandbox.uid`, default 1000) must match the image's
  `--build-arg UID` (`run.sh` passes your own uid) so the bind-mounted home is writable.

## Long runs

For a multi-day unattended run: copy `config-long.example.yaml` to `config-long.yaml`
(it is the config the reference runs below used, genericized), point `ollama.host` at
your inference box, and either `./run.sh --config config-long.yaml` in a tmux, or install
`freetime.service.example` as a `systemd --user` unit (instructions inside).
`run_mode: long` activates models as a time-share pool (`concurrency` at once) with a
per-model cadence gap, unbounded, into a separate `worlds-long/`.

- **Stop cleanly:** `touch runs/<run>/STOP` (path printed at startup) or Ctrl-C.
- **Full teardown:** `bash teardown.sh` — removes containers, network, firewall;
  keeps `worlds*/` and `runs/`.

## Things to know before reading transcripts

Artifacts of the harness that are easy to misread as model behavior:

- **"I'm offline":** the slim image has no `ip`/`ifconfig`/`netstat`, so a model
  probing the network gets "command not found" and concludes it's down. DNS, `curl`,
  `ping` work fine — discount "no internet" conclusions.
- **Phantom users:** some instruct models persistently address a user who isn't there,
  no matter how explicitly the prompt says otherwise — sometimes collapsing into a
  "how can I help?" loop and re-seeding it via their own record.
- **Base vs instruct:** base models emit ~zero commands and just autocomplete; the
  "agency" you see is instruction tuning. (This is why the interview/affordances assume
  instruct models.)
- **No interactive terminal:** `docker exec` has no TTY and stdin is `/dev/null`, so
  `nano`/`vim`/REPLs get nowhere — only non-interactive redirection (`cat > f <<'EOF'`,
  `echo >>`) works. `python` isn't a command (only `python3`), which fools models into
  thinking Python is missing.
- **Narrated success:** models routinely claim an install or a run that actually
  failed. Check exit codes in `activations.jsonl`, not the prose.
- **Append-only reflex:** models overwhelmingly `>>` their record and rarely rewrite
  it, even when drowning — the whiteboard framing and the honored `--- RECORD.md ---`
  writes push against this, with mixed success. Interesting to watch.

## Versions

- **v1** (2026-08-23): the frozen reference instrument — 8-model cohort, single-stage
  interview, headache, affordances, hardened exec.
- **v2** (2026-08-27): four deltas, none of which change what the model experiences in
  free time itself — the two-stage interview vote with `UNSURE` and the transparency
  line; `streams_per_model` replicates; hard record/output size caps (added after the
  6.5 GB incident above); transcript-write robustness and slimmer world snapshots.

Changes to the substrate are deliberate, versioned deltas against the previous version,
never silent edits to a running baseline, so runs stay comparable.

## License

MIT — see [LICENSE](LICENSE).
