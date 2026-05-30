#!/usr/bin/env bash
# Purge stale CF tunnel connections. Always exits 0 so a network blip never blocks restart.
TOKEN=$(grep CLOUDFLARE_API_TOKEN "$HOME/agency/.env" | cut -d= -f2)
ACCOUNT=c7f77378cf53b5436dacbc6a8e673cc4
TUNNEL_ID=2ef09425-ed87-4c07-a0e4-ecca2041dcdf
API="https://api.cloudflare.com/client/v4/accounts/$ACCOUNT/cfd_tunnel/$TUNNEL_ID"

# Delete connections — ignore failures (internet may be briefly down)
curl -sf --max-time 10 -X DELETE -H "Authorization: Bearer $TOKEN" "$API/connections" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); exit(0 if d[\"success\"] else 1)" \
  || true

# Wait up to 15s for CF to confirm 0 connections — skip silently if API unreachable
for i in $(seq 1 7); do
  COUNT=$(curl -sf --max-time 5 -H "Authorization: Bearer $TOKEN" "$API/connections" \
    | python3 -c "import json,sys; print(len(json.load(sys.stdin).get(\"result\") or []))" 2>/dev/null) || true
  [[ "$COUNT" == "0" ]] && exit 0
  sleep 2
done
exit 0
