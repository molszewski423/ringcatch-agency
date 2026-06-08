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
CHAT_MODEL   = os.environ.get("CHAT_MODEL", "qwen2.5:7b")
BREVO_KEY    = os.environ.get("BREVO_API_KEY", "")
FROM_EMAIL   = os.environ.get("BREVO_SENDER_EMAIL", "alex@ringcatch.io")
FROM_NAME    = os.environ.get("BREVO_SENDER_NAME", "Alex from RingCatch")
CAL_LINK     = os.environ.get("CAL_LINK", "https://cal.com/michael-olszewski-nn9caa/15-min-discovery-call")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
AGENT        = "agency-sales"

OBJECTIONS = {
    "too expensive": "I totally get it — $450 feels like a lot upfront. But think about it: if the chatbot captures just one extra job in its first month, it's already paid for itself. And at $89/month you're getting 24/7 coverage without hiring anyone.",
    "already have website chat": "That's great you have something! The difference with RingCatch is it's trained specifically on your business — your services, pricing, FAQ — so it actually qualifies leads instead of just saying 'fill out this form.' Happy to show you the difference.",
    "not ready yet": "Totally fair. Would it help if I sent you a quick overview so you have it when the timing is right? No pressure at all.",
    "need to think about it": "Of course! What's the one thing you'd want to think through most? I can probably give you a straight answer right now.",
    "how do i know it works": "Great question. Every chatbot we build gets tested with real scenarios before it goes live. And you'll see the conversation logs — every lead it captures shows up in a dashboard you control.",
    "what if i want to cancel": "No contracts, cancel anytime with 30 days notice. We don't believe in locking people in — if we're not delivering value, you shouldn't pay.",
    "do i own the chatbot": "Yes, 100%. All conversation data is yours. You own the leads it captures.",
    "what industries": "Any small business that talks to customers. HVAC, plumbing, dental, law firms, auto shops, salons, gyms — if you have a phone ringing after hours, a chatbot handles it.",
    "how long to set up": "48 hours from payment to live chatbot. We handle everything — design, installation, testing.",
    "i'll do it myself": "Totally reasonable! If you ever want to see what a fully managed version looks like, we're here. Takes about 40 hours to build one from scratch if you're starting fresh.",
}

start_time = datetime.now(UTC)
qualified_today = 0
emails_sent_today = 0


def get_db():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def init_tables():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT, business_name TEXT, email TEXT UNIQUE,
        phone TEXT, website TEXT, domain TEXT, address TEXT, city TEXT, niche TEXT,
        scraped_date TEXT, processed INTEGER DEFAULT 0,
        pipeline_stage TEXT DEFAULT 'scraped', qualified TEXT,
        qualification_reason TEXT, last_contacted TEXT, email_invalid INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS sequence_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER,
        step INTEGER, send_after TEXT, sent INTEGER DEFAULT 0
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
    CREATE TABLE IF NOT EXISTS outreach (
        id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER, email TEXT,
        email_body TEXT, sequence_step INTEGER DEFAULT 1,
        sent_at TEXT DEFAULT (datetime('now')), opened INTEGER DEFAULT 0, replied INTEGER DEFAULT 0
    );
    """)
    db.commit()
    # Migrations for existing databases
    try:
        db.execute("ALTER TABLE leads ADD COLUMN email_invalid INTEGER DEFAULT 0")
        db.commit()
    except Exception:
        pass
    db.close()


async def send_discord(message: str):
    if not DISCORD_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(DISCORD_URL, json={"content": f"🎯 **Sales**: {message}"})
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


async def _llm(prompt: str, json_mode: bool = False) -> str:
    """Call Groq first, fall back to Ollama."""
    if GROQ_API_KEY:
        try:
            kwargs = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 200}
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json=kwargs,
                )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"Groq failed: {e}, falling back to Ollama")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": CHAT_MODEL, "prompt": prompt, "stream": False, **({"format": "json"} if json_mode else {})},
        )
    return r.json()["response"].strip()


# Intent labels: hot, interested, question, not_interested, maybe_later, unsubscribe, out_of_office, bounce
UNSUBSCRIBE_INTENTS = {"unsubscribe", "bounce"}
COLD_INTENTS        = {"not_interested", "out_of_office"}
DEFER_INTENTS       = {"maybe_later"}
HOT_INTENTS         = {"hot", "interested", "question"}


async def classify_reply_intent(reply_text: str) -> str:
    """Classify an email reply into an intent bucket using LLM."""
    prompt = (
        f"Classify this email reply from a small business owner into exactly ONE label:\n"
        f"hot = excited, asking pricing, wants to talk now\n"
        f"interested = positive but vague, wants more info\n"
        f"question = has a specific question before deciding\n"
        f"not_interested = clearly says no thanks\n"
        f"maybe_later = timing issue, busy season, check back later\n"
        f"unsubscribe = asks to stop emails, remove me, unsubscribe\n"
        f"out_of_office = auto-reply, vacation, OOO\n"
        f"bounce = delivery failure, email not valid\n\n"
        f"Reply: \"{reply_text[:500]}\"\n\n"
        f"Respond with only the label word."
    )
    try:
        result = (await _llm(prompt)).lower().strip().split()[0]
        valid = {"hot","interested","question","not_interested","maybe_later","unsubscribe","out_of_office","bounce"}
        return result if result in valid else "interested"
    except Exception:
        return "interested"


async def qualify_lead(lead: dict) -> dict:
    prompt = f"""You are a sales qualification expert for RingCatch, which sells AI chatbots to local small businesses for $450 setup + $89/month.

Evaluate this lead and classify as hot/warm/cold:

Business: {lead.get('business_name', 'Unknown')}
Industry: {lead.get('niche', 'Unknown')}
City: {lead.get('city', 'Unknown')}
Email reply or context: {lead.get('email_reply', lead.get('challenge', 'Cold outreach target'))}

HOT = expressed interest, asked pricing, owner with specific problem, replied positively
WARM = engaged but vague, "maybe later", general inquiry
COLD = no engagement signals, competitor, out of budget signals

Respond in JSON only: {{"score": "hot|warm|cold", "reasoning": "one sentence", "confidence": 0.0-1.0}}"""

    try:
        result = await _llm(prompt, json_mode=True)
        return json.loads(result)
    except Exception as e:
        logger.warning(f"Qualification failed: {e}")
        return {"score": "warm", "reasoning": "Could not qualify — defaulting to warm", "confidence": 0.5}


async def generate_followup(lead: dict, touch: int = 1) -> str:
    prompts = {
        1: f"Write a 3-sentence personalized follow-up email from Alex at RingCatch to {lead.get('business_name')} ({lead.get('niche')} in {lead.get('city')}). They showed interest in our AI chatbot ($450+$89/mo). Include this booking link: {CAL_LINK}. Casual, friendly, no corporate speak. Sign as Alex.",
        2: f"Write a 2-sentence gentle follow-up from Alex at RingCatch to {lead.get('business_name')}. Reference your previous email. Keep it light. Sign as Alex.",
        3: f"Write a 2-sentence final follow-up from Alex at RingCatch to {lead.get('business_name')}. Friendly close, mention ringcatch.io. Zero pressure. Sign as Alex.",
    }
    try:
        return await _llm(prompts.get(touch, prompts[1]))
    except Exception:
        return f"Hi, just following up on RingCatch's AI chatbot offer for {lead.get('business_name')}. Happy to answer any questions. — Alex (ringcatch.io)"


async def handle_hot_lead(db, lead: dict):
    global emails_sent_today
    body = await generate_followup(lead, touch=1)
    sent = await send_brevo(lead["email"], lead["business_name"],
                            f"Quick question about {lead['business_name']}", body)
    if sent:
        emails_sent_today += 1
        db.execute("UPDATE leads SET pipeline_stage='hot', last_contacted=? WHERE id=?",
                   (datetime.now(UTC).isoformat(), lead["id"]))
        db.execute(
            "INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
            (AGENT, "HOT_LEAD", f"Hot followup sent to {lead['business_name']}", "orange")
        )
        db.commit()
        await send_discord(f"🔥 Hot lead contacted: {lead['business_name']} ({lead.get('niche')} / {lead.get('city')})")


async def schedule_warm_sequence(db, lead_id: int):
    now = datetime.now(UTC)
    for step, days in [(2, 3), (3, 7)]:
        send_after = (now + timedelta(days=days)).isoformat()
        db.execute(
            "INSERT INTO sequence_queue (lead_id, step, send_after) VALUES (?,?,?)",
            (lead_id, step, send_after)
        )
    db.commit()


async def process_warm_queue(db):
    global emails_sent_today
    now = datetime.now(UTC).isoformat()
    due = db.execute(
        "SELECT sq.*, l.* FROM sequence_queue sq JOIN leads l ON sq.lead_id=l.id WHERE sq.send_after<=? AND sq.sent=0",
        (now,)
    ).fetchall()
    for row in due:
        lead = dict(row)
        body = await generate_followup(lead, touch=lead["step"])
        sent = await send_brevo(lead["email"], lead["business_name"],
                                f"Re: AI chatbot for {lead['business_name']}", body)
        if sent:
            emails_sent_today += 1
            db.execute("UPDATE sequence_queue SET sent=1 WHERE id=?", (lead["id"],))
            db.execute("UPDATE leads SET last_contacted=? WHERE id=?",
                       (now, lead["lead_id"]))
            db.commit()


async def poll_event_bus(db):
    global qualified_today

    # Handle new leads for qualification
    new_lead_events = db.execute(
        "SELECT * FROM event_bus WHERE event_type='NEW_LEAD' AND status='pending'",
    ).fetchall()
    for ev in new_lead_events:
        payload = json.loads(ev["payload"] or "{}")
        lead_id = payload.get("lead_id")
        if not lead_id:
            # fallback: look up by email for events published before lead_id was included
            email = payload.get("email")
            if email:
                row = db.execute("SELECT id FROM leads WHERE email=?", (email,)).fetchone()
                if row:
                    lead_id = row["id"]
        if not lead_id:
            db.execute("UPDATE event_bus SET status='consumed', consumed_by=? WHERE id=?", (AGENT, ev["id"]))
            db.commit()
            continue
        lead_row = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead_row:
            db.execute("UPDATE event_bus SET status='consumed', consumed_by=? WHERE id=?", (AGENT, ev["id"]))
            db.commit()
            continue
        lead = dict(lead_row)
        result = await qualify_lead(lead)
        score = result.get("score", "warm")
        db.execute(
            "UPDATE leads SET qualified=?, qualification_reason=? WHERE id=?",
            (score, result.get("reasoning", ""), lead_id)
        )
        qualified_today += 1
        if score == "hot":
            await handle_hot_lead(db, lead)
        elif score == "warm":
            db.execute("UPDATE leads SET pipeline_stage='emailed' WHERE id=?", (lead_id,))
            # outreach agent handles step-2/3/4 follow-up sequences — no duplicate scheduling here
        db.execute(
            "UPDATE event_bus SET status='consumed', consumed_by=? WHERE id=?", (AGENT, ev["id"])
        )
        db.commit()

    # Handle reply events — classify intent before responding
    reply_events = db.execute(
        "SELECT * FROM event_bus WHERE event_type='REPLY_RECEIVED' AND status='pending'",
    ).fetchall()
    for ev in reply_events:
        payload = json.loads(ev["payload"] or "{}")
        lead_id = payload.get("lead_id")
        reply_text = payload.get("email_reply", "")
        if not lead_id:
            db.execute("UPDATE event_bus SET status='consumed', consumed_by=? WHERE id=?", (AGENT, ev["id"]))
            db.commit()
            continue
        lead_row = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if lead_row:
            lead = dict(lead_row)
            intent = await classify_reply_intent(reply_text)
            logger.info(f"Reply intent for lead {lead_id}: {intent}")

            if intent in UNSUBSCRIBE_INTENTS:
                db.execute(
                    "UPDATE leads SET email_invalid=1, pipeline_stage='unsubscribed' WHERE id=?",
                    (lead_id,)
                )
                db.execute("UPDATE sequence_queue SET sent=1 WHERE lead_id=? AND sent=0", (lead_id,))
                db.commit()
                await send_discord(
                    f"🚫 Unsubscribe from **{lead.get('business_name')}** — removed from all outreach"
                )
            elif intent in COLD_INTENTS:
                db.execute(
                    "UPDATE leads SET pipeline_stage='cold', qualified='cold' WHERE id=?",
                    (lead_id,)
                )
                db.execute("UPDATE sequence_queue SET sent=1 WHERE lead_id=? AND sent=0", (lead_id,))
                db.commit()
                await send_discord(
                    f"❄️ Not interested: **{lead.get('business_name')}** — marked cold, sequence stopped"
                )
            elif intent in DEFER_INTENTS:
                send_after = (datetime.now(UTC) + timedelta(days=30)).isoformat()
                db.execute("UPDATE sequence_queue SET sent=1 WHERE lead_id=? AND sent=0", (lead_id,))
                db.execute(
                    "INSERT INTO sequence_queue (lead_id, step, send_after) VALUES (?,?,?)",
                    (lead_id, 2, send_after)
                )
                db.execute(
                    "UPDATE leads SET pipeline_stage='deferred', qualified='warm' WHERE id=?",
                    (lead_id,)
                )
                db.commit()
                await send_discord(
                    f"⏰ Maybe later: **{lead.get('business_name')}** — follow-up rescheduled in 30 days"
                )
            else:
                # HOT_INTENTS: hot, interested, question — send personalized response
                db.execute("UPDATE leads SET pipeline_stage='hot', qualified='hot' WHERE id=?", (lead_id,))
                db.commit()
                reply_body = None
                try:
                    async with httpx.AsyncClient(timeout=30) as client:
                        r = await client.post(
                            "http://agency-marketing:8102/generate-reply",
                            json={"lead": lead, "reply_text": reply_text or "Yes I'm interested"}
                        )
                        if r.status_code == 200:
                            reply_body = r.json().get("reply")
                except Exception as e:
                    logger.warning(f"generate-reply failed: {e}")
                if not reply_body:
                    reply_body = await generate_followup(lead, touch=1)
                sent = await send_brevo(
                    lead["email"], lead.get("business_name", ""),
                    f"Re: AI chatbot for {lead.get('business_name', 'your business')}",
                    reply_body
                )
                if sent:
                    await send_discord(
                        f"💬 **Reply received & responded** [{intent}] — {lead.get('business_name')} ({lead.get('city')})\n"
                        f"Their message: _{reply_text[:200] if reply_text else 'No text'}_\n"
                        f"Auto-response sent."
                    )
                logger.info(f"Hot reply handled for lead {lead_id} ({lead.get('business_name')})")
        db.execute(
            "UPDATE event_bus SET status='consumed', consumed_by=? WHERE id=?", (AGENT, ev["id"])
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


async def main_loop():
    while True:
        try:
            db = get_db()
            await poll_event_bus(db)
            await process_warm_queue(db)
            db.close()
            update_heartbeat("processed leads and queue")
        except Exception as e:
            logger.error(f"Main loop error: {e}")
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_tables()
    task = asyncio.create_task(main_loop())
    logger.info("Sales agent started")
    yield
    task.cancel()


app = FastAPI(title="Agency Sales", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    db = get_db()
    row = db.execute("SELECT * FROM agent_status WHERE agent_name=?", (AGENT,)).fetchone()
    hot = db.execute("SELECT COUNT(*) as c FROM leads WHERE pipeline_stage='hot'").fetchone()["c"]
    warm = db.execute("SELECT COUNT(*) as c FROM leads WHERE qualified='warm'").fetchone()["c"]
    cold = db.execute("SELECT COUNT(*) as c FROM leads WHERE qualified='cold'").fetchone()["c"]
    db.close()
    return {
        "agent": AGENT,
        "qualified_today": qualified_today,
        "emails_sent_today": emails_sent_today,
        "hot_leads": hot, "warm_leads": warm, "cold_leads": cold,
        "actions_today": row["actions_today"] if row else 0,
    }


@app.post("/qualify-lead")
async def qualify_lead_endpoint(payload: dict):
    result = await qualify_lead(payload)
    lead_id = payload.get("lead_id")
    if lead_id:
        db = get_db()
        db.execute("UPDATE leads SET qualified=?, qualification_reason=? WHERE id=?",
                   (result["score"], result.get("reasoning"), lead_id))
        db.commit()
        db.close()
    return result


@app.get("/pipeline")
def pipeline():
    db = get_db()
    rows = db.execute(
        "SELECT pipeline_stage, COUNT(*) as count FROM leads GROUP BY pipeline_stage"
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.post("/handle-objection")
async def handle_objection(payload: dict):
    objection = payload.get("objection", "").lower()
    lead_id = payload.get("lead_id")
    # Check hardcoded responses first
    for key, response in OBJECTIONS.items():
        if key in objection:
            return {"response": response, "source": "template"}
    # Fall back to Ollama for unknown objections
    lead = {}
    if lead_id:
        db = get_db()
        row = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        lead = dict(row) if row else {}
        db.close()
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": CHAT_MODEL, "stream": False,
                      "prompt": f"You are Alex from RingCatch (AI chatbots for small businesses, $450+$89/mo). A lead said: '{objection}'. Write a 2-3 sentence warm, helpful response that addresses the concern without being pushy."}
            )
        return {"response": resp.json()["response"].strip(), "source": "llm"}
    except Exception as e:
        return {"response": "That's a great point — let me get back to you on that.", "source": "fallback"}


@app.get("/hot-leads")
def hot_leads():
    db = get_db()
    since = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    rows = db.execute(
        "SELECT * FROM leads WHERE pipeline_stage='hot' AND (last_contacted >= ? OR last_contacted IS NULL) ORDER BY last_contacted DESC",
        (since,)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]
