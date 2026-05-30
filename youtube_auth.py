#!/usr/bin/env python3
"""
One-time YouTube OAuth setup. Run this on your local machine (not in the container).

Prerequisites:
  pip install google-auth-oauthlib google-api-python-client

Steps:
  1. Go to https://console.cloud.google.com
  2. Create a project named "RingCatch"
  3. APIs & Services → Enable APIs → search "YouTube Data API v3" → Enable
  4. APIs & Services → Credentials → Create Credentials → OAuth client ID
     Application type: Desktop app  |  Name: ringcatch-video
  5. Download JSON  OR  note Client ID + Client Secret
  6. Run: python3 youtube_auth.py --client-id YOUR_ID --client-secret YOUR_SECRET
  7. A browser window opens — sign in with the RingCatch Google account and approve
  8. Copy the YOUTUBE_REFRESH_TOKEN printed at the end into your .env file

Usage:
  python3 youtube_auth.py --client-id xxx --client-secret yyy
  # OR set env vars:
  YOUTUBE_CLIENT_ID=xxx YOUTUBE_CLIENT_SECRET=yyy python3 youtube_auth.py
"""
import argparse
import os
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Missing dependency. Run:  pip install google-auth-oauthlib google-api-python-client")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

parser = argparse.ArgumentParser(description="Get YouTube OAuth refresh token")
parser.add_argument("--client-id",     default=os.environ.get("YOUTUBE_CLIENT_ID", ""))
parser.add_argument("--client-secret", default=os.environ.get("YOUTUBE_CLIENT_SECRET", ""))
args = parser.parse_args()

if not args.client_id or not args.client_secret:
    print("ERROR: provide --client-id and --client-secret (or set YOUTUBE_CLIENT_ID/SECRET env vars)")
    sys.exit(1)

client_config = {
    "installed": {
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=0)

print("\n" + "="*60)
print("SUCCESS — add these to ~/agency/.env:")
print("="*60)
print(f"YOUTUBE_CLIENT_ID={args.client_id}")
print(f"YOUTUBE_CLIENT_SECRET={args.client_secret}")
print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
print("="*60)
