#!/usr/bin/env bash
set -euo pipefail

PYCHECK='
import urllib.request, json, sys
try:
    r = urllib.request.urlopen("http://127.0.0.1:8080/chat/healthcheck", timeout=120)
    d = json.loads(r.read())
    print(json.dumps(d))
except Exception as e:
    print(json.dumps({"status": "fail", "error": str(e)}))
'

run_check() {
  podman exec agency-outreach python3 -c "$PYCHECK" 2>/dev/null
}

get_status() {
  echo "$1" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("status","fail"))'
}

result=$(run_check)
status=$(get_status "$result")

if [ "$status" = "ok" ]; then
  echo "Chat healthcheck OK"
  exit 0
fi

echo "Chat healthcheck FAILED — restarting agency-outreach..." >&2
systemctl --user restart agency-outreach
sleep 20

result=$(run_check)
status=$(get_status "$result")

if [ "$status" = "ok" ]; then
  echo "Chat healthcheck recovered after restart"
  exit 0
fi

echo "Chat healthcheck still FAILED after restart: $result" >&2
exit 1
