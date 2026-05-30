# RingCatch Agency — Claude Code Context

**Owner:** Mike Olszewski (molszewski423@gmail.com)  
**Wife:** Gergana Olszewski (gnikol87@gmail.com) — receives daily stats email  
**Last updated:** 2026-05-21

---

## BUSINESS

- **Product:** AI chatbots for local SMBs — $450 setup + $89/month recurring
- **Annual option:** $890/year ($74/mo) — offer before discounting
- **Floor:** Never below $69/month
- **Sprint goal:** 3 paying clients by 2026-06-01
- **Current:** 0 clients, pre-revenue. ~1,271 emails sent, 0.4% reply rate, 90 clicks to /book, 0 conversions

---

## MACHINES

| Machine | Role | Tailscale IP |
|---|---|---|
| **archbox** | All 13 agents, 24/7 | 100.96.122.27 |
| **MikePC (Debian 13) | 100.97.45.57 |
| **nixbook** | Mike's daily driver | — |

SSH to archbox: `ssh mike@100.96.122.27` (password auth enabled)  
**Always use `127.0.0.1` not `localhost` when curling archbox services via SSH** — IPv6 causes resets with passt proxy.

---

## SHELL

Mike uses **Fish shell**. Always write Fish-compatible commands:
- `source activate.fish` not `source activate`
- `set -x VAR val` not `export VAR=val`
- No bash-isms

---

## INFRASTRUCTURE

All agents run as rootless Podman containers in `agency-pod` on archbox.  
Managed by systemd quadlets at `~/.config/containers/systemd/` on archbox.

```fish
# Start/stop
systemctl --user restart agency-orchestrator

# Logs
journalctl --user -u agency-orchestrator -n 50 -f

# Rebuild after code change (MUST build from orchestrator/ dir — COPY main.py uses context root)
cd ~/agency/orchestrator
podman build -t localhost/agency-orchestrator:latest .
systemctl --user restart agency-orchestrator
```

Config: `~/agency/.env` on archbox (shared via EnvironmentFile in every container)  
Database: `agency-data` volume → `/data/agency.db` inside containers

### Port Map

| Agent | Port |
|---|---|
| agency-command (dashboard) | 8100 |
| agency-legal | 8101 |
| agency-marketing | 8102 |
| agency-discord | 8103 |
| agency-support | 8104 |
| agency-success | 8105 |
| agency-bi | 8106 |
| agency-sales | 8107 |
| agency-orchestrator | 8109 |
| agency-inbox | 8110 |
| agency-video | 8111 |
| agency-scraper | 8079 |
| agency-outreach | 8080 |
| agency-billing | 8082 |

Public: dashboard.ringcatch.io → 8100, ringcatch.io → Cloudflare Pages, ringcatch.io/book → outreach /chat

---

## LLM STACK (as of 2026-05-21)

### Orchestrator + Webchat
Gemini 2.5 Flash → Cerebras llama3.1-8b → Ollama qwen2.5:14b (MikePC 100.97.45.57) → Groq llama-3.3-70b-versatile

### Outreach
Gemini 2.5 Flash → Groq llama-3.1-8b-instant → Ollama gemma4:26b

### Video scripts
Gemini 2.5 Flash

### Key facts
- Gemini endpoint: `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions`
- Ollama is on **MikePC (Debian 13) | 100.97.45.57:11434`) — NOT archbox
- Cerebras model: `llama3.1-8b` (NOT llama-3.3-70b — that doesn't exist on Cerebras)
- GEMINI_API_KEY is in ~/agency/.env (Google AI Pro $20/mo subscription)

---

## WHAT WAS BUILT / FIXED (sessions through 2026-05-21)

| Item | Status |
|---|---|
| Gemini-first LLM stack (orchestrator + outreach + webchat) | ✅ Done |
| Personalized Gemini email openers (unique per lead, city+niche) | ✅ Done |
| Gemini 2.5 Flash for video scripts (was 2.0 Flash) | ✅ Done |
| Imagen 3 thumbnail generation for YouTube videos | ✅ Done |
| Email daily limit enforcement in follow-up steps | ✅ Fixed (was ignoring cap) |
| Discord bot /chat 404 (stale orchestrator image) | ✅ Fixed 2026-05-21 |
| dashboard.ringcatch.io 502 | ✅ Fixed (wrong port 8501→8100) |
| Ollama URL wrong (was archbox, fixed to MikeNixPC) | ✅ Fixed |
| Cerebras wrong model name | ✅ Fixed |
| Groq 413 payload too large | ✅ Fixed (token trimmer added) |
| SSH MikeNixPC → archbox | ✅ Fixed (password auth enabled on archbox) |

---

## PENDING TASKS

### Immediate
- [ ] Enable video timer on MikeNixPC: `systemctl --user enable --now ringcatch-video.timer`
- [ ] Investigate the 4 email replies found by inbox agent 2026-05-21
- [ ] Add nixbook SSH key to archbox `~/.ssh/authorized_keys`
- [ ] YouTube channel About page: manually add ringcatch.io link
- [ ] Groq developer plan upgrade (temporarily unavailable)

### This week
- [ ] LinkedIn business page for RingCatch
- [ ] TikTok @RingCatch_io account (API pending developer approval)
- [ ] Gmail API setup for instant reply detection (browser OAuth — see GEMINI_HANDOFF.md §17)
- [ ] n8n first-time browser setup (container at port 5678 on archbox)

### Decision point 2026-06-03
If ≥1 paying client → upgrade Brevo Business (~$25/mo), raise EMAIL_DAILY_LIMIT from 135 to 300–500.

---

## VIDEO PIPELINE

- **Generator: MikePC (RTX 5060 Ti) (RTX 5060 Ti, NVENC)
- **Rotation:** 25 niches, one per night starting at 2 AM
- **Script:** Gemini 2.5 Flash | **TTS:** Google Cloud TTS Journey-F | **Thumbnail:** Imagen 3
- **Niche index:** `~/agency/videos/.niche_index`
- **After upload:** rsync JSON metadata to archbox `/data/videos/`; delete video file 7 days after upload
- **4 live videos:** HVAC, Dental, Plumbing, Roofing on @RingCatch_io
- **Timer status:** NOT yet enabled on MikeNixPC as of 2026-05-21

---

## FILE STRUCTURE

```
~/agency/
├── .env                    # all secrets — archbox only
├── CLAUDE.md               # this file
├── GEMINI_HANDOFF.md       # full context doc for Gemini CLI
├── orchestrator/main.py    # AI brain — FastAPI, Gemini-first, /chat endpoint
├── outreach/main.py        # email + /book sales chat (Alex persona)
├── scraper/main.py         # Google Maps scraper
├── video/main.py           # YouTube Short generation (runs on MikeNixPC)
├── discord_bot/main.py     # Discord bot (calls orchestrator /chat)
├── sales/main.py           # lead qualification
├── billing/main.py         # Stripe webhooks
├── delivery/main.py        # post-payment chatbot delivery
├── success/main.py         # client onboarding sequences
├── marketing/main.py       # social content generation
├── bi/main.py              # BI reports
├── support/main.py         # health monitoring
├── inbox/main.py           # IMAP reply watcher
├── knowledge/              # AI knowledge base articles
├── quadlets/               # systemd container definitions (archbox)
└── targets.yaml            # scrape targets (niche × city)
```

---

## SUBSCRIPTIONS

| Service | Purpose | Cost |
|---|---|---|
| Google AI Pro | Gemini 2.5 Flash + Cloud TTS + Imagen 3 | $20/mo |
| Brevo | Email sending (4,080/mo) | ~$12/mo |
| Cerebras | llama3.1-8b fallback LLM | ~$10 credit |
| Stripe | Payments | 2.9%+30¢ |
| Cloudflare | Tunnel + DNS + Pages | Free |
| Cal.com | Discovery call bookings | Free |
| Tailscale | VPN mesh | Free |

---

## QUERYING THE LIVE ORCHESTRATOR

```bash
# Full status report
curl -s -X POST http://100.96.122.27:8100/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Give me a full status report", "session_id": "cli"}' | python3 -m json.tool

# Agent health
curl -s http://100.96.122.27:8100/api/agents | python3 -m json.tool

# Recent activity
curl -s "http://100.96.122.27:8100/api/activity?limit=20" | python3 -m json.tool
```

---

## GOTCHAS

1. Ollama is on MikePC (Debian 13) | 100.97.45.57:11434), NOT archbox
2. Cerebras model = `llama3.1-8b` (not `llama-3.3-70b`, not `llama3.1:8b`)
3. Use `127.0.0.1` not `localhost` when curling archbox services via SSH
4. Fish shell everywhere — no bash syntax
5. After editing Python on MikeNixPC: rsync → podman build → systemctl restart on archbox
6. `set_pricing_mode` tool requires Mike's explicit instruction — never call autonomously
