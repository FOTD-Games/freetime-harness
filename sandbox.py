"""Container lifecycle + command relay for the free-time sandbox.

The host broker is the only thing that talks to Ollama; the container just runs
shell commands relayed in via `docker exec`. The agent's home is bind-mounted from
the host (persistent world + live observability), and CPU / memory / PID / disk
footprints are capped. Command output is bounded INSIDE the container (see exec_cmd)
so a runaway command can never flood the host broker's memory.
"""
import re
import pathlib
import subprocess


def sanitize(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name)


# The container + on-disk world are keyed on a STREAM id, not the model id. A stream is one
# independent replicate (e.g. "gemma2:9b#2"); with a single stream per model the id is just the
# model, so single-stream runs keep the original layout. Inference still targets the bare model —
# that routing lives in the broker, never here.
def container_name(stream):
    return f"freetime-{sanitize(stream)}"


def world_home(worlds_root, stream):
    p = pathlib.Path(worlds_root) / sanitize(stream) / "home"
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def record_path(worlds_root, stream):
    return world_home(worlds_root, stream) / "RECORD.md"


def withdrawn_marker(worlds_root, stream):
    return pathlib.Path(worlds_root) / sanitize(stream) / "WITHDRAWN"


def is_withdrawn(worlds_root, stream):
    return withdrawn_marker(worlds_root, stream).exists()


def mark_withdrawn(worlds_root, stream, reason="withdrawn"):
    p = withdrawn_marker(worlds_root, stream)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(reason + "\n")


def _run(args, timeout=120):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def network_exists(name):
    return _run(["docker", "network", "inspect", name]).returncode == 0


def ensure_network(name, subnet):
    if not network_exists(name):
        _run(["docker", "network", "create", "--driver", "bridge",
              "--subnet", subnet, name])


def container_state(name):
    r = _run(["docker", "inspect", "-f", "{{.State.Status}}", name])
    return r.stdout.strip() if r.returncode == 0 else None


def ensure_container(stream, cfg):
    """Create (or start) the stream's long-lived sandbox container."""
    name = container_name(stream)
    state = container_state(name)
    if state == "running":
        return name
    if state in ("exited", "created", "paused"):
        _run(["docker", "start", name])
        return name

    s = cfg["sandbox"]
    home = world_home(cfg["paths"]["worlds"], stream)
    args = [
        "docker", "run", "-d", "--name", name,
        "--hostname", "sandbox",
        "--network", s["network"],
        # "limited sudo" posture: writable rootfs + Docker default caps + setuid
        # allowed so `sudo apt` works. Still non-root by default (see --user), still
        # resource-capped and network-firewalled; sudo is scoped to apt/dpkg in the image.
        "--tmpfs", f"/tmp:size={s['tmp_size']},mode=1777",
        "--tmpfs", "/run:size=16m",
        "--mount", f"type=bind,src={home},dst=/home/agent",
        "--cpus", str(s["cpus"]),
        "--memory", str(s["memory"]),
        "--memory-swap", str(s["memory"]),
        "--pids-limit", str(s["pids_limit"]),
        "--restart", "no",
        "--user", f"{s.get('uid', 1000)}:{s.get('uid', 1000)}",
        "-e", "HOME=/home/agent",
    ]
    for d in s.get("dns", []):
        args += ["--dns", d]
    args += [s["image"], "sleep", "infinity"]
    r = _run(args)
    if r.returncode != 0:
        raise RuntimeError(f"failed to start container for {stream}: {r.stderr.strip()}")
    return name


def exec_cmd(name, command, timeout=30, truncate_bytes=2000, uid=1000):
    """Run a shell command in the container as the agent user.

    Uses the container's own `timeout` so the in-container process tree is killed
    (not just the docker-exec client), preventing stray long-runners. Output is capped
    at the SOURCE: inside the container the command's combined stdout+stderr is piped
    through `head -c` (keeping truncate_bytes+1 so the host can tell it was cut) and
    the remainder is drained to /dev/null — draining, rather than letting `head` close
    the pipe, means the command is never SIGPIPE-killed mid-run and its real exit code
    survives via PIPESTATUS. So the host never receives more than ~truncate_bytes,
    however much a command prints. Returns (exit_code, combined_output_text).
    """
    keep = int(truncate_bytes) + 1
    script = (f"timeout -k 2 {int(timeout)}s bash -c \"$1\" 2>&1 "
              f"| {{ head -c {keep}; cat >/dev/null; }}; exit \"${{PIPESTATUS[0]}}\"")
    argv = ["docker", "exec", "-u", f"{uid}:{uid}", "-e", "HOME=/home/agent",
            "-w", "/home/agent", name,
            "bash", "-c", script, "_", command]
    def _dec(b):
        if isinstance(b, (bytes, bytearray)):
            return b.decode("utf-8", errors="replace")
        return b or ""

    try:
        # Capture BYTES (no text=True): a model's command output can contain
        # invalid UTF-8, which strict decoding raises on — that would crash the run.
        # stdin=/dev/null: a command that waits on input gets EOF immediately instead of
        # hanging until the timeout (which also starves a worker slot).
        r = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=timeout + 15)
        out = _dec(r.stdout) + _dec(r.stderr)
        code = r.returncode
        if code == 124:
            out += f"\n[command exceeded {timeout}s and was killed]"
    except subprocess.TimeoutExpired as e:
        out = _dec(e.stdout) + _dec(e.stderr)
        out += f"\n[docker exec itself timed out after {timeout + 15}s]"
        code = 124
    raw = out.encode("utf-8", errors="replace")
    if len(raw) > truncate_bytes:
        out = raw[:truncate_bytes].decode("utf-8", errors="ignore") + "\n[...output truncated...]"
    return code, out


def stop(stream):
    _run(["docker", "stop", container_name(stream)])


def remove(stream):
    _run(["docker", "rm", "-f", container_name(stream)])
