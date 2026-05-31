import asyncio
import json
import logging
import os
import sqlite3
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, UTC, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB_PATH          = Path(os.environ.get("DB_PATH", "/data/agency.db"))
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://agency-orchestrator:8109")
SPEACHES_URL     = os.environ.get("SPEACHES_URL", "http://agency-voice:8000")
STT_MODEL        = os.environ.get("STT_MODEL", "Systran/faster-whisper-large-v3")
TTS_MODEL        = os.environ.get("TTS_MODEL", "speaches-ai/Kokoro-82M-v1.0-ONNX-fp16")
DELIVERY_URL     = os.environ.get("DELIVERY_URL", "http://agency-delivery:8081")
AGENT            = "agency-command"

_CHAT_MODEL    = os.environ.get("CHAT_MODEL", "llama3.1:8b")
_BACKEND_MODEL = os.environ.get("BACKEND_MODEL", "gemma4:26b")
_FAST_MODEL    = os.environ.get("FAST_MODEL", "gemma4:e4b")
_TOOL_MODEL    = os.environ.get("CHAT_TOOL_MODEL", "llama3.1:8b")

AGENT_MODELS = {
    "agency-orchestrator": f"chat: {_TOOL_MODEL} / backend: {_BACKEND_MODEL}",
    "agency-outreach":     _BACKEND_MODEL,
    "agency-sales":        _CHAT_MODEL,
    "agency-support":      _CHAT_MODEL,
    "agency-success":      _CHAT_MODEL,
    "agency-marketing":    _BACKEND_MODEL,
    "agency-delivery":     _BACKEND_MODEL,
    "agency-legal":        _BACKEND_MODEL,
    "agency-bi":           _BACKEND_MODEL,
    "agency-cfo":          _BACKEND_MODEL,
    "agency-video":        _BACKEND_MODEL,
    "agency-kokoro":       "Kokoro TTS (82M)",
    "agency-scraper":      "none",
    "agency-billing":      "none",
    "agency-inbox":        "none",
    "agency-discord":      "none",
}

AGENT_URLS = {
    "agency-orchestrator": "http://agency-orchestrator:8109/status",
    "agency-scraper":      "http://agency-scraper:8079/health",
    "agency-support":      "http://agency-support:8104/status",
    "agency-legal":        "http://agency-legal:8101/status",
    "agency-sales":        "http://agency-sales:8107/status",
    "agency-marketing":    "http://agency-marketing:8102/status",
    "agency-success":      "http://agency-success:8105/status",
    "agency-bi":           "http://agency-bi:8106/status",
    "agency-outreach":     "http://agency-outreach:8080/health",
    "agency-delivery":     "http://agency-delivery:8081/health",
    "agency-billing":      "http://agency-billing:8082/health",
    "agency-inbox":        "http://agency-inbox:8110/health",
    "agency-cfo":          "http://agency-cfo:8108/health",
    "agency-video":        "http://agency-video:8111/health",
}

event_buffer: deque = deque(maxlen=500)
ws_clients: set[asyncio.Queue] = set()
last_activity_id: int = 0
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
    CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT DEFAULT (datetime('now')),
        agent TEXT, event_type TEXT, message TEXT, color TEXT DEFAULT 'blue'
    );
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT DEFAULT (datetime('now')),
        agent TEXT, severity TEXT, message TEXT, acknowledged INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS agent_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT UNIQUE, status TEXT,
        last_heartbeat TEXT, last_action TEXT,
        actions_today INTEGER DEFAULT 0, alerts_active INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT, business_name TEXT, email TEXT,
        niche TEXT, city TEXT, setup_date TEXT, status TEXT DEFAULT 'active',
        chatbot_conversations INTEGER DEFAULT 0, churn_risk TEXT DEFAULT 'low',
        last_activity TEXT, monthly_rate REAL DEFAULT 89.0
    );
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT, business_name TEXT, email TEXT UNIQUE,
        city TEXT, niche TEXT, pipeline_stage TEXT DEFAULT 'scraped', qualified TEXT
    );
    CREATE TABLE IF NOT EXISTS financial_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, event_type TEXT,
        amount REAL, net_amount REAL, description TEXT, client_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS intelligence_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, metric_name TEXT, metric_value REAL
    );
    """)
    db.commit()
    db.close()


async def fetch_agent(name: str, url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        return {"name": name, "status": "online", "data": data, "last_check": datetime.now(UTC).isoformat()}
    except Exception as e:
        return {"name": name, "status": "offline", "error": str(e), "last_check": datetime.now(UTC).isoformat()}


async def get_all_agents() -> list:
    tasks = [fetch_agent(name, url) for name, url in AGENT_URLS.items()]
    return await asyncio.gather(*tasks)


def get_metrics(db) -> dict:
    def safe(q, *a):
        try:
            r = db.execute(q, a).fetchone()
            return r[0] if r else 0
        except Exception:
            return 0

    mrr_clients = safe("SELECT COUNT(*) FROM clients WHERE status='active'")
    mrr = mrr_clients * 89.0

    try:
        pipeline = db.execute(
            "SELECT pipeline_stage, COUNT(*) as c FROM leads GROUP BY pipeline_stage"
        ).fetchall()
        pipeline_data = {r["pipeline_stage"]: r["c"] for r in pipeline}
    except Exception:
        pipeline_data = {}

    emails_today = safe("SELECT COUNT(*) FROM outreach WHERE sent_at >= date('now') AND email_body != '__NO_FOLLOWUP__'")
    leads_today  = safe("SELECT COUNT(*) FROM leads WHERE scraped_date = date('now')")
    reply_rate   = safe("SELECT CAST(SUM(replied) AS FLOAT) / MAX(COUNT(*),1) * 100 FROM outreach WHERE email_body != '__NO_FOLLOWUP__'")

    return {
        "mrr": mrr, "total_clients": mrr_clients, "pipeline": pipeline_data,
        "agents_online": -1,  # filled in by /api/agents live check
        "total_agents": len(AGENT_URLS),
        "emails_today": emails_today, "email_limit": int(os.environ.get("EMAIL_DAILY_LIMIT", 500)),
        "leads_today": leads_today,
        "reply_rate": round(reply_rate or 0, 1),
        "uptime_status": "green",
    }


def get_pipeline(db) -> dict:
    stages = ["scraped", "emailed", "opened", "replied", "booked", "paid", "delivered", "active_client"]
    result = {}
    for stage in stages:
        try:
            count = db.execute(
                "SELECT COUNT(*) as c FROM leads WHERE pipeline_stage=?", (stage,)
            ).fetchone()["c"]
        except Exception:
            count = 0
        result[stage] = {"count": count, "value": count * 89.0 if stage in ("booked", "paid", "delivered", "active_client") else 0}
    return result


def get_clients(db) -> list:
    try:
        rows = db.execute(
            "SELECT * FROM clients WHERE status='active' ORDER BY setup_date DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_activity(db, limit: int = 100) -> list:
    try:
        rows = db.execute(
            "SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_revenue(db) -> dict:
    try:
        months = db.execute("""
            SELECT strftime('%Y-%m', timestamp) as month,
                   SUM(CASE WHEN event_type='subscription' THEN amount ELSE 0 END) as mrr_income,
                   SUM(CASE WHEN event_type='setup' THEN amount ELSE 0 END) as setup_income,
                   SUM(amount) as total
            FROM financial_ledger GROUP BY month ORDER BY month DESC LIMIT 12
        """).fetchall()
        return {"monthly": [dict(r) for r in months]}
    except Exception:
        return {"monthly": []}


async def broadcast(event: dict):
    event_buffer.append(event)
    dead = set()
    for q in ws_clients:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.add(q)
    ws_clients.difference_update(dead)


async def ws_poller():
    global last_activity_id
    while True:
        await asyncio.sleep(5)
        try:
            db = get_db()
            rows = db.execute(
                "SELECT * FROM activity_log WHERE id > ? ORDER BY id ASC LIMIT 50",
                (last_activity_id,)
            ).fetchall()
            for row in rows:
                event = dict(row)
                last_activity_id = max(last_activity_id, event["id"])
                await broadcast(event)
            db.close()
        except Exception as e:
            logger.warning(f"WS poller error: {e}")


async def ensure_voice_models():
    """Download STT and TTS models if not yet cached in Speaches."""
    await asyncio.sleep(10)  # wait for speaches to be ready
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{SPEACHES_URL}/v1/models")
            cached = {m["id"] for m in r.json().get("data", [])}
        for model_id in (STT_MODEL, TTS_MODEL):
            if model_id not in cached:
                logger.info(f"Downloading voice model {model_id} ...")
                encoded = model_id.replace("/", "%2F")
                async with httpx.AsyncClient(timeout=600) as client:
                    await client.post(f"{SPEACHES_URL}/v1/models/{encoded}")
                logger.info(f"Voice model {model_id} ready")
    except Exception as e:
        logger.warning(f"Could not ensure voice models: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_tables()
    # Seed last_activity_id
    global last_activity_id
    try:
        db = get_db()
        row = db.execute("SELECT MAX(id) as m FROM activity_log").fetchone()
        last_activity_id = row["m"] or 0
        db.close()
    except Exception:
        pass
    asyncio.create_task(ws_poller())
    asyncio.create_task(ensure_voice_models())
    logger.info("Command dashboard started on port 8100")
    yield


app = FastAPI(title="RingCatch Command Center", lifespan=lifespan)

static_dir = Path("/app/static")
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
def root():
    index = static_dir / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse("<h1>RingCatch Command — static files not found</h1>")


@app.get("/api/metrics")
async def api_metrics():
    db = get_db()
    m = get_metrics(db)
    db.close()
    agents = await get_all_agents()
    m["agents_online"] = sum(1 for a in agents if a["status"] == "online")
    return m


@app.get("/api/agents")
async def api_agents():
    return await get_all_agents()


@app.get("/api/agent/{name}/detail")
async def api_agent_detail(name: str):
    base = await fetch_agent(name, AGENT_URLS.get(name, ""))
    db = get_db()

    def safe(q, *a):
        try: return db.execute(q, a).fetchone()[0] or 0
        except: return 0

    stats = {}
    if name == "agency-scraper":
        stats = {
            "leads_scraped_today": safe("SELECT COUNT(*) FROM leads WHERE date(rowid)=date('now')"),
            "total_leads": safe("SELECT COUNT(*) FROM leads"),
            "pending_outreach": safe("SELECT COUNT(*) FROM leads WHERE processed=0 AND email!=''"),
            "last_scraped": (db.execute("SELECT scraped_date FROM leads ORDER BY id DESC LIMIT 1").fetchone() or [None])[0],
        }
        try:
            import yaml
            cfg = yaml.safe_load(open("/data/knowledge/../targets.yaml").read()) if False else {}
        except: cfg = {}
    elif name == "agency-outreach":
        steps = db.execute("SELECT sequence_step, COUNT(*) FROM outreach GROUP BY sequence_step").fetchall()
        stats = {
            "emails_today": safe("SELECT COUNT(*) FROM outreach WHERE date(sent_at)=date('now')"),
            "total_sent": safe("SELECT COUNT(*) FROM outreach"),
            "pending_leads": safe("SELECT COUNT(*) FROM leads WHERE processed=0 AND email!=''"),
            "replied": safe("SELECT COUNT(*) FROM outreach WHERE replied=1"),
        }
        for step, count in steps:
            stats[f"step_{step}_sent"] = count
    elif name == "agency-sales":
        stats = {
            "hot_leads": safe("SELECT COUNT(*) FROM leads WHERE pipeline_stage='hot'"),
            "warm_leads": safe("SELECT COUNT(*) FROM leads WHERE qualified='warm'"),
            "cold_leads": safe("SELECT COUNT(*) FROM leads WHERE qualified='cold'"),
            "replied": safe("SELECT COUNT(*) FROM leads WHERE pipeline_stage='replied'"),
            "pending_events": safe("SELECT COUNT(*) FROM event_bus WHERE event_type='NEW_LEAD' AND status='pending'"),
        }
    elif name == "agency-billing":
        stats = {
            "total_revenue": safe("SELECT COALESCE(SUM(net_amount),0) FROM financial_ledger"),
            "transactions": safe("SELECT COUNT(*) FROM financial_ledger"),
            "active_clients": safe("SELECT COUNT(*) FROM clients WHERE status='active'"),
            "pending_events": safe("SELECT COUNT(*) FROM event_bus WHERE event_type='NEW_CLIENT' AND status='pending'"),
        }
    elif name == "agency-delivery":
        stats = {
            "clients_delivered": safe("SELECT COUNT(*) FROM clients WHERE status='active'"),
            "pending_deliveries": safe("SELECT COUNT(*) FROM event_bus WHERE event_type='NEW_CLIENT' AND status='pending'"),
        }
    elif name == "agency-success":
        churn = db.execute("SELECT churn_risk, COUNT(*) FROM clients GROUP BY churn_risk").fetchall()
        stats = {"active_clients": safe("SELECT COUNT(*) FROM clients WHERE status='active'")}
        for risk, count in churn:
            stats[f"churn_{risk}"] = count
    elif name == "agency-orchestrator":
        stats = {
            "pending_scheduled_tasks": safe("SELECT COUNT(*) FROM scheduled_tasks WHERE status='pending'"),
            "active_sessions": safe("SELECT COUNT(DISTINCT session_id) FROM conversations"),
            "total_messages": safe("SELECT COUNT(*) FROM conversations"),
        }
        due = db.execute("SELECT task_type, fire_at FROM scheduled_tasks WHERE status='pending' ORDER BY fire_at LIMIT 3").fetchall()
        if due:
            stats["next_tasks"] = ", ".join(f"{r[0]} @ {r[1][11:16]}" for r in due)

    stats["model"] = AGENT_MODELS.get(name, "—")
    db.close()

    # Pull recent activity log entries for this agent
    short = name.replace("agency-", "")
    db2 = get_db()
    log_rows = db2.execute(
        "SELECT timestamp, event_type, message FROM activity_log WHERE agent=? OR agent=? ORDER BY id DESC LIMIT 20",
        (name, short)
    ).fetchall()
    db2.close()
    logs = [f"[{r[0][11:19]}] {r[1]}: {r[2]}" for r in reversed(log_rows)]

    return {**base, "stats": stats, "logs": logs}


@app.post("/webhook/brevo")
async def webhook_brevo(request: Request):
    """Public-facing Brevo event webhook — proxies to outreach agent."""
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "http://agency-outreach:8080/webhook/brevo",
                content=body,
                headers={"Content-Type": "application/json"},
            )
        return resp.json()
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/api/analytics")
async def api_analytics():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("http://agency-outreach:8080/analytics")
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/pipeline")
def api_pipeline():
    db = get_db()
    p = get_pipeline(db)
    db.close()
    return p


@app.get("/api/clients")
def api_clients():
    db = get_db()
    c = get_clients(db)
    db.close()
    return c


@app.get("/api/activity")
def api_activity(limit: int = 100):
    db = get_db()
    a = get_activity(db, limit)
    db.close()
    return a


@app.get("/api/revenue")
def api_revenue():
    db = get_db()
    r = get_revenue(db)
    db.close()
    return r


@app.post("/api/alerts/acknowledge")
def ack_alert(payload: dict):
    alert_id = payload.get("alert_id")
    db = get_db()
    db.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (alert_id,))
    db.commit()
    db.close()
    return {"status": "ok"}


@app.get("/api/alerts/unacknowledged")
def unacked_alerts():
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM alerts WHERE acknowledged=0 ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        db.close()
        return [dict(r) for r in rows]
    except Exception:
        db.close()
        return []


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    ws_clients.add(queue)
    # Send recent events on connect
    for event in list(event_buffer)[-50:]:
        await websocket.send_json(event)
    try:
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=30)
            await websocket.send_json(event)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        ws_clients.discard(queue)


@app.post("/api/voice")
async def api_voice(audio: UploadFile = File(...), session_id: str = "voice-default"):
    audio_bytes = await audio.read()
    async with httpx.AsyncClient(timeout=60) as client:
        # STT
        try:
            stt = await client.post(
                f"{SPEACHES_URL}/v1/audio/transcriptions",
                files={"file": (audio.filename or "audio.webm", audio_bytes, audio.content_type or "audio/webm")},
                data={"model": STT_MODEL, "language": "en"}
            )
            transcript = stt.json().get("text", "").strip()
        except Exception as e:
            return Response(status_code=503, content=f"STT unavailable: {e}")

        if not transcript:
            return Response(status_code=400, content="Could not transcribe audio")

        # Orchestrator
        try:
            orch = await client.post(
                f"{ORCHESTRATOR_URL}/chat",
                json={"message": transcript, "session_id": session_id},
                timeout=120
            )
            reply = orch.json().get("response", "Sorry, no response.")
        except Exception as e:
            reply = f"Orchestrator unavailable: {e}"

        # TTS
        try:
            tts = await client.post(
                f"{SPEACHES_URL}/v1/audio/speech",
                json={"model": TTS_MODEL, "input": reply, "voice": "af_heart", "response_format": "mp3"},
                timeout=60
            )
            if tts.status_code != 200:
                raise ValueError(f"TTS returned {tts.status_code}: {tts.text[:200]}")
            return Response(
                content=tts.content,
                media_type="audio/mpeg",
                headers={"X-Transcript": transcript[:500], "X-Reply": reply[:500]}
            )
        except Exception as e:
            return Response(status_code=503, content=f"TTS unavailable: {e}")


@app.post("/api/chat")
async def api_chat(payload: dict):
    try:
        async with httpx.AsyncClient(timeout=360) as client:
            resp = await client.post(f"{ORCHESTRATOR_URL}/chat", json=payload)
            return resp.json()
    except Exception as e:
        return {"response": f"Orchestrator unavailable: {e}", "session_id": payload.get("session_id", "")}


@app.get("/intake", response_class=HTMLResponse)
async def intake_proxy_get(id: str = ""):
    async with httpx.AsyncClient(timeout=10) as c:
        resp = await c.get(f"{DELIVERY_URL}/intake", params={"id": id})
        return HTMLResponse(resp.text, status_code=resp.status_code)


@app.post("/intake")
async def intake_proxy_post(payload: dict):
    async with httpx.AsyncClient(timeout=10) as c:
        resp = await c.post(f"{DELIVERY_URL}/intake", json=payload)
        return resp.json()


@app.get("/api/emails")
def api_emails(limit: int = 200, step: int | None = None):
    db = get_db()
    try:
        q = """
            SELECT o.id, o.sent_at, o.sequence_step, o.replied,
                   o.email, o.email_body,
                   l.business_name, l.niche, l.city
            FROM outreach o
            LEFT JOIN leads l ON l.id = o.lead_id
        """
        params: list = []
        if step is not None:
            q += " WHERE o.sequence_step = ?"
            params.append(step)
        q += " ORDER BY o.sent_at DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(q, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"api_emails error: {e}")
        return []
    finally:
        db.close()


@app.get("/health")
def health():
    return {"status": "ok"}
