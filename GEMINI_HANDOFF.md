# RingCatch Agency — Full Handoff for Gemini CLI

**Date:** 2026-05-21  
**Owner:** Mike Olszewski (molszewski423@gmail.com)  
**Wife:** Gergana Olszewski (gnikol87@gmail.com) — receives daily stats email  
**Purpose:** Complete context transfer so Gemini CLI can assist with this business and codebase with no gaps.

---

## 1. BUSINESS OVERVIEW

**Company:** RingCatch (ringcatch.io)  
**Product:** AI chatbots for local small businesses — HVAC, plumbers, electricians, dental, auto repair, law firms, pest control, landscaping, roofing, property management across US cities.  
**Revenue model:** $450 one-time setup fee + $89/month recurring  
**Annual option:** $890/year ($74/month — offer this before discounting)  
**Negotiation floor:** Never below $69/month. Setup fee can be waived for strong referrals or 6-month prepay.  
**Target:** 100 active clients = $8,900 MRR / $106,800 ARR  
**LTV:** 24 months × $89 + $450 = ~$2,586 per client  
**Current status (as of 2026-05-21):** 0 clients, pre-revenue. 1,271 emails sent, 1.9% open rate, 7.1% click rate (90 clicks to ringcatch.io/book), 0% reply rate.

**Sprint goal:** 3 paying clients by 2026-06-01.

**Why the chatbot sells:**  
Local service businesses miss 30–40% of inbound calls. One missed HVAC call = $300–$3,000. At $89/month the chatbot pays for itself the first time it catches a missed lead.

---

## 2. INFRASTRUCTURE

### Machines

| Machine | Role | Tailscale IP | OS |
|---|---|---|---|
| **archbox** | Primary host — all 12 agents running 24/7 | 100.96.122.27 | Linux (Alienware Alpha, i3-4130T, 16GB RAM) |
| **MikeNixPC** | Video generation (GPU) + Ollama local LLM | 100.104.175.99 | NixOS, RTX 5060 Ti 16GB VRAM |
| **nixbook** | Mike's daily driver laptop | — | NixOS |

SSH to archbox: `ssh mike@100.96.122.27`  
SSH to MikeNixPC: `ssh mike@100.104.175.99`

**IMPORTANT:** Mike uses Fish shell. Always write Fish-compatible commands (`source activate.fish` not `source activate`, use `set -x VAR val` not `export VAR=val`, etc.).

### Containers on archbox

All agents run as rootless Podman containers in a shared pod called `agency-pod`. Managed by systemd quadlets at `~/agency/quadlets/`.

Service names follow pattern: `agency-<name>` (e.g., `agency-orchestrator`, `agency-outreach`)

Start/stop: `systemctl --user start agency-orchestrator`  
Logs: `journalctl --user -u agency-orchestrator -n 50`  
All containers read `~/agency/.env` for config.  
Shared data volume: `agency-data` mounted at `/data` inside containers.

### Port Map

| Agent | Port | Purpose |
|---|---|---|
| agency-command | 8100 | React dashboard (dashboard.ringcatch.io) |
| agency-legal | 8101 | Legal/compliance agent |
| agency-marketing | 8102 | Social content + email A/B |
| agency-discord | 8103 | Discord bot + HTTP alert server |
| agency-support | 8104 | Health monitor |
| agency-success | 8105 | Client onboarding/success |
| agency-bi | 8106 | Business intelligence |
| agency-sales | 8107 | Lead qualification |
| agency-orchestrator | 8109 | AI brain (primary chat endpoint) |
| agency-inbox | 8110 | Email inbox monitoring |
| agency-video | 8111 | YouTube Short generation (on MikeNixPC) |
| agency-scraper | 8079 | Google Maps lead scraper |
| agency-outreach | 8080 | Email sending + /book sales chat |
| agency-delivery | 8081 | Post-payment chatbot delivery |
| agency-billing | 8082 | Stripe webhooks |
| agency-admin | 8112 | Internal admin API (restart agents, read logs, update config) |

### Public URLs

- **Dashboard:** dashboard.ringcatch.io → port 8100 (via Cloudflare tunnel)
- **Website:** ringcatch.io → Cloudflare Pages static site
- **Demo/book:** ringcatch.io/book → interactive sales chat powered by agency-outreach
- **Cloudflare tunnel:** `agency-cloudflared` container, service `agency-tunnel`

### Database

SQLite at `/data/agency.db` (inside the `agency-data` volume).  
Path on host: `~/.local/share/containers/storage/volumes/agency-data/_data/agency.db`

Key tables:
- `leads` — all scraped prospects (business_name, email, city, niche, pipeline_stage, processed, qualified, email_invalid)
- `outreach` — email send history (lead_id, sequence_step, sent_at, opened, clicked, replied)
- `clients` — paying clients (status: active/churned)
- `conversations` — chat history across all sessions (session_id, role, content)
- `activity_log` — event stream across all agents (agent, event_type, message, timestamp)
- `event_bus` — inter-agent messaging (source_agent, target_agent, event_type, payload, status: pending/done)
- `financial_ledger` — all revenue events
- `scheduled_tasks` — future tasks queued by orchestrator
- `chat_analytics` — webchat session metrics (demo seen, lead captured, converted)

Pipeline stages (leads.pipeline_stage): `scraped → emailed → opened → replied → booked → active_client`

---

## 3. LLM STACK (as of 2026-05-21)

This was a major focus of the last session. The stack was updated tonight.

### Orchestrator (chat brain — Discord + dashboard)

**Provider order:** Gemini 2.5 Flash → Cerebras llama3.1-8b → Ollama qwen2.5:14b (on MikeNixPC) → Groq llama-3.3-70b-versatile (emergency)

- Gemini: OpenAI-compatible endpoint at `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions`, uses `GEMINI_API_KEY`
- Cerebras: `https://api.cerebras.ai/v1/chat/completions`, model `llama3.1-8b`, uses `CEREBRAS_API_KEY`
- Ollama: `http://100.104.175.99:11434` (MikeNixPC, not archbox — Ollama doesn't run on archbox), model `qwen2.5:14b`
- Groq: `https://api.groq.com/openai/v1/chat/completions`, model `llama-3.3-70b-versatile`, free tier 12,000 TPM

### Outreach agent (email generation + /book sales chat)

**Provider order:** Gemini 2.5 Flash → Groq llama-3.1-8b-instant → Ollama gemma4:26b

### Video agent (YouTube Short script writing)

**Model:** Gemini 2.5 Flash (updated tonight from 2.0 Flash)

### Why Gemini is primary

Mike pays $20/month for Google AI Pro which includes generous Gemini 2.5 Flash API access. No additional cost. Much higher quality than the old Cerebras llama3.1-8b or Groq fallbacks.

### Cerebras

Paid account. $10 credit loaded. Model: `llama3.1-8b` (NOT llama-3.3-70b which doesn't exist on Cerebras). Used as fallback when Gemini is unavailable. Low cost per token.

---

## 4. KEY ENVIRONMENT VARIABLES (`~/agency/.env` on archbox)

```
# LLM
GEMINI_API_KEY=AIzaSyBsNP52lD09yfRmnBVu4m5ttq8pFdIeUnA
GEMINI_MODEL=gemini-2.5-flash
CEREBRAS_API_KEY=csk-pkd39pjvyt8fn44jmjetrhtdvfmeppv96x5tn3hrnfym92j3
CEREBRAS_MODEL=llama3.1-8b
GROQ_API_KEY=<in .env>
GROQ_MODEL=llama-3.3-70b-versatile
OLLAMA_BASE_URL=http://100.104.175.99:11434  # MikeNixPC, NOT archbox

# Email
EMAIL_PROVIDER=brevo
BREVO_API_KEY=<in .env>
BREVO_SENDER_NAME=Alex from RingCatch
BREVO_SENDER_EMAIL=alex@ringcatch.io
EMAIL_DAILY_LIMIT=135  # Brevo plan: 4,080/month, spreading evenly

# Payments
STRIPE_SETUP_LINK=<Stripe payment link for $450>
STRIPE_MONTHLY_LINK=<Stripe payment link for $89/mo>

# YouTube (for video uploads)
YT_CLIENT_ID=<in .env>
YT_CLIENT_SECRET=<in .env>
YT_REFRESH_TOKEN=<in .env>

# Discord
DISCORD_BOT_TOKEN=<in .env>
OWNER_DISCORD_ID=<Mike's Discord user ID>
CHAT_CHANNEL_NAME=agency-alerts

# Infrastructure
DB_PATH=/data/agency.db
DISCORD_BOT_URL=http://agency-discord:8103/alert
ADMIN_URL=http://host.containers.internal:8112
ORCHESTRATOR_URL=http://127.0.0.1:8109
```

---

## 5. AGENT PIPELINE — HOW IT ALL WORKS

### Automated lead generation flow

```
1. agency-scraper (every 6 hrs if no new leads today)
   → scrapes Google Maps for [niche] businesses in [city]
   → inserts into leads table (pipeline_stage='scraped', processed=0)
   → fires NEW_LEAD event to event_bus

2. agency-sales (polls event_bus every 5 min)
   → qualifies each lead as hot/warm/cold using LLM
   → hot: immediate follow-up with booking link
   → warm: schedules step-2 at day 3, step-3 at day 7
   → cold: marks and skips

3. agency-outreach (autonomous loop every 10 min)
   → picks up all processed=0 leads and sends step-1 email
   → step-2 fires 2 days after step-1 (sprint timing)
   → step-3 fires 5 days after step-1 (sprint timing)
   → step-4 fires 10 days after step-1
   → emails sent via Brevo API (EMAIL_PROVIDER=brevo)

4. Lead clicks link → lands on ringcatch.io/book
   → starts AI sales chat with "Alex" (powered by agency-outreach /chat endpoint)
   → Alex qualifies lead across 5 conversation phases
   → phase 1: discovery (pain point)
   → phase 2: demo transition
   → phase 3: live demo (Alex pretends to BE their AI chatbot)
   → phase 4: close
   → phase 5: objection handling + booking link

5. Lead books demo call → Cal.com webhook → agency-orchestrator /cal-webhook
   → updates lead to pipeline_stage='booked'
   → Discord alert sent to Mike

6. Mike closes the call → lead pays via Stripe
   → Stripe webhook → agency-billing
   → fires NEW_CLIENT event
   → agency-delivery generates Botpress chatbot + PDF
   → agency-success starts 30-day onboarding sequence
```

### Scrape targets

Configured in `~/agency/targets.yaml`. Current targets: 22 cities × 11 niches.

Top niches (Tier 1): HVAC, Plumbing, Electrical, Roofing, Dental  
Top cities: Houston TX, Phoenix AZ, Chicago IL, Dallas TX, Atlanta GA, Los Angeles CA

---

## 6. FILE STRUCTURE

```
~/agency/                          # all code lives here
├── .env                           # all secrets and config
├── orchestrator/
│   └── main.py                    # AI brain — FastAPI + tool calling loop
├── outreach/
│   └── main.py                    # Email sending + /book sales chat (Alex)
├── scraper/
│   └── main.py                    # Google Maps scraper
├── video/
│   └── main.py                    # YouTube Short generation (runs on MikeNixPC)
├── discord_bot/
│   └── main.py                    # Discord bot
├── sales/main.py                  # Lead qualification
├── billing/main.py                # Stripe webhooks
├── delivery/main.py               # Post-payment chatbot delivery
├── success/main.py                # Client onboarding
├── marketing/main.py              # Social content generation
├── bi/main.py                     # Business intelligence reports
├── support/main.py                # Health monitoring
├── inbox/main.py                  # Email inbox monitoring
├── admin/main.py                  # Admin API (restart agents, read logs)
├── command/                       # React dashboard frontend
├── knowledge/                     # AI knowledge base (wiki articles)
│   ├── index.md
│   ├── business/overview.md
│   ├── business/pricing.md
│   ├── sales/pitch-script.md
│   ├── sales/objection-handling.md
│   ├── sales/email-templates.md
│   ├── sales/lead-qualification.md
│   ├── operations/agent-roles.md
│   ├── operations/sop-onboarding.md
│   └── marketing/target-niches.md
├── quadlets/                      # Systemd container definitions
│   ├── agency-pod.pod
│   ├── agency-orchestrator.container
│   └── ... (one .container per agent)
├── scripts/                       # Utility scripts
│   ├── backup-to-archbox.sh       # Backup from MikeNixPC to archbox
│   └── backup-to-nixpc.sh         # Backup from archbox to MikeNixPC
└── targets.yaml                   # Scrape targets (niche + cities)

~/agency/videos/                   # Video metadata JSONs (no .mp4 — those stay on MikeNixPC)
/data/                             # Inside containers — mapped from agency-data volume
/data/agency.db                    # SQLite database
/data/knowledge/                   # Knowledge base (same as ~/agency/knowledge)
/data/videos/                      # Video metadata JSONs (rsync'd from MikeNixPC)
/data/targets.yaml                 # Scrape targets
```

---

## 7. ORCHESTRATOR — AI BRAIN DETAILS

**File:** `~/agency/orchestrator/main.py`  
**Endpoint:** `http://127.0.0.1:8109/chat` (POST, JSON: `{"message": "...", "session_id": "..."}`)  
**Dashboard:** dashboard.ringcatch.io

The orchestrator is the central AI agent Mike talks to. It has:
- A system prompt defining its role and authority
- A tool-calling loop (up to 20 rounds per request)
- Conversation history stored in SQLite
- An autonomous monitor loop running every 15 minutes

### Available tools

| Tool | What it does |
|---|---|
| `get_business_metrics` | MRR, clients, lead counts by stage, emails today, agent status |
| `get_leads` | Query leads by pipeline stage |
| `get_revenue` | Financial summary from ledger |
| `read_wiki_article` | Read knowledge base articles |
| `send_discord_alert` | Post to #agency-alerts Discord channel |
| `get_recent_activity` | Last N activity log events |
| `trigger_scrape` | Launch Google Maps scrape |
| `trigger_outreach` | Trigger outreach to send step-1 emails now |
| `get_recent_conversations` | Chat history across all sessions |
| `get_agent_logs` | Read container logs for any agent |
| `update_scrape_targets` | Change niche/city targets and launch scrape |
| `run_health_check` | Ping all agents, return online/offline status |
| `diagnose_pipeline` | Full pipeline self-diagnosis with issues and fixes |
| `get_marketing_social` | Generate LinkedIn/Facebook/Reddit posts (send to Discord) |
| `advance_outreach_sequences` | Immediately advance due follow-up emails |
| `get_conversion_analytics` | Funnel metrics: leads → emails → replies → clients |
| `set_pricing_mode` | Switch between 'standard' and 'waive_setup' — ONLY with Mike's explicit approval |
| `schedule_task` | Schedule future report/scrape/outreach/custom task |
| `list_scheduled_tasks` | List pending scheduled tasks |
| `get_config` | Read current .env config values |
| `set_config` | Update a config value + optionally restart an agent |
| `restart_agent` | Restart a specific agent container |

### Important orchestrator rules (in system prompt)

- Mike's directives execute immediately — no confirmation needed unless genuinely ambiguous
- NEVER output raw JSON tool calls or function signatures in responses
- Only has tools listed in schema — do NOT invent tool names like `restart_pod` (use `restart_agent`)
- `set_pricing_mode` requires Mike's explicit instruction — never call autonomously
- Self-heals: if an agent is offline, check logs → diagnose → restart

### Conversation sessions

- `discord-owner` — Mike's shared Discord session (persistent history)
- `discord_{user_id}` — other Discord users
- `wc_{uuid}` — website visitor sessions
- Sessions stored in `conversations` table

---

## 8. OUTREACH AGENT — EMAIL DETAILS

**File:** `~/agency/outreach/main.py`  
**Endpoint:** port 8080  
**From address:** alex@ringcatch.io (via Brevo)

### Email sequence timing (sprint mode)
- Step 1: immediately on scrape
- Step 2: 2 days after step-1
- Step 3: 5 days after step-1
- Step 4: 10 days after step-1

### Email generation (updated 2026-05-21)
1. For step-1: Gemini generates a **personalized opening sentence** specific to the business's city and niche (e.g., *"Houston summers mean your phone rings nonstop — until it stops at 8pm."*)
2. This opener is injected as the literal first line of the email body
3. The rest of the email is generated by Gemini using niche-specific templates
4. Step-1 emails include a niche-specific YouTube Short link in the email footer (if a matching video exists in `/data/videos/`)

### Subject lines (step-1, niche-specific)
- HVAC: "AC calls after 5pm — where do they go?"
- Plumbing: "2am plumbing emergency — who answers for you?"
- Dental: "After-hours patient questions for {name}"
- etc. (full list in `outreach/main.py` NICHE_SUBJECTS_STEP1 dict)

### Brevo limits
- Current plan: 4,080 emails/month
- Daily limit: EMAIL_DAILY_LIMIT=135 (set in .env)
- **Decision point 2026-06-03:** If at least 1 client → upgrade to Brevo Business (~$25/mo, 20k–100k emails/month) and raise limit to 300–500/day

### /book sales chat (Alex)

The interactive demo at ringcatch.io/book is powered by `agency-outreach`. When a lead visits the page:
1. They fill in name, business type, pain point
2. `POST /chat/start` initializes a session with Alex
3. Alex runs through 5 phases (discovery → demo transition → live demo → close → conversion)
4. In demo mode Alex *becomes* the lead's chatbot (role-plays as it)
5. If lead shares contact info → `capture_lead` tool fires → NEW_LEAD event → sales agent follows up
6. Booking link: `https://cal.com/michael-olszewski-nn9caa/15-min-discovery-call`

---

## 9. VIDEO PIPELINE

**Generation machine:** MikeNixPC (RTX 5060 Ti, NVENC hardware encoding)  
**Script LLM:** Gemini 2.5 Flash (updated tonight from 2.0 Flash)  
**TTS:** Google Cloud TTS (Journey-F voice) via GCP_TTS_KEY (same as GEMINI_API_KEY)  
**Upload:** YouTube @RingCatch_io brand channel  
**Thumbnail:** Imagen 3 via Google AI API (added 2026-05-21)  
**TikTok:** Pending developer app approval  

### Nightly rotation (25 niches, one per night)

HVAC, Plumbing, Dental, Auto Repair, Law Firm, Property Management, Landscaping, Roofing, Pest Control, Electrician, Hair Salon, Veterinary, Chiropractic, Physical Therapy, Moving Company, House Painting, Home Cleaning, Pool Service, Tree Service, Locksmith, Daycare, Towing, Personal Training, Tax Preparation, Restaurant

**Timer:** `systemctl --user enable --now ringcatch-video.timer` (on MikeNixPC — NOT YET ENABLED as of 2026-05-21)  
**State file:** `~/agency/videos/.niche_index`  
**Cleanup:** Videos deleted 7 days after YouTube upload. Only `.json` metadata rsync'd to archbox at `/data/videos/`.

### How videos link into outreach

When sending step-1 emails, `get_niche_video_url()` checks `/data/videos/*.json` for a file matching the lead's niche with a `youtube_url` field. If found, the YouTube link appears as a P.S. in the plain-text email and as a styled card in the HTML email.

**4 videos currently live on YouTube @RingCatch_io:** HVAC, Dental, Plumbing, Roofing

---

## 10. DISCORD BOT

**File:** `~/agency/discord_bot/main.py`  
**Service:** `agency-discord`, port 8103  
**Channel:** #agency-alerts  

### Commands

| Command | What it does |
|---|---|
| `!help` / `!h` | Show command list |
| `!status` / `!s` | Business snapshot (MRR, clients, pipeline, emails, agent health) |
| `!leads` / `!l` / `!pipeline` | Pipeline breakdown + 5 most recent leads |
| `!activity` / `!a` | Last 20 system events |
| `!reset` | Clear Mike's conversation history |
| Any other message | Forwarded to orchestrator as agentic request |

### HTTP alert endpoint

`POST http://agency-discord:8103/alert` with `{"content": "message"}` — used by all agents to send Discord notifications.

### Session handling

- Mike (OWNER_DISCORD_ID) gets session `discord-owner` (persistent, shared history)
- Other users get `discord_{user_id}` (isolated)
- 240-second timeout on orchestrator calls (handles slow tool-call chains)

---

## 11. CONVERSIONS — CURRENT STATE & BOTTLENECK

As of 2026-05-21:
- **1,271 emails sent** 
- **1.9% open rate** (benchmark: 21% — very low, subject lines may need work)
- **7.1% click rate** (90 clicks to ringcatch.io/book — actually good! People are interested)
- **0% reply rate** (emails not converting to replies — copy issue or spam filter)
- **0 clients**

**Analysis:** The click rate (7.1%) is strong — the subject lines are getting attention and people are clicking. The bottleneck is likely:
1. The /book chat experience not closing (webchat quality)
2. Emails landing in spam (domain reputation, new sender)
3. Step-1 email copy not compelling enough

**Fixes applied 2026-05-21:**
- Website (ringcatch.io) updated to be more interactive
- Gemini 2.5 Flash now powers webchat (was Ollama llama3.1 which leaked function tags)
- Personalized email openers added (Gemini generates unique first line per lead)

**Next priorities:**
1. Gmail API integration for instant reply detection
2. LinkedIn business page (@RingCatch)
3. TikTok @RingCatch_io account

---

## 12. HOW TO REBUILD AND RESTART AGENTS

When you edit a Python file for any agent:

```fish
# On archbox (Tailscale: ssh mike@100.96.122.27)

# Build the specific agent image (e.g., orchestrator)
cd ~/agency
podman build -t agency-orchestrator -f orchestrator/Containerfile .

# Restart the service
systemctl --user restart agency-orchestrator

# Check it's running
systemctl --user status agency-orchestrator

# Watch logs live
journalctl --user -u agency-orchestrator -f
```

**Containerfile pattern:** Each agent has its own `Containerfile` that copies `main.py` and `requirements.txt`, installs deps, and runs with uvicorn. Example: `orchestrator/Containerfile`

**After editing .env:** You don't need to rebuild, just restart: `systemctl --user restart agency-<name>`

**Common pattern for config changes via orchestrator chat:**
1. Talk to orchestrator: "Get current config" → it calls `get_config`
2. "Set EMAIL_DAILY_LIMIT to 200" → it calls `set_config` + `restart_agent outreach`

---

## 13. LIVE DIAGNOSTIC — 2026-05-21 ~08:40 ET

### Agent Status (13/14 online)

| Agent | Status | Notes |
|---|---|---|
| agency-orchestrator | ✅ online | Gemini-first stack deployed |
| agency-outreach | ✅ online | Gemini + personalized openers deployed |
| agency-video | ✅ online | Gemini 2.5 Flash + Imagen deployed |
| agency-command | ✅ online | dashboard.ringcatch.io serving |
| agency-support | ✅ online | 10/10 services monitored healthy |
| agency-sales | ✅ online | 4 cold leads, 0 hot/warm |
| agency-inbox | ✅ online | IMAP configured, **4 replies found today** |
| agency-billing | ✅ online | Stripe live mode |
| agency-delivery | ✅ online | |
| agency-success | ✅ online | |
| agency-marketing | ✅ online | |
| agency-bi | ✅ online | |
| agency-legal | ✅ online | |
| **agency-scraper** | ❌ **OFFLINE** | Needs restart |

### Business Metrics
- MRR: $0 | Clients: 0
- Pipeline: emailed 972 | scraped (uncontacted) 9 | cold 4 | unsubscribed 6
- Emails sent today: **271** (over daily limit of 135 — Brevo quota risk)
- Reply rate: **0.4%** (first replies detected — up from 0%)
- Leads scraped today: 6
- **4 email replies found by inbox agent today** — these need follow-up

### Action Items from Diagnostic
1. **Restart scraper:** `systemctl --user restart agency-scraper` on archbox
2. **Check the 4 replies** — inbox agent found them, sales agent should be following up
3. **Email overage** — 271 sent vs 135 limit. Monitor Brevo quota.

---

## 14. SSH ACCESS (updated 2026-05-21)

MikeNixPC → archbox SSH now works (password auth enabled on archbox sshd).  
Key-based auth pending: run `ssh-copy-id mike@100.96.122.27` from MikeNixPC once more to set up keys permanently.  
**When curling archbox services from SSH session, always use `127.0.0.1` not `localhost`** — IPv6 resolution causes connection resets with the passt proxy.

---

## 15. WHAT WAS FIXED THIS SESSION (2026-05-21)

Everything that was broken before this session:

| Issue | Fix |
|---|---|
| dashboard.ringcatch.io returning 502 | Cloudflare tunnel pointed to port 8501 (Streamlit) — fixed to 8100 |
| "LLM unavailable" in Discord | Ollama URL pointed to archbox (where Ollama doesn't run) — fixed to MikeNixPC 100.104.175.99:11434 |
| `<function>get_business_metrics</function>` tags leaking in Discord | (1) Added XML tag detection in orchestrator, (2) Added sanitizer in Discord bot, (3) Fixed root cause: Cerebras was failing due to wrong model name (llama-3.3-70b → llama3.1-8b), causing fallthrough to Ollama llama3.1:8b which leaks tags |
| Groq 413 "payload too large" | Added `_trim_for_groq()` message trimmer (9000 token budget, 600 char tool result cap) |
| Null args crash in tool handler | Added `or {}` guards on fn.get("arguments") |
| Cerebras wrong model | Fixed from `llama-3.3-70b` to `llama3.1-8b` |

**New tonight:**
- Full Gemini-first LLM stack (orchestrator + outreach + webchat)
- Personalized email openers via Gemini (unique per lead, city+niche specific)
- Gemini 2.5 Flash for video scripts (was 2.0 Flash)
- Imagen 3 thumbnail generation for YouTube videos

---

## 16. PENDING TASKS (as of 2026-05-21)

### Immediate

- [ ] Enable video timer on MikeNixPC: `systemctl --user enable --now ringcatch-video.timer`
- [ ] Add nixbook SSH key to archbox `~/.ssh/authorized_keys`
- [ ] YouTube channel About page: manually add ringcatch.io link
- [ ] Groq developer plan upgrade — temporarily unavailable, check back

### This week

- [ ] Set up LinkedIn business page for RingCatch (marketing agent generates weekly posts to copy-paste)
- [ ] Create TikTok @RingCatch_io account manually (API pending developer approval)
- [ ] Gmail API setup for instant reply detection (requires browser OAuth — see section 15)
- [ ] n8n first-time browser setup (container running at port 5678)

### Decision point: 2026-06-03

If ≥1 paying client: upgrade Brevo to Business tier (~$25/mo), raise EMAIL_DAILY_LIMIT to 300–500

---

## 17. GMAIL API SETUP (NOT YET DONE)

Google AI Pro gives full Google API access. Gmail API would let the inbox agent detect replies instantly instead of polling.

**Steps:**
1. Go to console.cloud.google.com → APIs & Services → Library → Enable "Gmail API"
2. OAuth consent screen → External → Add molszewski423@gmail.com as test user
3. Credentials → Create OAuth 2.0 Client ID → Desktop app → Download JSON as `~/agency/gmail_credentials.json`
4. Run auth script once (creates `~/agency/gmail_token.json`):
   ```python
   # gmail_auth.py
   from google_auth_oauthlib.flow import InstalledAppFlow
   import json
   SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 'https://www.googleapis.com/auth/gmail.modify']
   flow = InstalledAppFlow.from_client_secrets_file('gmail_credentials.json', SCOPES)
   creds = flow.run_local_server(port=0)
   with open('gmail_token.json', 'w') as f:
       f.write(creds.to_json())
   print("Auth complete")
   ```
5. Update inbox agent to use Gmail API with saved token

---

## 18. SUBSCRIPTIONS & EXTERNAL SERVICES

| Service | Plan | Cost | Purpose |
|---|---|---|---|
| Google AI Pro | Paid | $20/mo | Gemini 2.5 Flash API (primary LLM) + Google Cloud TTS (video voice) + Imagen 3 (thumbnails) |
| Brevo | Paid (4,080/mo) | ~$12/mo | Email sending (outreach + alerts) |
| Cerebras | Pay-as-you-go | ~$10 credit loaded | llama3.1-8b fallback LLM |
| Groq | Free tier | $0 | Emergency LLM fallback (12k TPM) |
| Cloudflare | Free | $0 | Tunnel (dashboard), DNS, Pages (website) |
| Stripe | Pay-as-you-go | 2.9%+30¢ | Payment processing |
| Cal.com | Free | $0 | Discovery call bookings |
| Botpress | Free | $0 | Chatbot platform for delivery |
| Tailscale | Free | $0 | VPN mesh connecting all machines |
| YouTube | Free | $0 | @RingCatch_io channel (brand account, separate from Mike's personal @molszewski423) |
| TikTok | Free | $0 | @RingCatch_io (developer approval pending) |

---

## 19. KEY THINGS NOT TO DO

1. **Never call `set_pricing_mode` without Mike explicitly saying to** — orchestrator system prompt says suggest it via Discord first
2. **Never push to git or deploy without asking** — Mike reviews all changes
3. **Never use bash activate, always use activate.fish** — Mike uses Fish shell
4. **Ollama is on MikeNixPC (100.104.175.99:11434), NOT archbox** — archbox doesn't run Ollama
5. **Cerebras model is `llama3.1-8b`** — NOT `llama-3.3-70b` or `llama3.1:8b` (different format)
6. **Don't invent tool names** — orchestrator only has the tools listed in section 7

---

## 20. HOW TO WORK WITH MIKE

- Mike is technical but direct. He wants results, not explanations.
- He runs things from nixbook (his laptop), editing files on MikeNixPC or archbox via SSH
- The primary codebase lives on both MikeNixPC (`/home/mike/agency/`) and archbox (`~/agency/`) — they're kept in sync via rsync
- When something breaks in Discord or on the dashboard, the fix is usually: edit the Python file → rsync to archbox → podman build → systemctl restart
- Mike's email: molszewski423@gmail.com
- Sprint ends 2026-06-01. Every hour counts. Prioritize anything that directly moves leads to clients.

---

## 21. ASKING THE ORCHESTRATOR DIRECTLY

You can query the live orchestrator (if archbox is reachable) with:

```bash
curl -s -X POST http://100.96.122.27:8100/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Give me a full status report", "session_id": "gemini-cli"}' | python3 -m json.tool
```

Or check health of all agents:
```bash
curl -s http://100.96.122.27:8100/api/agents | python3 -m json.tool
```

Or check recent activity:
```bash
curl -s "http://100.96.122.27:8100/api/activity?limit=20" | python3 -m json.tool
```

---

*End of handoff. This document covers everything an AI assistant needs to work on RingCatch with full context.*
