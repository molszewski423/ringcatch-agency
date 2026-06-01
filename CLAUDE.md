# RingCatch Agency — Claude Code Context

**Product:** AI chatbots for local SMBs — $450 setup + $89/month  
**Stack:** 24 FastAPI microservices on k3s (archbox worker), PostgreSQL, Ollama, Brevo, Cloudflare Tunnel  
**Public:** ringcatch.io → agency-landing via Cloudflare Tunnel (no open ports)  
**Dashboard:** dashboard.ringcatch.io → agency-command:8100

---

## Infrastructure

All services run as k3s Deployments in the `agency` namespace on archbox. Manifests in `~/homelab-infra/k8s/agency.yaml` on mikepc.

```fish
# Check all services
ssh mikepc "kubectl get pods -n agency"

# Logs
ssh mikepc "kubectl logs -n agency deployment/agency-orchestrator --tail=50"

# Restart a service
ssh mikepc "kubectl rollout restart deployment/agency-orchestrator -n agency"

# Rebuild after Python edits on archbox:
# 1. podman build -t localhost/agency-<service>:latest .
# 2. podman save localhost/agency-<service>:latest | sudo k3s ctr -n k8s.io images import -
# 3. ssh mikepc "kubectl rollout restart deployment/agency-<service> -n agency"
```

Secrets: `~/agency/.env` on archbox — never commit.

---

## LLM Routing

Gemini 2.5 Flash → Ollama gemma4:26b → Groq llama-3.3-70b → Groq llama-3.1-8b

Ollama endpoint: `http://ollama.ai:11434` (cross-namespace) or `$OLLAMA_BASE_URL` from `.env`

---

## Key Files

```
orchestrator/main.py   # AI brain — FastAPI, 22 tool endpoints
outreach/main.py       # Email sending + /book sales chat (Alex persona)
scraper/main.py        # Google Maps lead scraper
delivery/main.py       # Chatbot delivery + onboarding
landing/nginx.conf     # nginx proxy: /api/chat/* → agency-outreach:8080
knowledge/             # Markdown KB articles injected into LLM prompts
```

---

## Rules

- Never call `set_pricing_mode` autonomously — requires explicit instruction
- `.env` lives only on archbox — never in git
- Custom images are `localhost/agency-*:latest` in k3s containerd — import manually after each rebuild

---

## Infrastructure notes (updated 2026-06-01)

**Podman is stopped.** The old `agency-pod` Podman pod was stopped 2026-06-01 after full k3s migration. Do not start it — all services run exclusively in k3s.

**agency-tunnel specifics:**
- `hostPID: true` + init container kills orphaned cloudflared (argv0 match) before registering new connections
- `strategy: Recreate` — no rolling overlap
- `--metrics 0.0.0.0:2000` + liveness probe on httpGet /ready :2000 (30s delay, 60s period, 3 failures)
- If site is down with 502/1033: check `kubectl get pods -n agency -l app=agency-tunnel` first

**outreach/main.py `_parse_suggestions()`:**
Always strips SUGGEST: [...] block from chat text before returning. Uses comma-split fallback when LLM returns unquoted items (json.loads would fail and previously returned full text including the raw instruction).
