import logging
import os
import sqlite3
from pathlib import Path

import httpx
import stripe
from fastapi import FastAPI, HTTPException, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Agency Billing")

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
WEBHOOK_SECRET  = os.environ["STRIPE_WEBHOOK_SECRET"]
DELIVERY_URL    = os.environ.get("DELIVERY_URL", "http://localhost:8081/generate-delivery")

STRIPE_SETUP_LINK   = os.environ.get("STRIPE_SETUP_LINK", "")
STRIPE_MONTHLY_LINK = os.environ.get("STRIPE_MONTHLY_LINK", "")
STRIPE_MODE         = os.environ.get("STRIPE_MODE", "sandbox")

BREVO_API_KEY       = os.environ.get("BREVO_API_KEY", "")
BREVO_SENDER_NAME   = os.environ.get("BREVO_SENDER_NAME", "Alex from RingCatch")
BREVO_SENDER_EMAIL  = os.environ.get("BREVO_SENDER_EMAIL", "alex@ringcatch.io")
MIKE_ALERT_EMAIL    = os.environ.get("MIKE_ALERT_EMAIL", "molszewski423@gmail.com")
INTAKE_BASE_URL     = os.environ.get("INTAKE_BASE_URL", "https://ringcatch.io")

DATA_DIR = Path("/data")
DB_PATH  = DATA_DIR / "agency.db"


def get_db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


@app.get("/payment-link")
def payment_link(type: str = "setup", client_id: str = ""):
    """Return the Stripe payment link, optionally with a tracking client_reference_id."""
    base = STRIPE_SETUP_LINK if type != "monthly" else STRIPE_MONTHLY_LINK
    if not base:
        raise HTTPException(500, f"STRIPE_{type.upper()}_LINK not configured in .env")
    link = base
    if client_id:
        sep = "&" if "?" in base else "?"
        link = f"{base}{sep}client_reference_id={client_id}"
    return {"link": link, "type": type, "mode": STRIPE_MODE}


async def _send_welcome_email(client_name: str, client_email: str, client_id: str, niche: str) -> None:
    if not client_email or not BREVO_API_KEY:
        logger.warning(f"Cannot send welcome email: no email or API key")
        return

    intake_link = f"{INTAKE_BASE_URL}/intake?id={client_id}"
    first = client_name.split()[0] if client_name else "there"

    body = (
        f"Hi {first},\n\n"
        f"Welcome to RingCatch — your AI chatbot setup is confirmed and we're getting started now.\n\n"
        f"To build your chatbot correctly, we need a few details about your business. "
        f"Please fill out this quick form (takes about 3 minutes):\n\n"
        f"{intake_link}\n\n"
        f"We'll cover:\n"
        f"  • Your business hours and service area\n"
        f"  • Services you offer (so the bot answers accurately)\n"
        f"  • Common customer questions you want the bot to handle\n"
        f"  • Where you want leads sent\n\n"
        f"Your chatbot will be live within 48 hours of receiving your info.\n\n"
        f"Questions? Reply to this email or text Mike directly.\n\n"
        f"Alex\n"
        f"RingCatch · ringcatch.io"
    )

    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to": [{"email": client_email, "name": client_name}],
        "subject": f"Welcome to RingCatch — complete your setup",
        "textContent": body,
    }
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            resp = await c.post(
                "https://api.brevo.com/v3/smtp/email",
                json=payload,
                headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            )
            if resp.status_code == 201:
                logger.info(f"Welcome email sent to {client_email}")
            else:
                logger.error(f"Welcome email failed {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            logger.error(f"Welcome email error: {exc}")

    # Also alert Mike
    alert = {
        "sender": {"name": "RingCatch System", "email": BREVO_SENDER_EMAIL},
        "to": [{"email": MIKE_ALERT_EMAIL, "name": "Mike"}],
        "subject": f"[RingCatch] New client payment: {client_name}",
        "textContent": (
            f"New client paid!\n\n"
            f"Name: {client_name}\n"
            f"Email: {client_email}\n"
            f"Niche: {niche}\n"
            f"Client ID: {client_id}\n\n"
            f"Welcome email sent. Intake form: {intake_link}"
        ),
    }
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            await c.post(
                "https://api.brevo.com/v3/smtp/email",
                json=alert,
                headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            )
        except Exception:
            pass


async def _record_payment_and_deliver(
    client_id: str, client_name: str, niche: str, amount_cents: int,
    client_email: str = ""
) -> None:
    import json as _json
    db = get_db()
    db.execute("""
        INSERT OR IGNORE INTO payments (client_id, client_name, amount, status, created_at)
        VALUES (?, ?, ?, 'paid', datetime('now'))
    """, (client_id, client_name, amount_cents))
    # Move matching lead to active_client stage
    if client_email:
        db.execute(
            "UPDATE leads SET pipeline_stage='active_client' WHERE email=?",
            (client_email,)
        )
    # Insert client record so success agent can start onboarding
    db.execute("""
        INSERT OR IGNORE INTO clients (business_name, email, niche, setup_date, status, monthly_rate)
        VALUES (?, ?, ?, date('now'), 'active', 89.0)
    """, (client_name, client_email, niche))
    # Write NEW_CLIENT event so success agent triggers onboarding sequence
    db.execute("""
        INSERT INTO event_bus (source_agent, target_agent, event_type, priority, payload)
        VALUES ('agency-billing', 'broadcast', 'NEW_CLIENT', 1, ?)
    """, (_json.dumps({
        "client_id":      client_id,
        "client_name":    client_name,
        "customer_email": client_email,
        "business_name":  client_name,
        "niche":          niche,
        "amount_cents":   amount_cents,
    }),))
    db.commit()
    db.close()
    logger.info(f"Payment confirmed [{STRIPE_MODE}]: {client_name} ${amount_cents / 100:.2f}")

    await _send_welcome_email(client_name, client_email, client_id, niche)

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            await client.post(DELIVERY_URL, json={
                "uid":            client_id,
                "organizer_name": client_name,
                "niche":          niche,
                "client_email":   client_email,
            })
        except Exception as exc:
            logger.error(f"Failed to trigger delivery: {exc}")


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except ValueError:
        raise HTTPException(400, "Invalid payload")
    except stripe.SignatureVerificationError:
        raise HTTPException(400, "Invalid signature")

    # Payment Links fire checkout.session.completed; direct charges use payment_intent.succeeded
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        if session.get("payment_status") != "paid":
            return {"status": "ok"}
        client_id    = session.get("client_reference_id") or session["id"]
        customer     = session.get("customer_details") or {}
        client_name  = customer.get("name", "Unknown")
        client_email = customer.get("email", "")
        meta         = session.get("metadata") or {}
        niche        = meta.get("niche", "small business")
        amount_cents = session.get("amount_total", 0)
        await _record_payment_and_deliver(client_id, client_name, niche, amount_cents, client_email)

    elif event["type"] == "payment_intent.succeeded":
        pi           = event["data"]["object"]
        meta         = pi.get("metadata", {})
        client_id    = meta.get("client_id", pi["id"])
        client_name  = meta.get("client_name", "Unknown")
        client_email = meta.get("client_email", "")
        niche        = meta.get("niche", "small business")
        amount_cents = pi["amount_received"]
        await _record_payment_and_deliver(client_id, client_name, niche, amount_cents, client_email)

    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok", "mode": STRIPE_MODE}
