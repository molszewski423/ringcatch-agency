#!/usr/bin/env bash
# Deploy RingCatch landing failover to archbox over Tailscale/LAN
set -euo pipefail

REMOTE="archbox"
LANDING_DIR="/home/mike/agency/landing"
FAILOVER_DIR="/home/mike/agency/scripts/archbox-failover"
TUNNEL_TOKEN=$(grep CLOUDFLARE_TUNNEL_TOKEN /home/mike/agency/.env | cut -d= -f2-)

echo "==> Syncing landing files to archbox..."
ssh "$REMOTE" "mkdir -p ~/ringcatch-landing"
rsync -az --no-perms \
    "$LANDING_DIR/Containerfile" \
    "$LANDING_DIR/index.html" \
    "$LANDING_DIR/book.html" \
    "$LANDING_DIR/bimi-logo.svg" \
    "$FAILOVER_DIR/nginx-archbox.conf" \
    "$REMOTE:~/ringcatch-landing/"

# Rename the archbox nginx config
ssh "$REMOTE" "mv ~/ringcatch-landing/nginx-archbox.conf ~/ringcatch-landing/nginx.conf"

echo "==> Running setup on archbox..."
rsync -az "$FAILOVER_DIR/setup-archbox.sh" "$REMOTE:~/ringcatch-landing/"
ssh "$REMOTE" "bash ~/ringcatch-landing/setup-archbox.sh '$TUNNEL_TOKEN'"

echo ""
echo "==> Failover deployed. Test: ssh archbox 'curl -s http://127.0.0.1:8090/ | head -5'"
