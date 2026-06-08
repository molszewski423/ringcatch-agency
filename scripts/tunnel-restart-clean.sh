#!/usr/bin/env bash
# Purge stale CF connection records and restart Podman tunnel service.
set -euo pipefail

ENV_FILE="$HOME/agency/.env"
TOKEN=$(grep CLOUDFLARE_API_TOKEN "$ENV_FILE" | cut -d= -f2)
ACCOUNT=c7f77378cf53b5436dacbc6a8e673cc4
TUNNEL_ID=2ef09425-ed87-4c07-a0e4-ecca2041dcdf

# Purge stale Cloudflare connection records
curl -sf -X DELETE \
  -H "Authorization: Bearer $TOKEN" \
  "https://api.cloudflare.com/client/v4/accounts/$ACCOUNT/cfd_tunnel/$TUNNEL_ID/connections" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); exit(0 if d["success"] else 1)'

sleep 2

# Restart the Podman systemd service
systemctl --user restart agency-tunnel.service
logger -t tunnel-restart "agency-tunnel.service restarted via systemd"
