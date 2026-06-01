#!/usr/bin/env bash
# Cloudflare Tunnel setup for ringcatch.io
# Run once before starting the agency-tunnel container.
#
# What this script does:
#   1. Creates (or reuses) a Cloudflare named tunnel called "ringcatch"
#   2. Writes the tunnel token to .env as CLOUDFLARE_TUNNEL_TOKEN
#   3. Configures ingress: ringcatch.io → http://localhost:8090
#   4. Upserts the CNAME DNS record in Cloudflare
#
# Prerequisites:
#   - curl and jq must be available (dnf install jq / toolbox run jq)
#   - CLOUDFLARE_API_TOKEN in .env with permissions:
#       Cloudflare Tunnel: Edit
#       DNS:               Edit
#       Zone: Zone:        Read
#
# Usage:
#   bash ~/agency/scripts/tunnel-setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
ZONE_ID="${CLOUDFLARE_ZONE_ID:?CLOUDFLARE_ZONE_ID not set in .env}"
TUNNEL_NAME="ringcatch"
LANDING_BACKEND="http://localhost:8090"
API="https://api.cloudflare.com/client/v4"

# ── Preflight ─────────────────────────────────────────────────────────────────
for bin in curl jq openssl; do
    if ! command -v "$bin" &>/dev/null; then
        echo "[error] $bin not found — install it and retry" >&2
        echo "  Fedora: toolbox run sudo dnf install -y $bin" >&2
        exit 1
    fi
done

read_env() { grep -m1 "^${1}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2-; }

TOKEN="$(read_env CLOUDFLARE_API_TOKEN)"
if [ -z "$TOKEN" ] || [[ "$TOKEN" == *"your_"* ]]; then
    echo "[error] CLOUDFLARE_API_TOKEN is not set in .env" >&2
    echo "  Create one at dash.cloudflare.com → My Profile → API Tokens" >&2
    echo "  Permissions needed: Cloudflare Tunnel:Edit  DNS:Edit  Zone:Read" >&2
    exit 1
fi

H_AUTH="Authorization: Bearer $TOKEN"
H_JSON="Content-Type: application/json"

cf()  { curl -sf -H "$H_AUTH" "$@"; }
cfj() { curl -sf -H "$H_AUTH" -H "$H_JSON" "$@"; }

# ── 1. Get account ID from zone ───────────────────────────────────────────────
echo "==> Resolving account from zone $ZONE_ID..."
ZONE_RESP=$(cf "$API/zones/$ZONE_ID")
SUCCESS=$(echo "$ZONE_RESP" | jq -r '.success')
if [ "$SUCCESS" != "true" ]; then
    echo "[error] Zone lookup failed — check API token permissions:" >&2
    echo "$ZONE_RESP" | jq '.errors' >&2
    exit 1
fi
ACCOUNT_ID=$(echo "$ZONE_RESP" | jq -r '.result.account.id')
echo "    Account ID: $ACCOUNT_ID"

# ── 2. Create or reuse tunnel ─────────────────────────────────────────────────
echo "==> Checking for existing tunnel '$TUNNEL_NAME'..."
TUNNEL_LIST=$(cf "$API/accounts/$ACCOUNT_ID/cfd_tunnel?name=$TUNNEL_NAME&is_deleted=false")
TUNNEL_ID=$(echo "$TUNNEL_LIST" | jq -r '.result[0].id // empty')

if [ -n "$TUNNEL_ID" ]; then
    echo "    Tunnel exists: $TUNNEL_ID"
    EXISTING_TK="$(read_env CLOUDFLARE_TUNNEL_TOKEN)"
    if [ -z "$EXISTING_TK" ] || [[ "$EXISTING_TK" == *"your_"* ]]; then
        echo ""
        echo "[!] Tunnel already exists but CLOUDFLARE_TUNNEL_TOKEN is not in .env."
        echo "    Cloudflare does not re-expose the token after creation."
        echo "    Options:"
        echo "      a) Delete the tunnel in Zero Trust → Networks → Tunnels → $TUNNEL_NAME"
        echo "         then re-run this script."
        echo "      b) Copy the token from Zero Trust → Tunnels → $TUNNEL_NAME → Configure"
        echo "         and set CLOUDFLARE_TUNNEL_TOKEN in .env manually."
        exit 1
    fi
    echo "    CLOUDFLARE_TUNNEL_TOKEN already present in .env — reusing."
else
    echo "==> Creating tunnel '$TUNNEL_NAME'..."
    SECRET=$(openssl rand -base64 32)
    CREATE_RESP=$(cfj -X POST "$API/accounts/$ACCOUNT_ID/cfd_tunnel" \
        -d "{\"name\":\"$TUNNEL_NAME\",\"tunnel_secret\":\"$SECRET\"}")
    if [ "$(echo "$CREATE_RESP" | jq -r '.success')" != "true" ]; then
        echo "[error] Tunnel creation failed:" >&2
        echo "$CREATE_RESP" | jq '.errors' >&2
        exit 1
    fi
    TUNNEL_ID=$(echo "$CREATE_RESP" | jq -r '.result.id')
    TUNNEL_TOKEN=$(echo "$CREATE_RESP" | jq -r '.result.token')
    echo "    Created: $TUNNEL_ID"

    # Write token into .env
    if grep -q '^CLOUDFLARE_TUNNEL_TOKEN=' "$ENV_FILE"; then
        sed -i "s|^CLOUDFLARE_TUNNEL_TOKEN=.*|CLOUDFLARE_TUNNEL_TOKEN=$TUNNEL_TOKEN|" "$ENV_FILE"
    else
        # Insert after the CLOUDFLARE_API_TOKEN line
        sed -i "/^CLOUDFLARE_API_TOKEN=/a CLOUDFLARE_TUNNEL_TOKEN=$TUNNEL_TOKEN" "$ENV_FILE"
    fi
    echo "    CLOUDFLARE_TUNNEL_TOKEN written to .env"
fi

# ── 3. Configure ingress rules (remotely managed tunnel) ──────────────────────
echo "==> Setting ingress: ringcatch.io → $LANDING_BACKEND..."
INGRESS_RESP=$(cfj -X PUT \
    "$API/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" \
    -d "{
        \"config\": {
            \"ingress\": [
                {\"hostname\": \"ringcatch.io\", \"service\": \"$LANDING_BACKEND\"},
                {\"service\": \"http_status:404\"}
            ]
        }
    }")
if [ "$(echo "$INGRESS_RESP" | jq -r '.success')" = "true" ]; then
    echo "    Ingress rules saved."
else
    echo "[!] Ingress config failed (may need to set in Zero Trust dashboard):" >&2
    echo "$INGRESS_RESP" | jq '.errors' >&2
fi

# ── 4. Upsert CNAME DNS record ────────────────────────────────────────────────
CNAME_CONTENT="$TUNNEL_ID.cfargotunnel.com"
echo "==> Upserting CNAME  ringcatch.io → $CNAME_CONTENT..."
DNS_PAYLOAD=$(jq -n \
    --arg content "$CNAME_CONTENT" \
    '{"type":"CNAME","name":"@","content":$content,"ttl":1,"proxied":true}')

EXISTING_ID=$(cf "$API/zones/$ZONE_ID/dns_records?type=CNAME&name=ringcatch.io" \
    | jq -r '.result[0].id // empty')

if [ -n "$EXISTING_ID" ]; then
    DNS_RESP=$(cfj -X PUT "$API/zones/$ZONE_ID/dns_records/$EXISTING_ID" -d "$DNS_PAYLOAD")
    ACTION="updated"
else
    DNS_RESP=$(cfj -X POST "$API/zones/$ZONE_ID/dns_records" -d "$DNS_PAYLOAD")
    ACTION="created"
fi

if [ "$(echo "$DNS_RESP" | jq -r '.success')" = "true" ]; then
    echo "    CNAME $ACTION: @ → $CNAME_CONTENT (proxied)"
else
    echo "[!] DNS upsert failed:" >&2
    echo "$DNS_RESP" | jq '.errors' >&2
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "╔════════════════════════════════════════════════════════════╗"
echo "║  Cloudflare tunnel provisioned                            ║"
echo "╠════════════════════════════════════════════════════════════╣"
printf "║  Tunnel   %-48s║\n" "$TUNNEL_NAME ($TUNNEL_ID)"
printf "║  Route    %-48s║\n" "ringcatch.io → $LANDING_BACKEND"
printf "║  DNS      %-48s║\n" "@ CNAME → $CNAME_CONTENT (proxied)"
echo "╠════════════════════════════════════════════════════════════╣"
echo "║  NEXT: systemctl --user daemon-reload                     ║"
echo "║  NEXT: systemctl --user restart agency-tunnel.service     ║"
echo "║  NEXT: curl -I https://ringcatch.io  (verify live)        ║"
echo "╚════════════════════════════════════════════════════════════╝"
