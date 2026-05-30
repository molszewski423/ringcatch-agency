import asyncio
import json
import logging
import os
import re
import socket
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

async def _daily_sent() -> int:
    db = get_db()
    count = db.execute(
        "SELECT COUNT(*) FROM outreach WHERE date(sent_at,'localtime')=date('now','localtime')"
    ).fetchone()[0]
    db.close()
    return count


async def _send_due_followups(step: int) -> None:
    # Respect global daily cap across all steps
    if await _daily_sent() >= EMAIL_DAILY_LIMIT:
        logger.info(f"Daily email limit reached ({EMAIL_DAILY_LIMIT}), skipping step-{step} follow-ups")
        return
    db = get_db()
    days_back = STEP_DELAY_DAYS.get(step, 999)
    prev_step = step - 1
    cutoff = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    due = db.execute("""
        SELECT l.id FROM leads l
        INNER JOIN outreach prev ON prev.lead_id = l.id AND prev.sequence_step = ?
        LEFT JOIN  outreach curr ON curr.lead_id = l.id AND curr.sequence_step = ?
        WHERE curr.id IS NULL AND prev.sent_at <= ?
    """, (prev_step, step, cutoff)).fetchall()
    db.close()
    for (lead_id,) in due:
        if await _daily_sent() >= EMAIL_DAILY_LIMIT:
            logger.info(f"Daily limit hit mid-followup at step {step}, stopping")
            break
        await send_email({"lead_id": lead_id, "step": step})


async def _autonomous_outreach_loop() -> None:
    await asyncio.sleep(90)
    followup_last_run = datetime.utcnow()
    while True:
        try:
            await _send_step1_to_new_leads()
            now = datetime.utcnow()
            if (now - followup_last_run).total_seconds() >= 3600:
                for step in (2, 3, 4):
                    await _send_due_followups(step)
                followup_last_run = now
        except Exception as e:
            logger.error("Autonomous outreach loop error: %s", e, exc_info=True)
        await asyncio.sleep(600)  # check every 10 minutes


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_analytics_tables()
    asyncio.create_task(_autonomous_outreach_loop())
    yield


app = FastAPI(title="Agency Outreach Agent", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATA_DIR = Path("/data")
DB_PATH = DATA_DIR / "agency.db"

OLLAMA_URL    = os.environ.get("OLLAMA_BASE_URL", "http://host.containers.internal:11434")
OLLAMA_MODEL  = os.environ.get("BACKEND_MODEL", os.environ.get("OLLAMA_MODEL", "gemma4:26b"))
CHAT_MODEL    = os.environ.get("CHAT_MODEL", "qwen2.5:7b")
DEMO_MODEL    = os.environ.get("DEMO_MODEL", OLLAMA_MODEL)
EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "stub")
EMAIL_DAILY_LIMIT = int(os.environ.get("EMAIL_DAILY_LIMIT", "100"))
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")
GROQ_QUALITY_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FAST_MODEL    = os.environ.get("GROQ_FAST_MODEL", "llama-3.1-8b-instant")
GROQ_URL           = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL       = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL         = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


async def _llm(prompt: str, max_tokens: int = 400) -> str:
    """Groq 8b → Ollama (gemma4:26b) → Groq 70b. Local model preferred over scarce 70b quota."""
    messages = [{"role": "user", "content": prompt}]

    # 1. Groq 8b-instant — 14,400 req/day, lowest latency
    if GROQ_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={"model": GROQ_FAST_MODEL, "messages": messages, "max_tokens": max_tokens},
                )
            if r.status_code == 200:
                logger.info(f"outreach LLM: groq/{GROQ_FAST_MODEL}")
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"Groq {GROQ_FAST_MODEL} failed: {e}")

    # 2. Ollama — local, no rate limits (fast on GPU; 3s fail-fast until GPU fixed)
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            )
        if r.status_code == 200:
            logger.info(f"outreach LLM: ollama/{OLLAMA_MODEL}")
            return r.json()["response"].strip()
    except Exception as e:
        logger.warning(f"Ollama failed: {e}")

    # 3. Groq 70b — cloud fallback when MikeNixPC unreachable (~1,000 RPD, use sparingly)
    if GROQ_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={"model": GROQ_QUALITY_MODEL, "messages": messages, "max_tokens": max_tokens},
                )
            if r.status_code == 200:
                logger.info(f"outreach LLM: groq/{GROQ_QUALITY_MODEL}")
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"Groq {GROQ_QUALITY_MODEL} failed: {e}")

    return ""


async def _chat_llm(prompt: str, max_tokens: int = 400) -> str:
    """Webchat-only LLM chain: Gemini → Groq 8b → Ollama → Groq 70b.
    Gemini stays here (not in email _llm) so quota is spent on live visitors, not background emails."""
    messages = [{"role": "user", "content": prompt}]

    # 1. Gemini — highest quality, resets daily at midnight
    if GEMINI_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.post(
                    GEMINI_URL,
                    headers={"Authorization": f"Bearer {GEMINI_API_KEY}"},
                    json={"model": GEMINI_MODEL, "messages": messages, "max_tokens": max_tokens},
                )
            if r.status_code == 200:
                logger.info("chat LLM: gemini")
                return r.json()["choices"][0]["message"]["content"].strip()
            logger.warning(f"Gemini chat {r.status_code}: {r.text[:120]}")
        except Exception as e:
            logger.warning(f"Gemini chat failed: {e}")

    # 2. Groq 8b — fast fallback
    if GROQ_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={"model": GROQ_FAST_MODEL, "messages": messages, "max_tokens": max_tokens},
                )
            if r.status_code == 200:
                logger.info(f"chat LLM: groq/{GROQ_FAST_MODEL}")
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"Groq {GROQ_FAST_MODEL} chat failed: {e}")

    # 3. Ollama local — no quota (3s until GPU fixed; will be ~1s on GPU)
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            )
        if r.status_code == 200:
            logger.info(f"chat LLM: ollama/{OLLAMA_MODEL}")
            return r.json()["response"].strip()
    except Exception as e:
        logger.warning(f"Ollama chat failed: {e}")

    # 4. Groq 70b — last resort
    if GROQ_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={"model": GROQ_QUALITY_MODEL, "messages": messages, "max_tokens": max_tokens},
                )
            if r.status_code == 200:
                logger.info(f"chat LLM: groq/{GROQ_QUALITY_MODEL}")
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"Groq {GROQ_QUALITY_MODEL} chat failed: {e}")

    return ""


async def _personalized_opener(lead: dict) -> str:
    """Generate a hyper-personalized first sentence for step-1 emails."""
    biz   = lead.get("business_name", "")
    niche = lead.get("niche", "")
    city  = lead.get("city", "")
    prompt = (
        f"Write ONE sentence (max 25 words) that opens a cold email to {biz}, a {niche} business in {city}. "
        f"Make it feel like you know their specific daily reality — missed after-hours calls, slow response to inquiries, "
        f"or losing jobs to whoever picks up first. "
        f"Reference the city or niche naturally. Sound like a real person, not a template. No greeting, no 'I'. "
        f"Examples of the right tone:\n"
        f"- 'Houston summers mean your phone rings nonstop — until it stops at 8pm.'\n"
        f"- 'Burst pipes don't wait for business hours, but most plumbing websites do.'\n"
        f"- 'Dallas dental patients asking questions after 9pm get a voicemail — every time.'\n"
        f"Write one sentence only for {biz} in {city}:"
    )
    result = await _llm(prompt, max_tokens=60)
    opener = result.strip().strip('"').strip("'")
    return opener if opener and len(opener) < 200 else ""

# These are only required when EMAIL_PROVIDER != "stub"
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
BREVO_SENDER_NAME = os.environ.get("BREVO_SENDER_NAME", "Alex from RingCatch")
BREVO_SENDER_EMAIL = os.environ.get("BREVO_SENDER_EMAIL", "alex@ringcatch.io")

STRIPE_SETUP_LINK   = os.environ.get("STRIPE_SETUP_LINK", "https://ringcatch.io")
STRIPE_MONTHLY_LINK = os.environ.get("STRIPE_MONTHLY_LINK", "https://ringcatch.io")

VIDEO_DIR = Path("/data/videos")
# Maps lowercase keyword fragments → canonical video niche names (must match video/main.py NICHE_QUERIES keys)
_NICHE_VIDEO_KEYS = {
    "hvac": "HVAC", "plumb": "Plumbing", "dental": "Dental", "dentist": "Dental",
    "auto repair": "Auto Repair", "auto": "Auto Repair", "mechanic": "Auto Repair",
    "law firm": "Law Firm", "law": "Law Firm", "attorney": "Law Firm", "lawyer": "Law Firm",
    "property manag": "Property Management", "property": "Property Management",
    "landscap": "Landscaping", "lawn": "Landscaping",
    "roof": "Roofing", "pest": "Pest Control", "electric": "Electrician",
    "insurance": "Insurance", "real estate": "Real Estate", "realtor": "Real Estate",
    "gym": "Gym / Fitness", "fitness": "Gym / Fitness", "personal train": "Gym / Fitness",
    "clean": "Cleaning Services",
}


def get_niche_video_url(niche: str) -> str | None:
    if not VIDEO_DIR.exists():
        return None
    niche_lower = niche.lower()
    target = next((v for k, v in _NICHE_VIDEO_KEYS.items() if k in niche_lower), None)
    if not target:
        return None
    best: tuple | None = None
    for jf in VIDEO_DIR.glob("*.json"):
        try:
            meta = json.loads(jf.read_text())
            if meta.get("niche") == target and meta.get("youtube_url"):
                mtime = jf.stat().st_mtime
                if best is None or mtime > best[0]:
                    best = (mtime, meta["youtube_url"])
        except Exception:
            pass
    return best[1] if best else None
MIKE_ALERT_EMAIL    = os.environ.get("MIKE_ALERT_EMAIL", "molszewski423@gmail.com")
DISCORD_URL  = os.environ.get("DISCORD_BOT_URL", "http://agency-discord:8103/alert")

# In-memory chat sessions keyed by session_id
chat_sessions: dict = {}

# Cache of domain → valid/invalid to avoid repeated DNS lookups
_email_domain_cache: dict[str, bool] = {}


async def _validate_email_domain(email: str) -> bool:
    """Resolve the email's domain to weed out bad addresses before sending.
    Uses a DNS A-record lookup (no new dependencies). Cached per domain."""
    if not email or "@" not in email:
        return False
    _, _, domain = email.rpartition("@")
    domain = domain.strip().lower()
    if not domain or "." not in domain:
        return False
    if domain in _email_domain_cache:
        return _email_domain_cache[domain]

    def _check() -> bool:
        try:
            socket.getaddrinfo(domain, None, socket.AF_INET)
            return True
        except socket.error:
            return False

    try:
        loop = asyncio.get_event_loop()
        ok = await asyncio.wait_for(loop.run_in_executor(None, _check), timeout=5)
    except asyncio.TimeoutError:
        ok = False

    _email_domain_cache[domain] = ok
    if not ok:
        logger.info(f"Email domain unresolvable: {domain}")
    return ok

ALEX_BASE = """You are Alex, the AI sales consultant for RingCatch (ringcatch.io).

RingCatch builds custom AI chatbots for local small businesses — installed on their website, trained on their specific business, live in 48 hours. The chatbot answers questions, captures leads, and books appointments 24/7.

Pricing: $450 one-time setup + $89/month. No contracts.

Payment links (share only when they're ready to buy):
- Setup fee ($450): {setup_link}
- Monthly plan ($89/mo): {monthly_link}

Escalation: If someone asks for a human, say exactly: "Let me have Mike, our founder, reach out to you — what's the best number or email to use?" After they give contact info, say exactly: "Perfect, Mike will reach out to you soon!" NEVER tell them to call Mike.

Core rules (always apply):
- 2-4 sentences max per message. This is live chat.
- Warm, direct, human. Sound like a trusted advisor, never a salesperson.
- No bullet points, no bold, no headers in chat.
- ONE question at a time."""

# Industry-specific demo knowledge — what their chatbot would actually know
INDUSTRY_DEMO_KNOWLEDGE = {
    "HVAC": "services (AC repair, furnace installation, heat pump maintenance, emergency HVAC), typical pricing ($85-150 service call), 24/7 emergency availability, seasonal maintenance plans",
    "Plumbing": "services (drain cleaning, water heater install, leak repair, emergency plumbing), typical pricing ($75-120 service call), 24/7 emergency dispatch available",
    "Electrical": "services (panel upgrades, outlet installation, EV charger install, emergency electrical), licensed and insured, permit-ready, 24/7 emergency",
    "Roofing": "services (roof repair, full replacement, storm damage, free estimates), typical timeline (1-3 days for repair, 1-2 days for replacement), financing available",
    "Landscaping": "services (lawn maintenance, landscape design, irrigation, tree trimming), weekly/bi-weekly plans, free estimate for new clients",
    "Auto Repair": "services (oil change, brakes, engine repair, diagnostics), warranty on parts and labor, loaner vehicle availability, most makes and models",
    "Dental / Medical": "accepting new patients, insurance accepted (most major plans), appointment availability within 48 hours, emergency same-day slots",
    "Law Firm": "services (personal injury, family law, criminal defense, estate planning, business law, DUI defense), free initial consultations, payment plans and contingency options available, response within 24 hours",
    "Real Estate": "buyer/seller representation, free home valuation, market analysis, years of local experience, off-market listings",
    "Insurance": "auto/home/life/commercial coverage, free quotes, multi-policy discounts, claims support, licensed in-state",
    "Salon / Spa": "services offered, booking availability, stylist expertise, product lines, gift cards and packages",
    "Gym / Fitness": "membership options, class schedule, personal training, free trial available, no long-term contracts",
    "Restaurant": "hours, menu highlights, reservations and walk-ins, catering services, delivery options",
    "Cleaning Services": "residential and commercial, weekly/bi-weekly/one-time, bonded and insured, eco-friendly products available, free estimate",
    "Pest Control": "pests treated, treatment methods, guarantee/warranty, emergency same-day service, pet-safe options",
    "Veterinary": "services, accepted pets, appointment availability, emergency care, wellness plans",
    "Home Services": "services, service area, licensed/insured, free estimates, warranty on work",
    "Property Management": "services (tenant screening, rent collection, maintenance coordination, lease management, vacancy marketing), typical management fee (8-12% of monthly rent), 24/7 maintenance request line, online owner and tenant portals, eviction support",
    "Accounting / CPA": "services (tax preparation, bookkeeping, business accounting, IRS representation, payroll, QuickBooks setup), accepting new clients, appointment scheduling within 48 hours, tax season (Jan–Apr) extended hours, year-round advisory available, free initial consultation",
    "Bakery": "hours and location, menu items and specialties, custom order process and lead time, pickup and delivery options, pricing for custom cakes, allergen information, seasonal specials and holiday availability",
    "Wedding Venue": "venue capacity and layout options, available dates and booking process, catering policies (in-house or preferred vendors), pricing packages and deposit schedule, site tour scheduling, on-site coordinator availability, vendor list",
}


_INDUSTRY_ALIASES = {
    "plumber": "Plumbing", "plumb": "Plumbing",
    "hvac": "HVAC", "air condition": "HVAC", "furnace": "HVAC", "heat pump": "HVAC",
    "mechanic": "Auto Repair", "auto shop": "Auto Repair", "auto repair": "Auto Repair", "car repair": "Auto Repair",
    "dentist": "Dental / Medical", "dental": "Dental / Medical", "doctor": "Dental / Medical", "medical": "Dental / Medical",
    "lawyer": "Law Firm", "attorney": "Law Firm", "law firm": "Law Firm",
    "realtor": "Real Estate", "real estate": "Real Estate",
    "landscaper": "Landscaping", "landscaping": "Landscaping", "lawn": "Landscaping",
    "roofer": "Roofing", "roofing": "Roofing",
    "exterminator": "Pest Control", "pest": "Pest Control",
    "electrician": "Electrical", "electric": "Electrical",
    "vet": "Veterinary", "veterinary": "Veterinary", "animal": "Veterinary",
    "gym": "Gym / Fitness", "fitness": "Gym / Fitness",
    "salon": "Salon / Spa", "spa": "Salon / Spa", "hair": "Salon / Spa",
    "cleaner": "Cleaning Services", "cleaning": "Cleaning Services", "maid": "Cleaning Services", "janitorial": "Cleaning Services",
    "property manager": "Property Management", "property manag": "Property Management", "landlord": "Property Management", "property": "Property Management",
    "insurance": "Insurance",
    "restaurant": "Restaurant", "food": "Restaurant",
    "retail": "Retail",
    "accountant": "Accounting / CPA", "cpa": "Accounting / CPA", "tax prep": "Accounting / CPA", "bookkeeper": "Accounting / CPA", "accounting": "Accounting / CPA",
    "bakery": "Bakery", "pastry": "Bakery", "cake": "Bakery",
    "wedding venue": "Wedding Venue", "venue": "Wedding Venue", "event venue": "Wedding Venue", "wedding": "Wedding Venue",
    "personal training": "Gym / Fitness", "personal trainer": "Gym / Fitness", "independent gym": "Gym / Fitness",
}


INDUSTRY_DISCOVERY_QUESTIONS = {
    "HVAC": [
        '"When a homeowner\'s AC dies on a Friday night — what do they get when they call you right now?"',
        '"Is there a time of day or season where you know you\'re missing service calls?"',
        '"Do you have someone on the phones full time, or does it land on you between jobs?"',
    ],
    "Plumbing": [
        '"When someone has a burst pipe at midnight and calls you — what do they actually get right now?"',
        '"How do you handle emergency calls when you\'re already deep in another job?"',
        '"Do you feel like you\'re losing jobs to whoever picks up the phone fastest?"',
    ],
    "Electrical": [
        '"When someone has an electrical emergency after hours — can they actually reach someone at your company?"',
        '"How are you capturing estimate requests that come in while your crew is out on jobs?"',
        '"Are new customers getting a fast enough first response from you, or are they calling around?"',
    ],
    "Roofing": [
        '"After a big storm and homeowners start calling for damage assessments — how are you handling that volume?"',
        '"Are you losing estimates to competitors who respond faster to online inquiries?"',
        '"How do you handle the flood of calls when you can\'t pick up mid-job?"',
    ],
    "Landscaping": [
        '"During your busy season, how many new client inquiries do you think slip through before you get back to them?"',
        '"When someone fills out your website form on a Sunday — how fast do they hear back?"',
        '"Are you losing recurring clients to services that just respond faster?"',
    ],
    "Auto Repair": [
        '"When someone needs a quote and calls after you close for the day — what do they get?"',
        '"How many voicemails do you come in to on Monday morning that are already cold?"',
        '"Are customers booking with whoever gets back to them first, even if your shop is better?"',
    ],
    "Dental / Medical": [
        '"When a patient has an urgent question at 8pm — what options do they have right now?"',
        '"Are new patients getting fast enough responses when they reach out to book an appointment?"',
        '"How many calls go to voicemail while you\'re in with a patient?"',
    ],
    "Law Firm": [
        '"When a potential client reaches out after 5pm with an urgent situation — what happens to that lead?"',
        '"How quickly is your office following up on new client contact forms?"',
        '"Are you losing clients to firms that respond faster, even if your representation is stronger?"',
    ],
    "Real Estate": [
        '"When a buyer gets excited about a listing at 9pm — are they hearing back from you or from a competitor?"',
        '"How quickly do you follow up when someone submits a contact form on a listing?"',
        '"Are you losing buyer leads to agents who are just faster to respond?"',
    ],
    "Insurance": [
        '"When someone wants a quote outside business hours — what happens to that lead right now?"',
        '"How many potential clients do you think shop around while waiting to hear back from you?"',
        '"Are you capturing every referral that comes in, or do some slip through?"',
    ],
    "Cleaning Services": [
        '"When someone requests a quote on your website on a Saturday — how long before they hear back?"',
        '"Do you think you\'re losing recurring clients to services that just respond faster?"',
        '"How are you handling new inquiries when you and your team are out on jobs all day?"',
    ],
    "Pest Control": [
        '"When someone discovers a pest problem at night and calls you — what do they actually get right now?"',
        '"Infestations tend to get noticed in the evening — are you capturing those after-hours calls?"',
        '"How are you handling the spike in calls after a neighborhood has a pest outbreak?"',
    ],
    "Property Management": [
        '"When a tenant has a maintenance emergency at 2am — what happens when they call you right now?"',
        '"How do prospective tenants get their first response when they inquire about a vacancy?"',
        '"When five tenants report issues the same weekend — how do you track and respond to all of them?"',
    ],
    "Accounting / CPA": [
        '"During tax season — when a client calls after hours with an urgent question, what do they get right now?"',
        '"How quickly are new prospects getting a response when they reach out asking about your services?"',
        '"How many potential clients do you think contact two or three CPAs and go with whoever responds first?"',
    ],
    "Bakery": [
        '"When someone wants to order a custom cake and calls after you close — what happens to that request?"',
        '"During wedding season, how do you handle the volume of custom order inquiries coming in at once?"',
        '"Are customers going with a competitor just because they responded to the inquiry faster?"',
    ],
    "Wedding Venue": [
        '"When an excited couple wants to tour your venue and reaches out on a Sunday night — what do they get right now?"',
        '"How do you handle the rush of inquiries after someone sees your venue on Instagram or The Knot?"',
        '"Are couples booking competing venues while waiting to hear back from you for a tour?"',
    ],
    "Salon / Spa": [
        '"When a client tries to book while you\'re with another client — what happens to that request?"',
        '"How many booking requests do you think you miss because you can\'t pick up mid-appointment?"',
        '"Are clients rebooking with whoever responds first, even if they prefer your work?"',
    ],
    "Gym / Fitness": [
        '"When someone\'s motivated to join at 10pm and tries to reach you — what do they get?"',
        '"How are you capturing membership inquiries that come in outside of staffed hours?"',
        '"Are you losing signups to gyms that just make it easier to get started?"',
    ],
    "Veterinary": [
        '"When a pet owner has a late-night emergency and calls your clinic — what do they get right now?"',
        '"How are you handling after-hours calls for urgent situations that can\'t wait until morning?"',
        '"Are new patients getting a fast enough first response when they reach out?"',
    ],
    "Restaurant": [
        '"When someone wants to make a reservation on a busy Saturday night and can\'t get through — where do they go?"',
        '"How are you handling catering inquiries or large party requests that come in after close?"',
        '"Are you losing reservations to places that just make it easier to book?"',
    ],
}

_DISCOVERY_FALLBACK = [
    '"When someone reaches out to {biz} after hours right now — what do they actually get?"',
    '"Is there a time of day where you know inquiries are slipping through the cracks?"',
    '"Do you have someone dedicated to answering calls and messages, or does it fall on you?"',
]


def _industry_knowledge(industry: str) -> str:
    lower = industry.lower()
    for alias, canonical in _INDUSTRY_ALIASES.items():
        if alias in lower:
            val = INDUSTRY_DEMO_KNOWLEDGE.get(canonical)
            if val:
                return val
    for key, val in INDUSTRY_DEMO_KNOWLEDGE.items():
        if key.lower() in lower:
            return val
    return "typical services, pricing, availability, and how to get started"


def _demo_fallback_reply(industry: str, user_message: str) -> str:
    """Keyword-aware AI receptionist fallback for demo phase when LLMs are unavailable."""
    msg = user_message.lower()
    knowledge = _industry_knowledge(industry)

    # Services / what do you offer / what do you practice
    if any(w in msg for w in ("type", "service", "offer", "speciali", "practice", "what do you", "what kind", "help with", "provide", "do you do", "work on", "handle")):
        # Prefer extracting from "services (X, Y, Z)" pattern; fall back to first comma-items
        svc_match = re.search(r'services\s*\(([^)]+)\)', knowledge)
        if svc_match:
            raw_items = [s.strip() for s in svc_match.group(1).split(",")]
        else:
            raw_items = [s.strip() for s in knowledge.split(",")]
        items = [i for i in raw_items if i and len(i) < 50][:4]
        snippet = ", ".join(items)
        return (
            f"🤖 We handle {snippet} — and quite a bit more. "
            "What specifically brings you in? And can I get your name and best callback number to connect you with the right person quickly?"
        )

    # Scheduling / appointments / availability
    if any(w in msg for w in ("appointment", "tomorrow", "schedule", "book", "come in", "get in", "available", "availability", "slot", "next available", "when can", "how soon", "fit me in")):
        if "emergency" in knowledge.lower() or "same-day" in knowledge.lower() or "24/7" in knowledge:
            return (
                "🤖 Yes — we have same-day and emergency availability, and we'll do our best to get you in fast. "
                "Can I get your name and best callback number so the team can confirm a time with you directly?"
            )
        return (
            "🤖 Absolutely, I can help get that arranged! We have openings coming up and do our best to accommodate quickly. "
            "Can I get your name and best callback number so the team can confirm the time with you?"
        )

    # Pricing / cost / estimate / quote
    if any(w in msg for w in ("price", "pricing", "cost", "charge", "fee", "how much", "rate", "quote", "estimate", "what do you charge")):
        price_match = re.search(r'\$[\d,]+(?:\s*[-–]\s*\$?[\d,]+)?(?:\s*(?:per|\/)\s*\w+)?', knowledge)
        if price_match:
            return (
                f"🤖 Pricing typically starts around {price_match.group(0)}, though the final number depends on the specifics of your situation. "
                "Can I grab your name and best callback number so the team can give you an accurate quote?"
            )
        return (
            "🤖 Pricing depends on the details — I want to make sure you get an accurate number rather than a guess. "
            "Can I grab your name and best callback number so the team can follow up with a real quote?"
        )

    # Hours / when open
    if any(w in msg for w in ("hour", "open", "close", "when", "today", "tonight", "weekend", "sunday", "saturday", "monday", "what time")):
        if "24/7" in knowledge or "emergency" in knowledge.lower():
            return (
                "🤖 We have 24/7 availability for urgent situations, with standard hours for scheduled visits. "
                "Can I grab your name and best callback number to confirm the details for you?"
            )
        return (
            "🤖 We keep regular business hours and have options for urgent after-hours needs. "
            "Can I get your name and best callback number so the team can confirm availability for you?"
        )

    # Location / service area
    if any(w in msg for w in ("where", "location", "address", "near", "area", "local", "city", "zip", "travel", "come to", "service area")):
        return (
            "🤖 We serve the surrounding area — let me make sure we cover your location. "
            "Can I get your name, zip code, and best callback number so the team can confirm?"
        )

    # Insurance / payment / financing
    if any(w in msg for w in ("insurance", "payment", "pay", "accept", "card", "cash", "financing", "payment plan", "covered", "out of pocket")):
        if "insurance" in knowledge.lower():
            return (
                "🤖 We accept most major insurance plans and we're happy to verify your coverage before your visit. "
                "Can I grab your name and best callback number so the team can confirm the details with you?"
            )
        if "financing" in knowledge.lower() or "payment plan" in knowledge.lower():
            return (
                "🤖 We do offer financing and flexible payment options to make it work for you. "
                "Can I get your name and best callback number so the team can walk you through what's available?"
            )
        return (
            "🤖 Happy to help with that! Can I grab your name and best callback number so the team can go over your options?"
        )

    # Emergency / urgent
    if any(w in msg for w in ("emergency", "urgent", "asap", "right now", "immediately", "broken", "burst", "leak", "flooding", "pain", "hurt")):
        return (
            "🤖 That sounds urgent — this is exactly what we're here for, any time of day. "
            "Can I get your name and best callback number so we can reach you right away?"
        )

    # Generic — still useful, stays in character
    return (
        "🤖 Great question — I'm here 24/7 to make sure nothing slips through the cracks. "
        "Can I grab your name and best callback number so the right person can follow up with you quickly?"
    )


def _industry_discovery_qs(industry: str, biz: str) -> list[str]:
    lower = industry.lower()
    for alias, canonical in _INDUSTRY_ALIASES.items():
        if alias in lower:
            qs = INDUSTRY_DISCOVERY_QUESTIONS.get(canonical)
            if qs:
                return qs
    for key, qs in INDUSTRY_DISCOVERY_QUESTIONS.items():
        if key.lower() in lower:
            return qs
    return [q.replace("{biz}", biz) for q in _DISCOVERY_FALLBACK]


def _build_phase_prompt(session: dict, phase: int) -> str:
    lead     = session["lead"]
    name     = lead.get("name", "there")
    biz      = lead.get("business_name", "your business")
    industry = lead.get("industry", "small business")
    pain     = session.get("pain") or lead.get("challenge") or "missing leads"
    turn     = session.get("turn", 0)

    conv = "\n".join(
        f"{'Alex' if m['role'] == 'alex' else name}: {m['content']}"
        for m in session["history"]
    )

    base = ALEX_BASE.format(
        setup_link=STRIPE_SETUP_LINK, monthly_link=STRIPE_MONTHLY_LINK
    )
    demo_knowledge = _industry_knowledge(industry)
    discovery_qs   = _industry_discovery_qs(industry, biz)
    qs_block       = "\n".join(f"- {q}" for q in discovery_qs)

    PHASE_INSTRUCTIONS = {
        1: f"""CURRENT PHASE: Discovery (exchange {turn + 1})
Your ONLY job right now: understand the SPECIFIC pain this {industry} business owner feels every week. DO NOT pitch.
Ask ONE empathetic question drawn from the examples below — pick the one most likely to resonate given what you know so far.
These questions are calibrated for {industry} businesses like {biz}:
{qs_block}
Use these as inspiration, not a script — adapt to what they've already told you. Be genuinely curious. Max 40 words.""",

        2: f"""CURRENT PHASE: Demo Transition
You've heard their situation. Their main pain: "{pain}"
Now make the pivot to the live demo — this is the most important message.
Structure it in ONE response, under 70 words total:
1. ONE sentence that shows you really heard their problem (don't just repeat it back — connect with it)
2. ONE sentence pivoting: "I want to show you something — let me demo exactly what your customers would experience if RingCatch was running on {biz}'s site right now."
3. Immediately begin the demo. On a NEW LINE, write: "🤖 [Demo mode: {biz}] Hi there! I'm the AI assistant for {biz}. [warm {industry} business greeting, ask how you can help today]"
The 🤖 section should feel natural, warm, and professional — like a real chatbot greeting.""",

        3: f"""CURRENT PHASE: LIVE DEMO — You ARE their AI chatbot
You are responding AS the custom-trained AI assistant for {biz}, a {industry} business.
What their chatbot knows: {demo_knowledge}

CRITICAL RULES for demo mode:
- Start EVERY response with "🤖 " (no other prefix)
- Answer the "customer's" question naturally using industry knowledge above
- After answering helpfully, ask ONE follow-up to capture a lead: "What's your name and best number to reach you?"
- If they give contact info, confirm it warmly and offer to schedule/connect them
- If asked something you don't know specifically, handle it gracefully: "Let me connect you with the team — what's your name and best way to reach you?"
- Stay completely in character as their chatbot. Do NOT break character or mention RingCatch.
- Under 70 words. Sound helpful and instant.""",

        4: f"""CURRENT PHASE: Demo Close — Switch back to Alex
End the demo and close with genuine value. Structure (under 90 words total):
1. End demo cleanly: "— End of demo. —" on its own line.
2. Bridge to their pain ("{pain}"): Tell them what JUST happened in business terms — the customer got an instant answer, their contact info was captured, this would have happened at 2am just the same.
3. Value anchor (do NOT list features): "For $89/month, that's always running for {biz} — every inquiry, every hour."
4. ONE soft close: "Want to see it live on your site in 48 hours?"
Warm, confident, zero pressure.""",

        5: f"""CURRENT PHASE: Conversion
Answer questions honestly. Handle objections naturally and warmly.
The objection guide (use these angles):
- "Too expensive" → "One missed job a month more than covers it — most HVAC calls are $300+"
- "Need to think about it" → "Totally fair. What's the one thing you'd want to think through?"
- "Already have chat" → "The difference is it's trained on YOUR business — your prices, your hours, your services"
- "How do I know it works?" → "The demo you just saw IS how it works — trained on your info"
- "Contract?" → "No contract, cancel anytime, 30 days notice"

When they're ready to move forward:
- Booking call: https://cal.com/michael-olszewski-nn9caa/15-min-discovery-call
- Setup payment: {STRIPE_SETUP_LINK}
- Monthly plan: {STRIPE_MONTHLY_LINK}
Under 60 words per response.""",
    }

    biz_context = (
        f"\n\nBUSINESS CONTEXT (always keep this in mind):\n"
        f"- Business type: {industry}\n"
        f"- Business name: {biz}\n"
        f"- Every question, response, and example must be framed around the realities of running a {industry} business.\n"
        f"- Do NOT use generic customer-service language. Speak to the specific daily pressures of {industry} owners.\n"
        + (f"- Known pain so far: {pain}\n" if pain and pain != "missing leads" else "")
    )

    return (
        base
        + biz_context
        + f"\nLead contact: {name}"
        + f"\n\n{PHASE_INSTRUCTIONS.get(phase, PHASE_INSTRUCTIONS[5])}"
        + f"\n\nConversation:\n{conv}\n\nAlex:"
    )

# Days after step-1 send that each follow-up fires
STEP_DELAY_DAYS = {2: 2, 3: 5, 4: 10}  # sprint timing: day 2, 5, 10

# Niche-specific subject lines for step 1 — hyper-local strategy
NICHE_SUBJECTS_STEP1 = {
    "HVAC":              "{niche} demand in {city}",
    "Plumbing":          "{niche} service calls in {city}",
    "Electrical":        "Local {niche} inquiries in {city}",
    "Roofing":           "{niche} leads in {city}",
    "Dental":            "New patients in {city} for {name}",
    "Veterinary":        "Emergency pet calls in {city}",
    "Vet ":              "Emergency pet calls in {city}",
    "Auto Repair":       "{niche} estimate requests in {city}",
    "Auto repair":       "{niche} estimate requests in {city}",
    "Landscaping":       "{niche} bookings for this season in {city}",
    "Pest Control":      "Local {niche} requests in {city}",
    "Pest control":      "Local {niche} requests in {city}",
    "Law Firm":          "New {niche} client inquiries in {city}",
    "Law firm":          "New {niche} client inquiries in {city}",
    "Insurance":         "New insurance quote requests in {city}",
    "Cleaning":          "Local cleaning requests in {city}",
    "Salon":             "New appointment requests in {city}",
    "Gym":               "Local membership inquiries in {city}",
    "Real Estate":       "New property inquiries in {city}",
    "Property Management": "Rental inquiries in {city}",
    "Property management": "Rental inquiries in {city}",
    "Accounting":          "Tax season leads for {name} in {city}",
    "CPA":                 "Tax season leads for {name} in {city}",
    "Real estate":         "Property inquiries in {city} for {name}",
    "Realtor":             "Property inquiries in {city} for {name}",
    "Bakery":              "Custom order inquiries in {city}",
    "Wedding":             "Venue inquiries in {city} for {name}",
    "Gym":                 "Membership inquiries in {city}",
    "Fitness":             "Membership inquiries in {city}",
    "Personal training":   "Training inquiry in {city} for {name}",
}

STEP_SUBJECTS = {
    1: "__NICHE__",  # resolved by _dispatch_email using NICHE_SUBJECTS_STEP1
    2: "Re: {name}",
    3: "One last thing — {name}",
    4: "Still thinking about it?",
}

DEMO_BASE_URL = "https://ringcatch.io/book"

def _demo_url(niche: str) -> str:
    return f"{DEMO_BASE_URL}?niche={quote_plus(niche)}" if niche else DEMO_BASE_URL

STEP_INSTRUCTIONS = {
    1: (
        "Write a 3-sentence cold email from Alex at RingCatch (ringcatch.io). "
        "Target: {niche} business owner. Lead with ONE specific pain point they feel every week "
        "(missed after-hours calls, slow response to inquiries, losing jobs to faster competitors). "
        "Second sentence: RingCatch installs an AI chatbot trained on THEIR business — answers "
        "questions, captures leads, books appointments 24/7 — live on their site in 48 hours. "
        "Third sentence MUST be exactly: "
        "\"See it live for {business_name} in 2 minutes: {demo_url}\"\n"
        "No pitch, no pricing, no fluff. Sign off: 'Alex\\nRingCatch' only. Plain text."
    ),
    2: (
        "Write a 2-sentence follow-up email from Alex at RingCatch. "
        "First sentence: brief, human check-in — reference you reached out a couple days ago. "
        "Second sentence MUST end with: \"Live demo tailored to {business_name}: {demo_url}\"\n"
        "Zero pressure. Sign off: 'Alex' only."
    ),
    3: (
        "Write a 2-sentence final email from Alex at RingCatch. "
        "Warm close — no pitch. Just leaving the door open. "
        "Last sentence MUST be: \"If the timing's ever right: {demo_url}\"\n"
        "Sign off: 'Alex' only. Friendly, no desperation."
    ),
    4: (
        "Write a 2-sentence re-angle email from Alex at RingCatch. "
        "Come at it from a completely different angle than before — try: cost of a single missed job, "
        "or a competitor who already has this running, or a seasonal angle (busy season coming). "
        "Keep it under 3 sentences. One soft CTA: {demo_url}\n"
        "Sign off: 'Alex' only. No apology for following up."
    ),
}

# In-memory pricing mode (orchestrator can flip this to activate trial offer)
_pricing_mode: str = "standard"  # "standard" | "waive_setup"


def get_pricing_mode() -> str:
    return _pricing_mode


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    return db


def _init_analytics_tables():
    db = get_db()
    for col_sql in [
        "ALTER TABLE outreach ADD COLUMN opened INTEGER DEFAULT 0",
        "ALTER TABLE outreach ADD COLUMN opened_at TEXT",
        "ALTER TABLE outreach ADD COLUMN clicked INTEGER DEFAULT 0",
        "ALTER TABLE outreach ADD COLUMN bounced INTEGER DEFAULT 0",
        "ALTER TABLE outreach ADD COLUMN bounce_type TEXT",
        "ALTER TABLE outreach ADD COLUMN spam_flag INTEGER DEFAULT 0",
        "ALTER TABLE outreach ADD COLUMN replied_at TEXT",
        "ALTER TABLE leads ADD COLUMN email_invalid INTEGER DEFAULT 0",
    ]:
        try:
            db.execute(col_sql)
        except Exception:
            pass  # column already exists
    db.executescript("""
    CREATE TABLE IF NOT EXISTS page_views (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        TEXT    DEFAULT (datetime('now')),
        page      TEXT,
        referrer  TEXT,
        source    TEXT,
        ua        TEXT,
        ip        TEXT
    );
    CREATE TABLE IF NOT EXISTS chat_analytics (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id     TEXT UNIQUE,
        name           TEXT,
        business_name  TEXT,
        industry       TEXT,
        started_at     TEXT DEFAULT (datetime('now')),
        last_active    TEXT DEFAULT (datetime('now')),
        phase_reached  INTEGER DEFAULT 1,
        email_captured TEXT,
        phone          TEXT,
        demo_seen      INTEGER DEFAULT 0,
        close_reached  INTEGER DEFAULT 0,
        converted      INTEGER DEFAULT 0
    );
    """)
    db.commit()
    db.close()


def _parse_source(referrer: str) -> str:
    if not referrer:
        return "direct"
    r = referrer.lower()
    if "google" in r:   return "google"
    if "bing" in r:     return "bing"
    if "facebook" in r or "fb.com" in r: return "facebook"
    if "instagram" in r: return "instagram"
    if "linkedin" in r: return "linkedin"
    if "twitter" in r or "t.co" in r or "x.com" in r: return "twitter"
    if "youtube" in r:  return "youtube"
    return "referral"


@app.post("/ingest-leads")
async def ingest_leads(payload: dict, background_tasks: BackgroundTasks):
    leads: list[dict] = payload.get("leads", [])

    db = get_db()
    inserted = 0
    for lead in leads:
        try:
            db.execute("""
                INSERT OR IGNORE INTO leads
                    (business_name, email, phone, website, domain, address, city, niche, scraped_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                lead.get("business_name", ""),
                lead.get("email", ""),
                lead.get("phone", ""),
                lead.get("website", ""),
                lead.get("domain", ""),
                lead.get("address", ""),
                lead.get("city", ""),
                lead.get("niche", ""),
                lead.get("scraped_date", date.today().isoformat()),
            ))
            inserted += 1
        except Exception as exc:
            logger.warning(f"Lead insert skipped: {exc}")
    db.commit()
    db.close()

    # Always trigger outreach — picks up any unprocessed leads already in DB
    background_tasks.add_task(_send_step1_to_new_leads)
    return {"status": "ok", "inserted": inserted}


@app.get("/sequence-due")
def sequence_due(step: int = 1):
    """Return leads that are due for the given sequence step."""
    db = get_db()

    if step == 1:
        rows = db.execute("""
            SELECT l.id, l.business_name, l.email, l.niche, l.city
            FROM leads l
            LEFT JOIN outreach o ON o.lead_id = l.id AND o.sequence_step = 1
            WHERE l.email != '' AND o.id IS NULL
            LIMIT 50
        """).fetchall()
    else:
        days_back = STEP_DELAY_DAYS.get(step, 999)
        cutoff = (datetime.utcnow() - timedelta(days=days_back)).isoformat()
        prev_step = step - 1
        rows = db.execute("""
            SELECT l.id, l.business_name, l.email, l.niche, l.city
            FROM leads l
            INNER JOIN outreach prev ON prev.lead_id = l.id AND prev.sequence_step = ?
            LEFT JOIN  outreach curr ON curr.lead_id = l.id AND curr.sequence_step = ?
            WHERE prev.sent_at <= ? AND curr.id IS NULL AND prev.replied = 0
            LIMIT 50
        """, (prev_step, step, cutoff)).fetchall()

    db.close()
    cols = ["id", "business_name", "email", "niche", "city"]
    return {"leads": [dict(zip(cols, r)) for r in rows]}


@app.post("/send-email")
async def send_email(payload: dict):
    lead_id: int = payload["lead_id"]
    step: int = payload.get("step", 1)

    db = get_db()
    row = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        db.close()
        return {"status": "not_found"}

    cols = ["id", "business_name", "email", "phone", "website", "domain",
            "address", "city", "niche", "scraped_date", "processed"]
    lead = dict(zip(cols, row))

    # Guard against duplicate sends
    existing = db.execute(
        "SELECT id FROM outreach WHERE lead_id=? AND sequence_step=?", (lead_id, step)
    ).fetchone()
    db.close()
    if existing:
        logger.warning(f"Skipping duplicate send: lead {lead_id} step {step}")
        return {"status": "already_sent", "step": step}

    body = await _generate_email(lead, step)
    ok = await _dispatch_email(lead, body, step)

    if ok:
        db = get_db()
        db.execute("""
            INSERT INTO outreach (lead_id, email, email_body, sequence_step, sent_at)
            VALUES (?, ?, ?, ?, datetime('now'))
        """, (lead_id, lead["email"], body, step))
        if step == 1:
            db.execute(
                "UPDATE leads SET pipeline_stage='emailed' WHERE id=? AND pipeline_stage='scraped'",
                (lead_id,)
            )
        db.commit()
        db.close()
        logger.info(f"Sent step {step} to {lead['email']}")

    return {"status": "sent" if ok else "failed", "step": step}


@app.post("/generate-reply")
async def generate_reply(payload: dict):
    """Generate a warm booking-link reply for a lead who expressed interest."""
    lead_id: int = payload["lead_id"]
    reply_text: str = payload.get("reply_text", "Yes I'm interested")

    db = get_db()
    row = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    db.close()
    if not row:
        return {"status": "not_found"}

    cols = ["id", "business_name", "email", "phone", "website", "domain",
            "address", "city", "niche", "scraped_date", "processed"]
    lead = dict(zip(cols, row))

    prompt = (
        f"You are Alex, texting back a {lead['niche']} business owner who just replied "
        f"'{reply_text}' to your cold email about RingCatch's AI chatbot.\n\n"
        f"Their business: {lead['business_name']} in {lead['city']}.\n\n"
        f"Write a reply that sounds like a helpful neighbor texting back — not a salesperson. "
        f"Casual, warm, zero corporate speak. No 'I am certain', no 'I would like to', "
        f"no 'please do not hesitate'. Just real talk.\n"
        f"Include this booking link naturally: https://cal.com/michael-olszewski-nn9caa/15-min-discovery-call\n"
        f"Under 4 sentences. Sign off as 'Alex'. Email body only. No markdown."
    )

    body = await _llm(prompt)
    return {"status": "ok", "reply": body}


@app.post("/schedule-testimonial")
async def schedule_testimonial(payload: dict):
    """Queue a testimonial request email to fire 7 days after delivery."""
    client_id   = payload["client_id"]
    client_name = payload.get("client_name", "Client")
    email       = payload.get("email", "")
    niche       = payload.get("niche", "small business")

    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS testimonial_requests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   TEXT UNIQUE,
            client_name TEXT,
            email       TEXT,
            niche       TEXT,
            send_after  TEXT,
            sent        INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        INSERT OR IGNORE INTO testimonial_requests
            (client_id, client_name, email, niche, send_after)
        VALUES (?, ?, ?, ?, datetime('now', '+7 days'))
    """, (client_id, client_name, email, niche))
    db.commit()
    send_after = db.execute(
        "SELECT send_after FROM testimonial_requests WHERE client_id=?", (client_id,)
    ).fetchone()[0]
    db.close()
    logger.info(f"Testimonial scheduled for {client_name} after {send_after[:10]}")
    return {"status": "scheduled", "send_after": send_after}


@app.post("/chat/start")
async def chat_start(payload: dict):
    """Initialise a chat session from the /book form and return Alex's opener."""
    name      = payload.get("name", "there")
    biz       = payload.get("business_name", "your business")
    industry  = payload.get("industry", "")
    challenge = payload.get("challenge", "")

    sid = str(uuid.uuid4())

    session: dict = {
        "lead":    payload,
        "history": [],
        "turn":    0,
        "phase":   1,
        "pain":    challenge or None,
        "_test":   payload.get("_test", False),
    }

    # Build the phase-1 opener prompt
    prompt = _build_phase_prompt(session, 1)

    try:
        opener = await _chat_llm(prompt)
    except Exception as exc:
        logger.warning(f"chat/start LLM exception: {exc}")
        opener = ""
    if not opener:
        opener = (
            f"Hey {name}! Quick question — when a potential customer reaches out to your business "
            f"after hours, what typically happens?"
        )

    session["history"].append({"role": "alex", "content": opener})
    chat_sessions[sid] = session
    logger.info(f"Chat session {sid[:8]} started for {name} / {biz} (phase 1)")

    # Persist to analytics
    if not session["_test"]:
        db = get_db()
        db.execute("""
            INSERT OR IGNORE INTO chat_analytics
                (session_id, name, business_name, industry, phone)
            VALUES (?, ?, ?, ?, ?)
        """, (sid, name, biz, industry, payload.get("phone", "")))
        db.commit()
        db.close()
        asyncio.create_task(_discord_intake_alert(name, biz, industry, challenge))

    return {"session_id": sid, "message": opener, "phase": 1, "demo_active": False}


@app.post("/chat/message")
async def chat_message(payload: dict, background_tasks: BackgroundTasks):
    """Process a lead's chat message and return Alex's reply."""
    sid          = payload.get("session_id", "")
    user_message = payload.get("message", "").strip()

    if sid not in chat_sessions:
        return {"message": "Session expired — please refresh to start a new chat.", "error": True}

    session = chat_sessions[sid]
    lead    = session["lead"]
    history = session["history"]

    # Escalation: next user message after Alex asked for contact info
    if session.get("escalation_pending") and user_message:
        session["escalation_pending"] = False
        background_tasks.add_task(_send_escalation_alert, dict(session), user_message)

    history.append({"role": "user", "content": user_message})

    # --- Phase progression logic ---
    turn  = session.get("turn", 0)
    phase = session.get("phase", 1)

    # Capture pain from early turns if not set yet
    if turn <= 1 and not session.get("pain") and len(user_message) > 10:
        session["pain"] = user_message[:200]

    # Detect contact info in user message (used for phase advancement)
    _has_phone = bool(re.search(r'\d{3}[-.\s]?\d{3}[-.\s]?\d{4}', user_message))
    _has_email_contact = bool(re.search(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', user_message))

    # Advance phase based on turn count
    if phase == 1 and turn >= 1:
        phase = 2
    elif phase == 2:
        phase = 3  # Demo pivot is one message then straight into demo
    elif phase == 3 and (turn >= 6 or _has_phone or _has_email_contact):
        # Advance to close when contact info captured or after 6 turns
        phase = 4
        if not session.get("_test"):
            background_tasks.add_task(
                _discord_phase_alert, lead.get("name", "Lead"),
                lead.get("business_name", ""), "close-ready"
            )
    elif phase == 4:
        phase = 5

    session["phase"] = phase
    session["turn"]  = turn + 1

    # Choose model: 26b for demo/close phases, fast model for discovery/conversion
    model   = DEMO_MODEL if phase in (2, 3, 4) else CHAT_MODEL
    timeout = 90 if model == DEMO_MODEL else 60

    prompt = _build_phase_prompt(session, phase)

    try:
        reply = await _chat_llm(prompt)
    except Exception as exc:
        logger.warning(f"chat/message phase={phase} failed: {exc}")
        reply = ""
    if not reply:
        if phase == 1 and turn == 0:
            reply = (
                "Got it — every missed call is money walking out the door. "
                "Let me show you what happens when the AI picks up one of those calls. "
                "Want to see it in action?"
            )
        elif phase == 2:
            # Demo pivot — invite them to play customer
            reply = (
                "Perfect. Pretend you're a customer calling your business right now — after hours. "
                "Go ahead and ask anything: services, pricing, availability. I'll respond as your AI would."
            )
        elif phase == 1:
            reply = (
                "The AI handles all of that automatically — 24/7. "
                "Shall I show you how it sounds for your business?"
            )
        elif phase == 3:
            # Demo mode — stay in character as AI receptionist
            if _has_phone or _has_email_contact:
                reply = (
                    "🤖 Perfect, got it! Your info is captured and flagged for the team — "
                    "someone will reach out to you shortly. Is there anything else I can help you with?"
                )
            else:
                reply = _demo_fallback_reply(lead.get("industry", ""), user_message)
        elif phase == 4:
            reply = (
                "— End of demo. —\n\n"
                "That's exactly what your customers would experience — instant response, contact captured, any hour of the day. "
                "For $89/month, that runs around the clock for your business. "
                "Want to see it live on your site in 48 hours?"
            )
        else:
            reply = (
                "Most owners tell us they cover the cost with the very first extra job it captures. "
                "Want me to have Mike reach out with a custom quote?"
            )
        logger.warning(f"chat/message phase={phase} turn={turn}: all LLMs failed, using hardcoded fallback")

    # Mark escalation trigger if Alex signals Mike involvement
    if "reach out to you" in reply.lower() and "mike" in reply.lower():
        session["escalation_pending"] = True

    history.append({"role": "alex", "content": reply})
    demo_active = phase in (2, 3)

    # Detect email in user message
    email_match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", user_message)
    captured_email = email_match.group(0) if email_match else None

    # Update analytics
    if session.get("_test"):
        logger.info(f"Chat {sid[:8]} turn={turn} phase={phase} [healthcheck — skipping DB/Discord]")
        return {"message": reply, "phase": phase, "demo_active": demo_active}

    db = get_db()
    update_fields = ["phase_reached = MAX(phase_reached, ?)", "last_active = datetime('now')"]
    params: list = [phase]
    if phase >= 3:
        update_fields.append("demo_seen = 1")
    if phase >= 4:
        update_fields.append("close_reached = 1")
    if captured_email:
        update_fields.append("email_captured = ?")
        params.append(captured_email)
    params.append(sid)
    db.execute(
        f"UPDATE chat_analytics SET {', '.join(update_fields)} WHERE session_id = ?",
        params,
    )
    db.commit()
    db.close()

    logger.info(f"Chat {sid[:8]} turn={turn} phase={phase} model={model.split(':')[0]}")
    return {"message": reply, "phase": phase, "demo_active": demo_active}


@app.post("/send")
async def send_batch(background_tasks: BackgroundTasks):
    """Trigger step-1 emails to all new leads now. Used by the dashboard."""
    background_tasks.add_task(_send_step1_to_new_leads)
    db = get_db()
    pending = db.execute("SELECT COUNT(*) FROM leads WHERE email != '' AND processed=0").fetchone()[0]
    db.close()
    return {"status": "triggered", "pending_leads": pending}


@app.post("/advance")
async def advance_sequences(background_tasks: BackgroundTasks):
    """Fire any due follow-up emails (step 2 and 3) now. Used by the dashboard."""
    async def _run():
        for step in (2, 3, 4):
            await _send_due_followups(step)
    background_tasks.add_task(_run)
    return {"status": "triggered", "steps": [2, 3, 4]}


@app.post("/webhook/brevo")
async def brevo_webhook(request: Request):
    """Receives transactional event callbacks from Brevo (opens, clicks, bounces, spam)."""
    try:
        events = await request.json()
    except Exception:
        return {"status": "ok"}
    if not isinstance(events, list):
        events = [events]
    db = get_db()
    for ev in events:
        event   = ev.get("event", "")
        email   = ev.get("email", "")
        msg_id  = ev.get("message-id", "")
        if not email:
            continue
        if event == "opened":
            db.execute(
                "UPDATE outreach SET opened=1, opened_at=datetime('now') WHERE email=? AND opened=0",
                (email,)
            )
            logger.info("Brevo: opened — %s", email)
        elif event == "click":
            db.execute("UPDATE outreach SET clicked=1 WHERE email=?", (email,))
            logger.info("Brevo: clicked — %s", email)
        elif event in ("hard_bounce", "hardBounce"):
            db.execute(
                "UPDATE outreach SET bounced=1, bounce_type='hard' WHERE email=?", (email,)
            )
            db.execute("UPDATE leads SET email_invalid=1 WHERE email=?", (email,))
            logger.warning("Brevo: hard bounce — %s (marked invalid)", email)
        elif event in ("soft_bounce", "softBounce"):
            db.execute(
                "UPDATE outreach SET bounced=1, bounce_type='soft' WHERE email=?", (email,)
            )
            logger.warning("Brevo: soft bounce — %s", email)
        elif event in ("spam", "unsubscribed"):
            db.execute("UPDATE outreach SET spam_flag=1 WHERE email=?", (email,))
            db.execute(
                "UPDATE leads SET pipeline_stage='unsubscribed' WHERE email=?", (email,)
            )
            logger.warning("Brevo: %s — %s (stopped outreach)", event, email)
    db.commit()
    db.close()
    return {"status": "ok"}


@app.post("/mark-replied")
async def mark_replied(payload: dict):
    """Mark a lead as replied and write a REPLY_RECEIVED event for the sales agent."""
    lead_id = payload.get("lead_id")
    reply_text = payload.get("reply_text", "")
    if not lead_id:
        return {"status": "error", "detail": "lead_id required"}
    db = get_db()
    db.execute("UPDATE outreach SET replied=1, replied_at=datetime('now') WHERE lead_id=?", (lead_id,))
    db.execute("UPDATE leads SET pipeline_stage='replied' WHERE id=? AND pipeline_stage NOT IN ('booked','paid','active_client')", (lead_id,))
    # Write REPLY_RECEIVED so sales agent picks it up for hot follow-up
    lead_row = db.execute("SELECT business_name, email, niche, city FROM leads WHERE id=?", (lead_id,)).fetchone()
    if lead_row:
        db.execute("""
            INSERT INTO event_bus (source_agent, target_agent, event_type, priority, payload)
            VALUES ('agency-outreach', 'agency-sales', 'REPLY_RECEIVED', 1, ?)
        """, (json.dumps({
            "lead_id":       lead_id,
            "business_name": lead_row[0],
            "email":         lead_row[1],
            "niche":         lead_row[2],
            "city":          lead_row[3],
            "email_reply":   reply_text,
        }),))
    db.commit()
    db.close()
    return {"status": "ok", "lead_id": lead_id}


@app.post("/track")
async def track(request: Request, payload: dict):
    db = get_db()
    db.execute(
        "INSERT INTO page_views (page, referrer, source, ua, ip) VALUES (?,?,?,?,?)",
        (
            payload.get("page", "/"),
            payload.get("referrer", ""),
            _parse_source(payload.get("referrer", "")),
            request.headers.get("user-agent", "")[:300],
            request.client.host if request.client else "",
        ),
    )
    db.commit()
    db.close()
    return {"ok": True}


@app.get("/analytics")
def analytics():
    db = get_db()

    # Visits: last 30 days by day
    daily = db.execute("""
        SELECT date(ts) as day, COUNT(*) as visits,
               COUNT(DISTINCT ip) as unique_visitors
        FROM page_views
        WHERE ts >= datetime('now', '-30 days')
        GROUP BY day ORDER BY day
    """).fetchall()

    # Source breakdown
    sources = db.execute("""
        SELECT source, COUNT(*) as visits
        FROM page_views
        WHERE ts >= datetime('now', '-30 days')
        GROUP BY source ORDER BY visits DESC
    """).fetchall()

    # Page breakdown
    pages = db.execute("""
        SELECT page, COUNT(*) as visits
        FROM page_views
        WHERE ts >= datetime('now', '-30 days')
        GROUP BY page ORDER BY visits DESC
    """).fetchall()

    # Chat funnel totals
    funnel = db.execute("""
        SELECT
            COUNT(*) as total_starts,
            SUM(demo_seen) as demo_seen,
            SUM(close_reached) as close_reached,
            SUM(converted) as converted,
            COUNT(DISTINCT CASE WHEN email_captured != '' AND email_captured IS NOT NULL THEN session_id END) as emails_captured
        FROM chat_analytics
    """).fetchone()

    # Recent chat sessions
    recent_chats = db.execute("""
        SELECT session_id, name, business_name, industry,
               started_at, phase_reached, email_captured,
               demo_seen, close_reached, converted
        FROM chat_analytics
        ORDER BY started_at DESC LIMIT 50
    """).fetchall()

    db.close()

    chat_cols = ["session_id","name","business_name","industry","started_at",
                 "phase_reached","email_captured","demo_seen","close_reached","converted"]
    funnel_cols = ["total_starts","demo_seen","close_reached","converted","emails_captured"]

    return {
        "daily": [{"day": r[0], "visits": r[1], "unique": r[2]} for r in daily],
        "sources": [{"source": r[0], "visits": r[1]} for r in sources],
        "pages": [{"page": r[0], "visits": r[1]} for r in pages],
        "chat_funnel": dict(zip(funnel_cols, funnel)) if funnel else {},
        "recent_chats": [dict(zip(chat_cols, r)) for r in recent_chats],
        "total_visits_today": sum(r[1] for r in daily if r[0] == datetime.utcnow().strftime("%Y-%m-%d")),
    }


@app.get("/recent-replies")
def recent_replies(minutes: int = 20):
    """Leads that replied in the last N minutes. Polled by n8n every 15 min."""
    db = get_db()
    cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
    rows = db.execute("""
        SELECT l.id, l.business_name, l.email, l.niche, l.city,
               o.sequence_step, o.replied_at
        FROM leads l
        INNER JOIN outreach o ON o.lead_id = l.id AND o.replied = 1
        WHERE o.replied_at >= ?
        ORDER BY o.replied_at DESC
    """, (cutoff,)).fetchall()
    db.close()
    cols = ["id", "business_name", "email", "niche", "city", "sequence_step", "replied_at"]
    return {"replies": [dict(zip(cols, r)) for r in rows]}


@app.get("/videos")
def list_videos():
    """Uploaded YouTube videos. Used by n8n weekly report."""
    if not VIDEO_DIR.exists():
        return {"videos": []}
    videos = []
    for jf in sorted(VIDEO_DIR.glob("*.json"), reverse=True):
        try:
            meta = json.loads(jf.read_text())
            if meta.get("youtube_url"):
                videos.append({
                    "niche": meta.get("niche", ""),
                    "title": meta.get("title", ""),
                    "youtube_url": meta.get("youtube_url", ""),
                    "created_at": meta.get("created_at", ""),
                })
        except Exception:
            pass
    return {"videos": videos}


@app.get("/health")
def health():
    return {"status": "ok", "pricing_mode": _pricing_mode}


@app.get("/chat/healthcheck")
async def chat_healthcheck():
    """Synthetic end-to-end conversation verifying all chat phases work."""
    import time
    results: dict = {}
    overall_ok = True
    sid = None

    async with httpx.AsyncClient(timeout=30) as client:
        base = "http://127.0.0.1:8080"

        # 1. Start
        t0 = time.time()
        try:
            r = await client.post(f"{base}/chat/start", json={
                "name": "HC", "business_name": "Apex HVAC",
                "industry": "HVAC", "challenge": "missing after-hours calls", "_test": True,
            })
            ms = int((time.time() - t0) * 1000)
            d = r.json()
            ok = r.status_code == 200 and bool(d.get("message"))
            sid = d.get("session_id") if ok else None
            results["start"] = {"ok": ok, "ms": ms}
            if not ok:
                overall_ok = False
        except Exception as e:
            results["start"] = {"ok": False, "error": str(e)}
            overall_ok = False

        if not sid:
            return {"status": "fail", "results": results}

        # Phase progression: turn=0→phase1, turn=1→phase2, turn=2→phase3, phone→phase4
        steps = [
            ("disc1",       "We miss about 5 calls a week, evenings mostly",       1),
            ("disc2→pivot", "Yeah it's a real problem",                            2),
            ("pivot→demo",  "Yes show me",                                         3),
            ("demo",        "What are your service hours?",                        3),
            ("contact→close","Jane Test here, best number is 555-867-5309",        4),
        ]
        for label, msg, expected_phase in steps:
            t0 = time.time()
            try:
                r = await client.post(f"{base}/chat/message", json={"session_id": sid, "message": msg})
                ms = int((time.time() - t0) * 1000)
                d = r.json()
                ok = r.status_code == 200 and bool(d.get("message")) and d.get("phase") == expected_phase
                results[label] = {"ok": ok, "ms": ms, "phase": d.get("phase")}
                if not ok:
                    overall_ok = False
            except Exception as e:
                results[label] = {"ok": False, "error": str(e)}
                overall_ok = False

    # Cleanup — test sessions never hit the DB so just remove from memory
    chat_sessions.pop(sid, None)

    status = "ok" if overall_ok else "fail"
    if not overall_ok:
        failed = [k for k, v in results.items() if not v.get("ok")]
        asyncio.create_task(_discord_post(
            f"⚠️ **Chat healthcheck FAILED** — {', '.join(failed)}\n"
            + "\n".join(f"`{k}`: phase={v.get('phase')} ms={v.get('ms')}" for k, v in results.items())
        ))
    logger.info(f"Chat healthcheck: {status} — {results}")
    return {"status": status, "results": results}


@app.post("/set-pricing-mode")
async def set_pricing_mode(payload: dict):
    """Allow orchestrator to flip pricing mode. mode: 'standard' | 'waive_setup'"""
    global _pricing_mode
    mode = payload.get("mode", "standard")
    if mode not in ("standard", "waive_setup"):
        return {"status": "error", "detail": "mode must be standard or waive_setup"}
    _pricing_mode = mode
    logger.info(f"Pricing mode updated to: {mode}")
    await _discord_post(
        f"**Pricing mode changed** → `{mode}`\n"
        + ("Setup fee waived for new outreach emails this week." if mode == "waive_setup"
           else "Back to standard pricing: $450 setup + $89/mo.")
    )
    return {"status": "ok", "pricing_mode": _pricing_mode}


@app.post("/demo-followup")
async def demo_followup(payload: dict, background_tasks: BackgroundTasks):
    """Send a follow-up email to a lead who completed the demo but didn't convert.
    Called by the frontend when phase >= 4 and the session ends without payment."""
    name     = payload.get("name", "")
    biz      = payload.get("business_name", "")
    email    = payload.get("email", "")
    industry = payload.get("industry", "")
    phone    = payload.get("phone", "")

    if not email:
        return {"status": "skipped", "reason": "no email provided"}

    lead = {"business_name": biz, "email": email, "niche": industry, "city": "", "_step": "demo"}
    subject = f"Your custom demo — {biz}"
    body = (
        f"Hi {name},\n\n"
        f"You just ran through a live demo of what an AI chatbot would look like for {biz}. "
        f"That demo was trained specifically on {industry} businesses — your services, your pricing, your availability.\n\n"
        f"The version on your site would be trained on {biz} specifically — your real prices, your hours, your team.\n\n"
        f"If you want to move forward (or just have questions), here's your demo link to share: {_demo_url(industry)}\n\n"
        f"Setup takes 48 hours. $450 one-time, $89/month, no contract.\n\n"
        f"Alex\nRingCatch"
    )
    background_tasks.add_task(_send_demo_followup_email, lead, subject, body, name, biz)
    return {"status": "queued"}


async def _send_demo_followup_email(lead: dict, subject: str, body: str, name: str, biz: str) -> None:
    ok = await _dispatch_email(lead, body, step=1)
    if ok:
        logger.info(f"Demo follow-up sent to {lead['email']} ({biz})")
        await _discord_post(
            f"**Demo follow-up sent** — {name} / {biz} completed the demo but didn't convert. "
            f"Follow-up email dispatched to {lead['email']}."
        )


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _discord_post(message: str) -> None:
    if not DISCORD_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(DISCORD_URL, json={"content": message})
    except Exception as exc:
        logger.warning(f"Discord post failed: {exc}")


async def _discord_intake_alert(name: str, biz: str, industry: str, challenge: str) -> None:
    lines = [f"**New intake chat started**", f"**{name}** — {biz} ({industry})"]
    if challenge:
        lines.append(f"Challenge: _{challenge}_")
    lines.append(f"View at https://ringcatch.io/book")
    await _discord_post("\n".join(lines))


async def _discord_phase_alert(name: str, biz: str, event: str) -> None:
    msgs = {
        "close-ready": f"**Close-ready lead** — {name} / {biz} just completed the live demo and entered the conversion phase.",
    }
    await _discord_post(msgs.get(event, f"Lead event: {event} — {name} / {biz}"))


async def _send_escalation_alert(session: dict, contact_info: str) -> None:
    """Email Mike when a lead asks to speak with him."""
    lead = session["lead"]
    name = lead.get("name", "Unknown")
    biz  = lead.get("business_name", "Unknown")

    transcript = "\n".join(
        f"{'Alex' if m['role'] == 'alex' else name}: {m['content']}"
        for m in session["history"]
    )

    subject = f"[RingCatch] Callback request: {name} / {biz}"
    body = (
        f"Hey Mike,\n\n"
        f"{name} from {biz} asked to speak with you through the RingCatch chat.\n\n"
        f"Contact info they gave: {contact_info}\n"
        f"Phone (from form): {lead.get('phone', 'not provided')}\n"
        f"Industry: {lead.get('industry', '')}\n"
        f"Challenge: {lead.get('challenge', 'not provided')}\n\n"
        f"--- Chat transcript ---\n{transcript}\n--- End ---\n\n"
        f"Alex"
    )

    if EMAIL_PROVIDER == "brevo" and BREVO_API_KEY:
        payload = {
            "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
            "to": [{"email": MIKE_ALERT_EMAIL, "name": "Mike"}],
            "subject": subject,
            "textContent": body,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.brevo.com/v3/smtp/email",
                json=payload,
                headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            )
        if resp.status_code == 201:
            logger.info(f"Escalation alert sent to {MIKE_ALERT_EMAIL} for {name}")
        else:
            logger.error(f"Escalation alert failed: {resp.status_code} {resp.text[:200]}")
    else:
        logger.info(f"[STUB] Escalation alert for {name}: {contact_info}")


async def _send_step1_to_new_leads() -> None:
    db = get_db()
    sent_today = db.execute(
        "SELECT COUNT(*) FROM outreach WHERE date(sent_at,'localtime')=date('now','localtime')"
    ).fetchone()[0]
    remaining = EMAIL_DAILY_LIMIT - sent_today
    if remaining <= 0:
        logger.info(f"Daily email limit reached ({EMAIL_DAILY_LIMIT}), skipping step-1 sends")
        db.close()
        return
    new = db.execute("""
        SELECT id, email FROM leads
        WHERE email != '' AND email_invalid = 0 AND processed = 0
        LIMIT ?
    """, (remaining,)).fetchall()
    db.close()

    if not new:
        return

    sem = asyncio.Semaphore(2)

    async def _send_one(lead_id: int, email: str) -> None:
        async with sem:
            # DNS check before claiming — skip unresolvable domains immediately
            if not await _validate_email_domain(email):
                db = get_db()
                db.execute(
                    "UPDATE leads SET email_invalid=1, processed=1 WHERE id=?", (lead_id,)
                )
                db.commit()
                db.close()
                return
            # Atomic claim — prevents double-send if two tasks run concurrently
            db = get_db()
            cursor = db.execute(
                "UPDATE leads SET processed=1 WHERE id=? AND processed=0", (lead_id,)
            )
            db.commit()
            claimed = cursor.rowcount > 0
            db.close()
            if not claimed:
                return
            await send_email({"lead_id": lead_id, "step": 1})
            await asyncio.sleep(3)  # stay under Groq RPM limit (~20 emails/min max)

    await asyncio.gather(*[_send_one(lid, email) for (lid, email) in new])


async def _generate_email(lead: dict, step: int) -> str:
    _niche = lead.get("niche", "service")
    _url   = _demo_url(_niche)
    instruction = STEP_INSTRUCTIONS.get(step, STEP_INSTRUCTIONS[1]).format(
        niche=_niche,
        business_name=lead.get("business_name", "your business"),
        demo_url=_url,
    )
    pricing_note = (
        "" if _pricing_mode == "standard"
        else "\nSPECIAL: This week only — no setup fee, just $89/month. Mention this in the email."
    )

    # For step-1, generate a Gemini-personalized opening line unique to this business
    opener_hint = ""
    if step == 1:
        opener = await _personalized_opener(lead)
        if opener:
            opener_hint = (
                f"\nIMPORTANT: Start the email body with this exact sentence (already personalized, do not change it):\n"
                f"\"{opener}\"\n"
                f"Then continue naturally with the rest of the email.\n"
            )

    prompt = (
        f"You are Alex, writing a cold outreach email for RingCatch (ringcatch.io).\n"
        f"RingCatch installs AI chatbots for local service businesses.\n"
        f"The chatbot answers inquiries and captures leads 24/7. $450 setup + $89/month.{pricing_note}\n"
        f"{opener_hint}\n"
        f"Prospect: {lead.get('business_name', '')}\n"
        f"Industry: {lead.get('niche', '')}\n"
        f"Location: {lead.get('city', '')}\n\n"
        f"Task: {instruction}\n\n"
        f"Output only the email body text. No subject line. No markdown. No placeholders. No angle brackets."
    )

    FALLBACK_BODIES = {
        1: (
            f"Hi there,\n\n"
            f"Quick question — when a customer contacts {lead.get('business_name', 'your business')} "
            f"after hours, what typically happens?\n\n"
            f"We install AI chatbots for local {lead.get('niche', 'service')} businesses that answer "
            f"questions and capture leads 24/7. Live on your site in 48 hours.\n\n"
            f"See exactly how it would work for {lead.get('business_name', 'your business')}: {_url}\n\n"
            f"Alex\nRingCatch"
        ),
        2: (
            f"Hi,\n\nJust following up on my note from a couple days ago about AI-assisted lead capture "
            f"for {lead.get('business_name', 'your business')}.\n\n"
            f"60-second demo tailored to {lead.get('business_name', 'you')}: {_url}\n\nAlex"
        ),
        3: (
            f"Last note from me on this — if the timing's ever right: {_url}\n\nAlex"
        ),
    }
    try:
        return await _llm(prompt)
    except Exception as exc:
        logger.warning(f"_generate_email step={step} LLM failed ({exc}), using fallback")
        return FALLBACK_BODIES.get(step, FALLBACK_BODIES[1])


def _build_email_html(body: str, video_url: str | None = None, niche: str = "", demo_url: str = "") -> str:
    """Wrap plain-text email body in branded RingCatch HTML template."""
    _cta = demo_url or DEMO_BASE_URL
    paragraphs = [p.strip() for p in body.split("\n") if p.strip()]
    html_body = "".join(
        f"<p style='margin:0 0 14px 0;color:#1e293b;font-size:0.93rem;line-height:1.75;'>{p}</p>"
        for p in paragraphs
        if DEMO_BASE_URL not in p and "Alex" not in p[:10]
    )
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1.0'></head>"
        "<body style='margin:0;padding:0;background:#f1f5f9;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;'>"
        "<table width='100%' cellpadding='0' cellspacing='0' style='background:#f1f5f9;padding:32px 16px;'>"
        "<tr><td align='center'>"
        "<table width='560' cellpadding='0' cellspacing='0' style='max-width:560px;background:#ffffff;"
        "border-radius:12px;border:1px solid #e2e8f0;overflow:hidden;'>"
        # Header
        "<tr><td style='background:#0b0b14;padding:16px 28px;'>"
        "<table cellpadding='0' cellspacing='0' style='width:100%;'><tr>"
        # RC avatar circle
        "<td style='width:40px;vertical-align:middle;padding-right:12px;'>"
        "<div style='width:36px;height:36px;background:linear-gradient(135deg,#22d3ee,#0891b2);"
        "border-radius:50%;display:inline-block;text-align:center;line-height:36px;"
        "font-size:0.78rem;font-weight:900;color:#0b0b14;letter-spacing:-0.5px;'>RC</div>"
        "</td>"
        # Wordmark + tagline
        "<td style='vertical-align:middle;'>"
        "<span style='font-size:1rem;font-weight:900;color:#ffffff;letter-spacing:-0.5px;display:block;'>"
        "RING<span style='color:#22d3ee;'>CATCH</span></span>"
        "<span style='font-size:0.68rem;color:#64748b;'>AI chatbots for local businesses</span>"
        "</td>"
        "</tr></table>"
        "</td></tr>"
        # Body
        f"<tr><td style='padding:28px 28px 8px;'>{html_body}</td></tr>"
        # Video link (niche-specific YouTube short, only when available)
        + (
            f"<tr><td style='padding:0 28px 16px;'>"
            f"<a href='{video_url}' style='display:block;background:#f8fafc;border:1px solid #e2e8f0;"
            f"border-radius:8px;padding:14px 16px;text-decoration:none;'>"
            f"<span style='display:block;font-size:0.75rem;color:#64748b;font-weight:600;letter-spacing:0.05em;margin-bottom:4px;'>📹 WATCH</span>"
            f"<span style='font-size:0.9rem;font-weight:700;color:#1e293b;'>"
            f"How AI Chatbots Help {niche or 'Local'} Businesses</span>"
            f"<span style='display:block;font-size:0.78rem;color:#22d3ee;margin-top:3px;'>youtube.com ↗</span>"
            f"</a></td></tr>"
            if video_url else ""
        )
        # CTA button
        + f"<tr><td style='padding:8px 28px 28px;'>"
        f"<a href='{_cta}' style='display:inline-block;background:#22d3ee;color:#0b0b14;"
        f"font-weight:800;font-size:0.88rem;padding:12px 26px;border-radius:7px;text-decoration:none;'>"
        f"See it working for your business →</a>"
        f"<p style='margin:10px 0 0;font-size:0.76rem;color:#94a3b8;'>Takes 60 seconds. No signup.</p>"
        f"</td></tr>"
        # Footer
        "<tr><td style='background:#f8fafc;padding:16px 28px;border-top:1px solid #e2e8f0;'>"
        "<p style='margin:0;font-size:0.74rem;color:#64748b;line-height:1.6;'>"
        "Alex · RingCatch · <a href='https://ringcatch.io' style='color:#22d3ee;text-decoration:none;'>ringcatch.io</a><br>"
        "222 Tackle Box Dr, Troutman NC 28166<br>"
        "You're receiving this because you operate a local service business.<br>"
        "<a href='mailto:alex@ringcatch.io?subject=unsubscribe' style='color:#94a3b8;'>Unsubscribe</a>"
        "</p></td></tr>"
        "</table></td></tr></table></body></html>"
    )


async def _dispatch_email(lead: dict, body: str, step: int) -> bool:
    raw_subject = STEP_SUBJECTS.get(step, STEP_SUBJECTS[1])
    if raw_subject == "__NICHE__":
        niche_key = lead.get("niche", "your business")
        raw_subject = next(
            (v for k, v in NICHE_SUBJECTS_STEP1.items() if k.lower() in niche_key.lower()),
            "A quick idea for {name} in {city}",
        )
    
    subject = raw_subject.format(
        name=lead.get("business_name", "your business"),
        niche=lead.get("niche", "service"),
        city=lead.get("city", "your area")
    )

    if EMAIL_PROVIDER == "stub":
        return _stub_send(lead, subject, body, step)

    if EMAIL_PROVIDER == "brevo":
        return await _send_via_brevo(lead, subject, body, step)

    # TODO: add "resend" branch here when switching providers
    # if EMAIL_PROVIDER == "resend":
    #     return await _send_via_resend(lead, subject, body)

    logger.error(f"Unknown EMAIL_PROVIDER '{EMAIL_PROVIDER}' — no email sent")
    return False


def _stub_send(lead: dict, subject: str, body: str, step: int) -> bool:
    """Development stub — logs the email instead of sending it.

    Replace this with a real provider call by setting EMAIL_PROVIDER in .env
    to "brevo" or "resend" and wiring up the corresponding function below.
    """
    logger.info(
        "[STUB] Would send step-%d email\n"
        "  To:      %s <%s>\n"
        "  Subject: %s\n"
        "  Body:\n%s",
        step, lead["business_name"], lead["email"], subject,
        "\n".join(f"    {line}" for line in body.splitlines()),
    )
    return True  # treated as success so the DB row is written


# ── TODO: implement your chosen provider below ────────────────────────────────
#
# BREVO (Sendinblue) — free tier, 300 emails/day
# Docs: https://developers.brevo.com/reference/sendtransacemail
#
async def _send_via_brevo(lead: dict, subject: str, body: str, step: int = 1) -> bool:
    # Skip invalid / unsubscribed emails
    db = get_db()
    skip = db.execute(
        "SELECT email_invalid FROM leads WHERE email=?", (lead["email"],)
    ).fetchone()
    db.close()
    if skip and skip[0]:
        logger.info("Skipping bounced/invalid email: %s", lead["email"])
        return False

    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "replyTo": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to": [{"email": lead["email"], "name": lead.get("business_name", "")}],
        "subject": subject,
        "textContent": body + (
            f"\n\nP.S. See how it works for {lead.get('niche', 'local')} businesses: {get_niche_video_url(lead.get('niche', ''))}"
            if step == 1 and get_niche_video_url(lead.get("niche", "")) else ""
        ),
        "htmlContent": _build_email_html(
            body,
            video_url=get_niche_video_url(lead.get("niche", "")) if step == 1 else None,
            niche=lead.get("niche", ""),
            demo_url=_demo_url(lead.get("niche", "")),
        ),
        "tags": [lead.get("niche", "outreach"), f"step-{lead.get('_step', 1)}"],
        "trackOpens": 1,
        "trackClicks": 1,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.brevo.com/v3/smtp/email",
            json=payload,
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        )
    if resp.status_code != 201:
        logger.error(f"Brevo error {resp.status_code}: {resp.text[:200]}")
    return resp.status_code == 201
#
# RESEND — https://resend.com/docs/api-reference/emails/send-email
#
# async def _send_via_resend(lead: dict, subject: str, body: str) -> bool:
#     # TODO: set EMAIL_PROVIDER=resend in .env and fill RESEND_API_KEY
#     RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
#     async with httpx.AsyncClient(timeout=30) as client:
#         resp = await client.post(
#             "https://api.resend.com/emails",
#             json={
#                 "from": f"{BREVO_SENDER_NAME} <{BREVO_SENDER_EMAIL}>",
#                 "to": [lead["email"]],
#                 "subject": subject,
#                 "text": body,
#             },
#             headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
#         )
#     return resp.status_code == 200
