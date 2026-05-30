#!/usr/bin/env python3
"""
One-time TikTok OAuth setup. Run this on your local machine (not in the container).

Prerequisites:
  pip install requests

Steps:
  1. Go to https://developers.tiktok.com — sign in with your RingCatch TikTok account
  2. Create an app:
       Display name: RingCatch
       Category: Business
  3. Add product: "Content Posting API"  (requires approval — usually 1-3 days)
  4. After approval, go to App Detail → get Client Key + Client Secret
  5. Add redirect URI: http://localhost:8888/callback
  6. Run: python3 tiktok_auth.py --client-key YOUR_KEY --client-secret YOUR_SECRET

Usage:
  python3 tiktok_auth.py --client-key xxx --client-secret yyy
  # OR set env vars:
  TIKTOK_CLIENT_KEY=xxx TIKTOK_CLIENT_SECRET=yyy python3 tiktok_auth.py
"""
import argparse
import os
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

try:
    import requests
except ImportError:
    print("Missing dependency. Run:  pip install requests")
    sys.exit(1)

REDIRECT_URI = "http://localhost:8888/callback"
SCOPES = "video.upload,user.info.basic"

parser = argparse.ArgumentParser(description="Get TikTok OAuth tokens")
parser.add_argument("--client-key",    default=os.environ.get("TIKTOK_CLIENT_KEY", ""))
parser.add_argument("--client-secret", default=os.environ.get("TIKTOK_CLIENT_SECRET", ""))
args = parser.parse_args()

if not args.client_key or not args.client_secret:
    print("ERROR: provide --client-key and --client-secret (or set TIKTOK_CLIENT_KEY/SECRET env vars)")
    sys.exit(1)

auth_code_holder = {}

class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if "code" in params:
            auth_code_holder["code"] = params["code"]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Authorized! You can close this window.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h2>No code received.</h2>")

server = HTTPServer(("localhost", 8888), CallbackHandler)
t = Thread(target=lambda: server.handle_request())
t.start()

auth_url = (
    "https://www.tiktok.com/v2/auth/authorize/"
    f"?client_key={args.client_key}"
    f"&scope={SCOPES}"
    f"&response_type=code"
    f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    "&state=ringcatch"
)

print(f"\nOpening browser for TikTok authorization...")
print(f"If browser doesn't open, go to:\n{auth_url}\n")
webbrowser.open(auth_url)
t.join(timeout=120)

if "code" not in auth_code_holder:
    print("ERROR: No auth code received. Check the URL and try again.")
    sys.exit(1)

code = auth_code_holder["code"]
resp = requests.post(
    "https://open.tiktokapis.com/v2/oauth/token/",
    data={
        "client_key": args.client_key,
        "client_secret": args.client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    },
)

data = resp.json()
if "access_token" not in data:
    print(f"ERROR: Token exchange failed: {data}")
    sys.exit(1)

print("\n" + "="*60)
print("SUCCESS — add these to ~/agency/.env:")
print("="*60)
print(f"TIKTOK_CLIENT_KEY={args.client_key}")
print(f"TIKTOK_CLIENT_SECRET={args.client_secret}")
print(f"TIKTOK_ACCESS_TOKEN={data['access_token']}")
print(f"TIKTOK_REFRESH_TOKEN={data.get('refresh_token', '')}")
print("="*60)
print(f"\nAccess token expires in: {data.get('expires_in', '?')} seconds")
print("The video agent auto-refreshes using TIKTOK_REFRESH_TOKEN.")
