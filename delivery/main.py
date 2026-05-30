import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Agency Delivery Engine")

DATA_DIR     = Path("/data")
DB_PATH      = DATA_DIR / "agency.db"
OLLAMA_URL   = os.environ.get("OLLAMA_BASE_URL", "http://host.containers.internal:11434")
OLLAMA_MODEL = os.environ.get("BACKEND_MODEL", os.environ.get("OLLAMA_MODEL", "gemma4:26b"))
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")


async def _llm(prompt: str, max_tokens: int = 800) -> str:
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
        r = await c.post(f"{OLLAMA_URL}/api/generate", json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False})
    return r.json()["response"].strip()
OUTREACH_URL     = os.environ.get("OUTREACH_URL", "http://localhost:8080")
BREVO_API_KEY    = os.environ.get("BREVO_API_KEY", "")
BREVO_SENDER_EMAIL = os.environ.get("BREVO_SENDER_EMAIL", "alex@ringcatch.io")
BREVO_SENDER_NAME  = os.environ.get("BREVO_SENDER_NAME", "Alex from RingCatch")
MIKE_ALERT_EMAIL = os.environ.get("MIKE_ALERT_EMAIL", "molszewski423@gmail.com")


def get_db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


@app.post("/generate-delivery")
async def generate_delivery(booking: dict):
    client_id = booking.get("uid", datetime.now().strftime("%Y%m%d%H%M%S"))
    client_name = booking.get("organizer_name") or booking.get("attendee_name", "Client")
    niche = booking.get("niche", "small business")
    start_time = booking.get("start_time", "")

    out_dir = DATA_DIR / "deliverables" / client_id
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Generating delivery for {client_name} ({niche}) → {out_dir}")

    flow = await _generate_botpress_flow(niche, client_name)
    (out_dir / "botpress-flow.json").write_text(json.dumps(flow, indent=2))
    _generate_pdf(out_dir / "onboarding.pdf", client_name, niche, flow)

    loom = await _generate_loom_script(niche, client_name)
    (out_dir / "loom-script.txt").write_text(loom)
    logger.info(f"Loom script written → {out_dir / 'loom-script.txt'}")

    db = get_db()
    db.execute("""
        INSERT OR REPLACE INTO deliveries (client_id, client_name, niche, status, created_at)
        VALUES (?, ?, ?, 'delivered', datetime('now'))
    """, (client_id, client_name, niche))
    db.execute("""
        INSERT OR IGNORE INTO bookings (client_id, client_name, niche, booking_time, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
    """, (client_id, client_name, niche, start_time))
    db.commit()
    db.close()

    # Schedule testimonial request 7 days post-delivery
    attendee_email = booking.get("attendee_email", "")
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            await c.post(f"{OUTREACH_URL}/schedule-testimonial", json={
                "client_id":   client_id,
                "client_name": client_name,
                "email":       attendee_email,
                "niche":       niche,
            })
        except Exception as exc:
            logger.warning(f"Could not schedule testimonial: {exc}")

    return {"status": "delivered", "client_id": client_id,
            "files": ["botpress-flow.json", "onboarding.pdf", "loom-script.txt"]}


@app.get("/intake", response_class=HTMLResponse)
def intake_form(id: str = ""):
    return HTMLResponse(_INTAKE_HTML.replace("{{CLIENT_ID}}", id))


@app.post("/intake")
async def submit_intake(payload: dict):
    client_id = payload.get("client_id", "")
    data = {k: v for k, v in payload.items() if k != "client_id"}

    # Persist intake data alongside deliverables
    if client_id:
        out_dir = DATA_DIR / "deliverables" / client_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "intake.json").write_text(json.dumps(data, indent=2))
        logger.info(f"Intake saved for {client_id}: {list(data.keys())}")

    # Alert Mike with the intake details
    if BREVO_API_KEY:
        business = data.get("business_name", client_id)
        summary = "\n".join(f"  {k}: {v}" for k, v in data.items() if v)
        alert_body = (
            f"Client intake received!\n\n"
            f"Client ID: {client_id}\n"
            f"Business: {business}\n\n"
            f"Details:\n{summary}\n\n"
            f"Files are in /data/deliverables/{client_id}/"
        )
        async with httpx.AsyncClient(timeout=10) as c:
            try:
                await c.post(
                    "https://api.brevo.com/v3/smtp/email",
                    json={
                        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
                        "to": [{"email": MIKE_ALERT_EMAIL, "name": "Mike"}],
                        "subject": f"[RingCatch] Intake received: {business}",
                        "textContent": alert_body,
                    },
                    headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
                )
            except Exception as exc:
                logger.warning(f"Intake alert failed: {exc}")

    return {"status": "received", "client_id": client_id}


@app.get("/health")
def health():
    return {"status": "ok"}


_INTAKE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RingCatch — Setup Your Chatbot</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f1117; color: #e2e8f0; font-family: -apple-system, 'Inter', sans-serif; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
  .card { background: #1a1d27; border: 1px solid #2d3448; border-radius: 12px; padding: 36px; max-width: 600px; width: 100%; }
  h1 { font-size: 24px; font-weight: 800; color: #06b6d4; margin-bottom: 6px; }
  .subtitle { color: #8892a4; font-size: 14px; margin-bottom: 28px; }
  .field { margin-bottom: 18px; }
  label { display: block; font-size: 12px; text-transform: uppercase; letter-spacing: .5px; color: #8892a4; font-weight: 700; margin-bottom: 6px; }
  input, textarea, select { width: 100%; background: #20253a; border: 1px solid #2d3448; border-radius: 6px; color: #e2e8f0; padding: 10px 12px; font-size: 14px; font-family: inherit; outline: none; transition: border-color .2s; }
  input:focus, textarea:focus, select:focus { border-color: #3b82f6; }
  textarea { resize: vertical; min-height: 80px; }
  .hint { font-size: 11px; color: #8892a4; margin-top: 4px; }
  .section-title { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #8892a4; font-weight: 700; border-bottom: 1px solid #2d3448; padding-bottom: 6px; margin: 24px 0 14px; }
  .submit-btn { width: 100%; background: #3b82f6; border: none; color: white; padding: 14px; border-radius: 8px; font-size: 16px; font-weight: 700; cursor: pointer; margin-top: 12px; transition: background .2s; }
  .submit-btn:hover { background: #2563eb; }
  .submit-btn:disabled { background: #2d3448; cursor: not-allowed; }
  .success { background: rgba(34,197,94,.1); border: 1px solid #22c55e; border-radius: 8px; padding: 16px; color: #22c55e; text-align: center; display: none; font-weight: 600; }
</style>
</head>
<body>
<div class="card">
  <h1>Set Up Your Chatbot</h1>
  <p class="subtitle">Takes 3 minutes — we use this to customize your AI chatbot for your specific business.</p>

  <form id="form">
    <input type="hidden" name="client_id" value="{{CLIENT_ID}}">

    <div class="section-title">Your Business</div>

    <div class="field">
      <label>Business Name *</label>
      <input type="text" name="business_name" placeholder="e.g. Smith's HVAC &amp; Plumbing" required>
    </div>

    <div class="field">
      <label>Industry / Niche *</label>
      <select name="niche" required>
        <option value="">Select your industry...</option>
        <option>HVAC</option>
        <option>Plumbing</option>
        <option>Electrical</option>
        <option>Roofing</option>
        <option>Auto Repair</option>
        <option>Landscaping</option>
        <option>Dental</option>
        <option>Law Firm</option>
        <option>Salon / Spa</option>
        <option>Gym / Fitness</option>
        <option>Restaurant</option>
        <option>Real Estate</option>
        <option>Other</option>
      </select>
    </div>

    <div class="field">
      <label>Business Phone</label>
      <input type="tel" name="phone" placeholder="(555) 555-5555">
    </div>

    <div class="field">
      <label>Website URL</label>
      <input type="url" name="website" placeholder="https://yourbusiness.com">
    </div>

    <div class="field">
      <label>City / Service Area</label>
      <input type="text" name="service_area" placeholder="e.g. Houston TX and surrounding 50 miles">
    </div>

    <div class="section-title">Hours &amp; Availability</div>

    <div class="field">
      <label>Business Hours</label>
      <input type="text" name="hours" placeholder="e.g. Mon–Fri 8am–6pm, Sat 9am–2pm, emergency line 24/7">
    </div>

    <div class="field">
      <label>Do you offer emergency / after-hours service?</label>
      <select name="emergency_service">
        <option value="">Select...</option>
        <option value="yes_24_7">Yes — 24/7</option>
        <option value="yes_limited">Yes — limited hours</option>
        <option value="no">No</option>
      </select>
    </div>

    <div class="section-title">Services</div>

    <div class="field">
      <label>Services You Offer *</label>
      <textarea name="services" placeholder="List your main services, one per line&#10;e.g.&#10;AC installation and replacement&#10;Furnace repair&#10;Duct cleaning&#10;Annual maintenance plans" required></textarea>
    </div>

    <div class="field">
      <label>Approximate Pricing (optional but helps the bot)</label>
      <textarea name="pricing" placeholder="e.g.&#10;Service call: $89&#10;AC tune-up: $129&#10;Full system install: $4,000–$8,000"></textarea>
    </div>

    <div class="section-title">Customer Questions</div>

    <div class="field">
      <label>Top 5 Questions Customers Ask You *</label>
      <textarea name="faq_questions" placeholder="One question per line&#10;e.g.&#10;How long does an AC installation take?&#10;Do you service my brand?&#10;What's your warranty?&#10;How do I schedule a service call?&#10;Do you offer financing?" required></textarea>
      <span class="hint">The bot will answer these automatically — saves your team from repeating them all day.</span>
    </div>

    <div class="section-title">Lead Capture</div>

    <div class="field">
      <label>Where should leads be sent? *</label>
      <input type="email" name="lead_email" placeholder="your@email.com" required>
    </div>

    <div class="field">
      <label>Anything else you want the bot to know?</label>
      <textarea name="notes" placeholder="e.g. We're family-owned since 1998. We do NOT service commercial buildings. Always mention our 2-year labor warranty."></textarea>
    </div>

    <button type="submit" class="submit-btn" id="submit-btn">Submit — Start Building My Chatbot</button>
  </form>

  <div class="success" id="success">
    ✓ Got it! Your chatbot will be live within 48 hours. Check your email for updates.
  </div>
</div>

<script>
document.getElementById('form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('submit-btn');
  btn.disabled = true;
  btn.textContent = 'Submitting...';
  const data = {};
  new FormData(e.target).forEach((v, k) => { data[k] = v; });
  try {
    const resp = await fetch('/intake', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data) });
    if (resp.ok) {
      e.target.style.display = 'none';
      document.getElementById('success').style.display = 'block';
    } else {
      btn.disabled = false;
      btn.textContent = 'Submit — Start Building My Chatbot';
      alert('Something went wrong. Please try again or email alex@ringcatch.io');
    }
  } catch(err) {
    btn.disabled = false;
    btn.textContent = 'Submit — Start Building My Chatbot';
    alert('Could not connect. Please try again.');
  }
});
</script>
</body>
</html>"""


async def _generate_loom_script(niche: str, client_name: str) -> str:
    prompt = (
        f"Write a Loom video handoff script for the owner of {client_name}, "
        f"a {niche} business that just received an AI chatbot from RingCatch.\n\n"
        f"Timestamps:\n"
        f"[0:00] What the chatbot does and why it matters for {niche} businesses\n"
        f"[0:30] How to test it — type a question, see the live response\n"
        f"[1:00] What happens when a new lead messages after hours\n"
        f"[1:45] How to edit an FAQ answer in Botpress (30-second task)\n"
        f"[2:15] Support: alex@ringcatchai.com or ringcatch.io\n\n"
        f"Tone: warm, conversational, like a colleague showing a friend. Under 3 minutes. "
        f"Plain text only. No markdown."
    )
    try:
        return await _llm(prompt, max_tokens=600)
    except Exception as exc:
        logger.warning(f"Loom script generation failed ({exc}) — using template")
        return (
            f"Loom Handoff — {client_name}\n"
            f"Presenter: Alex · RingCatch · ringcatch.io\n\n"
            f"[0:00]\nQuick 2-minute walkthrough of your new AI chatbot for {client_name}.\n\n"
            f"[0:30]\nGo to your website, click the chat bubble, type a question — "
            f"watch the bot answer in under a second.\n\n"
            f"[1:00]\nWhen a lead messages after hours the bot captures their info and "
            f"you get an email alert. Every conversation is saved in Botpress.\n\n"
            f"[1:45]\nTo update an answer: Botpress → FAQ node → Edit → Save. "
            f"30 seconds, goes live immediately.\n\n"
            f"[2:15]\nQuestions: alex@ringcatchai.com — usually same-day. "
            f"Welcome to RingCatch!\n"
        )


async def _generate_botpress_flow(niche: str, business_name: str) -> dict:
    prompt = (
        f'You are configuring a customer service chatbot for a {niche} business '
        f'called "{business_name}".\n\n'
        f"Generate exactly 8 common customer questions and concise answers for this {niche} business.\n\n"
        f'Format as a JSON array:\n'
        f'[{{"q": "question here", "a": "answer here"}}, ...]\n\n'
        f"Output only valid JSON. No markdown. No explanation."
    )

    raw = await _llm(prompt, max_tokens=800)
    # Strip markdown fences if the model wraps in ```json
    if "```" in raw:
        raw = raw.split("```")[1].lstrip("json").strip()

    try:
        qa_pairs: list[dict] = json.loads(raw)
    except Exception:
        logger.warning("LLM returned invalid JSON for FAQ — using fallback")
        qa_pairs = [
            {"q": "What services do you offer?",
             "a": f"We provide full {niche} services including installation, maintenance, and emergency repairs."},
            {"q": "How do I schedule a service call?",
             "a": "Call us or fill out the contact form on our website and we'll get back to you same day."},
        ]

    nodes = [
        {
            "name": "entry",
            "type": "standard",
            "onEnter": [],
            "onReceive": [{"type": "say",
                           "message": f"Hi! I'm the virtual assistant for {business_name}. How can I help?"}],
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
            "onReceive": [{"type": "say",
                           "message": "I'm not sure about that — let me connect you with our team. "
                                      "Would you like to leave your contact info?"}],
            "next": [],
        },
    ]
    for i, pair in enumerate(qa_pairs):
        nodes.append({
            "name": f"faq_{i}",
            "type": "standard",
            "onEnter": [],
            "onReceive": [{"type": "say", "message": pair["a"]}],
            "next": [{"condition": "true", "node": "faq_router"}],
        })

    return {
        "version": "0.1",
        "name": f"{business_name} Chatbot",
        "niche": niche,
        "generated_at": datetime.now().isoformat(),
        "flows": [{"name": "main", "startNode": "entry", "nodes": nodes}],
        "faq_pairs": qa_pairs,
    }


def _generate_pdf(path: Path, client_name: str, niche: str, flow: dict) -> None:
    doc = SimpleDocTemplate(str(path), pagesize=letter,
                            rightMargin=inch, leftMargin=inch,
                            topMargin=inch, bottomMargin=inch)
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=20, spaceAfter=14)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13, spaceAfter=8)
    body = styles["BodyText"]

    story = [
        Paragraph("AI Chatbot Onboarding Guide", h1),
        Paragraph(f"<b>Client:</b> {client_name}", body),
        Paragraph(f"<b>Industry:</b> {niche.title()}", body),
        Spacer(1, 0.3 * inch),
        Paragraph("What Your Chatbot Does", h2),
        Paragraph(
            f"Your chatbot is pre-configured for the {niche} industry. It handles "
            "common customer questions automatically — 24/7 — so your team can focus "
            "on billable work.",
            body,
        ),
        Spacer(1, 0.2 * inch),
        Paragraph("Pre-Loaded FAQ Responses", h2),
    ]

    for pair in flow.get("faq_pairs", []):
        story.append(Paragraph(f"<b>Q: {pair['q']}</b>", body))
        story.append(Paragraph(f"A: {pair['a']}", body))
        story.append(Spacer(1, 0.1 * inch))

    story += [
        Spacer(1, 0.3 * inch),
        Paragraph("Next Steps", h2),
        *[Paragraph(s, body) for s in [
            "1. Import <i>botpress-flow.json</i> into your Botpress dashboard.",
            "2. Connect Botpress to your website using the embed snippet.",
            "3. Use Botpress preview mode to test each FAQ response.",
            "4. Edit any responses directly in the Botpress editor.",
            "5. Go live — the chatbot handles inquiries 24/7 from day one.",
        ]],
    ]

    doc.build(story)
    logger.info(f"PDF written → {path}")
