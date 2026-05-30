# Agent Roles & Pipeline Flow

## End-to-End Pipeline
```
scraper → event_bus (NEW_LEAD) → sales (qualify) → outreach (email) →
  reply → event_bus (REPLY_RECEIVED) → sales (hot follow-up) →
    booking → billing (Stripe webhook) → event_bus (NEW_CLIENT) →
      delivery (Botpress + PDF) + success (onboarding)
```

## agency-scraper (port 8079)
Scrapes Google Maps for local business leads based on /config/targets.yaml.
Autonomous loop: scrapes once per day if no leads scraped yet (6-hour re-check).
Max 75 leads/day. Uses Hunter.io for email lookup if HUNTER_API_KEY is set.
**After each scrape**: writes NEW_LEAD events to event_bus for sales qualification.
Output: leads table + /data/leads/leads_YYYY-MM-DD.json files.
Endpoints: POST /scrape, GET /leads/today, GET /health

## agency-outreach (port 8080)
Handles all email sending and the "Alex" AI sales chat on /book page.
Autonomous loop: every 10 min sends step-1 to unprocessed leads; every 60 min advances step-2/3.
Step timing: step-2 fires 3 days after step-1; step-3 fires 7 days after step-1 (only if no reply).
Email provider: Brevo (300 emails/day free tier). Set EMAIL_PROVIDER=brevo in .env to activate.
**On reply marked**: writes REPLY_RECEIVED to event_bus so sales agent sends hot follow-up.
Endpoints: POST /send (trigger step-1 now), POST /advance (advance follow-ups now),
  POST /mark-replied, POST /generate-reply, POST /chat/start, POST /chat/message, GET /health

## agency-sales (port 8107)
Qualifies leads as hot/warm/cold using qwen2.5:7b (fast model).
Polls event_bus every 5 min for NEW_LEAD and REPLY_RECEIVED events.
HOT: sends immediate personalized follow-up with Cal.com booking link.
WARM: schedules sequence (step 2 at day 3, step 3 at day 7).
COLD: marks in DB, skips.
Endpoints: POST /qualify-lead, GET /status, GET /health

## agency-billing (port 8082)
Handles Stripe webhooks. Supports checkout.session.completed and payment_intent.succeeded.
**On payment**: records to payments table + updates leads.pipeline_stage to 'active_client' +
  inserts to clients table + writes NEW_CLIENT to event_bus (target: broadcast) + triggers delivery.
Also sends welcome email to client and alert email to Mike.
Endpoints: POST /stripe-webhook, GET /payment-link, GET /health

## agency-delivery (port 8081)
Generates AI-powered chatbot deliverables after payment.
Creates: Botpress flow JSON, onboarding PDF (via ReportLab), sends welcome email.
Schedules testimonial request via outreach agent at Day 7.
Endpoints: POST /generate-delivery, GET /intake (client onboarding form), GET /health

## agency-success (port 8105)
Polls event_bus every 60 sec for NEW_CLIENT events → inserts client, starts onboarding.
Daily loop: updates churn risk scores (low/med/high based on chatbot conversation count),
  flags high-risk clients to Discord, checks/sends testimonial requests at Day 30.
Endpoints: GET /health, GET /status, GET /client-report/{id}

## agency-marketing (port 8102)
Weekly loops: email A/B test analysis + performance report + **free social content batch**.
Social content generated weekly for:
  - LinkedIn (personal post from Mike's profile, ~150 words, founder voice)
  - Facebook (group post, relatable story, ~100 words)
  - Reddit (organic helpful comment for niche subreddit, ~120 words)
  - Google Business Profile update (~70 words)
All posts sent to Discord for copy-paste. Stored in social_content table + /data/social/.
Endpoints: POST /generate-social (on-demand batch), GET /social-content, POST /mark-posted,
  GET /weekly-report, GET /optimize-targeting, POST /research-niche, GET /ab-results

## agency-bi (port 8106)
Weekly executive summary via gemma4:26b with growth opportunities and bottleneck analysis.
Tracks daily intelligence_metrics (MRR, leads, open rates).
Endpoints: GET /executive-summary, GET /status, GET /health

## agency-support (port 8104)
Health monitor: pings all agents every 60 sec. Sends Discord alert on 3+ consecutive failures.
Attempts Podman container restart on persistent failures (requires PODMAN_SOCKET).
Monitors external URLs (ringcatch.io, dashboard.ringcatch.io).
Endpoints: GET /health, GET /status

## agency-command (port 8100)
React dashboard + FastAPI backend. Aggregates status from all agents in real time.
WebSocket /ws/live streams activity_log events. Voice chat via Speaches (STT/TTS).
Chat panel → POST /api/chat → orchestrator. Voice → POST /api/voice → Speaches → orchestrator.
URL: dashboard.ringcatch.io

## agency-orchestrator (port 8109)
AI brain powered by gemma4:26b. Autonomous monitor loop runs every 15 min.
Tools: get_business_metrics, get_leads, get_revenue, read_wiki_article, send_discord_alert,
  get_recent_activity, trigger_scrape, trigger_outreach, get_recent_conversations,
  get_agent_logs, update_scrape_targets, run_health_check, diagnose_pipeline,
  get_marketing_social, advance_outreach_sequences.
Daily Discord digest at 08:00 UTC.
Chat via dashboard.ringcatch.io or Discord bot (session: "discord-owner" for Mike).
