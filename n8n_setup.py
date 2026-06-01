#!/usr/bin/env python3
"""Create and activate all 4 RingCatch n8n workflows (stdlib only — no httpx)."""
import json, os, sys, uuid, urllib.request, urllib.error

N8N     = "http://localhost:5678"
API_KEY = os.environ["N8N_API_KEY"]
HDRS    = {"Content-Type": "application/json", "Accept": "application/json", "X-N8N-API-KEY": API_KEY}

def post(path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(N8N + path, data=data, headers=HDRS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def get(path):
    req = urllib.request.Request(N8N + path, headers=HDRS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, json.loads(r.read())

def uid(): return str(uuid.uuid4())[:8]

def discord_node(x, y):
    return {
        "id": uid(), "name": "Post to Discord",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [x, y],
        "parameters": {
            "method": "POST",
            "url": "http://localhost:8103/alert",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({content: $json.content}) }}",
            "options": {},
        },
    }

def chain(nodes):
    c = {}
    for i in range(len(nodes) - 1):
        c[nodes[i]["name"]] = {"main": [[{"node": nodes[i+1]["name"], "type": "main", "index": 0}]]}
    return c

# ── Workflow 1: Morning Stats ─────────────────────────────────────────────────
STATS_JS = r"""
const f    = $json.chat_funnel || {};
const date = new Date().toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric'});
const msg  = [
  '📊 **RingCatch — ' + date + '**', '',
  '🌐 Visits today: '    + ($json.total_visits_today || 0),
  '💬 Chat sessions: '   + (f.total_starts    || 0),
  '🎯 Demos seen: '      + (f.demo_seen       || 0),
  '🤝 Close-ready: '     + (f.close_reached   || 0),
  '💰 Converted: '       + (f.converted       || 0),
  '📧 Emails captured: ' + (f.emails_captured || 0),
].join('\n');
return {json: {content: msg}};
"""

wf1 = [
    {"id": uid(), "name": "Every Morning 8AM",
     "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [0, 0],
     "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "0 8 * * *"}]}}},
    {"id": uid(), "name": "Get Analytics",
     "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [220, 0],
     "parameters": {"method": "GET", "url": "http://localhost:8080/analytics", "options": {}}},
    {"id": uid(), "name": "Format Stats",
     "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [440, 0],
     "parameters": {"mode": "runOnceForEachItem", "jsCode": STATS_JS.strip()}},
    discord_node(660, 0),
]

# ── Workflow 2: Cal.com Booking ───────────────────────────────────────────────
CAL_JS = r"""
const body    = $json.body || $json;
const payload = body.payload || body;
const att     = (payload.attendees || [])[0] || {};
const start   = payload.startTime
  ? new Date(payload.startTime).toLocaleString('en-US',
      {weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'})
  : 'TBD';
const notes = payload.additionalNotes ? '\n📝 ' + payload.additionalNotes : '';
const msg = [
  '📅 **New Discovery Call Booked!**', '',
  '👤 ' + (att.name  || 'Unknown'),
  '📧 ' + (att.email || ''),
  '🕐 ' + start,
  notes,
].filter(Boolean).join('\n');
return {json: {content: msg}};
"""

WH_ID = str(uuid.uuid4())
wf2 = [
    {"id": uid(), "name": "Cal.com Webhook",
     "type": "n8n-nodes-base.webhook", "typeVersion": 2, "position": [0, 0],
     "webhookId": WH_ID,
     "parameters": {"httpMethod": "POST", "path": "calcom-booking",
                    "responseMode": "onReceived", "options": {}}},
    {"id": uid(), "name": "Format Booking",
     "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [220, 0],
     "parameters": {"mode": "runOnceForEachItem", "jsCode": CAL_JS.strip()}},
    discord_node(440, 0),
]

# ── Workflow 3: Reply Alert ───────────────────────────────────────────────────
REPLY_JS = r"""
const all     = $input.all();
const replies = all[0].json.replies || [];
if (replies.length === 0) return [];
const s     = replies.length > 1;
const lines = ['💬 **' + replies.length + ' New Email Repl' + (s?'ies':'y') + '!**', ''];
for (const r of replies) lines.push('• ' + r.business_name + ' (' + r.niche + ') — ' + r.email);
lines.push('', '⚡ Follow up now while they\'re warm.');
return [{json: {content: lines.join('\n')}}];
"""

wf3 = [
    {"id": uid(), "name": "Every 15 Min",
     "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [0, 0],
     "parameters": {"rule": {"interval": [{"field": "minutes", "minutesInterval": 15}]}}},
    {"id": uid(), "name": "Check Replies",
     "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [220, 0],
     "parameters": {"method": "GET", "url": "http://localhost:8080/recent-replies?minutes=16", "options": {}}},
    {"id": uid(), "name": "Format Reply Alert",
     "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [440, 0],
     "parameters": {"mode": "runOnceForAllItems", "jsCode": REPLY_JS.strip()}},
    discord_node(660, 0),
]

# ── Workflow 4: Weekly Video Report ───────────────────────────────────────────
VIDEO_JS = r"""
const all    = $input.all();
const videos = all[0].json.videos || [];
if (videos.length === 0) {
  return [{json: {content: '🎥 **Weekly Video Report**\n\nNo videos uploaded yet.'}}];
}
const s     = videos.length > 1;
const lines = ['🎥 **Weekly Video Report — ' + videos.length + ' video' + (s?'s':'') + ' live**', ''];
for (const v of videos) lines.push('• ' + v.niche + ': ' + v.youtube_url);
lines.push('', '🎯 Share these in outreach to warm up cold leads.');
return [{json: {content: lines.join('\n')}}];
"""

wf4 = [
    {"id": uid(), "name": "Every Monday 8AM",
     "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [0, 0],
     "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "0 8 * * 1"}]}}},
    {"id": uid(), "name": "Get Videos",
     "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [220, 0],
     "parameters": {"method": "GET", "url": "http://localhost:8080/videos", "options": {}}},
    {"id": uid(), "name": "Format Video List",
     "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [440, 0],
     "parameters": {"mode": "runOnceForAllItems", "jsCode": VIDEO_JS.strip()}},
    discord_node(660, 0),
]

WORKFLOWS = [
    {"name": "Morning Stats Digest",  "nodes": wf1, "connections": chain(wf1)},
    {"name": "Cal.com Booking Alert", "nodes": wf2, "connections": chain(wf2)},
    {"name": "Reply Received Alert",  "nodes": wf3, "connections": chain(wf3)},
    {"name": "Weekly Video Report",   "nodes": wf4, "connections": chain(wf4)},
]
for wf in WORKFLOWS:
    wf.update({"settings": {"executionOrder": "v1"}})


def main():
    # Verify API key works
    code, _ = get("/api/v1/workflows")
    if code not in (200, 201):
        print(f"API key check failed: {code}")
        sys.exit(1)
    print("API key valid ✓\n")

    for wf in WORKFLOWS:
        code, data = post("/api/v1/workflows", wf)
        if code not in (200, 201):
            print(f"  ✗ {wf['name']}: {code} {data}")
            continue
        wf_id = data.get("id")
        code2, _ = post(f"/api/v1/workflows/{wf_id}/activate")
        status = "✓ active" if code2 in (200, 201) else f"⚠ created (activate {code2})"
        print(f"  {status}  —  {wf['name']}  (id={wf_id})")

    print("""
─────────────────────────────────────────────────────────────
Cal.com Webhook URL — add in Cal.com → Settings → Webhooks:
  http://ARCHBOX_HOST:5678/webhook/calcom-booking
─────────────────────────────────────────────────────────────
""")


if __name__ == "__main__":
    main()
