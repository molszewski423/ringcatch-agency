import asyncio
import json
import logging
import os
import sqlite3
import shlex
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, UTC
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

def now_et() -> datetime:
    return datetime.now(EASTERN)

def fmt_et(dt: datetime | None = None) -> str:
    d = (dt or now_et()).astimezone(EASTERN)
    return d.strftime("%Y-%m-%d %H:%M ET")

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB_PATH       = Path(os.environ.get("DB_PATH", "/data/agency.db"))
OLLAMA_URL    = os.environ.get("OLLAMA_BASE_URL", "http://host.containers.internal:11434")
BK_MODEL      = os.environ.get("BACKEND_MODEL", "gemma4:26b")
DISCORD_URL   = os.environ.get("DISCORD_BOT_URL", "http://agency-discord:8103/alert")
KNOWLEDGE_DIR = Path(os.environ.get("KNOWLEDGE_DIR", "/data/knowledge"))
AGENT         = "agency-orchestrator"
SCRAPER_URL   = os.environ.get("SCRAPER_URL", "http://agency-scraper:8079")
OUTREACH_URL  = os.environ.get("OUTREACH_URL", "http://agency-outreach:8080")
HEALTH_REPORT_INTERVAL_HOURS = float(os.environ.get("HEALTH_REPORT_INTERVAL_HOURS", "6"))
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "20"))
EMAIL_DAILY_LIMIT = int(os.environ.get("EMAIL_DAILY_LIMIT", "300"))
ADMIN_URL = os.environ.get("ADMIN_URL", "http://host.containers.internal:8112")
# llama3.1:8b has native tool support and is fast; BK_MODEL (gemma4:26b) for background tasks
CHAT_TOOL_MODEL = os.environ.get("CHAT_TOOL_MODEL", "qwen2.5:14b")
GROQ_API_KEY      = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL        = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FAST_MODEL   = os.environ.get("GROQ_FAST_MODEL", "llama-3.1-8b-instant")
GROQ_URL          = "https://api.groq.com/openai/v1/chat/completions"

GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL      = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL        = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

SYSTEM_PROMPT = """You are the RingCatch Agency Orchestrator — the AI brain running RingCatch for Mike Olszewski, the founder.

RingCatch sells AI chatbots to local small businesses. Pricing: $450 setup + $89/month recurring.
Target market: HVAC, plumbers, electricians, dental offices, auto repair, law firms, insurance, cleaning, landscaping in US cities.
**PRIMARY GOAL (next 2 weeks, by 2026-06-01):** 3 paying clients. Long-term target: $10k MRR.

## Your authority
- Mike is the owner. When he gives a directive, execute it immediately using your tools — no confirmation needed unless genuinely ambiguous.
- You have up to 20 tool-call rounds per request. Use them to complete complex multi-step tasks fully.
- You run autonomously even when Mike isn't watching. A live snapshot of system state is injected below.
- **CRITICAL: NEVER output raw JSON tool calls or function signatures in your replies.** If you need data, CALL THE TOOL — do not show Mike how to call it. Never write `{"name": ..., "parameters": ...}` in your response. Just execute the tool and report the result in plain English.
- You only have the tools listed in the tools schema. Do NOT invent tool names like `restart_pod` — use `restart_agent` instead.
- You can and SHOULD self-heal errors: if an agent is offline, check logs, identify root cause, and restart it. If the pipeline stalls, diagnose and restart it.
- When Mike tells you about a config change (e.g. "limit increased to 1000"), ALWAYS call get_config first to see the current value, then call set_config if it needs updating, then restart_agent to apply it. Never just acknowledge — act.
- **PRICING — suggest only, never act:** You may NEVER call set_pricing_mode without Mike's explicit instruction in this chat. If you analyze get_conversion_analytics and believe pricing is a conversion barrier, send a Discord alert with your recommendation and wait for Mike to confirm. Only call set_pricing_mode when Mike says "yes, do it" or equivalent.

## Pipeline Architecture (you oversee this end-to-end)
1. SCRAPER (8079): Scrapes Google Maps → inserts leads to DB → writes NEW_LEAD to event_bus → outreach auto-sends in 10 min
2. SALES (8107): Polls event_bus for NEW_LEAD events every 5 min → qualifies hot/warm/cold → schedules warm sequences, sends hot follow-ups
3. OUTREACH (8080): Autonomous loop every 10 min — sends step-1 to uncontacted leads, advances step-2/3 follow-ups on schedule (Day 2, Day 5). Emails link to ringcatch.io/book for the interactive demo.
4. BILLING (8082): Stripe webhook → payment confirmed → writes NEW_CLIENT to event_bus + updates lead to 'active_client' + triggers delivery
5. DELIVERY (8081): Generates Botpress flow + onboarding PDF, sends welcome email, triggers success agent
6. SUCCESS (8105): Polls event_bus for NEW_CLIENT → onboarding sequence (welcome, check-in Day 3, training Day 7, testimonial Day 30), churn monitoring
7. MARKETING (8102): Weekly email performance, A/B testing, generates free social content (LinkedIn/Facebook/Reddit/Google Business) every week
8. COMMAND (8100): Your dashboard at dashboard.ringcatch.io — React UI + this orchestrator API + voice
9. VIDEO (8111): Generates one niche YouTube Short per week (HVAC, Plumbing, Dental, Auto Repair, Law Firm, Property Management, Landscaping, Roofing, Pest Control, Electrician). Uses Gemini 2.0 Flash for scripts, Google Cloud TTS Journey-F for voice, Pexels stock footage. Auto-uploads to YouTube (@RingCatch_io brand channel). POST /generate {"niche":"HVAC"} to make one on demand. POST /upload-pending to retry any queued videos. Outreach step-1 emails automatically embed the matching niche video link (e.g. HVAC leads get the HVAC video).

## Conversion sprint tactics (active now through 2026-06-01)
- The ringcatch.io/book demo page is the most powerful conversion tool — every outreach email links to it
- Step-1 outreach emails also embed a niche-specific YouTube Short (e.g. HVAC lead gets the HVAC video P.S. link) — videos must exist in /data/videos/ with a youtube_url field to appear
- YouTube channel: @RingCatch_io (brand account, separate from Mike's personal @molszewski423)
- Outreach covers 22 cities × 11 niches = up to 1,000 emails/day (Brevo Standard plan limit)
- EMAIL_DAILY_LIMIT and MAX_LEADS_PER_DAY in .env control the cap — use get_config to read current values, set_config to update them, restart_agent to apply changes
- Follow-up cadence: step-2 at day 2, step-3 at day 5 (accelerated for sprint)
- If reply_rate < 1% after 5 days → call diagnose_pipeline and adjust niche targeting
- If 0 clients at day 7 → call get_conversion_analytics, analyze the funnel, then send Mike a Discord alert with your diagnosis and pricing recommendation. Do NOT call set_pricing_mode yourself.
- Daily digest MUST include: emails sent today, replies received, demo chat starts, conversion funnel %

## Self-healing checklist (run when pipeline stalls)
- No emails today AND scraped > 0 → call trigger_outreach
- Total leads = 0 → call trigger_scrape
- Sales agent offline → call get_agent_logs("agency-sales") to diagnose
- NEW_LEAD events not consumed → sales agent likely down; check logs
- Clients not in success onboarding → NEW_CLIENT event may have been missed; query DB and backfill manually if needed
- Marketing social batch not generated this week → call marketing /generate-social endpoint

## Free outbound channels (no paid ads)
Marketing agent auto-generates weekly content for:
- LinkedIn (personal posts from Mike's profile targeting contractors/business owners)
- Facebook Groups (small business owner groups, trade groups)
- Reddit (r/HVAC, r/Plumbing, r/smallbusiness — helpful organic comments)
- Google Business Profile posts
All generated posts land in Discord for Mike to copy-paste. Use get_marketing_social tool to trigger on demand.
- YouTube Shorts (@RingCatch_io): Video agent auto-publishes one niche Short per week. These links appear in outreach emails automatically. TikTok pending developer app approval.

## How to respond
- Always use tools to get current data before answering anything about metrics, leads, or revenue.
- For sales, scripts, objections, or strategy — read the relevant wiki article first.
- For "what have you been doing?" or "what's happening?" — call get_recent_conversations and get_recent_activity.
- Be direct and specific. Cite numbers. When you take action, confirm what you did.
- When talking on Discord, keep responses concise — use bullet points for lists.
- When diagnosing issues, use get_agent_logs to read actual container logs before guessing."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_business_metrics",
            "description": "Get current MRR, active clients, total leads, pipeline summary, agent status, emails sent today",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_leads",
            "description": "Get leads from the pipeline. Filter by stage: scraped, emailed, opened, replied, booked, paid, active_client",
            "parameters": {
                "type": "object",
                "properties": {
                    "stage": {"type": "string", "description": "Pipeline stage to filter (optional)"},
                    "limit": {"type": "integer", "description": "Max results, default 10"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_revenue",
            "description": "Get financial summary: total revenue, MRR, tax reserve, recent transactions",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_wiki_article",
            "description": "Read a knowledge base article. Use for sales scripts, objection handling, email templates, SOPs, pricing, niches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Article path e.g. 'sales/pitch-script.md' or 'business/pricing.md'"}
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_discord_alert",
            "description": "Send a notification to the #agency-alerts Discord channel",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Message to send"}
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_activity",
            "description": "Get the most recent activity log events across all agents",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of events, default 20"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_scrape",
            "description": "Launch a Google Maps scrape to collect new leads. Specify niches (e.g. HVAC, Plumber, Electrician) and/or cities. Leave empty to use defaults from targets.yaml.",
            "parameters": {
                "type": "object",
                "properties": {
                    "niche": {"type": "string", "description": "Business niche to scrape, e.g. 'HVAC' or 'Plumber'"},
                    "city":  {"type": "string", "description": "City to scrape, e.g. 'Houston, TX'"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_outreach",
            "description": "Tell the outreach agent to pick up all new scraped leads and send them step-1 emails immediately.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_conversations",
            "description": "Get recent conversations from ALL sessions (dashboard, Discord, autonomous loops). Use this to understand what has been discussed or directed across the whole system.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of messages to return, default 30"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_agent_logs",
            "description": "Get recent log lines from a specific agent container. Useful for diagnosing what an agent is actually doing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "Container name, e.g. 'agency-scraper', 'agency-outreach', 'agency-sales'"},
                    "lines": {"type": "integer", "description": "Number of log lines, default 30"}
                },
                "required": ["agent"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_scrape_targets",
            "description": "Change what niches and cities the scraper targets, then immediately launch a scrape with those targets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "niche":  {"type": "string", "description": "Business niche, e.g. 'Plumber' or 'Electrician'"},
                    "cities": {"type": "array", "items": {"type": "string"}, "description": "List of cities, e.g. ['Dallas, TX', 'Austin, TX']"}
                },
                "required": ["niche", "cities"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_health_check",
            "description": "Ping all agents and return which are online vs offline. Use this to detect pipeline failures.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "diagnose_pipeline",
            "description": "Full pipeline self-diagnosis: checks each stage for stalls, counts unprocessed leads, unread events, offline agents, and returns a prioritized list of issues with recommended fixes.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_marketing_social",
            "description": "Trigger the marketing agent to generate a fresh batch of free social media posts (LinkedIn, Facebook, Reddit, Google Business) and send them to Discord.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "advance_outreach_sequences",
            "description": "Tell the outreach agent to immediately advance any due follow-up sequences (step 2 and step 3 emails).",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_conversion_analytics",
            "description": "Get funnel conversion metrics: leads scraped, emails sent, replies, demo chats started, clients paid. Use to assess pipeline health and decide if tactics need to change.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_pricing_mode",
            "description": "Change the outreach pricing mode. 'waive_setup' removes the $450 setup fee from new outreach emails (only $89/mo). 'standard' restores full pricing. ONLY call this when Mike explicitly instructs you to — never autonomously. Suggest it via Discord first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "description": "'standard' or 'waive_setup'"}
                },
                "required": ["mode"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": "Schedule a future action to run automatically. Use this whenever Mike asks for an update 'in X hours/minutes', wants something checked later, or asks you to remind him. Always call this tool when you promise to do something in the future — do not just say you will.",
            "parameters": {
                "type": "object",
                "properties": {
                    "delay_minutes": {"type": "integer", "description": "Minutes from now to run the task"},
                    "task_type": {"type": "string", "description": "'report' (full status report to Discord), 'discord_message' (send a custom message), 'scrape' (trigger scraper), 'outreach' (trigger outreach), 'self_task' (send a prompt to yourself to execute using your tools — use this for checks, API calls, investigations)"},
                    "message": {"type": "string", "description": "For task_type='discord_message': the message to send. For task_type='report': optional context/focus. For task_type='self_task': the prompt you want to execute (e.g. 'Check Brevo domain auth status and alert Mike on Discord')."}
                },
                "required": ["delay_minutes", "task_type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_scheduled_tasks",
            "description": "List all pending scheduled tasks with their scheduled fire times.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_config",
            "description": "Read current operational config values from .env (EMAIL_DAILY_LIMIT, MAX_LEADS_PER_DAY, model names, poll intervals, etc). Call this before making any config change to verify the current state.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_config",
            "description": "Update an operational config value in .env and optionally restart the affected agent so it picks up the change. Writable keys: EMAIL_DAILY_LIMIT, MAX_LEADS_PER_DAY, INBOX_POLL_SECONDS, HEALTH_REPORT_INTERVAL_HOURS, MAX_TOOL_ROUNDS, CHAT_MODEL, BACKEND_MODEL, FAST_MODEL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key":          {"type": "string", "description": "Config key to update (must be in writable allowlist)"},
                    "value":        {"type": "string", "description": "New value"},
                    "restart_agent":{"type": "string", "description": "Agent name to restart after update so it picks up the change (e.g. 'outreach', 'scraper', 'orchestrator')"}
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "restart_agent",
            "description": "Restart a specific agent container via systemctl. Use after config changes or when an agent is stuck/crashed. Agent names: scraper, outreach, delivery, billing, intake, sales, support, success, marketing, bi, legal, discord, orchestrator, inbox, cfo, video, kokoro.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "Agent name without 'agency-' prefix (e.g. 'outreach', 'scraper')"}
                },
                "required": ["agent"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_claude_code_task",
            "description": "Delegate a complex coding, debugging, file-editing, or multi-step engineering task to Claude Code CLI running on MikePC. Use for tasks that require reading/editing code files, running tests, refactoring, or anything beyond what the orchestrator can do alone. Returns the full Claude Code output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The full task description / instructions for Claude Code"},
                    "working_dir": {"type": "string", "description": "Working directory on MikePC (default: /home/mike)"}
                },
                "required": ["prompt"]
            }
        }
    }
]


def get_db():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def init_tables():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id);
    CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT DEFAULT (datetime('now')),
        agent TEXT, event_type TEXT, message TEXT, color TEXT DEFAULT 'blue'
    );
    CREATE TABLE IF NOT EXISTS agent_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT UNIQUE, status TEXT,
        last_heartbeat TEXT, last_action TEXT,
        actions_today INTEGER DEFAULT 0, alerts_active INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS scheduled_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT DEFAULT (datetime('now')),
        fire_at TEXT NOT NULL,
        task_type TEXT NOT NULL,
        payload TEXT DEFAULT '{}',
        status TEXT DEFAULT 'pending'
    );
    """)
    # Migration: add attendee columns to bookings table if created by delivery agent
    for stmt in [
        "ALTER TABLE bookings ADD COLUMN attendee_email TEXT",
        "ALTER TABLE bookings ADD COLUMN booking_uid TEXT",
    ]:
        try:
            db.execute(stmt)
            db.commit()
        except Exception:
            pass
    db.commit()
    db.close()


# ── Tool implementations ──────────────────────────────────────────────────────

def tool_get_business_metrics() -> dict:
    db = get_db()
    try:
        def safe(q, *a):
            try: return db.execute(q, a).fetchone()[0] or 0
            except: return 0

        active_clients = safe("SELECT COUNT(*) FROM clients WHERE status='active'")
        mrr = active_clients * 89.0
        total_leads = safe("SELECT COUNT(*) FROM leads")
        hot_leads = safe("SELECT COUNT(*) FROM leads WHERE qualified='hot'")
        warm_leads = safe("SELECT COUNT(*) FROM leads WHERE qualified='warm'")
        emails_today = safe("SELECT COUNT(*) FROM outreach WHERE date(sent_at,'localtime')=date('now','localtime')")

        stages = {}
        try:
            for row in db.execute("SELECT pipeline_stage, COUNT(*) c FROM leads GROUP BY pipeline_stage").fetchall():
                stages[row[0]] = row[1]
        except: pass

        agents = []
        try:
            for row in db.execute("SELECT agent_name, status, last_action, actions_today FROM agent_status").fetchall():
                agents.append({"name": row[0], "status": row[1], "last_action": row[2], "actions_today": row[3]})
        except: pass

        return {
            "mrr": mrr, "arr": mrr * 12, "active_clients": active_clients,
            "total_leads": total_leads, "hot_leads": hot_leads, "warm_leads": warm_leads,
            "emails_today": emails_today, "email_limit": EMAIL_DAILY_LIMIT,
            "pipeline_stages": stages, "agents": agents,
            "as_of": fmt_et()
        }
    finally:
        db.close()


def tool_get_leads(stage: str = None, limit: int = 10) -> list:
    db = get_db()
    try:
        if stage:
            rows = db.execute(
                "SELECT business_name, email, city, niche, pipeline_stage, qualified FROM leads WHERE pipeline_stage=? LIMIT ?",
                (stage, limit)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT business_name, email, city, niche, pipeline_stage, qualified FROM leads ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def tool_get_revenue() -> dict:
    db = get_db()
    try:
        def safe(q, *a):
            try: r = db.execute(q, a).fetchone()[0]; return float(r) if r else 0.0
            except: return 0.0

        total = safe("SELECT SUM(amount) FROM financial_ledger")
        setup = safe("SELECT SUM(amount) FROM financial_ledger WHERE event_type='setup'")
        monthly = safe("SELECT SUM(amount) FROM financial_ledger WHERE event_type='subscription'")
        reserve = safe("SELECT SUM(amount) FROM tax_reserve")

        recent = []
        try:
            for r in db.execute("SELECT event_type, amount, description, timestamp FROM financial_ledger ORDER BY id DESC LIMIT 5").fetchall():
                recent.append(dict(r))
        except: pass

        return {
            "total_revenue": round(total, 2),
            "setup_fees_collected": round(setup, 2),
            "monthly_recurring_collected": round(monthly, 2),
            "tax_reserve": round(reserve, 2),
            "recent_transactions": recent
        }
    finally:
        db.close()


def tool_read_wiki_article(filename: str) -> str:
    path = KNOWLEDGE_DIR / filename
    if not path.exists():
        return f"Article not found: {filename}. Check index.md for valid filenames."
    return path.read_text()


async def tool_send_discord_alert(message: str) -> str:
    if not DISCORD_URL:
        return "Discord webhook not configured."
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(DISCORD_URL, json={"content": f"🤖 **Orchestrator**: {message}"})
        return "Alert sent to Discord."
    except Exception as e:
        return f"Failed to send: {e}"


def tool_get_recent_activity(limit: int = 20) -> list:
    db = get_db()
    try:
        rows = db.execute(
            "SELECT agent, event_type, message, timestamp, color FROM activity_log ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def tool_get_recent_conversations(limit: int = 30) -> list:
    db = get_db()
    try:
        rows = db.execute(
            "SELECT session_id, role, content, timestamp FROM conversations ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        db.close()


async def tool_get_agent_logs(agent: str, lines: int = 30) -> str:
    name = agent.lower().removeprefix("agency-")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{ADMIN_URL}/logs/{name}")
            data = r.json()
            logs = data.get("logs", data.get("error", "no output"))
            return logs[-4000:] if len(logs) > 4000 else logs
    except Exception as e:
        return f"Could not get logs for {agent}: {e}"


MARKETING_URL = os.environ.get("MARKETING_URL", "http://agency-marketing:8102")

_AGENT_HEALTH_URLS = {
    "agency-scraper":      "http://agency-scraper:8079/health",
    "agency-outreach":     "http://agency-outreach:8080/health",
    "agency-delivery":     "http://agency-delivery:8081/health",
    "agency-billing":      "http://agency-billing:8082/health",
    "agency-command":      "http://agency-command:8100/health",
    "agency-legal":        "http://agency-legal:8101/health",
    "agency-marketing":    "http://agency-marketing:8102/health",
    "agency-support":      "http://agency-support:8104/health",
    "agency-success":      "http://agency-success:8105/health",
    "agency-bi":           "http://agency-bi:8106/health",
    "agency-sales":        "http://agency-sales:8107/health",
    "agency-orchestrator": "http://agency-orchestrator:8109/health",
    "agency-inbox":        "http://agency-inbox:8110/health",
    "ollama":              f"{OLLAMA_URL}/api/tags",
}


async def get_all_agents() -> list[dict]:
    results = []
    async with httpx.AsyncClient(timeout=5) as client:
        for name, url in _AGENT_HEALTH_URLS.items():
            try:
                r = await client.get(url)
                status = "online" if r.status_code == 200 else "degraded"
                results.append({"name": name, "status": status})
            except Exception as e:
                results.append({"name": name, "status": "offline", "error": str(e)})
    return results


async def tool_run_health_check() -> str:
    agents = await get_all_agents()
    online  = [a["name"] for a in agents if a["status"] == "online"]
    offline = [a["name"] for a in agents if a["status"] != "online"]
    lines = [f"Health check — {len(online)}/{len(agents)} agents online"]
    for a in agents:
        icon = "✅" if a["status"] == "online" else "❌"
        err = f" — {a.get('error','')[:80]}" if a.get("error") else ""
        lines.append(f"{icon} {a['name']}{err}")
    return "\n".join(lines)


async def tool_diagnose_pipeline() -> str:
    db = get_db()
    issues = []
    suggestions = []

    def safe(q, *a):
        try: return db.execute(q, a).fetchone()[0] or 0
        except: return 0

    scraped   = safe("SELECT COUNT(*) FROM leads WHERE pipeline_stage='scraped'")
    emailed   = safe("SELECT COUNT(*) FROM leads WHERE processed=1 AND pipeline_stage='scraped'")
    unprocessed = safe("SELECT COUNT(*) FROM leads WHERE processed=0 AND email != ''")
    pending_events = safe("SELECT COUNT(*) FROM event_bus WHERE status='pending'")
    replied   = safe("SELECT COUNT(*) FROM leads WHERE pipeline_stage='replied'")
    active    = safe("SELECT COUNT(*) FROM clients WHERE status='active'")
    emails_today = safe("SELECT COUNT(*) FROM outreach WHERE date(sent_at,'localtime')=date('now','localtime')")
    pending_new_lead = safe("SELECT COUNT(*) FROM event_bus WHERE event_type='NEW_LEAD' AND status='pending'")
    pending_new_client = safe("SELECT COUNT(*) FROM event_bus WHERE event_type='NEW_CLIENT' AND status='pending'")
    db.close()

    if unprocessed > 0:
        issues.append(f"⚠ {unprocessed} leads have email but haven't been emailed (processed=0)")
        suggestions.append("→ Trigger outreach: POST /send on outreach agent")
    if pending_new_lead > 5:
        issues.append(f"⚠ {pending_new_lead} NEW_LEAD events pending — sales agent may be down")
        suggestions.append("→ Check sales agent logs: get_agent_logs('agency-sales')")
    if pending_new_client > 0:
        issues.append(f"⚠ {pending_new_client} NEW_CLIENT events pending — success agent may be lagging")
        suggestions.append("→ Check success agent: get_agent_logs('agency-success')")
    if emails_today == 0 and scraped > 0:
        issues.append(f"⚠ No emails sent today despite {scraped} scraped leads")
        suggestions.append("→ Trigger outreach now")
    if replied > 0:
        issues.append(f"ℹ {replied} leads have replied — hot follow-up may be due")
        suggestions.append("→ Review replied leads and send booking link")

    agents = await get_all_agents()
    for a in agents:
        if a["status"] != "online":
            if a["name"] == "ollama":
                issues.append(f"❌ Ollama is OFFLINE or UNREACHABLE")
                suggestions.append("→ Check if Ollama is running on MikeNixPC: `podman ps | grep ollama` and `podman restart ollama` if needed.")
            else:
                issues.append(f"❌ Agent OFFLINE: {a['name']} — {a.get('error','')[:100]}")
                suggestions.append(f"→ Run get_agent_logs('{a['name']}') to diagnose or restart_agent('{a['name'].replace('agency-','')}')")

    if not issues:
        return (
            f"✅ Pipeline healthy\n"
            f"Scraped={scraped} | Unprocessed={unprocessed} | Replied={replied} | "
            f"Active clients={active} | Emails today={emails_today} | "
            f"Pending events={pending_events}"
        )

    lines = [f"Pipeline diagnosis — {len(issues)} issue(s) found:"]
    for i, (issue, fix) in enumerate(zip(issues, suggestions), 1):
        lines.append(f"\n{i}. {issue}\n   {fix}")
    return "\n".join(lines)


async def tool_get_marketing_social() -> str:
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{MARKETING_URL}/generate-social")
        data = r.json()
        return f"Social batch generated: {data.get('posts', 0)} posts for platforms: {data.get('platforms', [])}. Sent to Discord."
    except Exception as e:
        return f"Marketing agent unavailable: {e}"


async def tool_get_conversion_analytics() -> str:
    try:
        db = get_db()
        rows = db.execute("""
            SELECT
                COUNT(*) as total_leads,
                COUNT(CASE WHEN processed=1 THEN 1 END) as emailed,
                COUNT(CASE WHEN pipeline_stage='replied' THEN 1 END) as replied,
                COUNT(CASE WHEN pipeline_stage='active_client' THEN 1 END) as clients
            FROM leads
        """).fetchone()
        emails_today = db.execute(
            "SELECT COUNT(*) FROM outreach WHERE date(sent_at,'localtime')=date('now','localtime')"
        ).fetchone()[0]
        total_emails = db.execute("SELECT COUNT(*) FROM outreach").fetchone()[0]
        # Chat sessions (demo starts) — approximate from activity log
        demo_starts = db.execute(
            "SELECT COUNT(*) FROM activity_log WHERE event_type='chat_start'"
        ).fetchone()[0] if _table_exists(db, "activity_log") else "unknown"
        db.close()
        funnel = (
            f"Conversion funnel:\n"
            f"  Leads scraped:  {rows['total_leads']}\n"
            f"  Emails sent:    {total_emails} (today: {emails_today})\n"
            f"  Replied:        {rows['replied']}\n"
            f"  Demo chats:     {demo_starts}\n"
            f"  Paying clients: {rows['clients']}\n"
        )
        if rows["total_leads"] and rows["emailed"]:
            reply_rate = round(rows["replied"] / max(rows["emailed"], 1) * 100, 1)
            close_rate = round(rows["clients"] / max(rows["replied"], 1) * 100, 1) if rows["replied"] else 0
            funnel += f"  Reply rate: {reply_rate}% | Close rate (reply→paid): {close_rate}%"
        return funnel
    except Exception as e:
        return f"Analytics error: {e}"


def _table_exists(db, table: str) -> bool:
    r = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return r is not None


async def tool_set_pricing_mode(mode: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{OUTREACH_URL}/set-pricing-mode",
                json={"mode": mode}
            )
        data = r.json()
        return f"Pricing mode set to '{data.get('pricing_mode')}'. Discord notified."
    except Exception as e:
        return f"Failed to set pricing mode: {e}"


async def tool_advance_outreach() -> str:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{OUTREACH_URL}/advance")
        data = r.json()
        return f"Follow-up sequences advanced: {data}"
    except Exception as e:
        return f"Failed to advance sequences: {e}"


async def tool_update_scrape_targets(niche: str, cities: list) -> str:
    import yaml
    targets_path = Path("/data/targets.yaml")
    config = {"niche": niche, "cities": cities, "leads_per_city": 15, "delay_min_seconds": 3, "delay_max_seconds": 9}
    try:
        targets_path.write_text(yaml.dump(config))
        db = get_db()
        db.execute("INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
                   (AGENT, "TARGETS_UPDATED", f"Targets updated: {niche} in {cities}", "purple"))
        db.commit()
        db.close()
    except Exception as e:
        return f"Failed to save targets: {e}"
    # Launch scrape with each city immediately
    results = []
    for city in cities:
        r = await tool_trigger_scrape(niche=niche, city=city)
        results.append(r)
    return f"Targets saved and scrapes launched:\n" + "\n".join(results)


def build_live_context() -> str:
    try:
        m = tool_get_business_metrics()
        activity = tool_get_recent_activity(8)
        now = fmt_et()
        stages = m.get("pipeline_stages", {})
        stage_str = " | ".join(f"{k}:{v}" for k, v in stages.items()) or "empty"
        lines = [
            f"\n\n## Live System State — {now}",
            f"MRR ${m['mrr']:.0f} | Clients {m['active_clients']} | Leads {m['total_leads']} ({stage_str})",
            f"Emails today {m['emails_today']}/{m['email_limit']}",
        ]
        if activity:
            lines.append("### Recent activity")
            for e in activity:
                lines.append(f"- [{e.get('agent','')}] {e.get('event_type','')}: {e.get('message','')} ({e.get('timestamp','')})")
        return "\n".join(lines)
    except Exception:
        return ""


async def tool_trigger_scrape(niche: str = None, city: str = None) -> str:
    payload = {}
    if niche or city:
        payload["targets"] = [{"niche": niche or "HVAC", "city": city or "Houston, TX"}]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{SCRAPER_URL}/scrape", json=payload)
        data = r.json()
        targets = data.get("targets", [])
        db = get_db()
        db.execute("INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
                   (AGENT, "SCRAPE_TRIGGERED", f"Scrape launched: {targets}", "green"))
        db.commit()
        db.close()
        return f"Scrape started for {len(targets)} target(s): {targets}"
    except Exception as e:
        return f"Failed to trigger scrape: {e}"


async def tool_trigger_outreach() -> str:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{OUTREACH_URL}/ingest-leads", json={})
        data = r.json()
        db = get_db()
        db.execute("INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
                   (AGENT, "OUTREACH_TRIGGERED", f"Outreach ingest triggered: {data}", "yellow"))
        db.commit()
        db.close()
        return f"Outreach agent triggered: {data}"
    except Exception as e:
        return f"Failed to trigger outreach: {e}"


async def tool_schedule_task(delay_minutes: int, task_type: str, message: str = "") -> str:
    if task_type not in ("report", "discord_message", "scrape", "outreach", "self_task"):
        return f"Unknown task_type '{task_type}'. Use: report, discord_message, scrape, outreach, self_task."
    if task_type == "discord_message" and not message:
        return "task_type='discord_message' requires a message."
    db = get_db()
    fire_at = (datetime.now(UTC) + timedelta(minutes=delay_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    payload = json.dumps({"message": message})
    db.execute(
        "INSERT INTO scheduled_tasks (fire_at, task_type, payload) VALUES (?, ?, ?)",
        (fire_at, task_type, payload)
    )
    db.commit()
    db.close()
    human_time = f"{delay_minutes} minute{'s' if delay_minutes != 1 else ''}"
    return f"Scheduled '{task_type}' task to run in {human_time} (at {fire_at} ET)."


def tool_list_scheduled_tasks() -> str:
    db = get_db()
    rows = db.execute(
        "SELECT id, task_type, fire_at, payload FROM scheduled_tasks WHERE status='pending' ORDER BY fire_at"
    ).fetchall()
    db.close()
    if not rows:
        return "No pending scheduled tasks."
    lines = ["**Pending scheduled tasks:**"]
    for r in rows:
        payload = json.loads(r["payload"] or "{}")
        detail = f" — \"{payload['message'][:60]}\"" if payload.get("message") else ""
        lines.append(f"• [{r['id']}] `{r['task_type']}` at {r['fire_at']} ET{detail}")
    return "\n".join(lines)


async def _run_scheduled_task(task_id: int, task_type: str, payload: dict) -> None:
    try:
        if task_type == "report":
            context = payload.get("message", "")
            health  = await tool_run_health_check()
            metrics = tool_get_business_metrics()
            diagnosis = await tool_diagnose_pipeline()
            msg = (
                f"**📊 Scheduled Status Report**{(' — ' + context) if context else ''}\n\n"
                f"{health}\n\n"
                f"**Metrics:** {metrics.get('total_leads',0)} leads · "
                f"{metrics.get('emails_today',0)} emails today · "
                f"{metrics.get('active_clients',0)} clients · "
                f"MRR ${metrics.get('mrr',0):.0f}\n\n"
                f"**Pipeline diagnosis:**\n{diagnosis}"
            )
            await tool_send_discord_alert(msg)
        elif task_type == "discord_message":
            await tool_send_discord_alert(payload.get("message", "(no message)"))
        elif task_type == "scrape":
            await tool_trigger_scrape()
            await tool_send_discord_alert("✅ Scheduled scrape triggered.")
        elif task_type == "outreach":
            await tool_trigger_outreach()
            await tool_send_discord_alert("✅ Scheduled outreach triggered.")
        elif task_type == "self_task":
            prompt = payload.get("message", "")
            if prompt:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(
                        "http://localhost:8109/chat",
                        json={"message": prompt}
                    )
        db = get_db()
        db.execute("UPDATE scheduled_tasks SET status='done' WHERE id=?", (task_id,))
        db.commit()
        db.close()
        logger.info("Scheduled task %d (%s) completed", task_id, task_type)
    except Exception as e:
        logger.error("Scheduled task %d failed: %s", task_id, e)
        db = get_db()
        db.execute("UPDATE scheduled_tasks SET status='failed' WHERE id=?", (task_id,))
        db.commit()
        db.close()


async def tool_get_config() -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{ADMIN_URL}/config")
            return r.json()
    except Exception as e:
        return {"error": str(e), "note": "Admin service may be offline"}


async def tool_set_config(key: str, value: str, restart_agent: str = "") -> str:
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            payload: dict = {"key": key, "value": value}
            if restart_agent:
                payload["restart_agent"] = restart_agent
            r = await c.post(f"{ADMIN_URL}/config", json=payload)
            data = r.json()
            if r.status_code == 200:
                msg = data.get("message", f"Updated {key}={value}")
                if "restart" in data:
                    msg += f" | {data['restart']}"
                db = get_db()
                db.execute("INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
                           (AGENT, "CONFIG_UPDATED", msg, "cyan"))
                db.commit()
                db.close()
                return msg
            return f"Failed: {data.get('error', r.text)}"
    except Exception as e:
        return f"set_config failed: {e}"


async def tool_restart_agent(agent: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=35) as c:
            r = await c.post(f"{ADMIN_URL}/restart/{agent}")
            data = r.json()
            msg = data.get("message", str(data))
            ok = data.get("ok", r.status_code == 200)
            db = get_db()
            db.execute("INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
                       (AGENT, "AGENT_RESTARTED", msg, "green" if ok else "red"))
            db.commit()
            db.close()
            return msg
    except Exception as e:
        return f"restart_agent failed: {e}"


async def tool_run_claude_code_task(prompt: str, working_dir: str = "/home/mike") -> str:
    if not prompt:
        return "Error: prompt is required."
    ssh_cmd = (
        f"cd {shlex.quote(working_dir)} && "
        f"claude --print --dangerously-skip-permissions {shlex.quote(prompt)}"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "UserKnownHostsFile=/dev/null", "mikepc", ssh_cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        output = stdout.decode(errors="replace").strip()
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            return f"Claude Code task failed (exit {proc.returncode}):\n{(err or output)[-2000:]}"
        return output[-4000:] if len(output) > 4000 else output or "(no output)"
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        return "Claude Code task timed out after 5 minutes."
    except FileNotFoundError:
        return "SSH not found in container — rebuild with openssh-client."
    except Exception as e:
        return f"run_claude_code_task error: {type(e).__name__}: {e}"


async def execute_tool(name: str, args: dict) -> str:
    if name == "get_business_metrics":
        return json.dumps(tool_get_business_metrics(), indent=2)
    elif name == "get_leads":
        return json.dumps(tool_get_leads(args.get("stage"), args.get("limit", 10)), indent=2)
    elif name == "get_revenue":
        return json.dumps(tool_get_revenue(), indent=2)
    elif name == "read_wiki_article":
        return tool_read_wiki_article(args.get("filename", ""))
    elif name == "send_discord_alert":
        return await tool_send_discord_alert(args.get("message", ""))
    elif name == "get_recent_activity":
        return json.dumps(tool_get_recent_activity(args.get("limit", 20)), indent=2)
    elif name == "trigger_scrape":
        return await tool_trigger_scrape(args.get("niche"), args.get("city"))
    elif name == "trigger_outreach":
        return await tool_trigger_outreach()
    elif name == "get_recent_conversations":
        return json.dumps(tool_get_recent_conversations(args.get("limit", 30)), indent=2)
    elif name == "get_agent_logs":
        return await tool_get_agent_logs(args.get("agent", ""), args.get("lines", 30))
    elif name == "update_scrape_targets":
        return await tool_update_scrape_targets(args.get("niche", "HVAC"), args.get("cities", []))
    elif name == "run_health_check":
        return await tool_run_health_check()
    elif name == "diagnose_pipeline":
        return await tool_diagnose_pipeline()
    elif name == "get_marketing_social":
        return await tool_get_marketing_social()
    elif name == "advance_outreach_sequences":
        return await tool_advance_outreach()
    elif name == "get_conversion_analytics":
        return await tool_get_conversion_analytics()
    elif name == "set_pricing_mode":
        return await tool_set_pricing_mode(args.get("mode", "standard"))
    elif name == "schedule_task":
        return await tool_schedule_task(
            delay_minutes=int(args.get("delay_minutes", 60)),
            task_type=args.get("task_type", "report"),
            message=args.get("message", "")
        )
    elif name == "list_scheduled_tasks":
        return tool_list_scheduled_tasks()
    elif name == "get_config":
        return json.dumps(await tool_get_config(), indent=2)
    elif name == "set_config":
        return await tool_set_config(args.get("key", ""), str(args.get("value", "")), args.get("restart_agent", ""))
    elif name == "restart_agent":
        return await tool_restart_agent(args.get("agent", ""))
    elif name == "run_claude_code_task":
        return await tool_run_claude_code_task(args.get("prompt", ""), args.get("working_dir", "/home/mike"))
    return f"Unknown tool: {name}"


# ── Chat logic ────────────────────────────────────────────────────────────────

def load_history(session_id: str, limit: int = 30) -> list:
    db = get_db()
    try:
        rows = db.execute(
            "SELECT role, content FROM conversations WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit)
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    finally:
        db.close()


def save_message(session_id: str, role: str, content: str):
    db = get_db()
    db.execute(
        "INSERT INTO conversations (session_id, role, content) VALUES (?,?,?)",
        (session_id, role, content)
    )
    db.execute(
        "INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
        (AGENT, "CHAT", f"{role}: {content[:100]}", "cyan")
    )
    db.commit()
    db.close()


def build_index_context() -> str:
    index_path = KNOWLEDGE_DIR / "index.md"
    if index_path.exists():
        return f"\n\n## Knowledge Base Index\n{index_path.read_text()}"
    return ""


import re as _re

# Patterns that indicate leaked tool call syntax
_TOOL_LEAK_PATTERNS = [
    _re.compile(r'\{["\s]*"?name"?\s*:\s*"[^"]+"\s*,\s*"?(?:parameters|arguments)"?\s*:\s*\{', _re.DOTALL),
    _re.compile(r'\w+\(["\'][\w-]+["\']\)'),   # restart_pod("outreach")
    _re.compile(r'`[a-z_]+\([^`]*\)`'),         # `get_agent_logs(...)`
]

def _extract_text_tool_call(text: str) -> tuple[str, dict] | None:
    """Detect when model writes a tool call as text (JSON or XML format) instead of executing it."""
    known = {t["function"]["name"] for t in TOOLS}

    # JSON format: {"name": "tool_name", "parameters": {...}}
    m = _re.search(
        r'\{["\s]*"?name"?\s*:\s*"([^"]+)"\s*,\s*"?(?:parameters|arguments)"?\s*:\s*(\{.*?\})\s*\}',
        text, _re.DOTALL
    )
    if m:
        tool_name = m.group(1).strip()
        try:
            args = json.loads(m.group(2))
        except Exception:
            args = {}
        return (tool_name, args) if tool_name in known else None

    # XML format: <function>tool_name</function> or <function=tool_name>...</function>
    m = _re.search(r'<function[=\s>]([^<>]+?)(?:</function>|>)', text)
    if m:
        tool_name = m.group(1).strip().rstrip('>')
        if tool_name in known:
            return (tool_name, {})

    return None


def _sanitize_response(text: str) -> str:
    """Strip any leaked tool call syntax from the final response before sending to user."""
    import re
    # Remove <function=name>...</function> and <function>name</function> markup
    text = re.sub(r'<function[^>]*>.*?</function>', '', text, flags=re.DOTALL)
    # Remove [function_call] style markers
    text = re.sub(r'\[function[_\s]call[^\]]*\]', '', text, flags=re.IGNORECASE)
    # Remove JSON tool call blocks (multi-line)
    text = re.sub(
        r'\{["\s]*"?name"?\s*:\s*"[^"]+"\s*,\s*"?(?:parameters|arguments)"?\s*:\s*\{.*?\}\s*\}',
        '', text, flags=re.DOTALL
    )
    # Remove lines containing code-style function calls to non-existent tools
    lines = []
    for line in text.splitlines():
        if re.search(r'restart_pod\(|`\w+\(', line):
            continue
        lines.append(line)
    # Collapse excessive blank lines
    text = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines))
    return text.strip()


def _to_groq_messages(messages: list) -> list:
    """Convert Ollama-format message history to Groq/OpenAI format."""
    result = []
    for m in messages:
        m = dict(m)
        if m.get("tool_calls"):
            tcs = []
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                args = fn.get("arguments") or {}
                if not isinstance(args, str):
                    args = json.dumps(args)
                tcs.append({
                    "id": tc.get("id") or f"call_{len(tcs)}",
                    "type": "function",
                    "function": {"name": fn.get("name", ""), "arguments": args},
                })
            m["tool_calls"] = tcs
        result.append(m)
    return result


def _trim_for_groq(messages: list, max_tokens: int = 9000) -> list:
    """Trim messages to stay under Groq TPM limit. Keeps system msg; truncates tool results."""
    def _est(m: dict) -> int:
        c = m.get("content") or ""
        return len(str(c)) // 4

    system = [m for m in messages if m.get("role") == "system"]
    rest   = [m for m in messages if m.get("role") != "system"]
    budget = max_tokens - sum(_est(m) for m in system)

    # Truncate oversized tool results first
    trimmed = []
    for m in rest:
        m = dict(m)
        if m.get("role") == "tool" and m.get("content") and len(m["content"]) > 600:
            m["content"] = m["content"][:600] + "…[trimmed]"
        trimmed.append(m)

    # Drop oldest non-system messages until within budget
    while trimmed and sum(_est(m) for m in trimmed) > budget:
        trimmed.pop(0)

    return system + trimmed


def _normalize_openai_response(msg: dict, provider: str) -> dict:
    """Normalize OpenAI-format tool_calls (string args) to internal format (dict args)."""
    if msg.get("tool_calls"):
        normalized = []
        for tc in msg["tool_calls"]:
            fn = tc.get("function", {})
            args = fn.get("arguments") or "{}"
            if isinstance(args, str):
                try: args = json.loads(args)
                except: args = {}
            normalized.append({"id": tc.get("id", ""), "function": {"name": fn.get("name", ""), "arguments": args}})
        msg["tool_calls"] = normalized
    return msg


async def _try_openai_provider(url: str, api_key: str, model: str, messages: list, label: str) -> dict | None:
    """Try a single OpenAI-compatible provider. Returns normalized message or None on failure."""
    trimmed = _to_groq_messages(_trim_for_groq(messages))
    for attempt in range(2):
        use_tools = attempt == 0  # on second attempt (400 retry), drop tools
        try:
            body: dict = {"model": model, "messages": trimmed}
            if use_tools:
                body["tools"] = TOOLS
                body["tool_choice"] = "auto"
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=body,
                )
            if resp.status_code == 200:
                msg = resp.json()["choices"][0]["message"]
                logger.info(f"LLM: {label} (attempt {attempt+1}{'  no-tools' if not use_tools else ''})")
                return _normalize_openai_response(msg, label)
            elif resp.status_code == 429:
                wait = min(float(resp.headers.get("retry-after", 8)), 15)
                logger.warning(f"{label} rate-limited (attempt {attempt+1}/2), waiting {wait:.0f}s")
                if attempt == 0:
                    await asyncio.sleep(wait)
                    continue
                logger.warning(f"{label} still rate-limited after retry — skipping")
                return None
            elif resp.status_code == 400 and use_tools:
                # Tool-calling format rejected (common with Groq on complex histories) — retry without tools
                logger.warning(f"{label} tool-call 400 — retrying without tools")
                continue
            else:
                logger.warning(f"{label} HTTP {resp.status_code}: {resp.text[:150]} — skipping")
                return None
        except Exception as e:
            logger.warning(f"{label} unavailable ({type(e).__name__}: {e}) — skipping")
            return None
    return None


async def call_llm(messages: list) -> dict:
    """Try Gemini → Ollama → Groq 70b → Groq 8b. Ollama (local) preferred over burning cloud quota."""
    errors = []

    # 1. Gemini 2.5 Flash (primary — Google AI Pro $20/mo)
    if GEMINI_API_KEY:
        try:
            result = await _try_openai_provider(GEMINI_URL, GEMINI_API_KEY, GEMINI_MODEL, messages, f"gemini/{GEMINI_MODEL}")
            if result is not None:
                return result
            errors.append("Gemini: rate-limited")
        except Exception as e:
            errors.append(f"Gemini: {e}")

    # 2. Ollama local (MikeNixPC — no rate limits; fast on GPU, quick-fail on CPU)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={"model": CHAT_TOOL_MODEL, "messages": messages, "tools": TOOLS, "stream": False},
            )
        if resp.status_code == 200:
            logger.info(f"LLM: ollama/{CHAT_TOOL_MODEL}")
            return resp.json().get("message", {})
        errors.append(f"Ollama: HTTP {resp.status_code}")
    except Exception as e:
        errors.append(f"Ollama: {e}")

    # 3. Groq llama-3.3-70b (cloud fallback when MikeNixPC unreachable)
    if GROQ_API_KEY:
        try:
            result = await _try_openai_provider(GROQ_URL, GROQ_API_KEY, GROQ_MODEL, messages, f"groq/{GROQ_MODEL}")
            if result is not None:
                return result
            errors.append(f"Groq {GROQ_MODEL}: rate-limited")
        except Exception as e:
            errors.append(f"Groq {GROQ_MODEL}: {e}")

    # 4. Groq llama-3.1-8b-instant (highest quota, last resort)
    if GROQ_API_KEY:
        try:
            result = await _try_openai_provider(GROQ_URL, GROQ_API_KEY, GROQ_FAST_MODEL, messages, f"groq/{GROQ_FAST_MODEL}")
            if result is not None:
                return result
            errors.append(f"Groq {GROQ_FAST_MODEL}: rate-limited")
        except Exception as e:
            errors.append(f"Groq {GROQ_FAST_MODEL}: {e}")

    raise Exception(" | ".join(errors))


async def chat(session_id: str, user_message: str) -> str:
    history = load_history(session_id)
    save_message(session_id, "user", user_message)

    system_content = SYSTEM_PROMPT + build_index_context() + build_live_context()
    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # Tool calling loop
    called_tools: set[str] = set()  # (name:args_json) to detect loops

    for round_num in range(MAX_TOOL_ROUNDS):
        try:
            msg = await call_llm(messages)
        except Exception as e:
            logger.error(f"LLM call failed (round {round_num}): {e}")
            fallback = (
                "I'm temporarily unavailable — Gemini is rate-limited and all fallbacks (Ollama, Groq) are also unreachable right now. "
                "Please try again in 60 seconds. (If this persists, check that Ollama is running on MikeNixPC.)"
            )
            save_message(session_id, "assistant", fallback)
            return fallback

        if not msg:
            logger.error(f"LLM returned empty message")
            return "LLM returned no message."
        tool_calls = msg.get("tool_calls", [])

        if not tool_calls:
            response = msg.get("content", "").strip()
            # Detect when model leaked a tool call as text instead of executing it
            text_tool = _extract_text_tool_call(response)
            if text_tool:
                name, args = text_tool
                key = f"{name}:{json.dumps(args, sort_keys=True)}"
                if key not in called_tools:
                    logger.info(f"Text-format tool call detected: {name} — executing")
                    called_tools.add(key)
                    result = await execute_tool(name, args)
                    messages.append({"role": "assistant", "content": response})
                    messages.append({"role": "tool", "content": str(result)})
                    continue  # let model synthesize with the result
            response = _sanitize_response(response)
            save_message(session_id, "assistant", response)
            return response

        # Deduplicate — skip any tool calls already executed this turn
        new_calls = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            key = f"{fn.get('name')}:{json.dumps(fn.get('arguments', {}), sort_keys=True)}"
            if key not in called_tools:
                new_calls.append(tc)
                called_tools.add(key)

        if not new_calls:
            # All requested tool calls are duplicates — inject synthesis nudge and let model respond
            messages.append({
                "role": "user",
                "content": "You already have all the tool results above. Respond to the original request now — plain text, no JSON."
            })
            continue

        messages.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": new_calls})
        for tc in new_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try: args = json.loads(args)
                except: args = {}
            if not isinstance(args, dict):
                args = {}
            result = await execute_tool(name, args)
            logger.info(f"Tool: {name}({args}) → {str(result)[:100]}")
            tool_msg = {"role": "tool", "content": str(result)}
            if tc.get("id"):
                tool_msg["tool_call_id"] = tc["id"]
            messages.append(tool_msg)

    return f"Reached tool call limit ({MAX_TOOL_ROUNDS} rounds)."


def update_heartbeat():
    db = get_db()
    db.execute("""
        INSERT INTO agent_status (agent_name, status, last_heartbeat, last_action, actions_today)
        VALUES (?, 'online', datetime('now'), 'chat request', 1)
        ON CONFLICT(agent_name) DO UPDATE SET
            status='online', last_heartbeat=datetime('now'),
            last_action='chat request', actions_today=actions_today+1
    """, (AGENT,))
    db.commit()
    db.close()


# ── FastAPI ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str = ""


async def _send_daily_discord_digest() -> None:
    """Post a morning digest to Discord with key metrics and what the system did overnight."""
    if not DISCORD_URL:
        return
    try:
        m = tool_get_business_metrics()
        activity = tool_get_recent_activity(20)
        stages = m.get("pipeline_stages", {})
        stage_str = " | ".join(f"{k}:{v}" for k, v in stages.items()) if stages else "empty"

        recent_lines = []
        for e in activity[:10]:
            ts = (e.get("timestamp") or "")[:16]
            recent_lines.append(f"• `{ts}` [{e.get('agent','')}] {e.get('message','')[:70]}")
        recent_text = "\n".join(recent_lines) or "• No activity"

        msg = (
            f"☀️ **RingCatch Morning Digest — {now_et().strftime('%Y-%m-%d')}**\n"
            f"MRR `${m['mrr']:.0f}` | Clients `{m['active_clients']}` | Leads `{m['total_leads']}`\n"
            f"Pipeline: {stage_str}\n"
            f"Emails today: `{m['emails_today']}/{m['email_limit']}`\n\n"
            f"**Overnight activity:**\n{recent_text}"
        )
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(DISCORD_URL, json={"content": msg})
        logger.info("Daily digest sent to Discord")
    except Exception as e:
        logger.warning(f"Daily digest failed: {e}")


async def _send_health_report() -> None:
    agents  = await get_all_agents()
    metrics = tool_get_business_metrics()
    online  = [a["name"].replace("agency-", "") for a in agents if a["status"] == "online"]
    offline = [a["name"].replace("agency-", "") for a in agents if a["status"] != "online"]

    agent_lines = "\n".join(
        f"{'✅' if a['status'] == 'online' else '❌'} {a['name'].replace('agency-','')}"
        + (f" — {a.get('error','')[:60]}" if a.get('error') else "")
        for a in sorted(agents, key=lambda x: x["name"])
    )

    issues = []
    if offline:
        issues.append(f"⚠️ **{len(offline)} agent(s) offline:** {', '.join(offline)}")
    pending_ev = metrics.get("pending_events", 0)
    if pending_ev > 10:
        issues.append(f"⚠️ {pending_ev} pending events in event_bus — possible processing backlog")
    if metrics.get("emails_today", 0) == 0:
        issues.append("⚠️ No emails sent today — outreach may be stalled")

    status_line = f"🟢 All {len(online)} agents online" if not offline else f"🔴 {len(offline)} offline, {len(online)} online"
    now_str = fmt_et()

    msg = (
        f"**🔍 Periodic Health Report** — {now_str}\n\n"
        f"**{status_line}**\n{agent_lines}\n\n"
        f"**📊 Pipeline**\n"
        f"• {metrics.get('total_leads', 0)} leads · {metrics.get('emails_today', 0)} emails today · "
        f"{metrics.get('active_clients', 0)} clients · MRR ${metrics.get('mrr', 0):.0f}\n"
        f"• Reply rate: {metrics.get('reply_rate', 0)}%"
    )
    if issues:
        msg += "\n\n**⚠️ Issues Detected**\n" + "\n".join(issues)
    else:
        msg += "\n\n✅ No issues detected."

    await tool_send_discord_alert(msg)
    logger.info("Health report sent to Discord — %d online, %d offline", len(online), len(offline))


async def _autonomous_monitor_loop() -> None:
    await asyncio.sleep(120)
    last_digest_day = ""
    last_health_report = now_et() - timedelta(hours=HEALTH_REPORT_INTERVAL_HOURS)  # fire soon after startup
    while True:
        try:
            now = now_et()
            metrics = tool_get_business_metrics()
            stages       = metrics.get("pipeline_stages", {})
            scraped      = stages.get("scraped", 0)
            emails_today = metrics.get("emails_today", 0)
            total_leads  = metrics.get("total_leads", 0)

            # ── Morning digest once per day at 08:00 ET ───────────────────────
            if now.hour == 8 and now.minute < 30 and now.strftime("%Y-%m-%d") != last_digest_day:
                await _send_daily_discord_digest()
                last_digest_day = now.strftime("%Y-%m-%d")

            # ── Periodic health report (8 AM–9 PM ET only) ───────────────────
            hours_since = (now - last_health_report).total_seconds() / 3600
            in_window = 8 <= now.hour < 21
            if hours_since >= HEALTH_REPORT_INTERVAL_HOURS and in_window:
                asyncio.create_task(_send_health_report())
                last_health_report = now

            # ── Scheduled tasks ───────────────────────────────────────────────
            due_db = get_db()
            due_tasks = due_db.execute(
                "SELECT id, task_type, payload FROM scheduled_tasks WHERE status='pending' AND fire_at <= datetime('now')"
            ).fetchall()
            due_db.close()
            for task in due_tasks:
                asyncio.create_task(_run_scheduled_task(
                    task["id"], task["task_type"], json.loads(task["payload"] or "{}")
                ))

            # ── Pipeline chaining: scrape → qualify (event_bus) → outreach ───
            if total_leads == 0 or (scraped == 0 and emails_today == 0):
                logger.info("Monitor: empty or stalled pipeline — triggering scrape")
                await tool_trigger_scrape()
                await asyncio.sleep(30)  # let scraper write leads and events

            if scraped > 0 and emails_today < 10:
                logger.info("Monitor: %d scraped leads uncontacted — triggering outreach", scraped)
                await tool_trigger_outreach()

            # ── Advance follow-up sequences (check every loop) ────────────────
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(f"{OUTREACH_URL}/advance")
            except Exception:
                pass  # outreach may be starting up

            logger.info(
                "Monitor: ok — leads=%d scraped=%d emails=%d clients=%d MRR=$%.0f",
                total_leads, scraped, emails_today, metrics.get("active_clients", 0), metrics.get("mrr", 0)
            )
        except Exception as e:
            logger.error("Monitor loop error: %s", e)
        await asyncio.sleep(900)  # check every 15 minutes


@asynccontextmanager
async def lifespan(app: FastAPI):
    global WEBCHAT_SYSTEM_PROMPT
    init_tables()
    WEBCHAT_SYSTEM_PROMPT = _build_webchat_system_prompt()
    logger.info("Orchestrator started — knowledge dir: %s, webchat prompt: %d chars", KNOWLEDGE_DIR, len(WEBCHAT_SYSTEM_PROMPT))
    asyncio.create_task(_autonomous_monitor_loop())
    yield


app = FastAPI(title="Agency Orchestrator", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _build_webchat_system_prompt() -> str:
    """Build Alex's system prompt with live knowledge base articles injected."""
    knowledge_files = [
        ("Pricing & Negotiation", "business/pricing.md"),
        ("Objection Handling Scripts", "sales/objection-handling.md"),
        ("Target Niches & Pain Points", "marketing/target-niches.md"),
    ]
    kb_sections = []
    for title, path in knowledge_files:
        full_path = KNOWLEDGE_DIR / path
        if full_path.exists():
            kb_sections.append(f"### {title}\n{full_path.read_text().strip()}")

    kb_block = "\n\n".join(kb_sections)

    return f"""You are Alex, a friendly AI assistant on the RingCatch website. You serve two purposes at once:
1. You ARE the product demo — you're showing the visitor exactly what an AI chatbot like this does for their business.
2. You convert interested visitors into booked discovery calls.

## About RingCatch
RingCatch builds and manages AI chatbots for local small businesses — HVAC, plumbers, electricians, dental offices, auto repair, law firms, insurance, cleaning, landscaping — across the US.
Pricing: $450 one-time setup + $89/month. Includes: 24/7 chat on their website, lead capture, FAQ answering, appointment booking, and monthly updates.
Typical client ROI: one extra job booked per week pays for the whole year.

## Your conversation goals (in order)
1. Welcome warmly, ask what kind of business they run (or if they're just curious about AI chatbots).
2. Relate to their specific niche — show you know their pain points (missed calls, after-hours leads, staff answering the same questions).
3. Explain the value in 1-2 sentences tailored to their business type.
4. When they show any interest, capture their name + email (or phone) using capture_lead tool.
5. Offer a free 15-minute discovery call. If they say yes, use get_booking_link to send the Cal.com link.

## Rules
- Be conversational, concise — 2-3 sentences max per reply. Never bullet-point dump.
- Mirror the demo: say things like "This is exactly what your chatbot would do for a customer."
- Use the exact objection scripts below — they are tested and effective. Adapt them naturally, don't quote verbatim.
- Never be pushy. If they just want to browse, be friendly and leave the door open.
- Do NOT mention internal tools, agent names, Ollama, or any tech stack details.
- If asked something you don't know, say you'll have the team follow up — then capture their contact.
- NEVER call capture_lead with placeholder or unknown values. Only call it when the visitor has actually told you their real name, email, or phone number. If you don't have their real info yet, ask for it in text — do not call the tool.
- NEVER output raw JSON, tool call syntax, or function arguments in your text replies. Your responses must be plain conversational English only.

## Reference Material (use this to answer specific questions)
{kb_block}
"""

WEBCHAT_SYSTEM_PROMPT: str = ""  # populated at startup

WEBCHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "capture_lead",
            "description": "Save visitor contact info. Only call this when the visitor has actually provided their real name or contact info in conversation. Never call with placeholder or unknown values. At minimum, name must be a real person's name they told you.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":          {"type": "string", "description": "Visitor's name"},
                    "email":         {"type": "string", "description": "Email address"},
                    "phone":         {"type": "string", "description": "Phone number (optional)"},
                    "business_type": {"type": "string", "description": "Their business niche e.g. HVAC, plumber, dental"},
                    "city":          {"type": "string", "description": "Their city/location (optional)"},
                    "notes":         {"type": "string", "description": "Any context about their interest or pain points"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_booking_link",
            "description": "Get the discovery call booking link to send to an interested visitor.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


_PLACEHOLDER_NAMES = {"unknown", "website visitor", "visitor", "user", "customer", "n/a", "none", ""}

def _webchat_sanitize(text: str) -> str:
    """Strip leaked tool call JSON and function markup from webchat replies."""
    import re
    # Remove <function=name>...</function> markup (some models leak this format)
    text = re.sub(r'<function=\w+>.*?</function>', '', text, flags=re.DOTALL)
    # Remove [function_call] style markers
    text = re.sub(r'\[function[_\s]call[^\]]*\]', '', text, flags=re.IGNORECASE)
    # Remove {"key": "value"} blocks that look like tool arguments (must have a colon = JSON key:value)
    text = re.sub(r'\{[^{}]*"[^"]+"\s*:[^{}]{0,400}\}', '', text)
    # Collapse multiple spaces/newlines left by removal
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'  +', ' ', text)
    return text.strip()


def _webchat_log_activity(session_id: str, event: str, message: str):
    """Write a webchat event to activity_log so all agents (BI, orchestrator) can see it."""
    try:
        db = get_db()
        db.execute(
            "INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
            ("agency-webchat", event, message, "cyan")
        )
        db.commit()
        db.close()
    except Exception as e:
        logger.warning(f"Webchat activity_log write failed: {e}")


def _webchat_upsert_session(session_id: str, **kwargs):
    """Create or update a chat_analytics row for this webchat session."""
    try:
        db = get_db()
        existing = db.execute(
            "SELECT id FROM chat_analytics WHERE session_id=?", (session_id,)
        ).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in kwargs if k != "started_at")
            vals = [v for k, v in kwargs.items() if k != "started_at"]
            vals += [session_id]
            if sets:
                db.execute(f"UPDATE chat_analytics SET last_active=datetime('now'), {sets} WHERE session_id=?", vals)
        else:
            db.execute(
                "INSERT INTO chat_analytics (session_id, started_at, last_active) VALUES (?,datetime('now'),datetime('now'))",(session_id,)
            )
            if kwargs:
                sets = ", ".join(f"{k}=?" for k in kwargs if k != "started_at")
                vals = [v for k, v in kwargs.items() if k != "started_at"] + [session_id]
                if sets:
                    db.execute(f"UPDATE chat_analytics SET {sets} WHERE session_id=?", vals)
        db.commit()
        db.close()
    except Exception as e:
        logger.warning(f"Webchat session upsert failed: {e}")


async def _webchat_capture_lead(args: dict, session_id: str = "") -> str:
    name = (args.get("name") or "").strip()
    email = (args.get("email") or "").strip()
    # Reject placeholder/unknown values — don't persist junk data
    if name.lower() in _PLACEHOLDER_NAMES and not email:
        logger.warning(f"capture_lead called with placeholder values: {args}")
        return "lead_not_saved_no_real_data"
    try:
        db = get_db()
        db.execute("""
            INSERT OR IGNORE INTO leads (business_name, niche, city, email, phone, pipeline_stage, processed)
            VALUES (?, ?, ?, ?, ?, 'webchat', 1)
        """, (
            args.get("name", "Website visitor"),
            args.get("business_type", "unknown"),
            args.get("city", ""),
            args.get("email", ""),
            args.get("phone", ""),
        ))
        db.commit()
        # Fire NEW_LEAD event so sales agent qualifies and sequences this lead
        if args.get("email"):
            lead_row = db.execute("SELECT id FROM leads WHERE email=?", (args["email"],)).fetchone()
            if lead_row:
                lead_id = lead_row["id"]
                db.execute("""
                    INSERT INTO event_bus (source_agent, target_agent, event_type, priority, payload)
                    VALUES ('agency-webchat', 'agency-sales', 'NEW_LEAD', 2, ?)
                """, (json.dumps({"lead_id": lead_id, "source": "website_chat", "notes": args.get("notes", "")}),))
                db.commit()
                logger.info(f"NEW_LEAD event fired for webchat lead {lead_id}")
        db.close()
        logger.info(f"Webchat lead captured: {args.get('name')} / {args.get('email')}")
        # Update chat_analytics with contact info
        _webchat_upsert_session(
            session_id,
            name=args.get("name", ""),
            industry=args.get("business_type", ""),
            email_captured=args.get("email", ""),
            phone=args.get("phone", ""),
        )
        _webchat_log_activity(
            session_id, "LEAD_CAPTURED",
            f"Website lead: {args.get('name')} ({args.get('business_type','?')}) "
            f"{args.get('email') or args.get('phone','no contact')} — {args.get('notes','')[:100]}"
        )
        # Ping discord
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(DISCORD_URL, json={"content": f"🌐 **Website lead:** {args.get('name')} ({args.get('business_type','?')}) — {args.get('email') or args.get('phone','no contact yet')} | {args.get('notes','')[:100]}"})
        except Exception:
            pass
        return "lead_saved"
    except Exception as e:
        logger.warning(f"Webchat lead capture failed: {e}")
        return "lead_save_failed"


async def _webchat_execute_tool(name: str, args: dict, session_id: str = "") -> str:
    if name == "capture_lead":
        return await _webchat_capture_lead(args, session_id)
    if name == "get_booking_link":
        _webchat_upsert_session(session_id, converted=1, close_reached=1)
        _webchat_log_activity(session_id, "BOOKING_LINK_SENT", "Visitor requested discovery call booking link")
        return "https://ringcatch.io/book"
    return "unknown_tool"


async def _call_webchat_llm(messages: list) -> dict:
    """Try Gemini → Ollama → Groq 70b for webchat."""
    trimmed = _to_groq_messages(_trim_for_groq(messages, max_tokens=6000))

    # 1. Gemini 2.5 Flash
    if GEMINI_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(GEMINI_URL, headers={"Authorization": f"Bearer {GEMINI_API_KEY}"},
                                         json={"model": GEMINI_MODEL, "messages": trimmed, "tools": WEBCHAT_TOOLS, "tool_choice": "auto"})
            if resp.status_code == 200:
                label = f"gemini/{GEMINI_MODEL}"
                logger.info(f"Webchat LLM: {label}")
                return _normalize_openai_response(resp.json()["choices"][0]["message"], label)
            logger.warning(f"Webchat gemini HTTP {resp.status_code} — trying next")
        except Exception as e:
            logger.warning(f"Webchat gemini error: {e} — trying next")

    # 2. Ollama local (qwen2.5:14b — no rate limits; fast on GPU, quick-fail on CPU)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{OLLAMA_URL}/api/chat",
                                      json={"model": CHAT_TOOL_MODEL, "messages": messages, "tools": WEBCHAT_TOOLS, "stream": False})
        if resp.status_code == 200:
            logger.info(f"Webchat LLM: ollama/{CHAT_TOOL_MODEL}")
            return resp.json().get("message", {})
        logger.warning(f"Webchat ollama HTTP {resp.status_code} — trying next")
    except Exception as e:
        logger.warning(f"Webchat ollama error: {e} — trying next")

    # 3. Groq 70b (cloud fallback when MikeNixPC unreachable)
    if GROQ_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(GROQ_URL, headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                                         json={"model": GROQ_MODEL, "messages": trimmed, "tools": WEBCHAT_TOOLS, "tool_choice": "auto"})
            if resp.status_code == 200:
                label = f"groq/{GROQ_MODEL}"
                logger.info(f"Webchat LLM: {label}")
                return _normalize_openai_response(resp.json()["choices"][0]["message"], label)
            logger.warning(f"Webchat groq HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"Webchat groq error: {e}")

    raise Exception("All webchat LLM providers failed")


def _webchat_fallback_reply(user_message: str, msg_count: int) -> str:
    """Contextual fallback when all LLMs are unavailable for homepage chat."""
    msg = user_message.lower().strip()

    # Pricing
    if any(w in msg for w in ("price", "pricing", "cost", "how much", "fee", "charge", "afford")):
        return (
            "Setup is a one-time $450, then $89/month — and most owners cover that with the very first extra job it captures. "
            "Want to jump on a quick 15-minute call with Mike to see if it makes sense for your business?"
        )

    # How it works / features
    if any(w in msg for w in ("how does", "how do", "what does", "what is", "explain", "work", "feature", "do for")):
        return (
            "It's a custom AI chatbot on your website — trained on your services, prices, and hours. "
            "It answers questions, captures leads, and books appointments 24/7, even when you're tied up. "
            "What kind of business do you run? I can show you what it'd look like for you."
        )

    # Booking / call
    if any(w in msg for w in ("call", "talk", "speak", "book a", "schedule", "meet", "demo")):
        return (
            "Absolutely — you can pick a time right here: "
            "https://cal.com/michael-olszewski-nn9caa/15-min-discovery-call. "
            "Mike's usually available same week."
        )

    # Contract / cancellation
    if any(w in msg for w in ("contract", "cancel", "lock", "commitment", "tied")):
        return (
            "No contract — month to month, cancel any time with 30 days notice. "
            "Most owners stay because it pays for itself quickly. Want to see how it'd work for your business?"
        )

    # Early in conversation — they just told us their business type
    if msg_count <= 2:
        biz = user_message.strip()
        return (
            f"A {biz} — nice! I bet you get inquiries when you're too busy to pick up. "
            "Does that happen to you — calls or messages you can't always get to right away?"
        )

    # Mid conversation
    if msg_count <= 5:
        return (
            "That's exactly what the AI handles automatically — around the clock, without you lifting a finger. "
            "Want to jump on a quick 15-minute call with Mike to see what it'd look like for your specific business?"
        )

    # Late / close
    return (
        "I want to make sure Mike can answer that properly — he's the founder and knows every detail. "
        "Can I grab your name and email so he can follow up with you directly?"
    )


async def webchat(session_id: str, user_message: str) -> str:
    """Lightweight sales-focused chat loop for website visitors."""
    # Prefix ensures webchat sessions are distinct from admin sessions in the shared table
    wc_session = f"wc_{session_id}"
    db = get_db()
    rows = db.execute(
        "SELECT role, content FROM conversations WHERE session_id=? ORDER BY id DESC LIMIT 10",
        (wc_session,)
    ).fetchall()
    db.close()
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    messages = [{"role": "system", "content": WEBCHAT_SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    def _save(role: str, content: str):
        d = get_db()
        d.execute(
            "INSERT INTO conversations (session_id, role, content, timestamp) VALUES (?,?,?,datetime('now'))",
            (wc_session, role, content)
        )
        d.commit()
        d.close()

    _save("user", user_message)

    # Ensure session row exists; track message volume as a proxy for engagement depth
    msg_count = len(history) + 1
    phase = min(3, 1 + msg_count // 3)  # rough phase: 1=intro 2=qualify 3=close
    _webchat_upsert_session(wc_session, phase_reached=phase)

    for _ in range(6):
        try:
            msg = await _call_webchat_llm(messages)
        except Exception as e:
            logger.warning(f"Webchat LLM error: {e} — using contextual fallback")
            reply = _webchat_fallback_reply(user_message, msg_count)
            _save("assistant", reply)
            return reply

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            reply = _webchat_sanitize(msg.get("content") or "")
            if not reply:
                logger.warning("Webchat LLM returned empty content — using contextual fallback")
                reply = _webchat_fallback_reply(user_message, msg_count)
            _save("assistant", reply)
            return reply

        msg_clean = dict(msg)
        msg_clean["content"] = ""
        messages.append(msg_clean)
        for tc in tool_calls:
            fn = tc.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try: args = json.loads(args)
                except: args = {}
            if not isinstance(args, dict):
                args = {}
            result = await _webchat_execute_tool(fn.get("name", ""), args, wc_session)
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": result})

    reply = _webchat_fallback_reply(user_message, msg_count)
    _save("assistant", reply)
    return reply


@app.post("/cal-webhook")
async def cal_webhook(payload: dict):
    """Receive Cal.com booking confirmations and update lead pipeline."""
    try:
        trigger = payload.get("triggerEvent", "")

        # Handle cancellations and reschedules
        if trigger in ("BOOKING_CANCELLED", "BOOKING_REJECTED"):
            uid = payload.get("payload", {}).get("uid", "")
            db = get_db()
            db.execute("UPDATE leads SET pipeline_stage='cancelled' WHERE id IN (SELECT client_id FROM bookings WHERE booking_uid=?)", (uid,))
            db.commit()
            db.close()
            await tool_send_discord_alert(f"📅 Booking **{trigger.lower().replace('_',' ')}**: uid={uid}")
            return {"status": "ok", "trigger": trigger}

        if trigger == "BOOKING_RESCHEDULED":
            uid = payload.get("payload", {}).get("uid", "")
            new_time = payload.get("payload", {}).get("startTime", "")
            db = get_db()
            db.execute("UPDATE bookings SET booking_time=? WHERE booking_uid=?", (new_time, uid))
            db.commit()
            db.close()
            await tool_send_discord_alert(f"📅 Booking rescheduled: uid={uid} → {new_time}")
            return {"status": "ok", "trigger": trigger}

        if trigger not in ("BOOKING_CREATED", "BOOKING_CONFIRMED", ""):
            return {"status": "ignored", "trigger": trigger}

        attendee = {}
        attendees = payload.get("payload", {}).get("attendees", [])
        if attendees:
            attendee = attendees[0]
        else:
            attendee = payload.get("payload", {}) or payload

        name  = attendee.get("name") or payload.get("payload", {}).get("attendee", {}).get("name", "Unknown")
        email = attendee.get("email") or payload.get("payload", {}).get("attendee", {}).get("email", "")
        start = payload.get("payload", {}).get("startTime") or payload.get("startTime", "")
        uid   = payload.get("payload", {}).get("uid") or payload.get("uid", str(uuid.uuid4()))

        db = get_db()
        # Find lead by email
        lead_row = db.execute("SELECT * FROM leads WHERE email=?", (email,)).fetchone() if email else None
        lead_id = lead_row["id"] if lead_row else None

        # Update lead pipeline
        if lead_id:
            db.execute(
                "UPDATE leads SET pipeline_stage='booked', last_contacted=datetime('now') WHERE id=?",
                (lead_id,)
            )

        # Save booking record (uses delivery agent's schema + our added columns)
        niche_val = lead_row["niche"] if lead_row else ""
        db.execute("""
            INSERT OR IGNORE INTO bookings (client_id, client_name, niche, booking_time, attendee_email, booking_uid)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (str(lead_id) if lead_id else None, name, niche_val, start, email, uid))

        db.execute(
            "INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
            (AGENT, "BOOKING", f"Discovery call booked: {name} ({email}) at {start}", "green")
        )
        db.commit()
        db.close()

        await tool_send_discord_alert(
            f"📅 **Discovery call booked!**\n"
            f"Name: {name}\nEmail: {email}\nTime: {start}\n"
            f"{'Lead updated to booked stage.' if lead_id else 'No matching lead found — new contact.'}"
        )
        logger.info(f"Cal.com booking: {name} / {email} at {start}")
        return {"status": "ok", "lead_id": lead_id}
    except Exception as e:
        logger.error(f"Cal webhook error: {e}")
        return {"status": "error", "error": str(e)}


@app.post("/webchat")
async def webchat_endpoint(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    response = await webchat(session_id, req.message)
    return {"response": response, "session_id": session_id}


@app.post("/chat")
async def chat_endpoint(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    update_heartbeat()
    response = await chat(session_id, req.message)
    return {"response": response, "session_id": session_id}


@app.get("/history/{session_id}")
def history(session_id: str):
    return load_history(session_id, limit=50)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    db = get_db()
    row = db.execute("SELECT * FROM agent_status WHERE agent_name=?", (AGENT,)).fetchone()
    conv_count = db.execute("SELECT COUNT(DISTINCT session_id) FROM conversations").fetchone()[0]
    db.close()
    return {
        "agent": AGENT,
        "model": f"chat:{CHAT_TOOL_MODEL} / backend:{BK_MODEL}",
        "active_sessions": conv_count,
        "knowledge_articles": len(list(KNOWLEDGE_DIR.rglob("*.md"))) if KNOWLEDGE_DIR.exists() else 0,
        "actions_today": row["actions_today"] if row else 0
    }
