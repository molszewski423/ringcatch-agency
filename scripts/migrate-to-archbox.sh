#!/usr/bin/env bash
# Full agency stack migration to archbox
# Run from MikeNixPC after: ssh -t archbox "sudo pacman -S --noconfirm podman fuse-overlayfs slirp4netns"
set -euo pipefail

REMOTE="archbox"
AGENCY_DIR="/home/mike/agency"
DATA_VOL="/home/mike/.local/share/containers/storage/volumes/agency-data/_data"
REMOTE_AGENCY="$HOME/agency"

echo "═══════════════════════════════════════════"
echo "  RingCatch → archbox migration"
echo "═══════════════════════════════════════════"

# ── 1. Sync agency source code ────────────────
echo ""
echo "▸ Syncing agency source code..."
ssh "$REMOTE" "mkdir -p ~/agency"
rsync -az --no-perms \
    --exclude=".git" \
    --exclude="__pycache__" \
    --exclude="*.pyc" \
    --exclude="data/" \
    "$AGENCY_DIR/" "$REMOTE:~/agency/"
echo "  ✓ Source synced"

# ── 2. Sync database (hot-safe copy) ──────────
echo ""
echo "▸ Syncing database..."
if command -v sqlite3 &>/dev/null && [ -f "$DATA_VOL/agency.db" ]; then
    sqlite3 "$DATA_VOL/agency.db" ".backup /tmp/agency-migration.db"
    ssh "$REMOTE" "mkdir -p ~/.local/share/containers/storage/volumes/agency-data/_data"
    rsync -az --no-perms /tmp/agency-migration.db \
        "$REMOTE:~/.local/share/containers/storage/volumes/agency-data/_data/agency.db"
    rm -f /tmp/agency-migration.db
    echo "  ✓ Database migrated"
fi

# Also sync other data volume files (knowledge, strategy, etc.)
rsync -az --no-perms --exclude="*.db" \
    "$DATA_VOL/" \
    "$REMOTE:~/.local/share/containers/storage/volumes/agency-data/_data/" 2>/dev/null || true

# ── 3. Create archbox-specific .env ──────────
echo ""
echo "▸ Creating archbox .env (Ollama → MikeNixPC Tailscale)..."
# On archbox, Ollama fallback points to MikeNixPC over Tailscale
sed 's|OLLAMA_BASE_URL=http://host.containers.internal:11434|OLLAMA_BASE_URL=http://100.104.175.99:11434|g' \
    "$AGENCY_DIR/.env" > /tmp/archbox.env
rsync -az --no-perms /tmp/archbox.env "$REMOTE:~/agency/.env"
rm -f /tmp/archbox.env
echo "  ✓ .env ready (Ollama → 100.104.175.99)"

# ── 4. Configure podman on archbox ───────────
echo ""
echo "▸ Configuring podman on archbox..."
ssh "$REMOTE" "
    # Enable lingering so user services survive without login
    loginctl enable-linger mike 2>/dev/null || true

    # Enable podman socket (needed by support agent to monitor containers)
    systemctl --user enable --now podman.socket 2>/dev/null || true

    # Create required directories
    mkdir -p ~/.config/containers/systemd
    mkdir -p ~/.config/systemd/user
    mkdir -p ~/.local/share/containers/storage/volumes/agency-data/_data

    # Copy quadlets
    cp ~/agency/quadlets/*.container ~/.config/containers/systemd/
    cp ~/agency/quadlets/*.pod ~/.config/containers/systemd/

    # Create knowledge volume symlink
    mkdir -p ~/agency/knowledge
"
echo "  ✓ Podman configured"

# ── 5. Build all images on archbox ────────────
echo ""
echo "▸ Building container images on archbox (this takes a few minutes)..."
AGENTS="orchestrator outreach delivery marketing success support sales bi landing scraper billing legal cfo"
for agent in $AGENTS; do
    if [ -d "$AGENCY_DIR/$agent" ] && [ -f "$AGENCY_DIR/$agent/Containerfile" ]; then
        echo -n "  Building agency-$agent... "
        ssh "$REMOTE" "cd ~/agency/$agent && podman build -t localhost/agency-$agent:latest . -q" 2>&1 && echo "✓" || echo "✗ (check logs)"
    fi
done
# discord bot lives in discord_bot/
if [ -d "$AGENCY_DIR/discord_bot" ] && [ -f "$AGENCY_DIR/discord_bot/Containerfile" ]; then
    echo -n "  Building agency-discord... "
    ssh "$REMOTE" "cd ~/agency/discord_bot && podman build -t localhost/agency-discord:latest . -q" 2>&1 && echo "✓" || echo "✗ (check logs)"
fi

# ── 6. Set up systemd on archbox ─────────────
echo ""
echo "▸ Starting agency stack on archbox..."
ssh "$REMOTE" "
    systemctl --user daemon-reload
    systemctl --user enable --now agency-pod-pod.service 2>/dev/null || true
    sleep 3
    for svc in agency-discord agency-orchestrator agency-sales agency-outreach agency-marketing agency-success agency-bi agency-support agency-delivery agency-landing agency-scraper agency-billing; do
        systemctl --user enable \$svc 2>/dev/null || true
        systemctl --user start \$svc 2>/dev/null || true
    done
    sleep 5
    echo 'Service status:'
    systemctl --user is-active agency-landing agency-orchestrator agency-sales agency-outreach 2>&1 || true
"

echo ""
echo "═══════════════════════════════════════════"
echo "  Migration complete!"
echo ""
echo "  Next steps:"
echo "  1. Verify: ssh archbox 'curl -s http://127.0.0.1:8090/' | head -3"
echo "  2. archbox cloudflared is already running (failover setup)"
echo "  3. MikeNixPC stays running as standby — no changes needed"
echo "  4. Reverse backup now runs archbox → MikeNixPC nightly"
echo "═══════════════════════════════════════════"
