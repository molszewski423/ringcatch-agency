#!/usr/bin/env bash
# RingCatch failover setup for Arch Linux (no containers)
# Usage: bash setup-archbox.sh <CLOUDFLARE_TUNNEL_TOKEN>
set -euo pipefail

TUNNEL_TOKEN="${1:-}"
LANDING_DIR="$HOME/ringcatch-landing"
NGINX_CONF="/etc/nginx/nginx.conf"
NGINX_HTML="/usr/share/nginx/html"
CF_BIN="/usr/local/bin/cloudflared"

if [ -z "$TUNNEL_TOKEN" ]; then
    echo "Usage: $0 <CLOUDFLARE_TUNNEL_TOKEN>"
    exit 1
fi

echo "==> Installing nginx..."
sudo pacman -S --noconfirm nginx

echo "==> Deploying nginx config and site files..."
sudo cp "$LANDING_DIR/nginx-archbox.conf" "$NGINX_CONF"
sudo mkdir -p "$NGINX_HTML"
sudo cp "$LANDING_DIR/index.html"    "$NGINX_HTML/index.html"
sudo cp "$LANDING_DIR/book.html"     "$NGINX_HTML/book.html"
sudo cp "$LANDING_DIR/bimi-logo.svg" "$NGINX_HTML/bimi-logo.svg"

echo "==> Downloading cloudflared binary..."
sudo curl -fsSL \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" \
    -o "$CF_BIN"
sudo chmod +x "$CF_BIN"

echo "==> Creating systemd services..."

# Enable and start nginx
sudo systemctl enable --now nginx

# cloudflared tunnel service (system-level so it survives without login)
sudo tee /etc/systemd/system/ringcatch-cloudflared.service > /dev/null << EOF
[Unit]
Description=Cloudflare tunnel — RingCatch failover
After=network-online.target nginx.service
Wants=network-online.target

[Service]
ExecStart=$CF_BIN tunnel --no-autoupdate run --token ${TUNNEL_TOKEN}
Restart=on-failure
RestartSec=10
User=nobody

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ringcatch-cloudflared

echo ""
echo "==> Status:"
sudo systemctl status nginx --no-pager | head -6
sudo systemctl status ringcatch-cloudflared --no-pager | head -6
echo ""
echo "Failover active. Cloudflare will route to archbox when main PC goes offline."
