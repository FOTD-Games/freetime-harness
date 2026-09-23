#!/usr/bin/env bash
# The "go" button: sanity-check docker + ollama, build the sandbox image if
# missing, then launch the scheduler. Extra args pass through to scheduler.py
# (e.g. ./run.sh --once, ./run.sh --config config-long.yaml, ./run.sh --model M).
set -euo pipefail
cd "$(dirname "$0")"

CONFIG="config.yaml"
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  [ "${args[i]}" = "--config" ] && CONFIG="${args[i+1]:-config.yaml}"
done

command -v docker >/dev/null || { echo "docker not found — install Docker first."; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker daemon not reachable (is it running? are you in the docker group?)"; exit 1; }

host=$(python3 -c "import yaml; print(yaml.safe_load(open('$CONFIG'))['ollama']['host'])")
curl -sf --max-time 5 "$host/api/tags" >/dev/null || {
  echo "Ollama not reachable at $host — start it (or fix ollama.host in $CONFIG)."; exit 1; }

if ! docker image inspect freetime-sandbox:latest >/dev/null 2>&1; then
  echo "building sandbox image (one-time) ..."
  docker build --build-arg UID="$(id -u)" -t freetime-sandbox:latest image/
fi

if ! sudo -n iptables -C INPUT -s 172.20.0.0/16 -j DROP 2>/dev/null; then
  echo "NOTE: sandbox firewall not detected. Recommended (once per boot):"
  echo "      sudo bash net/net-setup.sh"
  echo "      (blocks sandbox -> this host + LAN; internet stays open)"
fi

exec python3 scheduler.py --config "$CONFIG" "$@"
