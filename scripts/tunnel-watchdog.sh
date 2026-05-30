#!/usr/bin/env bash
# Restart tunnel only after 2 consecutive 502s — avoids thrashing on transient CF blips.
set -euo pipefail

STATE_FILE=/tmp/tunnel-watchdog-failures

STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 https://ringcatch.io)

if [[ "$STATUS" == "502" || "$STATUS" == "000" ]]; then
  FAILS=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
  FAILS=$((FAILS + 1))
  echo "$FAILS" > "$STATE_FILE"
  if [[ "$FAILS" -ge 2 ]]; then
    logger -t tunnel-watchdog "ringcatch.io $STATUS for $FAILS consecutive checks — restarting"
    rm -f "$STATE_FILE"
    ~/agency/scripts/tunnel-restart-clean.sh
    logger -t tunnel-watchdog "tunnel restarted"
  else
    logger -t tunnel-watchdog "ringcatch.io $STATUS (check $FAILS/2 — waiting for confirmation)"
  fi
else
  rm -f "$STATE_FILE"
  logger -t tunnel-watchdog "ringcatch.io OK ($STATUS)"
fi
