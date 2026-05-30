import asyncio
import json
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
COMPANY_NAME = os.environ.get("COMPANY_NAME", "RingCatch")
LEGAL_DIR    = Path("/data/legal")
AGENT        = "agency-legal"

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
        contract_pdf TEXT, monthly_rate REAL DEFAULT 89.0, setup_fee REAL DEFAULT 450.0
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
    LEGAL_DIR.mkdir(parents=True, exist_ok=True)


async def send_discord(message: str):
    if not DISCORD_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(DISCORD_URL, json={"content": f"⚖️ **Legal**: {message}"})
    except Exception as e:
        logger.warning(f"Discord failed: {e}")


def generate_agreement_pdf(client: dict) -> str:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas as rl_canvas
    except ImportError:
        logger.error("reportlab not installed")
        return ""

    client_id = client.get("id", "new")
    today = date.today().isoformat()
    filename = f"agreement_{client_id}_{today}.pdf"
    path = LEGAL_DIR / filename

    c = rl_canvas.Canvas(str(path), pagesize=letter)
    w, h = letter

    # Header
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(w / 2, h - 72, f"{COMPANY_NAME} — Service Agreement")
    c.setFont("Helvetica", 10)
    c.drawCentredString(w / 2, h - 90, f"ringcatch.io  |  alex@ringcatch.io")

    c.line(72, h - 100, w - 72, h - 100)

    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, h - 130, "Client Information")
    c.setFont("Helvetica", 11)
    y = h - 152
    for label, val in [
        ("Business Name", client.get("business_name", "")),
        ("Contact Email", client.get("email", "")),
        ("Industry", client.get("niche", "")),
        ("City", client.get("city", "")),
        ("Agreement Date", today),
    ]:
        c.drawString(72, y, f"{label}: {val}")
        y -= 18

    y -= 10
    c.line(72, y, w - 72, y)
    y -= 20

    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, y, "Services")
    y -= 18
    c.setFont("Helvetica", 11)
    services = [
        "• Custom AI chatbot design and development tailored to your business",
        "• Chatbot installation on your website (48-hour turnaround)",
        "• Conversation flow configuration and testing",
        "• Monthly maintenance, updates, and performance monitoring",
        "• Lead capture and conversation logging",
        "• Email support during business hours",
    ]
    for s in services:
        c.drawString(72, y, s)
        y -= 16

    y -= 10
    c.line(72, y, w - 72, y)
    y -= 20

    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, y, "Payment Terms")
    y -= 18
    c.setFont("Helvetica", 11)
    for line in [
        f"• One-Time Setup Fee: $450.00 (due at signing)",
        f"• Monthly Maintenance: $89.00/month (billed monthly)",
        "• No contracts — cancel anytime with 30 days notice",
        "• Payments processed securely via Stripe",
    ]:
        c.drawString(72, y, line)
        y -= 16

    y -= 10
    c.line(72, y, w - 72, y)
    y -= 20

    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, y, "Terms & Conditions")
    y -= 18
    c.setFont("Helvetica", 10)
    terms = [
        "Data Ownership: Client retains full ownership of all conversation data and leads.",
        "Limitation of Liability: RingCatch liability is limited to fees paid in the prior 30 days.",
        "Cancellation: Either party may terminate with 30 days written notice.",
        "Confidentiality: Both parties agree to keep proprietary information confidential.",
        "Governing Law: This agreement is governed by the laws of the State of North Carolina.",
    ]
    for t in terms:
        c.drawString(72, y, f"• {t}")
        y -= 14

    y -= 20
    c.line(72, y, w - 72, y)
    y -= 30
    c.setFont("Helvetica-Bold", 11)
    c.drawString(72, y, "Signature")
    y -= 24
    c.setFont("Helvetica", 11)
    c.drawString(72, y, "Client Signature: _________________________   Date: ___________")
    y -= 24
    c.drawString(72, y, f"{COMPANY_NAME}: _________________________   Date: {today}")

    c.save()
    logger.info(f"Agreement generated: {path}")
    return str(path)


def compliance_check(email_body: str) -> dict:
    issues = []
    lower = email_body.lower()
    if "unsubscribe" not in lower and "opt out" not in lower and "opt-out" not in lower:
        issues.append("Missing unsubscribe mechanism (CAN-SPAM §6)")
    if "ringcatch" not in lower and "alex" not in lower:
        issues.append("Sender identification unclear")
    # Physical address check — simple heuristic
    import re
    has_address = bool(re.search(r'\d{2,5}\s+\w+\s+(st|ave|blvd|rd|dr|ln|way)', lower))
    if not has_address:
        issues.append("Physical mailing address not detected (CAN-SPAM §6)")
    return {"compliant": len(issues) == 0, "issues": issues}


def check_llc_milestone(db) -> bool:
    row = db.execute("SELECT COUNT(*) as c FROM financial_ledger WHERE event_type='setup'").fetchone()
    return row["c"] == 3


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


async def process_event_bus():
    db = get_db()
    events = db.execute(
        "SELECT * FROM event_bus WHERE event_type='NEW_CLIENT' AND status='pending' AND (target_agent=? OR target_agent='broadcast')",
        (AGENT,)
    ).fetchall()
    for ev in events:
        try:
            payload = json.loads(ev["payload"] or "{}")
            # Generate agreement for the client
            client_email = payload.get("customer_email", "")
            client_row = db.execute(
                "SELECT * FROM clients WHERE email=?", (client_email,)
            ).fetchone()
            client = dict(client_row) if client_row else {"email": client_email, "business_name": "New Client"}
            path = generate_agreement_pdf(client)
            if path and client_row:
                db.execute("UPDATE clients SET contract_pdf=? WHERE email=?", (path, client_email))
            db.execute(
                "UPDATE event_bus SET status='consumed', consumed_by=? WHERE id=?",
                (AGENT, ev["id"])
            )
            if check_llc_milestone(db):
                await send_discord("🏢 3rd payment cleared! Time to consider LLC formation.")
        except Exception as e:
            logger.error(f"Event processing failed: {e}")
    db.commit()
    db.close()
    update_heartbeat("processed event bus")


async def event_loop():
    while True:
        await asyncio.sleep(30)
        try:
            await process_event_bus()
        except Exception as e:
            logger.error(f"Event loop error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_tables()
    task = asyncio.create_task(event_loop())
    logger.info("Legal agent started")
    yield
    task.cancel()


app = FastAPI(title="Agency Legal", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    agreements = list(LEGAL_DIR.glob("*.pdf")) if LEGAL_DIR.exists() else []
    row_db = get_db()
    row = row_db.execute("SELECT * FROM agent_status WHERE agent_name=?", (AGENT,)).fetchone()
    row_db.close()
    return {
        "agent": AGENT,
        "agreements_generated": len(agreements),
        "uptime": str(datetime.now(UTC) - start_time).split(".")[0],
        "actions_today": row["actions_today"] if row else 0,
    }


@app.post("/generate-agreement")
async def generate_agreement(payload: dict):
    path = generate_agreement_pdf(payload)
    if path:
        db = get_db()
        db.execute(
            "UPDATE clients SET contract_pdf=? WHERE email=?",
            (path, payload.get("email", ""))
        )
        db.commit()
        db.close()
        await send_discord(f"📄 Agreement generated for {payload.get('business_name')}")
        return {"pdf_path": path, "status": "generated"}
    return {"status": "error", "detail": "PDF generation failed (reportlab missing?)"}


@app.post("/compliance-check")
def run_compliance_check(payload: dict):
    return compliance_check(payload.get("email_body", ""))


@app.post("/dispute-response")
def dispute_response(payload: dict):
    pid = payload.get("payment_id", "unknown")
    reason = payload.get("dispute_reason", "")
    template = f"""DISPUTE RESPONSE — {COMPANY_NAME}
Payment ID: {pid}
Dispute Reason: {reason}

Evidence of Service Delivery:
1. Service Agreement signed on [DATE] — attached
2. Chatbot deployed and live at client website — URL: [CLIENT_URL]
3. Conversation logs showing active chatbot usage — [LOG_COUNT] conversations
4. Client onboarding email sent on [ONBOARD_DATE]
5. Monthly maintenance performed as per agreement

We respectfully request this dispute be resolved in our favor based on documented service delivery.
Contact: alex@ringcatch.io | ringcatch.io
"""
    return {"response_document": template}


@app.get("/agreements")
def list_agreements():
    if not LEGAL_DIR.exists():
        return []
    return [{"filename": f.name, "size": f.stat().st_size, "date": f.stat().st_mtime}
            for f in sorted(LEGAL_DIR.glob("*.pdf"), reverse=True)]
