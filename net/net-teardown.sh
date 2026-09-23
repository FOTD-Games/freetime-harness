#!/usr/bin/env bash
# Remove exactly the rules net-setup.sh added. Safe to run repeatedly.
set -euo pipefail
SUBNET="${FREETIME_SUBNET:-172.20.0.0/16}"
sudo iptables -D INPUT -s "$SUBNET" -j DROP 2>/dev/null || true
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16; do
  sudo iptables -D DOCKER-USER -s "$SUBNET" -d "$net" -j DROP 2>/dev/null || true
done
echo "freetime firewall removed for $SUBNET."
