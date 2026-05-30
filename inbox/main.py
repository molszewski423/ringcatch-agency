import asyncio
import email
import imaplib
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, UTC
from pathlib import Path

import httpx
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB_PATH      = Path(os.environ.get("DB_PATH", "/data/agency.db"))
DISCORD_URL  = os.environ.get("DISCORD_BOT_URL", "http://agency-discord:8103/alert")
OUTREACH_URL = os.environ.get("OUTREACH_URL", "http://agency-outreach:8080")
AGENT        = "agency-inbox"

IMAP_HOST    = os.environ.get("ZOHO_IMAP_HOST", "imap.zoho.com")
IMAP_PORT    = int(os.environ.get("ZOHO_IMAP_PORT", "993"))
IMAP_USER    = os.environ.get("ZOHO_IMAP_USER", "")
IMAP_PASS    = os.environ.get("ZOHO_IMAP_PASS", "")
POLL_SECONDS = int(os.environ.get("INBOX_POLL_SECONDS", "120"))

start_time   = datetime.now(UTC)
last_checked = None
replies_found_today = 0


def get_db():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def init_tables():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS inbox_seen (
            message_id TEXT PRIMARY KEY,
            seen_at    TEXT
        )
    """)
    db.commit()
    db.close()


async def send_discord(message: str):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(DISCORD_URL, json={"content": f"📬 **Inbox**: {message}"})
    except Exception as e:
        logger.warning(f"Discord failed: {e}")


def get_email_body(msg) -> str:
    html = None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    pass
            if ct == "text/html":
                try:
                    html = part.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    pass
    else:
        ct = msg.get_content_type()
        if ct == "text/plain":
            try:
                return msg.get_payload(decode=True).decode("utf-8", errors="replace")
            except Exception:
                pass
        if ct == "text/html":
            try:
                html = msg.get_payload(decode=True).decode("utf-8", errors="replace")
            except Exception:
                pass
    
    if html:
        import re
        # Crude tag stripping for preview/logs
        text = re.sub('<[^<]+?>', '', html)
        return text.replace('&nbsp;', ' ').strip()
    return ""


async def fetch_new_replies():
    global last_checked, replies_found_today
    if not IMAP_USER or not IMAP_PASS:
        logger.warning("Zoho IMAP credentials not set — skipping inbox poll")
        return

    db = get_db()
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASS)
        mail.select("INBOX")
        _, data = mail.search(None, "UNSEEN")
        msg_ids = data[0].split()
        logger.info(f"Inbox poll: {len(msg_ids)} unseen messages")
        for num in msg_ids:
            _, msg_data = mail.fetch(num, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            message_id = msg.get("Message-ID", "").strip()
            if not message_id:
                continue
            already_seen = db.execute(
                "SELECT 1 FROM inbox_seen WHERE message_id=?", (message_id,)
            ).fetchone()
            if already_seen:
                continue
            db.execute(
                "INSERT OR IGNORE INTO inbox_seen (message_id, seen_at) VALUES (?,?)",
                (message_id, datetime.now(UTC).isoformat())
            )
            db.commit()
            from_addr = email.utils.parseaddr(msg.get("From", ""))[1].lower()
            subject   = msg.get("Subject", "")
            body      = get_email_body(msg)[:1000]
            logger.info(f"New reply from {from_addr}: {subject[:60]}")
            lead_row = db.execute(
                "SELECT * FROM leads WHERE LOWER(email)=?", (from_addr,)
            ).fetchone()
            if lead_row:
                lead = dict(lead_row)
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.post(
                        f"{OUTREACH_URL}/mark-replied",
                        json={"lead_id": lead["id"], "reply_text": body}
                    )
                replies_found_today += 1
                await send_discord(
                    f"📩 Reply from **{lead.get('business_name', from_addr)}** ({lead.get('city', '')})\n"
                    f"Subject: _{subject}_\n"
                    f"Preview: _{body[:200]}_"
                )
            else:
                await send_discord(
                    f"📩 Email from unknown address: {from_addr}\n"
                    f"Subject: _{subject}_\n"
                    f"Preview: _{body[:200]}_"
                )
        mail.logout()
    except Exception as e:
        logger.error(f"IMAP error: {e}")
    finally:
        db.close()
    last_checked = datetime.now(UTC).isoformat()


def update_heartbeat():
    db = get_db()
    db.execute("""
        INSERT INTO agent_status (agent_name, status, last_heartbeat, last_action, actions_today)
        VALUES (?, 'running', ?, 'inbox_poll', ?)
        ON CONFLICT(agent_name) DO UPDATE SET
            status='running', last_heartbeat=excluded.last_heartbeat,
            last_action=excluded.last_action, actions_today=excluded.actions_today
    """, (AGENT, datetime.now(UTC).isoformat(), replies_found_today))
    db.commit()
    db.close()


async def poll_loop():
    while True:
        await fetch_new_replies()
        update_heartbeat()
        await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_tables()
    if IMAP_USER and IMAP_PASS:
        asyncio.create_task(poll_loop())
        logger.info(f"Inbox agent started — polling {IMAP_USER} every {POLL_SECONDS}s")
    else:
        logger.warning("Inbox agent started in standby — set ZOHO_IMAP_USER and ZOHO_IMAP_PASS to activate")
    yield


app = FastAPI(title="Agency Inbox", lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "imap_configured": bool(IMAP_USER and IMAP_PASS),
        "last_checked": last_checked,
        "replies_found_today": replies_found_today,
    }


@app.get("/status")
def status():
    db = get_db()
    row = db.execute("SELECT COUNT(*) as c FROM inbox_seen").fetchone()
    db.close()
    return {
        "status": "ok",
        "imap_user": IMAP_USER or "NOT SET",
        "imap_configured": bool(IMAP_USER and IMAP_PASS),
        "last_checked": last_checked,
        "replies_found_today": replies_found_today,
        "total_messages_seen": row["c"] if row else 0,
    }


@app.post("/check-now")
async def check_now():
    await fetch_new_replies()
    return {"status": "ok", "last_checked": last_checked}
