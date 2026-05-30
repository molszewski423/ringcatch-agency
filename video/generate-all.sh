#!/usr/bin/env bash
# Generates one video per niche sequentially — runs on MikeNixPC Monday mornings
set -euo pipefail

VENV=/home/mike/.venv/video/bin/python3
SCRIPT=/home/mike/agency/video/generate.py

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
)

for niche in "${NICHES[@]}"; do
    echo "▸ Generating: $niche"
    $VENV $SCRIPT --niche "$niche" && echo "  ✓ $niche done" || echo "  ✗ $niche failed"
    sleep 30  # brief pause between jobs
done

echo "All niches complete."
