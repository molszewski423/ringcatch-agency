import asyncio
import json
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, UTC, date
from pathlib import Path

import httpx
from fastapi import FastAPI

try:
    import praw
    PRAW_AVAILABLE = True
except ImportError:
    PRAW_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB_PATH      = Path(os.environ.get("DB_PATH", "/data/agency.db"))
DISCORD_URL  = os.environ.get("DISCORD_BOT_URL", "http://agency-discord:8103/alert")
OLLAMA_URL   = os.environ.get("OLLAMA_BASE_URL", "http://host.containers.internal:11434")
BK_MODEL     = os.environ.get("FAST_MODEL", os.environ.get("BACKEND_MODEL", "gemma4:e4b"))
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
AGENT        = "agency-marketing"


async def _llm(prompt: str, max_tokens: int = 600) -> str:
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
REPORTS_DIR = Path("/data/reports")

REDDIT_CLIENT_ID     = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
REDDIT_USERNAME      = os.environ.get("REDDIT_USERNAME", "")
REDDIT_PASSWORD      = os.environ.get("REDDIT_PASSWORD", "")
SOCIAL_DIR  = Path("/data/social")

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
    CREATE TABLE IF NOT EXISTS ab_tests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, variant_name TEXT, subject TEXT,
        sends INTEGER DEFAULT 0, opens INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')), winner INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS social_content (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        content_type TEXT,
        body TEXT NOT NULL,
        hook TEXT,
        target_niche TEXT,
        target_subreddit TEXT,
        generated_at TEXT DEFAULT (datetime('now')),
        posted INTEGER DEFAULT 0,
        posted_at TEXT
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
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    SOCIAL_DIR.mkdir(parents=True, exist_ok=True)


async def send_discord(message: str):
    if not DISCORD_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(DISCORD_URL, json={"content": f"📣 **Marketing**: {message}"})
    except Exception as e:
        logger.warning(f"Discord failed: {e}")


async def analyze_email_performance(db) -> dict:
    rows = db.execute("""
        SELECT email_body, COUNT(*) as sends, SUM(opened) as opens, SUM(replied) as replies
        FROM outreach GROUP BY sequence_step
    """).fetchall() if db.execute("SELECT name FROM sqlite_master WHERE name='outreach'").fetchone() else []
    total_sends = sum(r["sends"] for r in rows)
    total_opens = sum(r["opens"] or 0 for r in rows)
    return {
        "total_sends": total_sends,
        "total_opens": total_opens,
        "open_rate": round(total_opens / total_sends, 3) if total_sends else 0,
    }


async def promote_ab_winner(db):
    tests = db.execute(
        "SELECT *, CAST(opens AS REAL)/NULLIF(sends,0) as open_rate FROM ab_tests WHERE winner=0 AND sends >= 50"
    ).fetchall()
    if not tests:
        return
    best = max(tests, key=lambda r: r["open_rate"] or 0)
    db.execute("UPDATE ab_tests SET winner=1 WHERE id=?", (best["id"],))
    db.execute(
        "INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
        (AGENT, "AB_WINNER", f"A/B winner: '{best['subject']}' ({best['open_rate']:.1%} open rate)", "green")
    )
    db.commit()
    await send_discord(f"🏆 A/B winner promoted: '{best['subject']}' with {best['open_rate']:.1%} open rate")


async def research_niche(niche: str) -> str:
    try:
        query = f"AI chatbot {niche} small business opportunity"
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}")
        text = re.sub(r'<[^>]+>', ' ', resp.text)
        text = re.sub(r'\s+', ' ', text)[:2000]
    except Exception:
        text = f"Research unavailable for {niche}"

    try:
        return await _llm(f"Based on this web content about AI chatbots for {niche} businesses, what is the market opportunity and key pain points? Be specific and actionable in 3-4 sentences.\n\nContent: {text}")
    except Exception as e:
        return f"Could not analyze {niche}: {e}"


async def optimize_targeting(db) -> list:
    rows = db.execute("""
        SELECT niche, city, COUNT(*) as total,
               SUM(CASE WHEN pipeline_stage IN ('paid','active_client') THEN 1 ELSE 0 END) as converted
        FROM leads GROUP BY niche, city HAVING total >= 3
        ORDER BY CAST(converted AS REAL)/total DESC LIMIT 10
    """).fetchall()
    return [dict(r) for r in rows]


async def generate_weekly_report(db) -> str:
    perf = await analyze_email_performance(db)
    niches = db.execute(
        "SELECT niche, COUNT(*) as c FROM leads GROUP BY niche ORDER BY c DESC LIMIT 5"
    ).fetchall()
    clients = db.execute("SELECT COUNT(*) as c FROM clients WHERE status='active'").fetchone()
    new_leads = db.execute(
        "SELECT COUNT(*) as c FROM leads WHERE scraped_date >= date('now', '-7 days')"
    ).fetchone()

    summary = f"""RingCatch Weekly Marketing Report — {date.today()}
Emails sent: {perf['total_sends']} | Open rate: {perf['open_rate']:.1%}
Active clients: {clients['c'] if clients else 0} | New leads this week: {new_leads['c'] if new_leads else 0}
Top niches: {', '.join(r['niche'] for r in niches[:3])}"""

    try:
        recommendations = await _llm(f"You are the marketing director for RingCatch (AI chatbots for small businesses, $450+$89/mo). Here are this week's metrics:\n\n{summary}\n\nWrite 3 specific, actionable marketing recommendations for next week. Focus on what will increase lead-to-client conversion. Be direct and practical.", max_tokens=500)
    except Exception:
        recommendations = "Could not generate recommendations."

    report = f"{summary}\n\n## Recommendations\n{recommendations}"
    path = REPORTS_DIR / f"marketing_{date.today()}.md"
    path.write_text(report)
    return report


# ── Free social media content generation ────────────────────────────────────

LINKEDIN_HOOKS = [
    "Most small business owners don't realize they're losing customers every night.",
    "We set up a chatbot for an HVAC company last week. It captured 3 leads while the owner was asleep.",
    "The businesses winning in 2026 aren't spending more on ads. They're responding faster.",
    "Hot take: a $89/month AI chatbot beats a $2,000/month part-time receptionist.",
    "I talk to 20+ small business owners a week. The #1 complaint is always the same.",
]

REDDIT_SUBREDDITS = {
    "HVAC":             ["r/HVAC", "r/hvacadvice"],
    "Plumbing":         ["r/Plumbing", "r/DIY"],
    "Electrician":      ["r/electricians"],
    "Dental / Medical": ["r/dentistry", "r/smallbusiness"],
    "Law Firm":         ["r/LawFirm", "r/smallbusiness"],
    "Auto Repair":      ["r/MechanicAdvice", "r/AutoRepair"],
    "Restaurant":       ["r/restaurant", "r/restaurantowners"],
    "default":          ["r/smallbusiness", "r/Entrepreneur", "r/startups"],
}

FACEBOOK_GROUPS = [
    "HVAC Business Owners",
    "Plumbing Business Owners Network",
    "Small Business Owners USA",
    "Contractor & Trades Business Owners",
    "Local Business Marketing Tips",
]

GBP_TOPICS = [
    "Answering customer questions 24/7 with AI",
    "Never miss a lead again — even at 2am",
    "48-hour chatbot setup for local businesses",
    "How AI is changing customer service for small businesses",
]




async def generate_linkedin_post(top_niche: str = "HVAC") -> dict:
    import random
    hook = random.choice(LINKEDIN_HOOKS)
    prompt = (
        f"You are Mike Olszewski, founder of RingCatch (ringcatch.io), which builds AI chatbots "
        f"for local small businesses ($450 setup + $89/mo). Write a LinkedIn post.\n\n"
        f"Hook (first line, use this exactly): {hook}\n\n"
        f"Rules:\n"
        f"- Target audience: {top_niche} business owners and local service contractors\n"
        f"- Voice: authentic founder, not corporate. Like texting a friend who owns a business.\n"
        f"- Structure: hook → 3-4 short punchy lines → soft CTA mentioning ringcatch.io\n"
        f"- Max 150 words total. No hashtags more than 3. No bullet lists.\n"
        f"- Do NOT use phrases like 'I am delighted' or 'I would like to share'.\n"
        f"- End with 1 question to drive comments.\n"
        f"Output only the post text."
    )
    body = await _llm(prompt)
    return {"platform": "linkedin", "content_type": "post", "body": body,
            "hook": hook, "target_niche": top_niche}


async def generate_facebook_post(top_niche: str = "HVAC") -> dict:
    prompt = (
        f"Write a Facebook post for a small business owners group (not a business page).\n"
        f"You are Mike from RingCatch. You just helped a {top_niche} business owner set up an "
        f"AI chatbot and want to share what you learned — casual, helpful, story-based.\n\n"
        f"Rules:\n"
        f"- Speak as a real person, not a marketer. Start with 'Hey guys' or a relatable observation.\n"
        f"- Tell a brief story or share one insight from working with {top_niche} businesses.\n"
        f"- Mention ringcatch.io naturally at the end — not a hard sell.\n"
        f"- 80-120 words. Conversational. One short paragraph.\n"
        f"Output only the post text."
    )
    body = await _llm(prompt)
    return {"platform": "facebook", "content_type": "group_post", "body": body,
            "target_niche": top_niche}


async def generate_reddit_comment(top_niche: str = "HVAC") -> dict:
    subreddits = REDDIT_SUBREDDITS.get(top_niche, REDDIT_SUBREDDITS["default"])
    import random
    subreddit = random.choice(subreddits)
    prompt = (
        f"Write a genuinely helpful Reddit comment for {subreddit} that could be posted as a "
        f"response to a post like 'How do you handle after-hours calls?' or 'How do you not miss leads?'\n\n"
        f"You are Mike, an operator who runs RingCatch (AI chatbots for local service businesses).\n\n"
        f"Rules:\n"
        f"- Lead with real practical advice that helps ANY {top_niche} business — not just customers of RingCatch.\n"
        f"- Only mention RingCatch in the last sentence, casually, as 'this is what we built at ringcatch.io'.\n"
        f"- Do NOT sound like an ad. Sound like a practitioner sharing what works.\n"
        f"- 80-130 words. No headers, no bullet points, plain Reddit prose.\n"
        f"Output only the comment text."
    )
    body = await _llm(prompt)
    return {"platform": "reddit", "content_type": "comment", "body": body,
            "target_niche": top_niche, "target_subreddit": subreddit}


async def generate_google_business_post(top_niche: str = "HVAC") -> dict:
    import random
    topic = random.choice(GBP_TOPICS)
    prompt = (
        f"Write a Google Business Profile post for RingCatch (ringcatch.io).\n"
        f"Topic: {topic}\n"
        f"Target: {top_niche} businesses and local service companies\n\n"
        f"Rules:\n"
        f"- 60-80 words max (GBP limit).\n"
        f"- Start with a customer benefit, not a product feature.\n"
        f"- End with a soft CTA: 'Book a free call at ringcatch.io'\n"
        f"- No hashtags. Professional but warm.\n"
        f"Output only the post text."
    )
    body = await _llm(prompt)
    return {"platform": "google_business", "content_type": "update", "body": body,
            "target_niche": top_niche}


async def generate_weekly_social_batch(db) -> list:
    """Generate one post for each platform and save to DB + file. Send to Discord."""
    # Pick the top niche by lead count for personalization
    try:
        row = db.execute(
            "SELECT niche FROM leads GROUP BY niche ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        top_niche = row[0] if row else "HVAC"
    except Exception:
        top_niche = "HVAC"

    posts = await asyncio.gather(
        generate_linkedin_post(top_niche),
        generate_facebook_post(top_niche),
        generate_reddit_comment(top_niche),
        generate_google_business_post(top_niche),
    )

    saved = []
    for p in posts:
        db.execute("""
            INSERT INTO social_content (platform, content_type, body, hook, target_niche, target_subreddit)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (p["platform"], p.get("content_type"), p["body"],
              p.get("hook"), p.get("target_niche"), p.get("target_subreddit")))
        saved.append(p)
    db.commit()

    # Save to file for easy copy-paste
    today = date.today().isoformat()
    batch_file = SOCIAL_DIR / f"social_{today}.md"
    lines = [f"# RingCatch Social Content — {today} (niche: {top_niche})\n"]
    for p in saved:
        platform = p["platform"].replace("_", " ").title()
        sub = f" ({p['target_subreddit']})" if p.get("target_subreddit") else ""
        lines.append(f"## {platform}{sub}\n\n{p['body']}\n")
    batch_file.write_text("\n".join(lines))

    db.execute(
        "INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
        (AGENT, "SOCIAL_BATCH", f"Social batch generated for niche={top_niche}: {len(saved)} posts", "purple")
    )
    db.commit()

    return saved


def _get_reddit():
    if not PRAW_AVAILABLE or not all([REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD]):
        return None
    return praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
        user_agent="RingCatch:v1.0 (by /u/" + REDDIT_USERNAME + ")",
    )


async def _post_reddit_content(posts: list) -> list:
    reddit = _get_reddit()
    if not reddit:
        return []
    posted = []
    reddit_posts = [p for p in posts if p.get("platform") == "reddit"]
    for p in reddit_posts:
        sub_name = p.get("target_subreddit", "smallbusiness").lstrip("r/")
        body = p.get("body", "")
        try:
            subreddit = reddit.subreddit(sub_name)
            title = body.split("\n")[0][:300]
            text  = body[len(title):].strip() or body
            submission = subreddit.submit(title=title, selftext=text)
            posted.append(f"r/{sub_name}: {submission.shortlink}")
            logger.info(f"Posted to r/{sub_name}: {submission.shortlink}")
        except Exception as e:
            logger.warning(f"Reddit post to r/{sub_name} failed: {e}")
    return posted


async def _post_social_to_discord(posts: list) -> None:
    if not DISCORD_URL or not posts:
        return
    lines = ["📱 **Weekly Social Content — ready to post (free channels)**\n"]
    labels = {
        "linkedin":       "🔵 **LinkedIn post** — post from your personal profile",
        "facebook":       "📘 **Facebook group post**",
        "reddit":         "🟠 **Reddit comment**",
        "google_business": "🟢 **Google Business Profile update**",
    }
    for p in posts:
        label = labels.get(p["platform"], f"**{p['platform']}**")
        sub   = f" → {p['target_subreddit']}" if p.get("target_subreddit") else ""
        lines.append(f"{label}{sub}")
        # Trim to keep Discord message under 2000 chars
        body_preview = p["body"][:400] + ("…" if len(p["body"]) > 400 else "")
        lines.append(f"```\n{body_preview}\n```")
    msg = "\n".join(lines)
    # Split if too long
    for chunk in [msg[i:i+1900] for i in range(0, len(msg), 1900)]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(DISCORD_URL, json={"content": chunk})
        except Exception as e:
            logger.warning(f"Discord social post failed: {e}")


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
        await analyze_email_performance(db)
        await promote_ab_winner(db)
        db.close()
        update_heartbeat("daily email performance analysis")


async def weekly_loop():
    while True:
        await asyncio.sleep(604800)
        db = get_db()
        report = await generate_weekly_report(db)
        posts  = await generate_weekly_social_batch(db)
        db.close()
        await send_discord(f"📊 Weekly report ready:\n```{report[:600]}```")
        await _post_social_to_discord(posts)
        reddit_links = await _post_reddit_content(posts)
        if reddit_links:
            await send_discord("✅ **Auto-posted to Reddit:**\n" + "\n".join(f"• {l}" for l in reddit_links))
        update_heartbeat("weekly report + social batch generated")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_tables()
    asyncio.create_task(daily_loop())
    asyncio.create_task(weekly_loop())
    logger.info("Marketing agent started")
    yield


app = FastAPI(title="Agency Marketing", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    reports = list(REPORTS_DIR.glob("*.md")) if REPORTS_DIR.exists() else []
    db = get_db()
    tests = db.execute("SELECT COUNT(*) as c FROM ab_tests WHERE winner=0").fetchone()["c"]
    row = db.execute("SELECT * FROM agent_status WHERE agent_name=?", (AGENT,)).fetchone()
    db.close()
    return {
        "agent": AGENT,
        "ab_tests_running": tests,
        "reports_generated": len(reports),
        "last_report": max((r.name for r in reports), default=None),
        "actions_today": row["actions_today"] if row else 0,
    }


@app.get("/weekly-report")
async def weekly_report():
    db = get_db()
    report = await generate_weekly_report(db)
    db.close()
    return {"report": report}


@app.get("/optimize-targeting")
async def opt_targeting():
    db = get_db()
    result = await optimize_targeting(db)
    db.close()
    return result


@app.post("/research-niche")
async def research(payload: dict):
    niche = payload.get("niche", "")
    result = await research_niche(niche)
    return {"niche": niche, "analysis": result}


@app.get("/ab-results")
def ab_results():
    db = get_db()
    rows = db.execute(
        "SELECT *, CAST(opens AS REAL)/NULLIF(sends,0) as open_rate FROM ab_tests ORDER BY created_at DESC"
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.post("/generate-social")
async def generate_social(payload: dict = {}):
    """Generate a fresh social media content batch on demand and send to Discord."""
    db = get_db()
    posts = await generate_weekly_social_batch(db)
    db.close()
    await _post_social_to_discord(posts)
    update_heartbeat("on-demand social batch")
    return {"status": "ok", "posts": len(posts), "platforms": [p["platform"] for p in posts]}


@app.get("/social-content")
def social_content(limit: int = 20, platform: str = None):
    """List recent generated social posts."""
    db = get_db()
    q = "SELECT * FROM social_content"
    params = []
    if platform:
        q += " WHERE platform=?"
        params.append(platform)
    q += " ORDER BY generated_at DESC LIMIT ?"
    params.append(limit)
    rows = db.execute(q, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.post("/mark-posted")
def mark_posted(payload: dict):
    """Mark a social post as manually posted."""
    post_id = payload.get("id")
    db = get_db()
    db.execute("UPDATE social_content SET posted=1, posted_at=datetime('now') WHERE id=?", (post_id,))
    db.commit()
    db.close()
    return {"status": "ok"}
