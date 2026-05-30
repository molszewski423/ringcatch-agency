# RingCatch Agent Suite — Build Context
# Last updated: 2026-05-16 21:45 EDT by Claude Code
# SSH working — Mike now has 2 laptop terminals into this PC

## HOW TO INTERACT WITH LOCAL MODELS
Run this in a terminal to ask local models questions:

```python
python3 - <<'EOF'
import requests

def ask(prompt, model="gemma4:26b"):
    r = requests.post("http://localhost:11434/api/generate",
        json={"model": model, "prompt": prompt, "stream": False}, timeout=300)
    return r.json()["response"]

print(ask("What needs to be done next to complete the RingCatch agent suite? Read BUILD_CONTEXT.md first."))
EOF
```

## CURRENT STATUS (2026-05-16 19:50 EDT)

### ✅ DONE — All agents built and running
| Agent | Port | Status | Health URL |
|-------|------|--------|-----------|
| agency-command | 8100 | active | localhost:8100/health |
| agency-legal | 8101 | active | localhost:8101/health |
| agency-marketing | 8102 | active | localhost:8102/health |
| agency-cfo | 8103 | active | localhost:8103/health |
| agency-support | 8104 | active | localhost:8104/health |
| agency-success | 8105 | active | localhost:8105/health |
| agency-bi | 8106 | active | localhost:8106/health |
| agency-sales | 8107 | active | localhost:8107/health |

All images built: `podman images | grep agency`
All Quadlets in: ~/.config/containers/systemd/
All source in: ~/agency/{support,cfo,legal,sales,marketing,success,bi,command}/

### ✅ DONE (2026-05-16 21:45 EDT)

1. ✅ Pod restarted with ports 8100-8107 published
2. ✅ All 8 agents active and passing health checks
3. ✅ DB schema migrations applied (churn_risk, pipeline_stage, last_activity columns)
4. ✅ Integration test passed — all 8 health checks green
5. ✅ SSH enabled — Mike has 2 laptop terminals into 192.168.4.54
6. ✅ Podman socket enabled (/run/user/1000/podman/podman.sock)

### ❌ REMAINING — Mike's manual tasks

1. **Add real Discord webhook URL to .env**
   ```bash
   nano ~/agency/.env
   # Replace DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN
   # Get from: Discord → Server Settings → Integrations → Webhooks → New Webhook
   ```
   Then restart agents: `systemctl --user restart agency-support agency-cfo agency-marketing agency-bi`

2. **Add dashboard.ringcatch.io to Cloudflare tunnel**
   Cloudflare Zero Trust → Networks → Tunnels → ringcatch → Configure →
   Add Public Hostname: dashboard.ringcatch.io → http://127.0.0.1:8100
   (support agent will stop logging EXTERNAL_DOWN alerts once this resolves)

3. **Add cfo.ringcatch.io to Cloudflare tunnel** (for Stripe webhooks)
   Add Public Hostname: cfo.ringcatch.io → http://127.0.0.1:8103
   Then in Stripe dashboard: point webhook to https://cfo.ringcatch.io/stripe-webhook

4. **Reboot when convenient** — tmux + NVIDIA/SDDM updates pending via rpm-ostree

### ⚠️ KNOWN ISSUE
- Agents reachable via 192.168.4.54:PORT not localhost:PORT
  (pasta networking quirk — ports bind to host IP, not loopback)
- Integration tests should use: HOST=192.168.4.54

## FILE LOCATIONS

Source code (edit these):
- ~/agency/support/main.py   - Support agent (port 8104)
- ~/agency/command/main.py   - Dashboard backend (port 8100)  
- ~/agency/command/static/index.html - React dashboard UI
- ~/agency/cfo/main.py       - CFO agent (port 8103)
- ~/agency/legal/main.py     - Legal agent (port 8101)
- ~/agency/sales/main.py     - Sales agent (port 8107)
- ~/agency/marketing/main.py - Marketing agent (port 8102)
- ~/agency/success/main.py   - Success agent (port 8105)
- ~/agency/bi/main.py        - BI agent (port 8106)
- ~/agency/.env              - ALL secrets (add DISCORD_WEBHOOK_URL)

Quadlets (system config, deployed to ~/.config/containers/systemd/):
- ~/agency/quadlets/agency-pod.pod  ← UPDATED with ports 8100-8107
- ~/agency/quadlets/agency-{name}.container

## HOW TO REBUILD A SINGLE AGENT

```bash
cd ~/agency
# Edit the agent code
nano {agent_name}/main.py

# Rebuild image
podman build -t localhost/agency-{name}:latest -f {name}/Containerfile {name}/

# Restart just that service
systemctl --user restart agency-{name}.service

# Check logs
journalctl --user -u agency-{name}.service -f
```

## HOW TO CHECK AGENT STATUS

```bash
# All agents at once
for a in support cfo legal sales marketing success bi command; do
  echo "$a: $(systemctl --user is-active agency-$a)"
done

# Individual agent JSON status
curl -s http://localhost:8100/api/agents | python3 -m json.tool

# Activity log
curl -s http://localhost:8100/api/activity | python3 -m json.tool

# CFO metrics
curl -s http://localhost:8103/status | python3 -m json.tool

# BI executive summary (uses gemma4:26b - takes 30-90s)
curl -s http://localhost:8106/executive-summary | python3 -m json.tool
```

## INTER-AGENT COMMUNICATION

Agents talk via SQLite event_bus table at /data/agency.db.
To manually trigger an event:
```python
import sqlite3, json
db = sqlite3.connect("/var/home/mike/agency/data/agency.db")
db.execute("INSERT INTO event_bus (source_agent,target_agent,event_type,payload) VALUES (?,?,?,?)",
    ("manual", "agency-legal", "NEW_CLIENT", json.dumps({"customer_email":"test@example.com","business_name":"Test Co","amount":450})))
db.commit()
```

## KNOWN ISSUES / NEXT STEPS

1. Port 8080 = outreach, 8081 = delivery (these are INSIDE the pod on those ports)
2. Agency-support mounts /run/podman/podman.sock — requires podman.socket enabled (now done)
3. Cloudflare tunnel needs dashboard.ringcatch.io entry added manually
4. DISCORD_WEBHOOK_URL not yet in .env - agents will skip Discord alerts silently
5. Stripe webhook needs to point to /stripe-webhook on CFO agent (port 8103 internally)
   From outside: add ingress cfo.ringcatch.io → 127.0.0.1:8103 in tunnel, or use outreach webhook

## ARCHITECTURE SUMMARY (from gemma4:26b)
- Blackboard pattern: agents read/write event_bus table in SQLite
- All containers share agency-pod network namespace (so 127.0.0.1:PORT works between them)
- Shared volume agency-data mounted at /data in all containers
- DB at /data/agency.db with WAL mode
- No direct agent-to-agent HTTP calls for data (only event_bus)
- command dashboard (8100) reads all /status endpoints + SQLite directly
