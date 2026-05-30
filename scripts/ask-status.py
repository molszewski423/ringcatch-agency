#!/usr/bin/env python3
"""
Ask local models about RingCatch agent suite status.
Run: python3 ~/agency/scripts/ask-status.py
"""
import requests, sqlite3, json, sys

OLLAMA = "http://localhost:11434"
DB     = "/var/home/mike/agency/data/agency.db"

def ask(prompt, model="gemma4:26b"):
    print(f"\n[Querying {model}...]\n")
    r = requests.post(f"{OLLAMA}/api/generate",
        json={"model": model, "prompt": prompt, "stream": True}, timeout=300, stream=True)
    for line in r.iter_lines():
        if line:
            chunk = json.loads(line)
            print(chunk.get("response", ""), end="", flush=True)
            if chunk.get("done"):
                break
    print()

def get_db_summary():
    try:
        db = sqlite3.connect(DB)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        summary = {}
        for table, col in [("clients","status='active'"), ("leads","1=1"), ("financial_ledger","1=1"), ("activity_log","1=1")]:
            try:
                summary[table] = db.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}").fetchone()[0]
            except: summary[table] = "n/a"
        try:
            agents = db.execute("SELECT agent_name, status, last_action FROM agent_status").fetchall()
            summary["agents"] = [dict(a) for a in agents]
        except: summary["agents"] = []
        try:
            recent = db.execute("SELECT agent, event_type, message FROM activity_log ORDER BY id DESC LIMIT 5").fetchall()
            summary["recent_activity"] = [dict(r) for r in recent]
        except: summary["recent_activity"] = []
        mrr = summary.get("clients", 0)
        if isinstance(mrr, int): summary["mrr_estimate"] = mrr * 89.0
        db.close()
        return summary
    except Exception as e:
        return {"error": str(e)}

COMMANDS = {
    "status":   "What is the current status of all 8 RingCatch agents and the overall business?",
    "next":     "What are the top 3 things that should be done next to grow RingCatch?",
    "pipeline": "Analyze the current lead pipeline. Where are the bottlenecks? What should be done this week?",
    "risks":    "What are the biggest risks to RingCatch right now? How should we address them?",
    "summary":  "Give me a 100-word executive summary of RingCatch's current state.",
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    print("=== RingCatch Local Model Status ===")
    db_data = get_db_summary()
    print(f"DB Summary: {json.dumps(db_data, indent=2)}")

    base_context = f"""You are advising the founder of RingCatch, a micro-SaaS that sells AI chatbots to local small businesses ($450 setup + $89/month).
The system has 8 autonomous AI agents running (support, cfo, legal, sales, marketing, success, bi, command dashboard).
Current database state: {json.dumps(db_data)}
All agents: support(8104), cfo(8103), legal(8101), sales(8107), marketing(8102), success(8105), bi(8106), command(8100).
Dashboard at http://localhost:8100 — React UI with live WebSocket feed.

Question: {COMMANDS.get(cmd, cmd)}"""

    ask(base_context)
