import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, UTC, date
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB_PATH             = Path(os.environ.get("DB_PATH", "/data/agency.db"))
DISCORD_URL         = os.environ.get("DISCORD_WEBHOOK_URL", "")
STRIPE_SECRET       = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SEC  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
SETUP_FEE           = 450.0
MONTHLY_FEE         = 89.0
TAX_RATE            = 0.28
MRR_MILESTONES      = [500, 1000, 2500, 5000]
AGENT               = "agency-cfo"
FINANCE_DIR         = Path("/data/finance")

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
        monthly_rate REAL DEFAULT 89.0, setup_fee REAL DEFAULT 450.0
    );
    CREATE TABLE IF NOT EXISTS financial_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT DEFAULT (datetime('now')),
        event_type TEXT, amount REAL, tax_reserve REAL, net_amount REAL,
        stripe_payment_id TEXT, client_id INTEGER, description TEXT
    );
    CREATE TABLE IF NOT EXISTS tax_reserve (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT DEFAULT (datetime('now')),
        amount REAL, source_payment_id TEXT, quarter TEXT
    );
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, category TEXT,
        description TEXT, amount REAL, recurring INTEGER DEFAULT 0
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
    FINANCE_DIR.mkdir(parents=True, exist_ok=True)


def get_quarter(dt: date) -> str:
    q = (dt.month - 1) // 3 + 1
    return f"Q{q}-{dt.year}"


def record_payment(db, stripe_payment_id: str, amount: float,
                   client_id: int | None, description: str, event_type: str = "payment"):
    tax = round(amount * TAX_RATE, 2)
    net = round(amount - tax, 2)
    db.execute(
        "INSERT INTO financial_ledger (event_type,amount,tax_reserve,net_amount,stripe_payment_id,client_id,description) VALUES (?,?,?,?,?,?,?)",
        (event_type, amount, tax, net, stripe_payment_id, client_id, description)
    )
    db.execute(
        "INSERT INTO tax_reserve (amount, source_payment_id, quarter) VALUES (?,?,?)",
        (tax, stripe_payment_id, get_quarter(date.today()))
    )
    db.execute(
        "INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
        (AGENT, "PAYMENT", f"${amount:.2f} recorded — ${tax:.2f} tax reserve", "green")
    )
    db.commit()


def calculate_mrr(db) -> float:
    row = db.execute("SELECT COUNT(*) as c FROM clients WHERE status='active'").fetchone()
    return row["c"] * MONTHLY_FEE


def calculate_arr(mrr: float) -> float:
    return mrr * 12


def calculate_churn_rate(db) -> float:
    month_start = date.today().replace(day=1).isoformat()
    lost = db.execute(
        "SELECT COUNT(*) as c FROM clients WHERE status='churned' AND last_activity >= ?",
        (month_start,)
    ).fetchone()["c"]
    total_start = db.execute(
        "SELECT COUNT(*) as c FROM clients WHERE setup_date < ?", (month_start,)
    ).fetchone()["c"]
    return round((lost / total_start * 100) if total_start else 0.0, 2)


def calculate_ltv(db) -> float:
    row = db.execute(
        "SELECT AVG(julianday('now') - julianday(setup_date)) / 30.0 as avg_months FROM clients WHERE status='active'"
    ).fetchone()
    avg_months = row["avg_months"] or 12.0
    return round(avg_months * MONTHLY_FEE, 2)


def get_metrics(db) -> dict:
    mrr = calculate_mrr(db)
    tax_row = db.execute("SELECT COALESCE(SUM(amount),0) as t FROM tax_reserve").fetchone()
    mtd_start = date.today().replace(day=1).isoformat()
    setup_row = db.execute(
        "SELECT COALESCE(SUM(amount),0) as s FROM financial_ledger WHERE event_type='setup' AND timestamp >= ?",
        (mtd_start,)
    ).fetchone()
    net_row = db.execute(
        "SELECT COALESCE(SUM(net_amount),0) as n FROM financial_ledger WHERE timestamp >= ?",
        (mtd_start,)
    ).fetchone()
    clients = db.execute("SELECT COUNT(*) as c FROM clients WHERE status='active'").fetchone()["c"]
    return {
        "mrr": mrr, "arr": calculate_arr(mrr), "client_count": clients,
        "churn_rate": calculate_churn_rate(db), "ltv": calculate_ltv(db),
        "total_tax_reserve": round(tax_row["t"], 2),
        "setup_fees_mtd": round(setup_row["s"], 2),
        "net_revenue_mtd": round(net_row["n"], 2),
    }


async def send_discord(message: str):
    if not DISCORD_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(DISCORD_URL, json={"content": f"💰 **CFO**: {message}"})
    except Exception as e:
        logger.warning(f"Discord failed: {e}")


async def check_milestones(db, new_mrr: float):
    for milestone in MRR_MILESTONES:
        prev_mrr = new_mrr - MONTHLY_FEE
        if prev_mrr < milestone <= new_mrr:
            await send_discord(f"🎉 MRR milestone hit: ${milestone}/mo! Current MRR: ${new_mrr:.0f}")
            db.execute(
                "INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
                (AGENT, "MILESTONE", f"MRR milestone: ${milestone}", "gold")
            )
            db.commit()


async def check_quarterly_reminders():
    today = date.today()
    tax_dates = [(4, 15), (6, 15), (9, 15), (1, 15)]
    for month, day in tax_dates:
        try:
            target = date(today.year, month, day)
        except ValueError:
            continue
        days_until = (target - today).days
        if 0 <= days_until <= 7:
            await send_discord(
                f"📋 Quarterly estimated tax due {target.strftime('%B %d')} — "
                f"{days_until} days away. Check tax reserve balance."
            )


async def generate_pnl_pdf(month_year: str) -> str:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas as rl_canvas
    except ImportError:
        return ""
    db = get_db()
    month_start = f"{month_year}-01"
    income = db.execute(
        "SELECT COALESCE(SUM(amount),0) as t FROM financial_ledger WHERE timestamp >= ?",
        (month_start,)
    ).fetchone()["t"]
    tax = db.execute(
        "SELECT COALESCE(SUM(amount),0) as t FROM tax_reserve WHERE timestamp >= ?",
        (month_start,)
    ).fetchone()["t"]
    expenses_total = db.execute(
        "SELECT COALESCE(SUM(amount),0) as t FROM expenses WHERE date >= ?",
        (month_start,)
    ).fetchone()["t"]
    mrr = calculate_mrr(db)
    db.close()

    path = FINANCE_DIR / f"pnl_{month_year}.pdf"
    c = rl_canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(72, 750, f"RingCatch P&L — {month_year}")
    c.setFont("Helvetica", 12)
    y = 710
    for label, val in [
        ("Gross Revenue", f"${income:.2f}"),
        ("Tax Reserve (28%)", f"-${tax:.2f}"),
        ("Business Expenses", f"-${expenses_total:.2f}"),
        ("Net Income", f"${income - tax - expenses_total:.2f}"),
        ("Current MRR", f"${mrr:.2f}"),
        ("ARR Run Rate", f"${mrr * 12:.2f}"),
    ]:
        c.drawString(72, y, f"{label}:")
        c.drawString(300, y, val)
        y -= 24
    c.save()
    logger.info(f"P&L PDF generated: {path}")
    return str(path)


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
        metrics = get_metrics(db)
        await check_milestones(db, metrics["mrr"])
        db.close()
        await check_quarterly_reminders()
        update_heartbeat("daily metrics check")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_tables()
    task = asyncio.create_task(daily_loop())
    logger.info("CFO agent started")
    yield
    task.cancel()


app = FastAPI(title="Agency CFO", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    db = get_db()
    m = get_metrics(db)
    row = db.execute("SELECT * FROM agent_status WHERE agent_name=?", (AGENT,)).fetchone()
    db.close()
    return {"agent": AGENT, "uptime": str(datetime.now(UTC) - start_time).split(".")[0],
            **m, "actions_today": row["actions_today"] if row else 0}


@app.get("/metrics")
def metrics():
    db = get_db()
    m = get_metrics(db)
    # Last 12 months breakdown
    months = db.execute("""
        SELECT strftime('%Y-%m', timestamp) as month,
               SUM(amount) as gross, SUM(net_amount) as net
        FROM financial_ledger
        GROUP BY month ORDER BY month DESC LIMIT 12
    """).fetchall()
    db.close()
    return {**m, "monthly_breakdown": [dict(r) for r in months]}


@app.get("/pnl-report")
async def pnl_report(month: str = None):
    if not month:
        month = date.today().strftime("%Y-%m")
    path = await generate_pnl_pdf(month)
    if path and Path(path).exists():
        return FileResponse(path, media_type="application/pdf", filename=f"pnl_{month}.pdf")
    return {"error": "reportlab not installed or generation failed"}


@app.get("/tax-summary")
def tax_summary():
    db = get_db()
    rows = db.execute(
        "SELECT quarter, SUM(amount) as total FROM tax_reserve GROUP BY quarter ORDER BY quarter DESC"
    ).fetchall()
    total = db.execute("SELECT COALESCE(SUM(amount),0) as t FROM tax_reserve").fetchone()["t"]
    db.close()
    return {"total_reserved": round(total, 2), "by_quarter": [dict(r) for r in rows]}


@app.get("/projections")
def projections():
    db = get_db()
    mrr = calculate_mrr(db)
    pipeline = db.execute(
        "SELECT COUNT(*) as c FROM leads WHERE pipeline_stage IN ('booked','paid')"
    ).fetchone()["c"]
    db.close()
    projected = mrr + (pipeline * MONTHLY_FEE * 0.5)
    return {
        "current_mrr": mrr,
        "next_month_projected": round(projected, 2),
        "month_2": round(projected * 1.05, 2),
        "month_3": round(projected * 1.10, 2),
    }


@app.get("/expenses")
def get_expenses():
    db = get_db()
    rows = db.execute("SELECT * FROM expenses ORDER BY date DESC LIMIT 50").fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if STRIPE_WEBHOOK_SEC and sig:
        try:
            parts = {k: v for p in sig.split(",") for k, v in [p.split("=", 1)]}
            ts = parts.get("t", "")
            signed_payload = f"{ts}.{body.decode()}"
            expected = hmac.new(
                STRIPE_WEBHOOK_SEC.encode(), signed_payload.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, parts.get("v1", "")):
                raise HTTPException(400, "Invalid signature")
        except HTTPException:
            raise
        except Exception:
            pass  # Skip sig check if malformed

    event = json.loads(body)
    db = get_db()
    if event["type"] == "checkout.session.completed":
        sess = event["data"]["object"]
        amount = (sess.get("amount_total") or 0) / 100.0
        payment_id = sess.get("payment_intent", sess.get("id"))
        is_setup = amount >= SETUP_FEE
        record_payment(db, payment_id, amount, None,
                       "Setup fee" if is_setup else "Monthly subscription",
                       "setup" if is_setup else "subscription")
        mrr = calculate_mrr(db)
        await check_milestones(db, mrr)
        # Publish NEW_CLIENT event for other agents
        db.execute(
            "INSERT INTO event_bus (source_agent,target_agent,event_type,payload) VALUES (?,?,?,?)",
            (AGENT, "broadcast", "NEW_CLIENT", json.dumps({
                "stripe_session_id": sess.get("id"),
                "amount": amount, "customer_email": sess.get("customer_details", {}).get("email"),
            }))
        )
        db.commit()

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        cid = sub.get("customer")
        db.execute(
            "UPDATE clients SET status='churned' WHERE stripe_customer_id=?", (cid,)
        )
        db.commit()
        await send_discord(f"⚠️ Subscription cancelled for customer {cid}")

    db.close()
    return {"status": "ok"}
