import asyncio
import json
import logging
import os
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, UTC, timedelta

import httpx
from fastapi import FastAPI
import socket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB_PATH      = os.environ.get("DB_PATH", "/data/agency.db")
DISCORD_URL  = os.environ.get("DISCORD_BOT_URL", "http://agency-discord:8103/alert")
PODMAN_SOCK  = os.environ.get("PODMAN_SOCKET", "/run/podman/podman.sock")
VRAM_THRESH  = int(os.environ.get("VRAM_THRESHOLD", "90"))

AGENT = "agency-support"

MONITORED = [
    # Core pipeline
    {"name": "agency-scraper",       "url": "http://127.0.0.1:8079/health",       "container": "agency-scraper"},
    {"name": "agency-outreach",      "url": "http://127.0.0.1:8080/health",       "container": "agency-outreach"},
    {"name": "agency-delivery",      "url": "http://127.0.0.1:8081/health",       "container": "agency-delivery"},
    {"name": "agency-billing",       "url": "http://127.0.0.1:8082/health",       "container": "agency-billing"},
    # AI / orchestration
    {"name": "agency-orchestrator",  "url": "http://127.0.0.1:8109/health",       "container": "agency-orchestrator"},
    {"name": "agency-inbox",         "url": "http://127.0.0.1:8110/health",       "container": "agency-inbox"},
    # Client-facing agents
    {"name": "agency-legal",         "url": "http://127.0.0.1:8101/health",       "container": "agency-legal"},
    {"name": "agency-marketing",     "url": "http://127.0.0.1:8102/health",       "container": "agency-marketing"},
    {"name": "agency-cfo",           "url": "http://127.0.0.1:8108/health",       "container": "agency-cfo"},
    {"name": "agency-success",       "url": "http://127.0.0.1:8105/health",       "container": "agency-success"},
    {"name": "agency-bi",            "url": "http://127.0.0.1:8106/health",       "container": "agency-bi"},
    {"name": "agency-sales",         "url": "http://127.0.0.1:8107/health",       "container": "agency-sales"},
    # Infrastructure / support
    {"name": "agency-command",       "url": "http://127.0.0.1:8100/health",       "container": "agency-command"},
    {"name": "agency-discord",       "url": "http://127.0.0.1:8103/health",       "container": "agency-discord"},
    {"name": "agency-video",         "url": "http://127.0.0.1:8111/health",       "container": "agency-video"},
    {"name": "agency-voice",         "url": "http://127.0.0.1:8880/health",       "container": "agency-kokoro"},
    {"name": "agency-landing",       "url": "http://127.0.0.1:8090/",             "container": "agency-landing"},
    {"name": "agency-n8n",           "url": "http://127.0.0.1:5678/healthz",      "container": "agency-n8n"},
    {"name": "agency-dashboard",     "url": "http://127.0.0.1:8501/_stcore/health", "container": "agency-dashboard"},
]

# TCP-only services (no HTTP health endpoint, or app not yet configured)
TCP_MONITORED = [
    {"name": "agency-postgres", "host": "127.0.0.1", "port": 5432, "container": "agency-postgres"},
    {"name": "agency-calcom",   "host": "127.0.0.1", "port": 3000, "container": "agency-calcom"},
]

EXTERNAL = ["https://ringcatch.io", "https://dashboard.ringcatch.io"]

failures: dict[str, int] = {}
service_status: dict[str, dict] = {}
external_down: dict[str, bool] = {}   # tracks last known state for transition alerting
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
    CREATE TABLE IF NOT EXISTS incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT DEFAULT (datetime('now')),
        service TEXT, event_type TEXT, details TEXT,
        resolved INTEGER DEFAULT 0, resolved_at TEXT
    );
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
    """)
    db.commit()
    db.close()


def log_incident(service: str, event_type: str, details: str):
    db = get_db()
    db.execute(
        "INSERT INTO incidents (service, event_type, details) VALUES (?,?,?)",
        (service, event_type, details)
    )
    db.execute(
        "INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
        (AGENT, event_type, f"{service}: {details}", "red")
    )
    db.execute(
        "INSERT INTO alerts (agent, severity, message) VALUES (?,?,?)",
        (AGENT, "critical" if event_type == "FAILURE" else "info", f"{service}: {details}")
    )
    db.commit()
    db.close()


def log_recovery(service: str):
    db = get_db()
    db.execute(
        "UPDATE incidents SET resolved=1, resolved_at=datetime('now') WHERE service=? AND resolved=0",
        (service,)
    )
    db.execute(
        "INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
        (AGENT, "RECOVERY", f"{service} is back online", "green")
    )
    db.commit()
    db.close()


async def send_discord(message: str):
    if not DISCORD_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(DISCORD_URL, json={"content": f"🔧 **Support**: {message}"})
    except Exception as e:
        logger.warning(f"Discord send failed: {e}")


async def restart_container(name: str):
    try:
        transport = httpx.AsyncHTTPTransport(uds=PODMAN_SOCK)
        async with httpx.AsyncClient(transport=transport, timeout=30) as client:
            resp = await client.post(
                f"http://localhost/v4.0.0/libpod/containers/{name}/restart"
            )
            if resp.status_code in (200, 204):
                logger.info(f"Restarted {name} via Podman socket")
                log_incident(name, "RESTART", "Auto-restarted by support agent")
            else:
                logger.warning(f"Restart {name} returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"Podman socket restart failed for {name}: {e}")
        # Fallback: systemctl via subprocess (requires host socket mount)
        try:
            subprocess.run(
                ["systemctl", "--user", "restart", f"{name}.service"],
                timeout=30, check=False
            )
        except Exception as e2:
            logger.error(f"systemctl restart also failed: {e2}")


async def check_service(name: str, url: str, container: str):
    was_failing = failures.get(name, 0) >= 2
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            try:
                body = resp.json()
                svc_status = body.get("status", "ok")
            except Exception:
                svc_status = "ok"

            if svc_status == "degraded":
                # Loop is stalled but process is alive — treat as a soft failure
                failures[name] = failures.get(name, 0) + 1
                details = body.get("last_error", "") or f"loop idle {body.get('hours_since_ok')}h"
                service_status[name] = {"status": "degraded", "failures": failures[name], "last_check": datetime.now(UTC).isoformat()}
                logger.warning(f"{name} degraded ({failures[name]}): {details}")
                if failures[name] == 2:
                    log_incident(name, "DEGRADED", details)
                    await send_discord(f"🔄 {name} loop stalled — auto-restarting\n`{details}`")
                    await restart_container(container)
            else:
                if was_failing:
                    log_recovery(name)
                    await send_discord(f"✅ {name} recovered")
                failures[name] = 0
                service_status[name] = {"status": "online", "last_check": datetime.now(UTC).isoformat()}
        else:
            raise ValueError(f"HTTP {resp.status_code}")
    except Exception as e:
        failures[name] = failures.get(name, 0) + 1
        service_status[name] = {"status": "offline", "failures": failures[name], "last_check": datetime.now(UTC).isoformat()}
        logger.warning(f"{name} check failed ({failures[name]}): {e}")
        if failures[name] == 2:
            log_incident(name, "FAILURE", f"Unreachable: {e}")
            await send_discord(f"🚨 {name} DOWN — auto-restarting")
            await restart_container(container)


async def check_external(url: str):
    """Alert only on state transitions: up→down and down→up. Never spam."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.head(url)
        if resp.status_code >= 500:
            raise ValueError(f"HTTP {resp.status_code}")
        service_status[url] = {"status": "online", "last_check": datetime.now(UTC).isoformat()}
        if external_down.get(url):
            # Recovered — send one recovery alert
            external_down[url] = False
            await send_discord(f"✅ External URL recovered: {url}")
    except Exception as e:
        was_down = external_down.get(url, False)
        external_down[url] = True
        service_status[url] = {"status": "offline", "last_check": datetime.now(UTC).isoformat()}
        if not was_down:
            # Newly down — alert once
            log_incident(url, "EXTERNAL_DOWN", str(e))
            await send_discord(f"🌐 External URL down: {url}")


async def check_vram():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            used, total = map(int, result.stdout.strip().split(", "))
            pct = (used / total) * 100
            db = get_db()
            db.execute(
                "INSERT INTO activity_log (agent, event_type, message, color) VALUES (?,?,?,?)",
                (AGENT, "VRAM", f"GPU VRAM: {used}MB/{total}MB ({pct:.1f}%)",
                 "orange" if pct > VRAM_THRESH else "blue")
            )
            db.commit()
            db.close()
            if pct > VRAM_THRESH:
                await send_discord(f"⚠️ GPU VRAM at {pct:.1f}% ({used}/{total}MB)")
    except FileNotFoundError:
        pass  # nvidia-smi not available
    except Exception as e:
        logger.warning(f"VRAM check failed: {e}")


def update_heartbeat(last_action: str = "monitoring"):
    db = get_db()
    db.execute("""
        INSERT INTO agent_status (agent_name, status, last_heartbeat, last_action, actions_today)
        VALUES (?, 'online', datetime('now'), ?, 1)
        ON CONFLICT(agent_name) DO UPDATE SET
            status='online', last_heartbeat=datetime('now'),
            last_action=excluded.last_action,
            actions_today=actions_today+1
    """, (AGENT, last_action))
    db.commit()
    db.close()


def _tcp_open(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def check_tcp(name: str, host: str, port: int, container: str):
    was_failing = failures.get(name, 0) >= 2
    reachable = await asyncio.get_event_loop().run_in_executor(
        None, _tcp_open, host, port
    )
    if reachable:
        if was_failing:
            log_recovery(name)
            await send_discord(f"✅ {name} recovered")
        failures[name] = 0
        service_status[name] = {"status": "online", "last_check": datetime.now(UTC).isoformat()}
    else:
        failures[name] = failures.get(name, 0) + 1
        service_status[name] = {"status": "offline", "failures": failures[name], "last_check": datetime.now(UTC).isoformat()}
        logger.warning(f"{name} TCP check failed ({failures[name]}): {host}:{port} unreachable")
        if failures[name] == 2:
            log_incident(name, "FAILURE", f"TCP port {port} unreachable")
            await send_discord(f"🚨 {name} DOWN (port {port}) — auto-restarting")
            await restart_container(container)


async def monitor_loop():
    vram_tick = 0
    while True:
        for svc in MONITORED:
            await check_service(svc["name"], svc["url"], svc["container"])
        for svc in TCP_MONITORED:
            await check_tcp(svc["name"], svc["host"], svc["port"], svc["container"])
        for url in EXTERNAL:
            await check_external(url)
        vram_tick += 1
        if vram_tick >= 5:
            await check_vram()
            vram_tick = 0
        update_heartbeat("checked all services")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_tables()
    task = asyncio.create_task(monitor_loop())
    logger.info("Support agent started — monitoring all services")
    yield
    task.cancel()


app = FastAPI(title="Agency Support", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    db = get_db()
    row = db.execute("SELECT * FROM agent_status WHERE agent_name=?", (AGENT,)).fetchone()
    db.close()
    uptime = str(datetime.now(UTC) - start_time).split(".")[0]
    return {
        "agent": AGENT,
        "uptime": uptime,
        "services_monitored": len(MONITORED) + len(TCP_MONITORED) + len(EXTERNAL),
        "services_online": sum(1 for s in service_status.values() if s.get("status") == "online"),
        "current_failures": {k: v for k, v in failures.items() if v > 0},
        "service_status": service_status,
        "actions_today": row["actions_today"] if row else 0,
    }


@app.get("/container-status")
def container_status():
    result = []
    for svc in MONITORED:
        st = service_status.get(svc["name"], {"status": "unknown"})
        result.append({"name": svc["name"], **st, "failures": failures.get(svc["name"], 0)})
    for url in EXTERNAL:
        st = service_status.get(url, {"status": "unknown"})
        result.append({"name": url, "type": "external", **st})
    return result


@app.post("/restart/{container_name}")
async def manual_restart(container_name: str):
    await restart_container(container_name)
    await send_discord(f"Manual restart triggered for {container_name}")
    return {"status": "restart_triggered", "container": container_name}


@app.get("/health-report")
def health_report():
    db = get_db()
    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    rows = db.execute(
        "SELECT * FROM incidents WHERE timestamp >= ? ORDER BY timestamp DESC", (since,)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]
