import asyncio
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, UTC, date, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB_PATH      = Path(os.environ.get("DB_PATH", "/data/agency.db"))
DISCORD_URL  = os.environ.get("DISCORD_BOT_URL", "http://agency-discord:8103/alert")
OLLAMA_URL   = os.environ.get("OLLAMA_BASE_URL", "http://host.containers.internal:11434")
BK_MODEL     = os.environ.get("FAST_MODEL", os.environ.get("BACKEND_MODEL", "gemma4:e4b"))
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
BREVO_KEY    = os.environ.get("BREVO_API_KEY", "")
FROM_EMAIL   = os.environ.get("BREVO_SENDER_EMAIL", "alex@ringcatch.io")
FROM_NAME    = os.environ.get("BREVO_SENDER_NAME", "Alex from RingCatch")
AGENT        = "agency-success"


async def _llm(prompt: str, max_tokens: int = 500) -> str:
    if GROQ_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens},
                )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"Groq failed: {e}")
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(f"{OLLAMA_URL}/api/generate", json={"model": BK_MODEL, "prompt": prompt, "stream": False})
    return r.json()["response"].strip()

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
    CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT, business_name TEXT, email TEXT,
        niche TEXT, city TEXT, setup_date TEXT, stripe_customer_id TEXT,
        stripe_subscription_id TEXT, status TEXT DEFAULT 'active',
        chatbot_conversations INTEGER DEFAULT 0, churn_risk TEXT DEFAULT 'low',
        last_activity TEXT, contract_pdf TEXT,
        monthly_rate REAL DEFAULT 89.0, setup_fee REAL DEFAULT 450.0,
        testimonial_sent INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS event_bus (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT DEFAULT (datetime('now')),
        source_agent TEXT, target_agent TEXT, event_type TEXT, priority INTEGER DEFAULT 1,
        payload TEXT, status TEXT DEFAULT 'pending', consumed_by TEXT
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


async def send_discord(message: str):
    if not DISCORD_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(DISCORD_URL, json={"content": f"💚 **Success**: {message}"})
    except Exception as e:
        logger.warning(f"Discord failed: {e}")


async def send_brevo(to_email: str, to_name: str, subject: str, body: str) -> bool:
    if not BREVO_KEY:
        logger.info(f"[STUB] Email to {to_email}: {subject}")
        return True
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.brevo.com/v3/smtp/email",
            json={"sender": {"name": FROM_NAME, "email": FROM_EMAIL},
                  "to": [{"email": to_email, "name": to_name}],
                  "subject": subject, "textContent": body},
            headers={"api-key": BREVO_KEY}
        )
    return resp.status_code == 201


def calculate_churn_risk(client: dict) -> str:
    convos = client.get("chatbot_conversations", 0) or 0
    last_activity = client.get("last_activity") or client.get("setup_date") or ""
    days_inactive = 999
    if last_activity:
        try:
            last = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
            days_inactive = (datetime.now(UTC) - last.replace(tzinfo=last.tzinfo or UTC)).days
        except Exception:
            pass
    if days_inactive > 30 or convos == 0:
        return "high"
    if days_inactive > 14 or convos < 5:
        return "medium"
    return "low"


async def update_churn_risks(db):
    clients = db.execute("SELECT * FROM clients WHERE status='active'").fetchall()
    for c in clients:
        risk = calculate_churn_risk(dict(c))
        db.execute("UPDATE clients SET churn_risk=? WHERE id=?", (risk, c["id"]))
    db.commit()


async def generate_client_report(client: dict) -> str:
    convos = client.get("chatbot_conversations", 0) or 0
    niche = client.get("niche", "your industry")
    biz = client.get("business_name", "your business")
    month = date.today().strftime("%B %Y")

    prompt = f"""Write a friendly 100-150 word monthly performance report email from Alex at RingCatch to {biz}.
Stats: {convos} chatbot conversations this month in {niche}.
Tone: warm, professional, helpful neighbor.
Include: what the chatbot handled, any suggested improvements, appreciation for being a client.
Sign as Alex from RingCatch. No subject line, email body only."""

    try:
        return await _llm(prompt)
    except Exception:
        return (f"Hi {biz},\n\nYour AI chatbot had {convos} conversations in {month}. "
                f"It's been working hard capturing leads and answering questions for you 24/7. "
                f"Let us know if you'd like any adjustments!\n\nThanks,\nAlex\nRingCatch")


async def send_monthly_reports(db):
    clients = db.execute("SELECT * FROM clients WHERE status='active' AND email != ''").fetchall()
    sent_count = 0
    for c in clients:
        client = dict(c)
        body = await generate_client_report(client)
        month = date.today().strftime("%B %Y")
        subject = f"Your RingCatch chatbot — {month} performance"
        if await send_brevo(client["email"], client["business_name"], subject, body):
            sent_count += 1
            db.execute(
                "INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
                (AGENT, "MONTHLY_REPORT", f"Report sent to {client['business_name']}", "green")
            )
    db.commit()
    logger.info(f"Monthly reports sent: {sent_count}")
    await send_discord(f"📧 Monthly reports sent to {sent_count} clients")


async def identify_upsell_candidates(db) -> list:
    rows = db.execute(
        "SELECT * FROM clients WHERE chatbot_conversations > 50 AND status='active'"
    ).fetchall()
    return [dict(r) for r in rows]


async def send_testimonial_request(db, client: dict) -> bool:
    if client.get("testimonial_sent"):
        return False
    body = (f"Hi {client['business_name']},\n\n"
            f"It's been a week since your AI chatbot went live — we hope it's been helpful!\n\n"
            f"If you've had a good experience, we'd love a quick testimonial. Even a sentence or two "
            f"makes a huge difference for other small businesses deciding if RingCatch is right for them.\n\n"
            f"You can reply directly to this email or leave a Google review. Either works!\n\n"
            f"Thanks so much,\nAlex\nRingCatch | ringcatch.io")
    sent = await send_brevo(client["email"], client["business_name"],
                            "Quick favor — testimonial for RingCatch?", body)
    if sent:
        db.execute("UPDATE clients SET testimonial_sent=1 WHERE id=?", (client["id"],))
        db.commit()
    return sent


async def check_testimonials(db):
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    clients = db.execute(
        "SELECT * FROM clients WHERE setup_date <= ? AND testimonial_sent=0 AND status='active' AND email != ''",
        (cutoff,)
    ).fetchall()
    for c in clients:
        await send_testimonial_request(db, dict(c))


async def flag_churn_risks(db):
    high_risk = db.execute(
        "SELECT * FROM clients WHERE churn_risk='high' AND status='active'"
    ).fetchall()
    if high_risk:
        names = ", ".join(r["business_name"] for r in high_risk)
        await send_discord(f"⚠️ High churn risk clients: {names}")


async def poll_events(db):
    events = db.execute(
        "SELECT * FROM event_bus WHERE event_type='NEW_CLIENT' AND status='pending' AND (target_agent='agency-success' OR target_agent='broadcast')"
    ).fetchall()
    for ev in events:
        payload = json.loads(ev["payload"] or "{}")
        email = payload.get("customer_email", "")
        if email:
            db.execute(
                "INSERT OR IGNORE INTO clients (email, business_name, setup_date, status) VALUES (?,?,?,?)",
                (email, payload.get("business_name", email), date.today().isoformat(), "active")
            )
        db.execute("UPDATE event_bus SET status='consumed', consumed_by=? WHERE id=?", (AGENT, ev["id"]))
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
        await update_churn_risks(db)
        await flag_churn_risks(db)
        await check_testimonials(db)
        await poll_events(db)
        db.close()
        update_heartbeat("daily client health check")


async def event_loop():
    while True:
        await asyncio.sleep(60)
        try:
            db = get_db()
            await poll_events(db)
            db.close()
        except Exception as e:
            logger.error(f"Event loop error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_tables()
    asyncio.create_task(daily_loop())
    asyncio.create_task(event_loop())
    logger.info("Client success agent started")
    yield


app = FastAPI(title="Agency Client Success", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    db = get_db()
    total = db.execute("SELECT COUNT(*) as c FROM clients WHERE status='active'").fetchone()["c"]
    high = db.execute("SELECT COUNT(*) as c FROM clients WHERE churn_risk='high' AND status='active'").fetchone()["c"]
    row = db.execute("SELECT * FROM agent_status WHERE agent_name=?", (AGENT,)).fetchone()
    db.close()
    return {
        "agent": AGENT, "active_clients": total, "high_churn_risk": high,
        "uptime": str(datetime.now(UTC) - start_time).split(".")[0],
        "actions_today": row["actions_today"] if row else 0,
    }


@app.get("/client-report")
async def client_report(client_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    db.close()
    if not row:
        return {"error": "client not found"}
    return {"client_id": client_id, "report": await generate_client_report(dict(row))}


@app.get("/churn-risks")
def churn_risks():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM clients WHERE churn_risk != 'low' AND status='active' ORDER BY churn_risk DESC"
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.get("/upsell-candidates")
async def upsell():
    db = get_db()
    result = await identify_upsell_candidates(db)
    db.close()
    return result


@app.post("/testimonials")
async def trigger_testimonials():
    db = get_db()
    await check_testimonials(db)
    db.close()
    return {"status": "testimonial check complete"}
