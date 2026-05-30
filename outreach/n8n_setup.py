#!/usr/bin/env python3
"""Create and activate all 4 RingCatch n8n workflows via the REST API."""
import json, sys, uuid
import httpx

N8N   = "http://localhost:5678"
CREDS = {"email": "molszewski423@gmail.com", "password": "N8N_PASSWORD_FROM_ENV"}

def uid(): return str(uuid.uuid4())[:8]

def discord_post_node(x, y):
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
    conns = {}
    for i in range(len(nodes) - 1):
        conns[nodes[i]["name"]] = {
            "main": [[{"node": nodes[i+1]["name"], "type": "main", "index": 0}]]
        }
    return conns

# ── Workflow 1: Morning Stats Digest ─────────────────────────────────────────
STATS_JS = r"""
const f = $json.chat_funnel || {};
const date = new Date().toLocaleDateString('en-US', {weekday:'short',month:'short',day:'numeric'});
const msg = [
  '📊 **RingCatch — ' + date + '**',
  '',
  '🌐 Visits today: '       + ($json.total_visits_today || 0),
  '💬 Chat sessions: '      + (f.total_starts     || 0),
  '🎯 Demos seen: '         + (f.demo_seen        || 0),
  '🤝 Close-ready: '        + (f.close_reached    || 0),
  '💰 Converted: '          + (f.converted        || 0),
  '📧 Emails captured: '    + (f.emails_captured  || 0),
].join('\n');
return {json: {content: msg}};
"""

wf1_nodes = [
    {"id": uid(), "name": "Every Morning 8AM",
     "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [0, 0],
     "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "0 8 * * *"}]}}},
    {"id": uid(), "name": "Get Analytics",
     "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [220, 0],
     "parameters": {"method": "GET", "url": "http://localhost:8080/analytics", "options": {}}},
    {"id": uid(), "name": "Format Stats",
     "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [440, 0],
     "parameters": {"mode": "runOnceForEachItem", "jsCode": STATS_JS.strip()}},
    discord_post_node(660, 0),
]

# ── Workflow 2: Cal.com Booking Alert ─────────────────────────────────────────
CAL_JS = r"""
const body    = $json.body || $json;
const payload = body.payload || body;
const att     = (payload.attendees || [])[0] || {};
const start   = payload.startTime
  ? new Date(payload.startTime).toLocaleString('en-US',
      {weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'})
  : 'TBD';
const msg = [
  '📅 **New Discovery Call Booked!**',
  '',
  '👤 ' + (att.name  || 'Unknown'),
  '📧 ' + (att.email || ''),
  '🕐 ' + start,
  (payload.additionalNotes ? '\n📝 ' + payload.additionalNotes : ''),
].filter(Boolean).join('\n');
return {json: {content: msg}};
"""

WH_ID = str(uuid.uuid4())
wf2_nodes = [
    {"id": uid(), "name": "Cal.com Webhook",
     "type": "n8n-nodes-base.webhook", "typeVersion": 2, "position": [0, 0],
     "webhookId": WH_ID,
     "parameters": {"httpMethod": "POST", "path": "calcom-booking",
                    "responseMode": "onReceived", "options": {}}},
    {"id": uid(), "name": "Format Booking",
     "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [220, 0],
     "parameters": {"mode": "runOnceForEachItem", "jsCode": CAL_JS.strip()}},
    discord_post_node(440, 0),
]

# ── Workflow 3: Reply Received Alert ──────────────────────────────────────────
REPLY_JS = r"""
const all     = $input.all();
const replies = all[0].json.replies || [];
if (replies.length === 0) return [];
const lines = [
  '💬 **' + replies.length + ' New Email Repl' + (replies.length > 1 ? 'ies' : 'y') + '!**',
  '',
];
for (const r of replies) {
  lines.push('• ' + r.business_name + ' (' + r.niche + ') — ' + r.email);
}
lines.push('', '⚡ Follow up now while they\'re warm.');
return [{json: {content: lines.join('\n')}}];
"""

wf3_nodes = [
    {"id": uid(), "name": "Every 15 Min",
     "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [0, 0],
     "parameters": {"rule": {"interval": [{"field": "minutes", "minutesInterval": 15}]}}},
    {"id": uid(), "name": "Check Replies",
     "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [220, 0],
     "parameters": {"method": "GET", "url": "http://localhost:8080/recent-replies?minutes=16", "options": {}}},
    {"id": uid(), "name": "Format Reply Alert",
     "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [440, 0],
     "parameters": {"mode": "runOnceForAllItems", "jsCode": REPLY_JS.strip()}},
    discord_post_node(660, 0),
]

# ── Workflow 4: Weekly Video Report ───────────────────────────────────────────
VIDEO_JS = r"""
const all    = $input.all();
const videos = all[0].json.videos || [];
if (videos.length === 0) {
  return [{json: {content: '📹 **Weekly Video Report**\n\nNo uploaded videos yet — check back next week.'}}];
}
const lines = ['📹 **Weekly Video Report — ' + videos.length + ' video' + (videos.length > 1 ? 's' : '') + ' live**', ''];
for (const v of videos) {
  lines.push('• ' + v.niche + ': ' + v.youtube_url);
}
lines.push('', '🎯 Share these in outreach to warm up cold leads.');
return [{json: {content: lines.join('\n')}}];
"""

wf4_nodes = [
    {"id": uid(), "name": "Every Monday 8AM",
     "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [0, 0],
     "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "0 8 * * 1"}]}}},
    {"id": uid(), "name": "Get Videos",
     "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [220, 0],
     "parameters": {"method": "GET", "url": "http://localhost:8080/videos", "options": {}}},
    {"id": uid(), "name": "Format Video List",
     "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [440, 0],
     "parameters": {"mode": "runOnceForAllItems", "jsCode": VIDEO_JS.strip()}},
    discord_post_node(660, 0),
]

WORKFLOWS = [
    {"name": "Morning Stats Digest",   "nodes": wf1_nodes, "connections": chain(wf1_nodes)},
    {"name": "Cal.com Booking Alert",  "nodes": wf2_nodes, "connections": chain(wf2_nodes)},
    {"name": "Reply Received Alert",   "nodes": wf3_nodes, "connections": chain(wf3_nodes)},
    {"name": "Weekly Video Report",    "nodes": wf4_nodes, "connections": chain(wf4_nodes)},
]
for wf in WORKFLOWS:
    wf.update({"active": True, "settings": {"executionOrder": "v1"}})


def main():
    with httpx.Client(base_url=N8N, timeout=30) as c:
        r = c.post("/rest/login", json=CREDS)
        if r.status_code not in (200, 201):
            print(f"Login failed {r.status_code}: {r.text[:300]}")
            sys.exit(1)
        print("Logged in ✓\n")

        for wf in WORKFLOWS:
            r = c.post("/rest/workflows", json=wf)
            if r.status_code not in (200, 201):
                print(f"  ✗ {wf['name']}: {r.status_code} {r.text[:200]}")
                continue
            wf_id = r.json().get("id")
            r2 = c.post(f"/rest/workflows/{wf_id}/activate")
            status = "✓ active" if r2.status_code in (200, 201) else f"⚠ activate failed ({r2.status_code})"
            print(f"  {status}  {wf['name']}  (id={wf_id})")

        print(f"""
─────────────────────────────────────────────────
Cal.com Webhook URL (add in Cal.com → Settings → Webhooks):
  http://100.96.122.27:5678/webhook/calcom-booking
─────────────────────────────────────────────────
""")


if __name__ == "__main__":
    main()
