#!/usr/bin/env bash
# Stop + remove all sandbox containers, remove the network, and (with sudo) the
# firewall rules. Worlds/ and runs/ on the host are left intact.
set -uo pipefail
cd "$(dirname "$0")"
for name in $(docker ps -a --filter "name=freetime-" --format '{{.Names}}'); do
  echo "removing $name"; docker rm -f "$name" >/dev/null 2>&1 || true
done
docker network rm freetime-net >/dev/null 2>&1 || true
bash net/net-teardown.sh || true
echo "teardown complete (worlds/ and runs/ preserved)."
