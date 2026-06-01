#!/usr/bin/env bash
# Run on archbox — backs up agency data to MikeNixPC over Tailscale
set -euo pipefail

REMOTE="mike@REMOTE_HOST"
REMOTE_DIR="/home/mike/backups/agency-from-archbox"
DATA_VOL="$HOME/.local/share/containers/storage/volumes/agency-data/_data"
LOG="/tmp/agency-backup-to-nixpc.log"

echo "[$(date -Iseconds)] Backing up to MikeNixPC..." | tee -a "$LOG"

ssh -o ConnectTimeout=10 -o BatchMode=yes "$REMOTE" "mkdir -p $REMOTE_DIR/data"

# Hot-safe DB copy
if command -v sqlite3 &>/dev/null && [ -f "$DATA_VOL/agency.db" ]; then
    sqlite3 "$DATA_VOL/agency.db" ".backup /tmp/archbox-backup.db"
    rsync -az --no-perms /tmp/archbox-backup.db "$REMOTE:$REMOTE_DIR/data/agency.db"
    rm -f /tmp/archbox-backup.db
fi

rsync -az --no-perms --exclude="*.db" "$DATA_VOL/" "$REMOTE:$REMOTE_DIR/data/" 2>/dev/null || true
rsync -az --no-perms "$HOME/agency/.env" "$REMOTE:$REMOTE_DIR/.env"

echo "[$(date -Iseconds)] Backup to MikeNixPC complete" | tee -a "$LOG"
