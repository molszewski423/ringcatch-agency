import asyncio
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

import httpx
import yaml
from fastapi import BackgroundTasks, FastAPI

from enricher import enrich
from maps_scraper import extract_email_from_website, hunter_lookup, scrape_google_maps
from scorer import SCORE_THRESHOLD, score_lead

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Loop health tracking — exposed via /health so support agent can detect stalls
_loop_last_ok: datetime | None = None      # last time a scrape completed without error
_loop_last_error: str = ""                 # last error message
_loop_consecutive_errors: int = 0


async def _discord_alert(msg: str) -> None:
    url = os.environ.get("DISCORD_BOT_URL", "")
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(url, json={"content": msg})
    except Exception:
        pass


async def _autonomous_scrape_loop() -> None:
    global _loop_last_ok, _loop_last_error, _loop_consecutive_errors
    await asyncio.sleep(60)
    while True:
        try:
            today = date.today().isoformat()
            db = get_db()
            scraped_today = db.execute(
                "SELECT COUNT(*) FROM leads WHERE scraped_date=?", (today,)
            ).fetchone()[0]
            db.close()
            if scraped_today < MAX_LEADS_PER_DAY:
                logger.info("Autonomous: %d/%d leads today — running scrape", scraped_today, MAX_LEADS_PER_DAY)
                targets = _load_targets(None, None)
                await _scrape_all(targets)
            else:
                logger.info("Autonomous: daily cap reached (%d) — waiting", scraped_today)
            _loop_last_ok = datetime.utcnow()
            _loop_last_error = ""
            _loop_consecutive_errors = 0
            await asyncio.sleep(2 * 3600)
        except Exception as e:
            _loop_last_error = str(e)
            _loop_consecutive_errors += 1
            logger.error("Autonomous scrape loop error (#%d): %s", _loop_consecutive_errors, e)
            await _discord_alert(
                f"⚠️ **Scraper loop error** (#{_loop_consecutive_errors})\n`{str(e)[:300]}`\n"
                f"Retrying in {'15 min' if _loop_consecutive_errors <= 3 else '2 hrs'}."
            )
            # Fast retry for first 3 failures, then back off to 2 hours
            retry_secs = 900 if _loop_consecutive_errors <= 3 else 7200
            await asyncio.sleep(retry_secs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    for sub in ("leads", "deliverables", "logs"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    init_db()
    logger.info("Scraper ready — DB initialised")
    asyncio.create_task(_autonomous_scrape_loop())
    yield


app = FastAPI(title="Agency Lead Scraper", lifespan=lifespan)

DATA_DIR = Path("/data")
CONFIG_DIR = Path("/config")
HUNTER_KEY = os.environ.get("HUNTER_API_KEY", "")
MAX_LEADS_PER_DAY = int(os.environ.get("MAX_LEADS_PER_DAY", "75"))

DB_PATH = DATA_DIR / "agency.db"


def get_db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            business_name TEXT NOT NULL,
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
    """)
    # Idempotent column additions — enrichment + scoring signals
    for col_sql in [
        "ALTER TABLE leads ADD COLUMN score INTEGER DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN has_chatbot INTEGER DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN chatbot_type TEXT",
        "ALTER TABLE leads ADD COLUMN cms TEXT",
        "ALTER TABLE leads ADD COLUMN has_google_ads INTEGER DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN domain_age_years REAL",
        "ALTER TABLE leads ADD COLUMN site_response_ms INTEGER",
        "ALTER TABLE leads ADD COLUMN gbp_rating REAL",
        "ALTER TABLE leads ADD COLUMN gbp_review_count INTEGER",
    ]:
        try:
            db.execute(col_sql)
        except Exception:
            pass  # column already exists
    db.executescript("""
        CREATE TABLE IF NOT EXISTS outreach (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id        INTEGER,
            email          TEXT,
            email_body     TEXT,
            sequence_step  INTEGER DEFAULT 1,
            sent_at        TEXT,
            replied        INTEGER DEFAULT 0,
            FOREIGN KEY(lead_id) REFERENCES leads(id)
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id    TEXT,
            client_name  TEXT,
            niche        TEXT,
            booking_time TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   TEXT,
            client_name TEXT,
            amount      INTEGER,
            status      TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS deliveries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   TEXT,
            client_name TEXT,
            niche       TEXT,
            status      TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS testimonial_requests (
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
    db.close()




@app.post("/scrape")
async def trigger_scrape(
    background_tasks: BackgroundTasks,
    niche: str | None = None,
    city: str | None = None,
):
    targets = _load_targets(niche, city)
    background_tasks.add_task(_scrape_all, targets)
    return {"status": "started", "targets": targets}


@app.get("/leads/today")
def today_leads():
    today = date.today().isoformat()
    db = get_db()
    rows = db.execute(
        "SELECT id,business_name,email,phone,city,niche FROM leads WHERE scraped_date=? AND processed=0",
        (today,),
    ).fetchall()
    db.close()
    return {
        "leads": [
            dict(zip(["id", "business_name", "email", "phone", "city", "niche"], r))
            for r in rows
        ]
    }


@app.get("/health")
def health():
    now = datetime.utcnow()
    hours_since_ok = (
        round((now - _loop_last_ok).total_seconds() / 3600, 1)
        if _loop_last_ok else None
    )
    # Degraded if no successful run in 5+ hours (2-hr cycle + buffer)
    degraded = hours_since_ok is not None and hours_since_ok > 5
    return {
        "status": "degraded" if degraded else "ok",
        "loop_last_ok": _loop_last_ok.isoformat() if _loop_last_ok else None,
        "hours_since_ok": hours_since_ok,
        "consecutive_errors": _loop_consecutive_errors,
        "last_error": _loop_last_error[:200] if _loop_last_error else None,
    }


def _load_targets(niche: str | None, city: str | None) -> list[dict]:
    cfg_file = CONFIG_DIR / "targets.yaml"
    if cfg_file.exists():
        cfg = yaml.safe_load(cfg_file.read_text())
    else:
        cfg = {"niches": ["HVAC"], "cities": ["Houston, TX"]}

    if niche and city:
        return [{"niche": niche, "city": city}]

    # Support both `niche` (str) and `niches` (list)
    niches_raw = cfg.get("niches") or [cfg.get("niche", "HVAC")]
    niches = [niche] if niche else (niches_raw if isinstance(niches_raw, list) else [niches_raw])
    cities = [city] if city else cfg.get("cities", ["Houston, TX"])
    return [{"niche": n, "city": c} for n in niches for c in cities]


def _load_per_city() -> int:
    cfg_file = CONFIG_DIR / "targets.yaml"
    if cfg_file.exists():
        cfg = yaml.safe_load(cfg_file.read_text())
        return min(int(cfg.get("leads_per_city", 15)), 50)
    return 15


async def _scrape_all(targets: list[dict]) -> None:
    db = get_db()
    today = date.today().isoformat()
    daily_count = db.execute(
        "SELECT COUNT(*) FROM leads WHERE scraped_date=?", (today,)
    ).fetchone()[0]
    db.close()

    # Use config leads_per_city — how many Google Maps results to pull per
    # niche/city combo. More results = better email hit rate. Daily cap still
    # prevents storing more than MAX_LEADS_PER_DAY.
    per_city = _load_per_city()

    all_new: list[dict] = []

    for target in targets:
        if daily_count >= MAX_LEADS_PER_DAY:
            logger.info(f"Daily cap {MAX_LEADS_PER_DAY} reached — stopping")
            break

        logger.info(f"Scraping '{target['niche']}' in '{target['city']}' (up to {per_city})")

        raw_leads = await scrape_google_maps(target["niche"], target["city"], max_results=per_city)

        for lead in raw_leads:
            email, domain = "", ""

            if lead.get("website"):
                email, domain = await extract_email_from_website(lead["website"])
                if not email and domain and HUNTER_KEY:
                    email = await hunter_lookup(domain, HUNTER_KEY)

            if not email:
                continue

            lead.update({"email": email, "domain": domain, "scraped_date": today})

            # Enrich and score — skip low-quality leads before storing
            enrichment = await enrich(lead.get("website", ""), domain)
            s = score_lead(lead, enrichment)
            if s < SCORE_THRESHOLD:
                logger.info(
                    "Skipping low-score lead: %s (%s) score=%d chatbot=%s cms=%s",
                    lead["business_name"], lead["niche"], s,
                    enrichment.get("chatbot_type") or "none",
                    enrichment.get("cms") or "unknown",
                )
                continue

            logger.info(
                "Qualified lead: %s score=%d chatbot=%s cms=%s ads=%s",
                lead["business_name"], s,
                enrichment.get("chatbot_type") or "none",
                enrichment.get("cms") or "unknown",
                enrichment.get("has_google_ads"),
            )

            db = get_db()
            try:
                db.execute("""
                    INSERT OR IGNORE INTO leads
                        (business_name, email, phone, website, domain, address, city, niche,
                         scraped_date, score, has_chatbot, chatbot_type, cms, has_google_ads,
                         domain_age_years, site_response_ms, gbp_rating, gbp_review_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    lead["business_name"], lead["email"], lead["phone"],
                    lead["website"], domain, lead["address"],
                    lead["city"], lead["niche"], today,
                    s, enrichment["has_chatbot"], enrichment["chatbot_type"],
                    enrichment["cms"], enrichment["has_google_ads"],
                    enrichment["domain_age_years"], enrichment["site_response_ms"],
                    lead.get("gbp_rating"), lead.get("gbp_review_count"),
                ))
                db.commit()
                daily_count += 1
                all_new.append(lead)
            except Exception as exc:
                logger.warning(f"DB insert failed: {exc}")
            finally:
                db.close()

    # Dump today's leads to file for n8n / manual inspection
    out_file = DATA_DIR / "leads" / f"leads_{today}.json"
    out_file.write_text(json.dumps(all_new, indent=2, default=str))
    logger.info(f"Done — {len(all_new)} new leads with emails → {out_file}")

    # Write NEW_LEAD events to event_bus so sales agent can qualify them
    if all_new:
        db = get_db()
        try:
            for lead in all_new:
                db.execute("""
                    INSERT INTO event_bus (source_agent, target_agent, event_type, priority, payload)
                    VALUES ('agency-scraper', 'agency-sales', 'NEW_LEAD', 2, ?)
                """, (json.dumps({
                    "business_name": lead.get("business_name"),
                    "email":         lead.get("email"),
                    "niche":         lead.get("niche"),
                    "city":          lead.get("city"),
                }),))
            db.commit()
            logger.info(f"Wrote {len(all_new)} NEW_LEAD events to event_bus")
        except Exception as exc:
            logger.warning(f"event_bus write failed: {exc}")
        finally:
            db.close()
