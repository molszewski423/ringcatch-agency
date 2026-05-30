#!/usr/bin/env python3
"""
Agency Admin Service — runs on the HOST (not in a container).
Gives pod containers the ability to read/update .env and restart systemd services.
Port 8112, reachable from containers via host.containers.internal:8112
"""
import json
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

ENV_PATH = Path.home() / "agency" / ".env"
PORT = 8112

# Safe to return to orchestrator (no secrets)
READABLE_KEYS = {
    "EMAIL_DAILY_LIMIT", "MAX_LEADS_PER_DAY", "EMAIL_PROVIDER",
    "BREVO_SENDER_NAME", "BREVO_SENDER_EMAIL",
    "OLLAMA_BASE_URL", "CHAT_MODEL", "BACKEND_MODEL", "FAST_MODEL", "OLLAMA_MODEL",
    "SCRAPER_URL", "OUTREACH_URL", "DELIVERY_URL", "BILLING_URL",
    "ORCHESTRATOR_URL", "SPEACHES_URL", "KNOWLEDGE_DIR",
    "HEALTH_REPORT_INTERVAL_HOURS", "MAX_TOOL_ROUNDS",
    "INBOX_POLL_SECONDS", "CHAT_CHANNEL_NAME", "INTAKE_BASE_URL",
    "STRIPE_MODE", "NEXTAUTH_URL", "STRIPE_SETUP_LINK", "STRIPE_MONTHLY_LINK",
}

# Orchestrator is allowed to change these
WRITABLE_KEYS = {
    "EMAIL_DAILY_LIMIT", "MAX_LEADS_PER_DAY", "INBOX_POLL_SECONDS",
    "HEALTH_REPORT_INTERVAL_HOURS", "MAX_TOOL_ROUNDS",
    "CHAT_MODEL", "BACKEND_MODEL", "FAST_MODEL",
    # Social media credentials (set once during onboarding)
    "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
    "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET",
    "TIKTOK_ACCESS_TOKEN", "TIKTOK_REFRESH_TOKEN",
    "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET",
    "REDDIT_USERNAME", "REDDIT_PASSWORD",
}

KNOWN_AGENTS = {
    "scraper", "outreach", "delivery", "billing", "intake",
    "sales", "support", "success", "marketing", "bi", "legal",
    "discord", "orchestrator", "inbox", "cfo", "video", "kokoro",
}


def read_env() -> dict:
    env = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip()
    return env


def write_env_key(key: str, value: str) -> tuple[bool, str]:
    if key not in WRITABLE_KEYS:
        return False, f"{key} is not in the writable allowlist"
    content = ENV_PATH.read_text()
    pattern = re.compile(rf"^({re.escape(key)}=).*$", re.MULTILINE)
    if pattern.search(content):
        new_content = pattern.sub(rf"\g<1>{value}", content)
    else:
        new_content = content.rstrip() + f"\n{key}={value}\n"
    ENV_PATH.write_text(new_content)
    return True, f"Set {key}={value}"


def restart_agent(agent: str) -> tuple[bool, str]:
    name = agent.lower().removeprefix("agency-")
    if name not in KNOWN_AGENTS:
        return False, f"Unknown agent '{name}'. Known: {sorted(KNOWN_AGENTS)}"
    service = f"agency-{name}"
    try:
        # Start the restart in background — don't block waiting for container to come up
        subprocess.Popen(["systemctl", "--user", "restart", service])
        return True, f"Restart triggered for {service} (running in background)"
    except Exception as e:
        return False, str(e)


def agent_status_all() -> dict:
    result = {}
    for name in KNOWN_AGENTS:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", f"agency-{name}"],
            capture_output=True, text=True,
        )
        result[f"agency-{name}"] = r.stdout.strip()
    return result


class AdminHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[admin] {self.address_string()} — {fmt % args}", flush=True)

    def send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length > 0 else {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self.send_json(200, {"status": "ok", "service": "agency-admin"})
        elif path == "/config":
            env = read_env()
            self.send_json(200, {k: v for k, v in env.items() if k in READABLE_KEYS})
        elif path == "/agents":
            self.send_json(200, agent_status_all())
        elif path.startswith("/logs/"):
            agent = path.split("/logs/", 1)[1]
            name = agent.lower().removeprefix("agency-")
            lines = 50
            try:
                r = subprocess.run(
                    ["podman", "logs", "--tail", str(lines), f"agency-{name}"],
                    capture_output=True, text=True, timeout=10
                )
                logs = (r.stdout + r.stderr).strip()
                self.send_json(200, {"agent": f"agency-{name}", "logs": logs or "(no output)"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/config":
            key   = body.get("key", "").strip()
            value = str(body.get("value", "")).strip()
            restart = body.get("restart_agent", "").strip()
            if not key or not value:
                self.send_json(400, {"error": "key and value required"})
                return
            ok, msg = write_env_key(key, value)
            if not ok:
                self.send_json(403, {"error": msg})
                return
            result: dict = {"updated": key, "value": value, "message": msg}
            if restart:
                rok, rmsg = restart_agent(restart)
                result["restart"] = rmsg
                result["restart_ok"] = rok
            self.send_json(200, result)

        elif path.startswith("/restart/"):
            agent = path.split("/restart/", 1)[1]
            ok, msg = restart_agent(agent)
            self.send_json(200 if ok else 500, {"ok": ok, "message": msg})

        else:
            self.send_json(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"Agency admin service starting on port {PORT}", flush=True)
    print(f"ENV file: {ENV_PATH}", flush=True)
    HTTPServer(("0.0.0.0", PORT), AdminHandler).serve_forever()
