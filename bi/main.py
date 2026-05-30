import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, UTC, date
from pathlib import Path

import httpx
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB_PATH      = Path(os.environ.get("DB_PATH", "/data/agency.db"))
DISCORD_URL  = os.environ.get("DISCORD_BOT_URL", "http://agency-discord:8103/alert")
OLLAMA_URL   = os.environ.get("OLLAMA_BASE_URL", "http://host.containers.internal:11434")
BK_MODEL     = os.environ.get("BACKEND_MODEL", "gemma4:26b")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
AGENT        = "agency-bi"
STRATEGY_DIR = Path("/data/strategy")

BENCHMARKS = {
    "email_open_rate": 0.21,
    "reply_rate": 0.035,
    "lead_to_client_conversion": 0.02,
    "monthly_churn_rate": 0.05,
    "avg_client_ltv_months": 24,
}

start_time = datetime.now(UTC)


def get_db():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def init_tables():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS intelligence_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, metric_name TEXT,
        metric_value REAL, notes TEXT
    );
    CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT DEFAULT (datetime('now')),
        agent TEXT, event_type TEXT, message TEXT, color TEXT DEFAULT 'blue'
    );
    CREATE TABLE IF NOT EXISTS agent_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT UNIQUE, status TEXT,
        last_heartbeat TEXT, last_action TEXT,
        actions_today INTEGER DEFAULT 0, alerts_active INTEGER DEFAULT 0
    );
    """)
    db.commit()
    db.close()
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)


async def send_discord(message: str):
    if not DISCORD_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(DISCORD_URL, json={"content": f"🧠 **BI**: {message}"})
    except Exception as e:
        logger.warning(f"Discord failed: {e}")


def aggregate_metrics(db) -> dict:
    def safe_count(query, *args):
        try:
            return db.execute(query, args).fetchone()[0]
        except Exception:
            return 0

    def safe_sum(query, *args):
        try:
            r = db.execute(query, args).fetchone()[0]
            return r or 0.0
        except Exception:
            return 0.0

    total_leads = safe_count("SELECT COUNT(*) FROM leads")
    active_clients = safe_count("SELECT COUNT(*) FROM clients WHERE status='active'")
    total_revenue = safe_sum("SELECT SUM(amount) FROM financial_ledger")
    setup_fees = safe_sum("SELECT SUM(amount) FROM financial_ledger WHERE event_type='setup'")
    mrr = active_clients * 89.0

    # Pipeline by stage
    try:
        stage_rows = db.execute(
            "SELECT pipeline_stage, COUNT(*) as c FROM leads GROUP BY pipeline_stage"
        ).fetchall()
        pipeline = {r["pipeline_stage"]: r["c"] for r in stage_rows}
    except Exception:
        pipeline = {}

    # Top niches
    try:
        niche_rows = db.execute(
            "SELECT niche, COUNT(*) as c FROM leads GROUP BY niche ORDER BY c DESC LIMIT 5"
        ).fetchall()
        top_niches = [{"niche": r["niche"], "count": r["c"]} for r in niche_rows]
    except Exception:
        top_niches = []

    # Email performance
    try:
        email_row = db.execute(
            "SELECT COUNT(*) as sends, SUM(opened) as opens FROM outreach"
        ).fetchone()
        sends = email_row["sends"] or 0
        opens = email_row["opens"] or 0
        open_rate = opens / sends if sends else 0
    except Exception:
        sends, open_rate = 0, 0

    # Page view analytics
    try:
        pv_total = safe_count("SELECT SUM(hits) FROM page_views")
        pv_today = safe_count("SELECT SUM(hits) FROM page_views WHERE date=date('now')")
        pv_rows = db.execute("""
            SELECT referrer, SUM(hits) as c FROM page_views
            WHERE referrer IS NOT NULL AND referrer != ''
            GROUP BY referrer ORDER BY c DESC LIMIT 5
        """).fetchall()
        top_referrers = [{"referrer": r["referrer"], "hits": r["c"]} for r in pv_rows]
    except Exception:
        pv_total, pv_today, top_referrers = 0, 0, []

    # Webchat analytics
    webchat_sessions  = safe_count("SELECT COUNT(*) FROM chat_analytics")
    webchat_leads     = safe_count("SELECT COUNT(*) FROM chat_analytics WHERE email_captured != '' AND email_captured IS NOT NULL")
    webchat_converted = safe_count("SELECT COUNT(*) FROM chat_analytics WHERE converted=1")
    webchat_conv_rate = round(webchat_converted / webchat_sessions, 3) if webchat_sessions else 0
    try:
        wc_niches = db.execute("""
            SELECT industry, COUNT(*) as c FROM chat_analytics
            WHERE industry IS NOT NULL AND industry != ''
            GROUP BY industry ORDER BY c DESC LIMIT 5
        """).fetchall()
        webchat_niches = [{"niche": r["industry"], "count": r["c"]} for r in wc_niches]
    except Exception:
        webchat_niches = []

    return {
        "total_leads": total_leads, "active_clients": active_clients,
        "mrr": mrr, "arr": mrr * 12, "total_revenue": round(total_revenue, 2),
        "setup_fees": round(setup_fees, 2), "pipeline": pipeline,
        "top_niches": top_niches, "email_sends": sends, "email_open_rate": round(open_rate, 3),
        "pv_total": pv_total, "pv_today": pv_today, "top_referrers": top_referrers,
        "webchat_sessions": webchat_sessions, "webchat_leads": webchat_leads,
        "webchat_converted": webchat_converted, "webchat_conv_rate": webchat_conv_rate,
        "webchat_niches": webchat_niches,
    }


async def analyze_patterns(metrics: dict) -> str:
    wc_niche_str = ", ".join(f"{n['niche']}({n['count']})" for n in metrics.get("webchat_niches", [])[:5]) or "none yet"
    ref_str = ", ".join(f"{r['referrer']}({r['hits']})" for r in metrics.get("top_referrers", [])[:3]) or "none"
    summary = (
        f"Active clients: {metrics['active_clients']}, MRR: ${metrics['mrr']:.0f}, "
        f"Total leads: {metrics['total_leads']}, Open rate: {metrics['email_open_rate']:.1%}, "
        f"Top outreach niches: {', '.join(n['niche'] for n in metrics['top_niches'][:3])}, "
        f"Pipeline: {metrics['pipeline']}, "
        f"Page views today: {metrics['pv_today']} (total: {metrics['pv_total']}), Top referrers: {ref_str}, "
        f"Website chat: {metrics['webchat_sessions']} sessions, {metrics['webchat_leads']} contacts captured, "
        f"{metrics['webchat_conv_rate']:.1%} booking conversion, "
        f"Visitor niches: {wc_niche_str}"
    )
    prompt = (
        f"You are a business analyst for RingCatch, a micro-SaaS selling AI chatbots to local "
        f"businesses for $450 setup + $89/month. Analyze these metrics and identify:\n"
        f"1) Top 3 growth opportunities\n2) Biggest bottleneck in the pipeline\n"
        f"3) Best performing niche (cross-reference outreach niches vs website visitor niches)\n"
        f"4) One specific action to take this week based on website chat behavior\n"
        f"5) If webchat conversion rate is low, suggest messaging or targeting changes\n\n"
        f"Metrics: {summary}\n\nBe specific and data-driven. Under 250 words."
    )
    if GROQ_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 400},
                )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"Groq failed: {e}")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": BK_MODEL, "prompt": prompt, "stream": False}
            )
        return resp.json()["response"].strip()
    except Exception as e:
        return f"Analysis unavailable: {e}"


async def generate_executive_summary(db) -> str:
    metrics = aggregate_metrics(db)
    analysis = await analyze_patterns(metrics)

    wc = metrics
    report = f"""# RingCatch Executive Summary — {date.today()}

## Business Metrics
- MRR: ${metrics['mrr']:.0f} | ARR: ${metrics['arr']:.0f}
- Active Clients: {metrics['active_clients']}
- Total Leads: {metrics['total_leads']} | Pipeline: {metrics['pipeline']}
- Email Open Rate: {metrics['email_open_rate']:.1%}
- Top Outreach Niches: {', '.join(n['niche'] for n in metrics['top_niches'][:3])}

## Website Traffic
- Page views today: {metrics['pv_today']} | Total: {metrics['pv_total']}
- Top referrers: {', '.join(f"{r['referrer']}({r['hits']})" for r in metrics['top_referrers']) or 'none yet'}

## Website Chat Analytics
- Sessions: {wc['webchat_sessions']} | Contacts captured: {wc['webchat_leads']} | Booking conversions: {wc['webchat_converted']} ({wc['webchat_conv_rate']:.1%})
- Visitor niches: {', '.join(f"{n['niche']}({n['count']})" for n in wc['webchat_niches']) or 'none yet'}

## Strategic Analysis
{analysis}
"""
    path = STRATEGY_DIR / f"executive_summary_{date.today()}.md"
    path.write_text(report)
    db.execute(
        "INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
        (AGENT, "SUMMARY", f"Executive summary generated for {date.today()}", "purple")
    )
    db.commit()
    return report


async def identify_growth_opportunities(db) -> list:
    opportunities = []
    try:
        # Niches with leads but no clients
        rows = db.execute("""
            SELECT l.niche, COUNT(*) as leads
            FROM leads l
            WHERE l.niche NOT IN (SELECT DISTINCT niche FROM clients WHERE status='active')
            GROUP BY l.niche ORDER BY leads DESC LIMIT 3
        """).fetchall()
        for r in rows:
            opportunities.append({
                "type": "untapped_niche",
                "description": f"{r['niche']} has {r['leads']} leads but no active clients",
                "priority": "high"
            })
    except Exception:
        pass
    try:
        # Cities with high lead count but no conversions
        rows = db.execute("""
            SELECT city, COUNT(*) as leads
            FROM leads WHERE pipeline_stage NOT IN ('paid','active_client')
            GROUP BY city ORDER BY leads DESC LIMIT 3
        """).fetchall()
        for r in rows:
            opportunities.append({
                "type": "city_opportunity",
                "description": f"{r['city']} has {r['leads']} unconverted leads",
                "priority": "medium"
            })
    except Exception:
        pass
    return opportunities[:3]


def save_daily_snapshot(db, metrics: dict):
    today = date.today().isoformat()
    for key, val in [
        ("mrr", metrics["mrr"]), ("active_clients", metrics["active_clients"]),
        ("total_leads", metrics["total_leads"]), ("email_open_rate", metrics["email_open_rate"]),
    ]:
        db.execute(
            "INSERT INTO intelligence_metrics (date, metric_name, metric_value) VALUES (?,?,?)",
            (today, key, val)
        )
    db.commit()


def update_heartbeat(action: str = "idle"):
    db = get_db()
    db.execute("""
        INSERT INTO agent_status (agent_name, status, last_heartbeat, last_action, actions_today)
        VALUES (?, 'online', datetime('now'), ?, 1)
        ON CONFLICT(agent_name) DO UPDATE SET
            status='online', last_heartbeat=datetime('now'),
            last_action=excluded.last_action, actions_today=actions_today+1
    """, (AGENT, action))
    db.commit()
    db.close()


async def daily_loop():
    while True:
        await asyncio.sleep(86400)
        db = get_db()
        metrics = aggregate_metrics(db)
        save_daily_snapshot(db, metrics)
        db.close()
        update_heartbeat("daily metrics snapshot")
        leads     = metrics.get("total_leads", 0)
        emailed   = metrics.get("emailed_leads", 0)
        replied   = metrics.get("replied_leads", 0)
        clients   = metrics.get("active_clients", 0)
        mrr       = metrics.get("mrr", 0)
        opens     = metrics.get("open_rate", 0)
        open_pct  = f"{opens*100:.1f}%" if opens else "0%"
        reply_pct = f"{replied/emailed*100:.1f}%" if emailed else "0%"
        flags = []
        if opens < BENCHMARKS["email_open_rate"]:
            flags.append(f"⚠️ Open rate {open_pct} below benchmark {BENCHMARKS['email_open_rate']*100:.0f}%")
        if emailed > 0 and replied / emailed < BENCHMARKS["reply_rate"]:
            flags.append(f"⚠️ Reply rate {reply_pct} below benchmark {BENCHMARKS['reply_rate']*100:.1f}%")
        flag_str = "\n".join(flags) if flags else "✅ All metrics on track"
        await send_discord(
            f"**📊 Daily Digest**\n"
            f"Leads: {leads} | Emailed: {emailed} | Replied: {replied} | Clients: {clients} | MRR: ${mrr:.0f}\n"
            f"Open rate: {open_pct} | Reply rate: {reply_pct}\n"
            f"{flag_str}"
        )


async def weekly_loop():
    while True:
        await asyncio.sleep(604800)
        db = get_db()
        summary = await generate_executive_summary(db)
        db.close()
        await send_discord(f"📈 Weekly executive summary:\n```{summary[:800]}```")
        update_heartbeat("weekly executive summary")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_tables()
    asyncio.create_task(daily_loop())
    asyncio.create_task(weekly_loop())
    logger.info("BI agent started")
    yield


app = FastAPI(title="Agency BI", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    db = get_db()
    metrics = aggregate_metrics(db)
    row = db.execute("SELECT * FROM agent_status WHERE agent_name=?", (AGENT,)).fetchone()
    db.close()
    summaries = list(STRATEGY_DIR.glob("*.md")) if STRATEGY_DIR.exists() else []
    return {
        "agent": AGENT, "mrr": metrics["mrr"], "active_clients": metrics["active_clients"],
        "total_leads": metrics["total_leads"], "summaries_generated": len(summaries),
        "last_summary": max((f.name for f in summaries), default=None),
        "actions_today": row["actions_today"] if row else 0,
    }


@app.get("/executive-summary")
async def executive_summary():
    db = get_db()
    summary = await generate_executive_summary(db)
    db.close()
    return {"summary": summary}


@app.get("/growth-opportunities")
async def growth_opps():
    db = get_db()
    opps = await identify_growth_opportunities(db)
    db.close()
    return opps


@app.get("/benchmarks")
def benchmarks():
    db = get_db()
    metrics = aggregate_metrics(db)
    db.close()
    comparison = {}
    for key, benchmark in BENCHMARKS.items():
        actual = metrics.get(key, 0)
        comparison[key] = {
            "benchmark": benchmark, "actual": actual,
            "delta": round(actual - benchmark, 4),
            "status": "above" if actual >= benchmark else "below"
        }
    return comparison


@app.get("/strategy")
def strategy():
    if not STRATEGY_DIR.exists():
        return {"files": []}
    files = sorted(STRATEGY_DIR.glob("*.md"), reverse=True)
    if files:
        return {"latest": files[0].read_text(), "files": [f.name for f in files]}
    return {"files": []}
