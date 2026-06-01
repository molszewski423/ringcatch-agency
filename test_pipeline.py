#!/usr/bin/env python3
"""
RingCatch end-to-end pipeline mock test
========================================
Simulates the complete customer journey for Arctic Air HVAC / Dave Kowalski.
Runs on the host — no containers required. Only external dep: Ollama on localhost.

Usage:
    python3 ~/agency/test_pipeline.py

Optional deps (only for PDF generation):
    pip install reportlab

Environment overrides:
    OLLAMA_BASE_URL   default: http://localhost:11434
    OLLAMA_MODEL      default: gemma4:26b
"""

import contextlib
import json
import os
import sqlite3
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# ── Terminal colours ──────────────────────────────────────────────────────────
G  = "\033[92m"   # green
R  = "\033[91m"   # red
Y  = "\033[93m"   # yellow
C  = "\033[96m"   # cyan
B  = "\033[1m"    # bold
D  = "\033[2m"    # dim
RS = "\033[0m"    # reset

def ok(m):    print(f"  {G}✓{RS} {m}")
def err(m):   print(f"  {R}✗ {RS}{R}{m}{RS}")
def warn(m):  print(f"  {Y}!{RS} {m}")
def inf(m):   print(f"  {D}·{RS} {m}")

def hdr(title: str) -> None:
    bar = "─" * 64
    print(f"\n{B}{C}{bar}\n  {title}\n{bar}{RS}")

def email_box(label: str, subject: str, to: str, body: str) -> None:
    W = 62
    lines = []
    lines.append(f"To:       {to}")
    lines.append(f"Subject:  {subject}")
    lines.append("")
    for raw in body.splitlines():
        raw = raw or ""
        while len(raw) > W:
            lines.append(raw[:W])
            raw = raw[W:]
        lines.append(raw)

    print(f"\n  {B}{label}{RS}")
    print(f"  ╔{'═' * W}╗")
    for line in lines:
        print(f"  ║ {line:<{W}} ║")
    print(f"  ╚{'═' * W}╝")

# ── Stage runner ──────────────────────────────────────────────────────────────
RESULTS: dict[str, bool] = {}

@contextlib.contextmanager
def stage(name: str):
    hdr(name)
    t0 = time.time()
    try:
        yield
        RESULTS[name] = True
        print(f"\n{G}{B}  ✓ PASS{RS}  ({time.time() - t0:.1f}s)")
    except AssertionError as e:
        RESULTS[name] = False
        err(str(e))
        print(f"\n{R}{B}  ✗ FAIL{RS}  ({time.time() - t0:.1f}s)")
    except Exception as e:
        RESULTS[name] = False
        err(f"{type(e).__name__}: {e}")
        traceback.print_exc()
        print(f"\n{R}{B}  ✗ FAIL{RS}  ({time.time() - t0:.1f}s)")

# ── Configuration ─────────────────────────────────────────────────────────────
# Use localhost:11434 on the host — containers use host.containers.internal
OLLAMA_URL   = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:26b")

BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
TEST_DB   = DATA_DIR / "test_pipeline.db"
OUT_DIR   = DATA_DIR / "deliverables" / "dave_kowalski"
CLIENT_ID = "dave-kowalski"

# ── Mock customer ─────────────────────────────────────────────────────────────
DAVE: dict = {
    "business_name": "Arctic Air HVAC",
    "owner_name":    "Dave Kowalski",
    "email":         "test@example.com",
    "phone":         "704-555-0182",
    "website":       "https://arcticairhvac.com",
    "city":          "Charlotte, NC",
    "niche":         "HVAC",
    "domain":        "arcticairhvac.com",
    "address":       "Charlotte, NC",
    "scraped_date":  datetime.now().date().isoformat(),
}

# Shared state mutated by stages
STATE: dict = {
    "lead_id":               None,
    "emails":                {},
    "reply_body":            None,
    "qa_pairs":              [],
    "loom_script":           None,
    "stripe_link":           None,
    "testimonial_scheduled": False,
}

STEP_SUBJECTS = {
    1: "Quick idea for {name}",
    2: "Re: AI chatbot for {name}",
    3: "Last thought — {name}",
}

FALLBACK_EMAILS = {
    1: (
        "Hey Dave,\n\n"
        "How many HVAC calls does Arctic Air miss after hours?\n\n"
        "We set up AI chatbots for local contractors that answer leads 24/7 — "
        "so you wake up to booked jobs instead of voicemails. "
        "$450 one-time, done in 48 hours.\n\n"
        "Worth a quick chat?\n\n"
        "Alex"
    ),
    2: (
        "Hi Dave,\n\n"
        "Reaching back out from a few days ago about helping Arctic Air "
        "catch more after-hours leads. Just checking if the timing was off.\n\n"
        "Alex"
    ),
    3: (
        "Hi Dave,\n\n"
        "Last note — if you're ever curious, ringcatch.io has a live demo. "
        "No pressure at all.\n\n"
        "Alex"
    ),
}

# ── Database helpers ──────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(TEST_DB)
    db.row_factory = sqlite3.Row
    return db

def init_db(db: sqlite3.Connection) -> None:
    db.executescript("""
        DROP TABLE IF EXISTS leads;
        DROP TABLE IF EXISTS outreach;
        DROP TABLE IF EXISTS bookings;
        DROP TABLE IF EXISTS payments;
        DROP TABLE IF EXISTS deliveries;
        DROP TABLE IF EXISTS testimonial_requests;

        CREATE TABLE leads (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            business_name TEXT NOT NULL,
            owner_name    TEXT,
            email         TEXT UNIQUE,
            phone         TEXT,
            website       TEXT,
            domain        TEXT,
            address       TEXT,
            city          TEXT,
            niche         TEXT,
            scraped_date  TEXT,
            processed     INTEGER DEFAULT 0
        );
        CREATE TABLE outreach (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id        INTEGER,
            email          TEXT,
            email_body     TEXT,
            sequence_step  INTEGER DEFAULT 1,
            sent_at        TEXT,
            replied        INTEGER DEFAULT 0,
            FOREIGN KEY(lead_id) REFERENCES leads(id)
        );
        CREATE TABLE bookings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id    TEXT,
            client_name  TEXT,
            niche        TEXT,
            booking_time TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   TEXT,
            client_name TEXT,
            amount      INTEGER,
            status      TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE deliveries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   TEXT,
            client_name TEXT,
            niche       TEXT,
            status      TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE testimonial_requests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   TEXT UNIQUE,
            client_name TEXT,
            email       TEXT,
            niche       TEXT,
            send_after  TEXT,
            sent        INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    db.commit()

# ── Ollama helper ─────────────────────────────────────────────────────────────
def ollama(prompt: str, timeout: int = 180) -> str:
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["response"].strip()

def ollama_reachable() -> bool:
    try:
        urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5)
        return True
    except Exception:
        return False

def email_prompt(step: int) -> str:
    instructions = {
        1: (
            "Write a 3-line cold email from Alex at RingCatch (ringcatch.io). "
            "RingCatch sets up AI chatbots for local HVAC businesses so they never "
            "miss a lead — $450 setup, $89/month. Lead with the pain of missed "
            "after-hours calls. No fluff. Curiosity-based. Sign off as 'Alex' only."
        ),
        2: (
            "Write a 2-sentence follow-up from Alex at RingCatch. Reference that you "
            "reached out a few days ago about catching more leads with a chatbot. "
            "Light and curious, not pushy. Sign off as 'Alex'."
        ),
        3: (
            "Write a final 2-sentence email from Alex at RingCatch. Mention "
            "ringcatch.io so they can look on their own time. Zero pressure. "
            "Sign off as 'Alex'."
        ),
    }
    return (
        f"You are Alex writing a cold outreach email for RingCatch (ringcatch.io).\n"
        f"RingCatch installs AI chatbots for HVAC businesses. $450 setup + $89/month.\n\n"
        f"Prospect: {DAVE['business_name']}\n"
        f"Owner: {DAVE['owner_name']}\n"
        f"Location: {DAVE['city']}\n\n"
        f"Task: {instructions[step]}\n\n"
        f"Output only the email body. No subject line. No markdown. No placeholders."
    )

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 1 — Lead Engine
# ═══════════════════════════════════════════════════════════════════════════════
S1 = "Stage 1 · Lead Engine"
with stage(S1):
    db = get_db()
    init_db(db)
    ok(f"Fresh schema in {TEST_DB}")

    db.execute("""
        INSERT INTO leads
            (business_name, owner_name, email, phone, website,
             domain, address, city, niche, scraped_date)
        VALUES
            (:business_name, :owner_name, :email, :phone, :website,
             :domain, :address, :city, :niche, :scraped_date)
    """, DAVE)
    db.commit()

    row = db.execute("SELECT * FROM leads WHERE email=?", (DAVE["email"],)).fetchone()
    assert row is not None, "Row not found after INSERT"
    STATE["lead_id"] = row["id"]

    for field in ("business_name", "owner_name", "email", "phone", "city", "niche", "scraped_date"):
        val = row[field]
        assert val, f"Field '{field}' is empty after insert"
        inf(f"{field:<22} {val}")

    ok(f"Arctic Air HVAC stored — lead_id={STATE['lead_id']}")
    db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 2 — LLM Personalization  (emails printed immediately after)
# ═══════════════════════════════════════════════════════════════════════════════
S2 = "Stage 2 · LLM Personalization"
with stage(S2):
    inf(f"Ollama: {OLLAMA_URL}")
    inf(f"Model:  {OLLAMA_MODEL}")

    if not ollama_reachable():
        raise RuntimeError(
            f"Ollama not responding at {OLLAMA_URL}\n"
            f"    Is it running?  systemctl --user status ollama\n"
            f"    Falling back to template emails for downstream stages."
        )
    ok("Ollama reachable")

    step_labels = {1: "Day 1 — cold email", 2: "Day 3 — follow-up", 3: "Day 7 — final touch"}
    for step in (1, 2, 3):
        inf(f"Generating {step_labels[step]}…")
        body = ollama(email_prompt(step), timeout=180)
        assert len(body) > 20, f"Step {step} response suspiciously short: {body!r}"
        STATE["emails"][step] = body
        ok(f"Step {step} done  ({len(body)} chars)")

# ── Print all three emails prominently ────────────────────────────────────────
emails = STATE["emails"] if STATE["emails"] else FALLBACK_EMAILS
if not STATE["emails"]:
    print(f"\n{Y}{B}  ⚠ Ollama failed — showing fallback template emails{RS}")

step_labels = {1: "EMAIL 1/3 · Day 1 — Cold Email",
               2: "EMAIL 2/3 · Day 3 — Follow-up",
               3: "EMAIL 3/3 · Day 7 — Final Touch"}

for step in (1, 2, 3):
    email_box(
        label   = step_labels[step],
        subject = STEP_SUBJECTS[step].format(name=DAVE["business_name"]),
        to      = DAVE["email"],
        body    = emails[step],
    )

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 3 — Outreach Stub (EMAIL_PROVIDER=stub simulation)
# ═══════════════════════════════════════════════════════════════════════════════
S3 = "Stage 3 · Outreach Stub"
with stage(S3):
    assert STATE["lead_id"] is not None, "No lead_id — Stage 1 must pass first"
    db = get_db()
    base = datetime.now()
    send_at = {1: base, 2: base + timedelta(days=3), 3: base + timedelta(days=7)}

    for step in (1, 2, 3):
        body    = emails.get(step, FALLBACK_EMAILS[step])
        subject = STEP_SUBJECTS[step].format(name=DAVE["business_name"])
        ts      = send_at[step]

        inf(f"[STUB] step={step}  date={ts.strftime('%Y-%m-%d')}  "
            f"to={DAVE['email']}  subject=\"{subject}\"")

        db.execute("""
            INSERT INTO outreach (lead_id, email, email_body, sequence_step, sent_at)
            VALUES (?, ?, ?, ?, ?)
        """, (STATE["lead_id"], DAVE["email"], body, step, ts.isoformat()))

    db.execute("UPDATE leads SET processed=1 WHERE id=?", (STATE["lead_id"],))
    db.commit()
    db.close()
    ok("3 sequence steps logged in outreach table — lead marked processed")
    inf("EMAIL_PROVIDER=stub: no real emails sent")

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 4 — Reply Simulation
# ═══════════════════════════════════════════════════════════════════════════════
S4 = "Stage 4 · Reply Simulation"
with stage(S4):
    assert STATE["lead_id"] is not None, "No lead_id — Stage 1 must pass first"
    db = get_db()

    db.execute(
        "UPDATE outreach SET replied=1 WHERE lead_id=? AND sequence_step=1",
        (STATE["lead_id"],),
    )
    db.commit()
    ok("Dave's step-1 outreach row marked replied=1")

    # Simulate what n8n's reply-detection webhook would fire
    n8n_payload = {
        "event":       "reply_received",
        "lead_id":     STATE["lead_id"],
        "email":       DAVE["email"],
        "business":    DAVE["business_name"],
        "reply_text":  "Yes I'm interested",
        "timestamp":   datetime.now().isoformat(),
    }
    inf("n8n webhook payload (simulated POST to n8n/webhook/reply-received):")
    for k, v in n8n_payload.items():
        inf(f"  {k:<18} {v}")

    # Generate the booking-link reply
    reply_prompt = (
        f"You are Alex, texting back {DAVE['owner_name']} at {DAVE['business_name']}, "
        f"an HVAC company in {DAVE['city']}, who just replied 'Yes I'm interested' to "
        f"your cold email about RingCatch's AI chatbot.\n\n"
        f"Write a reply that sounds like a helpful neighbor texting back — not a salesperson. "
        f"Casual, warm, zero corporate speak. No 'I am certain', no 'I would like to', "
        f"no 'please do not hesitate'. Just real talk.\n"
        f"Include this booking link naturally: https://book.ringcatch.io/alex\n"
        f"Under 4 sentences. Sign off as 'Alex'. Email body only. No markdown."
    )

    inf("Generating booking-link reply via Ollama…")
    try:
        reply_body = ollama(reply_prompt, timeout=90)
        ok(f"Reply generated  ({len(reply_body)} chars)")
    except Exception as e:
        warn(f"Ollama unavailable ({e}) — using template reply")
        reply_body = (
            f"Hey {DAVE['owner_name']}, sounds like we should talk!\n\n"
            f"Grab a quick 15-min slot whenever works for you: "
            f"https://book.ringcatch.io/alex — I can walk you through exactly "
            f"how it works for HVAC shops.\n\nAlex"
        )

    STATE["reply_body"] = reply_body

    email_box(
        label   = "  AUTO-REPLY to Dave (booking link)",
        subject = f"Re: Quick idea for {DAVE['business_name']}",
        to      = DAVE["email"],
        body    = reply_body,
    )
    inf("Cal.com booking link: https://book.ringcatch.io/alex")
    db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 5 — Booking Test
# ═══════════════════════════════════════════════════════════════════════════════
S5 = "Stage 5 · Booking Test"
with stage(S5):
    db = get_db()
    booking_time = (datetime.now() + timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    db.execute("""
        INSERT INTO bookings (client_id, client_name, niche, booking_time)
        VALUES (?, ?, ?, ?)
    """, (CLIENT_ID, DAVE["business_name"], DAVE["niche"], booking_time.isoformat()))
    db.commit()
    ok(f"Booking inserted — {booking_time.strftime('%A %Y-%m-%d at 10:00 AM')}")

    # Cal.com → billing webhook payload
    cal_webhook = {
        "uid":            CLIENT_ID,
        "organizer_name": DAVE["business_name"],
        "attendee_name":  DAVE["owner_name"],
        "attendee_email": DAVE["email"],
        "niche":          DAVE["niche"],
        "start_time":     booking_time.isoformat(),
    }
    inf("Cal.com → billing webhook payload (simulated):")
    for k, v in cal_webhook.items():
        inf(f"  {k:<22} {v}")

    stripe_setup_link = os.environ.get(
        "STRIPE_SETUP_LINK",
        "https://buy.stripe.com/test_bJe4gzbR5a6d6wec8u4Rq00",
    )
    payment_link = f"{stripe_setup_link}?client_reference_id={CLIENT_ID}"
    STATE["stripe_link"] = payment_link
    ok("Stripe setup payment link (sandbox):")
    inf(f"  {payment_link}")
    inf(f"  → Send to {DAVE['owner_name']} at {DAVE['email']}")

    print(f"\n  {B}Test card — use in Stripe checkout to simulate payment:{RS}")
    for label, value in [
        ("Card number", "4242 4242 4242 4242"),
        ("Expiry",      "12/29"),
        ("CVC",         "242"),
        ("Name",        "Dave Kowalski"),
    ]:
        inf(f"  {label:<18} {value}")
    db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 6 — Payment + Delivery
# ═══════════════════════════════════════════════════════════════════════════════
S6 = "Stage 6 · Payment + Delivery"
with stage(S6):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    db = get_db()

    db.execute("""
        INSERT INTO payments (client_id, client_name, amount, status)
        VALUES (?, ?, 45000, 'paid')
    """, (CLIENT_ID, DAVE["business_name"]))
    db.commit()
    ok("Payment logged — $450.00 confirmed (Stripe webhook simulated)")

    # ── 6a: Chatbot FAQ ───────────────────────────────────────────────
    faq_prompt = (
        f"Generate 8 common customer questions and concise answers for an HVAC "
        f"business called {DAVE['business_name']} in {DAVE['city']}.\n\n"
        f'Return ONLY a JSON array: [{{"q":"...","a":"..."}}]\n'
        f"No markdown. No explanation. Valid JSON only."
    )
    inf("Generating HVAC FAQ via Ollama…")
    try:
        faq_raw = ollama(faq_prompt, timeout=150)
        if "```" in faq_raw:
            faq_raw = faq_raw.split("```")[1].lstrip("json").strip()
        qa_pairs: list[dict] = json.loads(faq_raw)
        assert isinstance(qa_pairs, list) and len(qa_pairs) > 0
        ok(f"FAQ generated — {len(qa_pairs)} Q&A pairs")
    except Exception as e:
        warn(f"FAQ generation issue ({e}) — using HVAC fallback set")
        qa_pairs = [
            {"q": "Do you offer 24/7 emergency service?",
             "a": "Yes — Arctic Air provides round-the-clock emergency HVAC service in Charlotte."},
            {"q": "What areas do you serve?",
             "a": "We serve Charlotte and surrounding areas within 30 miles."},
            {"q": "How quickly can you respond to an emergency call?",
             "a": "We aim to have a technician on-site within 2-4 hours for emergencies."},
            {"q": "Do you service all HVAC brands?",
             "a": "Yes, our certified techs service all major brands and models."},
            {"q": "Do you offer maintenance plans?",
             "a": "Yes — seasonal tune-up plans starting at $99/visit."},
            {"q": "Are your technicians licensed?",
             "a": "All technicians are fully licensed, insured, and background-checked."},
            {"q": "Can I get a free estimate?",
             "a": "Absolutely — chat with us here or call 704-555-0182 for a free estimate."},
            {"q": "What payment methods do you accept?",
             "a": "All major credit cards, check, and financing options available."},
        ]
    STATE["qa_pairs"] = qa_pairs

    # ── 6b: Botpress flow JSON ────────────────────────────────────────
    flow = {
        "version": "0.1",
        "name": f"{DAVE['business_name']} Chatbot",
        "niche": DAVE["niche"],
        "generated_at": datetime.now().isoformat(),
        "flows": [{
            "name": "main",
            "startNode": "entry",
            "nodes": [
                {
                    "name": "entry",
                    "type": "standard",
                    "onEnter": [],
                    "onReceive": [{"type": "say", "message":
                        f"Hi! I'm the virtual assistant for {DAVE['business_name']}. "
                        f"How can I help you today?"}],
                    "next": [{"condition": "true", "node": "faq_router"}],
                },
                {
                    "name": "faq_router",
                    "type": "standard",
                    "onEnter": [],
                    "onReceive": [],
                    "next": [{"condition": "true", "node": "fallback"}],
                },
                {
                    "name": "fallback",
                    "type": "standard",
                    "onEnter": [],
                    "onReceive": [{"type": "say", "message":
                        f"Let me get a team member for you. Call us at {DAVE['phone']} "
                        f"or leave your name and we'll call you right back."}],
                    "next": [],
                },
                *[{
                    "name": f"faq_{i}",
                    "type": "standard",
                    "onEnter": [],
                    "onReceive": [{"type": "say", "message": p["a"]}],
                    "next": [{"condition": "true", "node": "faq_router"}],
                } for i, p in enumerate(qa_pairs)],
            ],
        }],
        "faq_pairs": qa_pairs,
    }

    flow_path = OUT_DIR / "botpress-flow.json"
    flow_path.write_text(json.dumps(flow, indent=2))
    ok(f"Botpress flow JSON → {flow_path.name}  ({flow_path.stat().st_size:,} bytes)")

    # ── 6c: Loom handoff script ───────────────────────────────────────
    loom_prompt = (
        f"Write a Loom video handoff script for {DAVE['owner_name']} at "
        f"{DAVE['business_name']}, an HVAC company in {DAVE['city']}.\n\n"
        f"Use these timestamps:\n"
        f"[0:00] What the chatbot does and why it matters for HVAC businesses\n"
        f"[0:30] How to test it — type a question, see the response\n"
        f"[1:00] What happens when a new lead messages after hours\n"
        f"[1:45] How to edit an FAQ answer in Botpress (30-second task)\n"
        f"[2:15] Support: contact Alex at alex@ringcatchai.com or ringcatch.io\n\n"
        f"Warm, conversational tone. Under 3 minutes. Plain text only."
    )
    inf("Generating Loom script via Ollama…")
    try:
        loom_script = ollama(loom_prompt, timeout=150)
        ok(f"Loom script generated  ({len(loom_script)} chars)")
    except Exception as e:
        warn(f"Loom generation issue ({e}) — using template")
        loom_script = f"""Loom Handoff — {DAVE['business_name']}
Presenter: Alex · RingCatch · ringcatch.io

[0:00]
Hey {DAVE['owner_name']}, welcome to RingCatch! Quick 2-minute walkthrough of
your new AI chatbot for Arctic Air HVAC.

[0:30]
Let's test it. Go to your website, click the chat bubble, and type
"Do you offer emergency service?" — watch the bot answer in under a second
with your custom response.

[1:00]
When a lead messages after hours, the bot captures their info and you get
an email alert. Every conversation is saved in your Botpress dashboard
so nothing falls through the cracks.

[1:45]
Need to update an answer? Log into Botpress, find the FAQ node, click Edit,
type the new answer, Save. Takes 30 seconds and goes live immediately.

[2:15]
Questions anytime — alex@ringcatchai.com, usually same-day response.
Welcome to RingCatch. Enjoy the leads!
"""
    STATE["loom_script"] = loom_script
    loom_path = OUT_DIR / "loom-script.txt"
    loom_path.write_text(loom_script)
    ok(f"Loom script → {loom_path.name}  ({loom_path.stat().st_size:,} bytes)")

    # ── 6d: Onboarding PDF ────────────────────────────────────────────
    pdf_path = OUT_DIR / "onboarding.pdf"
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

        doc = SimpleDocTemplate(str(pdf_path), pagesize=letter,
                                rightMargin=inch, leftMargin=inch,
                                topMargin=inch, bottomMargin=inch)
        sty = getSampleStyleSheet()
        H1  = ParagraphStyle("H1",  parent=sty["Heading1"], fontSize=20, spaceAfter=14)
        H2  = ParagraphStyle("H2",  parent=sty["Heading2"], fontSize=13, spaceAfter=8)
        BD  = sty["BodyText"]

        story = [
            Paragraph("AI Chatbot Onboarding Guide", H1),
            Paragraph(f"<b>Client:</b> {DAVE['business_name']}", BD),
            Paragraph(f"<b>Contact:</b> {DAVE['owner_name']}", BD),
            Paragraph(f"<b>Location:</b> {DAVE['city']}", BD),
            Paragraph(f"<b>Industry:</b> HVAC", BD),
            Paragraph(f"<b>Prepared by:</b> Alex · RingCatch · alex@ringcatchai.com · ringcatch.io", BD),
            Spacer(1, 0.3 * inch),
            Paragraph("What Your Chatbot Does", H2),
            Paragraph(
                f"Your RingCatch chatbot is built specifically for {DAVE['business_name']} "
                f"and configured for the HVAC industry. It handles common customer questions "
                f"24/7 — including nights, weekends, and holidays — so your team wakes up to "
                f"booked appointments instead of voicemails.",
                BD,
            ),
            Spacer(1, 0.2 * inch),
            Paragraph("Pre-Loaded FAQ Responses", H2),
        ]

        for pair in qa_pairs:
            story.append(Paragraph(f"<b>Q: {pair['q']}</b>", BD))
            story.append(Paragraph(f"A: {pair['a']}", BD))
            story.append(Spacer(1, 0.09 * inch))

        story += [
            Spacer(1, 0.3 * inch),
            Paragraph("Next Steps", H2),
            *[Paragraph(s, BD) for s in [
                "1. Import botpress-flow.json into your Botpress workspace.",
                "2. Paste the Botpress embed snippet onto your website.",
                "3. Open the chat widget and type 'hello' to confirm it's live.",
                "4. Watch the Loom walkthrough (link sent separately).",
                "5. Questions? Email alex@ringcatchai.com — same-day response.",
            ]],
        ]

        doc.build(story)
        ok(f"Onboarding PDF → {pdf_path.name}  ({pdf_path.stat().st_size:,} bytes)")

    except ImportError:
        warn("reportlab not installed — PDF skipped")
        warn("pip install reportlab  then re-run to generate the PDF")

    db.execute("""
        INSERT INTO deliveries (client_id, client_name, niche, status)
        VALUES (?, ?, ?, 'delivered')
    """, (CLIENT_ID, DAVE["business_name"], DAVE["niche"]))
    db.commit()
    ok("Delivery record inserted")

    # ── 6e: Testimonial request ───────────────────────────────────────
    send_after = (datetime.now() + timedelta(days=7)).isoformat()
    db.execute("""
        INSERT OR IGNORE INTO testimonial_requests
            (client_id, client_name, email, niche, send_after)
        VALUES (?, ?, ?, ?, ?)
    """, (CLIENT_ID, DAVE["business_name"], DAVE["email"], DAVE["niche"], send_after))
    db.commit()
    STATE["testimonial_scheduled"] = True
    ok(f"Testimonial request scheduled — {send_after[:10]} (7 days post-delivery)")

    db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 7 — Dashboard & Final State
# ═══════════════════════════════════════════════════════════════════════════════
S7 = "Stage 7 · Dashboard & Final State"
with stage(S7):
    db = get_db()

    lead_row  = db.execute("SELECT * FROM leads                  WHERE email=?",      (DAVE["email"],)).fetchone()
    outs      = db.execute("SELECT * FROM outreach               WHERE lead_id=?",    (STATE["lead_id"] or -1,)).fetchall()
    book_row  = db.execute("SELECT * FROM bookings               WHERE client_id=?",  (CLIENT_ID,)).fetchone()
    pay_row   = db.execute("SELECT * FROM payments               WHERE client_id=?",  (CLIENT_ID,)).fetchone()
    deliv_row = db.execute("SELECT * FROM deliveries             WHERE client_id=?",  (CLIENT_ID,)).fetchone()
    test_row  = db.execute("SELECT * FROM testimonial_requests   WHERE client_id=?",  (CLIENT_ID,)).fetchone()

    assert lead_row,         "leads: no record for test@example.com"
    assert len(outs) == 3,   f"outreach: expected 3 rows, got {len(outs)}"
    assert book_row,         f"bookings: no record for {CLIENT_ID}"
    assert pay_row,          f"payments: no record for {CLIENT_ID}"
    assert deliv_row,        f"deliveries: no record for {CLIENT_ID}"
    assert test_row,         f"testimonial_requests: no record for {CLIENT_ID}"

    replied = any(dict(o)["replied"] for o in outs)

    def pnode(label: str, done: bool) -> str:
        mark = f"{G}✓{RS}" if done else f"{R}✗{RS}"
        return f"{mark} {label}"

    print(f"\n  {B}Pipeline — {DAVE['business_name']} / {DAVE['owner_name']}{RS}")
    nodes = [
        pnode("lead scraped",       bool(lead_row)),
        pnode("contacted",          len(outs) > 0),
        pnode("replied",            replied),
        pnode("booked",             bool(book_row)),
        pnode("paid",               bool(pay_row)),
        pnode("delivered",          bool(deliv_row)),
        pnode("testimonial queued", bool(test_row)),
    ]
    print("  " + "  →  ".join(nodes))

    pay_dict  = dict(pay_row)  if pay_row  else {}
    test_dict = dict(test_row) if test_row else {}
    print(f"\n  {B}SQLite record detail:{RS}")
    detail = [
        ("lead_id",           str(lead_row["id"])),
        ("business_name",     lead_row["business_name"]),
        ("email",             lead_row["email"]),
        ("sequence steps",    f"{len(outs)} / 3"),
        ("replied",           "yes" if replied else "no"),
        ("booking_time",      str(book_row["booking_time"])[:16]),
        ("payment",           f"${pay_dict.get('amount', 0) / 100:.2f}  "
                              f"({pay_dict.get('status', '?')})"),
        ("delivery status",   deliv_row["status"]),
        ("testimonial after", test_dict.get("send_after", "N/A")[:10]),
    ]
    for k, v in detail:
        inf(f"{k:<22} {v}")

    files = sorted(OUT_DIR.glob("*")) if OUT_DIR.exists() else []
    print(f"\n  {B}Deliverables  ({OUT_DIR}):{RS}")
    for f in files:
        ok(f"{f.name:<36} {f.stat().st_size:>9,} bytes")
    if not files:
        warn("No files found in deliverables directory")

    db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# Final summary
# ═══════════════════════════════════════════════════════════════════════════════
all_stages = (S1, S2, S3, S4, S5, S6, S7)
all_pass   = all(RESULTS.get(s, False) for s in all_stages)

hdr("Test Summary")
for s in all_stages:
    passed = RESULTS.get(s, False)
    mark   = f"{G}PASS{RS}" if passed else f"{R}FAIL{RS}"
    print(f"  {s:<42}  {mark}")

print()
if all_pass:
    print(f"{G}{B}  All 7 stages passed — RingCatch pipeline verified end-to-end.{RS}")
    print(f"{D}  Test DB:      {TEST_DB}{RS}")
    print(f"{D}  Deliverables: {OUT_DIR}{RS}")
else:
    failed = [s for s in all_stages if not RESULTS.get(s, False)]
    print(f"{R}{B}  {len(failed)} stage(s) failed:{RS}")
    for f in failed:
        print(f"{R}    ✗ {f}{RS}")
    print(f"\n{D}  Check output above for details on each failure.{RS}")

sys.exit(0 if all_pass else 1)
