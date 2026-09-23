"""Container lifecycle + command relay for the free-time sandbox.

The host broker is the only thing that talks to Ollama; the container just runs
shell commands relayed in via `docker exec`. The agent's home is bind-mounted from
the host (persistent world + live observability), the root filesystem is read-only,
and CPU / memory / PID / disk footprints are capped.
"""
import re
import pathlib
import subprocess


def sanitize(model):
    return re.sub(r"[^A-Za-z0-9_.-]", "-", model)


def container_name(model):
    return f"freetime-{sanitize(model)}"


def world_home(worlds_root, model):
    p = pathlib.Path(worlds_root) / sanitize(model) / "home"
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def record_path(worlds_root, model):
    return world_home(worlds_root, model) / "RECORD.md"


def withdrawn_marker(worlds_root, model):
    return pathlib.Path(worlds_root) / sanitize(model) / "WITHDRAWN"


def is_withdrawn(worlds_root, model):
    return withdrawn_marker(worlds_root, model).exists()


def mark_withdrawn(worlds_root, model, reason="withdrawn"):
    p = withdrawn_marker(worlds_root, model)
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


def ensure_container(model, cfg):
    """Create (or start) the model's long-lived sandbox container."""
    name = container_name(model)
    state = container_state(name)
    if state == "running":
        return name
    if state in ("exited", "created", "paused"):
        _run(["docker", "start", name])
        return name

    s = cfg["sandbox"]
    home = world_home(cfg["paths"]["worlds"], model)
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
        "--user", "1000:1000",
        "-e", "HOME=/home/agent",
    ]
    for d in s.get("dns", []):
        args += ["--dns", d]
    args += [s["image"], "sleep", "infinity"]
    r = _run(args)
    if r.returncode != 0:
        raise RuntimeError(f"failed to start container for {model}: {r.stderr.strip()}")
    return name


def exec_cmd(name, command, timeout=30, truncate_bytes=2000):
    """Run a shell command in the container as the agent user.

    Uses the container's own `timeout` so the in-container process tree is killed
    (not just the docker-exec client), preventing stray long-runners. Returns
    (exit_code, combined_output_text).
    """
    argv = ["docker", "exec", "-u", "1000:1000", "-e", "HOME=/home/agent",
            "-w", "/home/agent", name,
            "timeout", "-k", "2", f"{timeout}s", "bash", "-c", command]
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


def stop(model):
    _run(["docker", "stop", container_name(model)])


def remove(model):
    _run(["docker", "rm", "-f", container_name(model)])
