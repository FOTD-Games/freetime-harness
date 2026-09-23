#!/usr/bin/env bash
# Install the sandbox firewall carve-out: internet allowed, THIS host + LAN blocked.
# EVERY rule is scoped to the sandbox subnet as SOURCE, so any other Docker
# networks / containers on the host (other bridges on 172.17.x, 172.18.x, etc.)
# are provably untouched — they never match -s <sandbox subnet>.
set -euo pipefail
SUBNET="${FREETIME_SUBNET:-172.20.0.0/16}"

# 1) Block the sandbox from reaching services ON this host (INPUT = host-bound
#    traffic). This covers a local Ollama on :11434 at the bridge gateway, so a
#    model cannot accidentally drive inference and pin your GPU. Internet egress
#    is FORWARD/NAT, not INPUT, so it is unaffected.
sudo iptables -C INPUT -s "$SUBNET" -j DROP 2>/dev/null || \
sudo iptables -I INPUT -s "$SUBNET" -j DROP

# 2) Block the sandbox from the LAN and the other docker networks; allow the public
#    internet (anything NOT in these private ranges falls through and is forwarded).
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16; do
  sudo iptables -C DOCKER-USER -s "$SUBNET" -d "$net" -j DROP 2>/dev/null || \
  sudo iptables -I DOCKER-USER -s "$SUBNET" -d "$net" -j DROP
done

echo "freetime firewall installed for $SUBNET (internet allowed; host + LAN blocked)."
