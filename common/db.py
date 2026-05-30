"""Shared SQLite helpers for all agency agents."""
import json
import sqlite3
from datetime import datetime, UTC
from pathlib import Path

DB_PATH = Path("/data/agency.db")


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS event_bus (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp     TEXT    DEFAULT (datetime('now')),
        source_agent  TEXT,
        target_agent  TEXT,
        event_type    TEXT,
        priority      INTEGER DEFAULT 1,
        payload       TEXT,
        status        TEXT    DEFAULT 'pending',
        consumed_by   TEXT
    );
    CREATE TABLE IF NOT EXISTS leads (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        business_name         TEXT,
        email                 TEXT UNIQUE,
        phone                 TEXT,
        website               TEXT,
        domain                TEXT,
        address               TEXT,
        city                  TEXT,
        niche                 TEXT,
        scraped_date          TEXT,
        processed             INTEGER DEFAULT 0,
        pipeline_stage        TEXT    DEFAULT 'scraped',
        qualified             TEXT,
        qualification_reason  TEXT,
        last_contacted        TEXT
    );
    CREATE TABLE IF NOT EXISTS clients (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        business_name         TEXT,
        email                 TEXT,
        phone                 TEXT,
        niche                 TEXT,
        city                  TEXT,
        setup_date            TEXT,
        stripe_customer_id    TEXT,
        stripe_subscription_id TEXT,
        status                TEXT    DEFAULT 'active',
        chatbot_conversations INTEGER DEFAULT 0,
        churn_risk            TEXT    DEFAULT 'low',
        last_activity         TEXT,
        contract_pdf          TEXT,
        monthly_rate          REAL    DEFAULT 89.0,
        setup_fee             REAL    DEFAULT 450.0,
        testimonial_sent      INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS financial_ledger (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp         TEXT    DEFAULT (datetime('now')),
        event_type        TEXT,
        amount            REAL,
        tax_reserve       REAL,
        net_amount        REAL,
        stripe_payment_id TEXT,
        client_id         INTEGER,
        description       TEXT
    );
    CREATE TABLE IF NOT EXISTS tax_reserve (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp         TEXT    DEFAULT (datetime('now')),
        amount            REAL,
        source_payment_id TEXT,
        quarter           TEXT
    );
    CREATE TABLE IF NOT EXISTS expenses (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        date      TEXT,
        category  TEXT,
        description TEXT,
        amount    REAL,
        recurring INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS incidents (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp   TEXT    DEFAULT (datetime('now')),
        service     TEXT,
        event_type  TEXT,
        details     TEXT,
        resolved    INTEGER DEFAULT 0,
        resolved_at TEXT
    );
    CREATE TABLE IF NOT EXISTS agent_status (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_name     TEXT UNIQUE,
        status         TEXT,
        last_heartbeat TEXT,
        last_action    TEXT,
        actions_today  INTEGER DEFAULT 0,
        alerts_active  INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS activity_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp  TEXT DEFAULT (datetime('now')),
        agent      TEXT,
        event_type TEXT,
        message    TEXT,
        color      TEXT DEFAULT 'blue'
    );
    CREATE TABLE IF NOT EXISTS alerts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp    TEXT DEFAULT (datetime('now')),
        agent        TEXT,
        severity     TEXT,
        message      TEXT,
        acknowledged INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS sequence_queue (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id    INTEGER,
        step       INTEGER,
        send_after TEXT,
        sent       INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS intelligence_metrics (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        date         TEXT,
        metric_name  TEXT,
        metric_value REAL,
        notes        TEXT
    );
    CREATE TABLE IF NOT EXISTS ab_tests (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        variant_name TEXT,
        subject      TEXT,
        sends        INTEGER DEFAULT 0,
        opens        INTEGER DEFAULT 0,
        created_at   TEXT DEFAULT (datetime('now')),
        winner       INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS outreach (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id       INTEGER,
        email         TEXT,
        email_body    TEXT,
        sequence_step INTEGER DEFAULT 1,
        sent_at       TEXT DEFAULT (datetime('now')),
        opened        INTEGER DEFAULT 0,
        replied       INTEGER DEFAULT 0
    );
    """)
    db.commit()


def publish_event(db, source: str, target: str, event_type: str,
                  payload: dict, priority: int = 1) -> None:
    db.execute(
        "INSERT INTO event_bus (source_agent,target_agent,event_type,priority,payload) VALUES (?,?,?,?,?)",
        (source, target, event_type, priority, json.dumps(payload))
    )
    db.commit()


def log_activity(db, agent: str, event_type: str, message: str, color: str = "blue") -> None:
    db.execute(
        "INSERT INTO activity_log (agent,event_type,message,color) VALUES (?,?,?,?)",
        (agent, event_type, message, color)
    )
    db.commit()


def heartbeat(db, agent_name: str, last_action: str) -> None:
    db.execute("""
        INSERT INTO agent_status (agent_name, status, last_heartbeat, last_action, actions_today)
        VALUES (?, 'online', datetime('now'), ?, 1)
        ON CONFLICT(agent_name) DO UPDATE SET
            status='online', last_heartbeat=datetime('now'),
            last_action=excluded.last_action,
            actions_today=actions_today+1
    """, (agent_name, last_action))
    db.commit()
