#!/usr/bin/env bash
# Nightly backup of RingCatch agency data to archbox over Tailscale
set -euo pipefail

REMOTE="mike@100.96.122.27"
REMOTE_DIR="/home/mike/backups/agency"
DATA_VOL="/home/mike/.local/share/containers/storage/volumes/agency-data/_data"
AGENCY_DIR="/home/mike/agency"
LOG="/tmp/agency-backup.log"

echo "[$(date -Iseconds)] Starting agency backup to archbox" | tee -a "$LOG"

# Ensure remote directory exists
ssh -o ConnectTimeout=10 -o BatchMode=yes "$REMOTE" "mkdir -p $REMOTE_DIR/data $REMOTE_DIR/env $REMOTE_DIR/knowledge"

# SQLite db — use a hot-safe copy via sqlite3 .backup
if command -v sqlite3 &>/dev/null && [ -f "$DATA_VOL/agency.db" ]; then
    sqlite3 "$DATA_VOL/agency.db" ".backup /tmp/agency-backup.db"
    rsync -az --no-perms /tmp/agency-backup.db "$REMOTE:$REMOTE_DIR/data/agency.db"
    rm -f /tmp/agency-backup.db
    echo "  ✓ SQLite database backed up" | tee -a "$LOG"
fi

# Strategy/knowledge files from data volume
rsync -az --no-perms --exclude="*.db" "$DATA_VOL/" "$REMOTE:$REMOTE_DIR/data/" 2>/dev/null || true
echo "  ✓ Data volume files synced" | tee -a "$LOG"

# .env (strip secrets from log but back it up)
rsync -az --no-perms "$AGENCY_DIR/.env" "$REMOTE:$REMOTE_DIR/env/.env"
echo "  ✓ .env backed up" | tee -a "$LOG"

# Knowledge base
rsync -az --no-perms "$AGENCY_DIR/knowledge/" "$REMOTE:$REMOTE_DIR/knowledge/" 2>/dev/null || true
echo "  ✓ Knowledge base synced" | tee -a "$LOG"

echo "[$(date -Iseconds)] Backup complete" | tee -a "$LOG"
