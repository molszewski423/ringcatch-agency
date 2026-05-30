#!/usr/bin/env bash
# Nightly video generation — runs at 2 AM daily, rotates one niche per night.
# State file tracks which niche is next so we cycle evenly across all 25.
set -euo pipefail

VENV=/home/mike/.venv/video/bin/python3
SCRIPT=/home/mike/agency/video/generate.py
STATE=/home/mike/agency/videos/.niche_index
LOG_DIR=/home/mike/agency/videos/logs
mkdir -p "$LOG_DIR"

NICHES=(
    "HVAC"
    "Plumbing"
    "Dental"
    "Auto Repair"
    "Law Firm"
    "Property Management"
    "Landscaping"
    "Roofing"
    "Pest Control"
    "Electrician"
    "Hair Salon"
    "Veterinary"
    "Chiropractic"
    "Physical Therapy"
    "Moving Company"
    "House Painting"
    "Home Cleaning"
    "Pool Service"
    "Tree Service"
    "Locksmith"
    "Daycare"
    "Towing"
    "Personal Training"
    "Tax Preparation"
    "Restaurant"
)
N=${#NICHES[@]}

# Read current index (default 0)
if [[ -f "$STATE" ]]; then
    IDX=$(<"$STATE")
else
    IDX=0
fi

# Clamp to valid range
IDX=$(( IDX % N ))
NICHE="${NICHES[$IDX]}"

echo "$(date '+%Y-%m-%d %H:%M:%S') — Nightly video: $NICHE (index $IDX / $N)"

# Clean up uploaded videos older than 7 days
CLEANUP_LOG="$LOG_DIR/$(date +%Y%m%d)_cleanup.log"
$VENV "$SCRIPT" --cleanup >"$CLEANUP_LOG" 2>&1 || true

# Upload any videos that failed last time before generating a new one
RETRY_LOG="$LOG_DIR/$(date +%Y%m%d)_retry.log"
$VENV "$SCRIPT" --retry-pending >"$RETRY_LOG" 2>&1 || true

LOG="$LOG_DIR/$(date +%Y%m%d)_${NICHE// /_}.log"

if $VENV "$SCRIPT" --niche "$NICHE" >"$LOG" 2>&1; then
    echo "  ✓ $NICHE done — $(tail -1 "$LOG")"
    # Advance to next niche
    echo $(( (IDX + 1) % N )) > "$STATE"
else
    echo "  ✗ $NICHE failed — check $LOG"
    # Still advance so a broken niche doesn't block the rotation
    echo $(( (IDX + 1) % N )) > "$STATE"
    exit 1
fi
