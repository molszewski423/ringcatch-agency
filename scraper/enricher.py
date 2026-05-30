import logging
import re
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_CHATBOT_PATTERNS = [
    ("intercom",   "intercom"),
    ("drift",      "drift.com"),
    ("tidio",      "tidio"),
    ("livechat",   "livechatinc.com"),
    ("crisp",      "crisp.chat"),
    ("tawk",       "tawk.to"),
    ("hubspot",    "js.hs-scripts.com"),
    ("zendesk",    "zopim"),
    ("freshdesk",  "freshchat"),
    ("olark",      "olark"),
    ("smartsupp",  "smartsupp"),
    ("purechat",   "purechat"),
    ("userlike",   "userlike"),
]

_CMS_PATTERNS = [
    ("wordpress",   ["wp-content", "wp-includes"]),
    ("wix",         ["wix.com", "wixstatic"]),
    ("squarespace", ["squarespace.com", "static1.squarespace"]),
    ("shopify",     ["cdn.shopify.com"]),
    ("webflow",     ["webflow.com"]),
    ("weebly",      ["weebly.com"]),
    ("godaddy",     ["godaddysites.com"]),
    ("jimdo",       ["jimdostatic"]),
]

_GOOGLE_ADS_PATTERNS = ["googletag", "gtag(", "google_ad_client", "adsbygoogle"]


async def enrich(website_url: str, domain: str) -> dict:
    """Fetch website and extract lead-quality signals. Always returns a dict."""
    result = {
        "has_chatbot": 0,
        "chatbot_type": "",
        "cms": "",
        "has_google_ads": 0,
        "domain_age_years": None,
        "site_response_ms": None,
    }

    if website_url:
        html, ms = await _fetch_html(website_url)
        if html:
            result["site_response_ms"] = ms
            result["has_chatbot"], result["chatbot_type"] = _detect_chatbot(html)
            result["cms"] = _detect_cms(html)
            result["has_google_ads"] = _detect_google_ads(html)

    if domain:
        result["domain_age_years"] = await _domain_age(domain)

    return result


async def _fetch_html(url: str) -> tuple[str, int]:
    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=10,
            headers={"User-Agent": _UA}
        ) as c:
            resp = await c.get(url)
        ms = int((time.monotonic() - t0) * 1000)
        return resp.text[:200_000], ms
    except Exception as e:
        logger.debug("Enricher fetch failed for %s: %s", url, e)
        return "", 0


def _detect_chatbot(html: str) -> tuple[int, str]:
    lower = html.lower()
    for name, pattern in _CHATBOT_PATTERNS:
        if pattern in lower:
            return 1, name
    return 0, ""


def _detect_cms(html: str) -> str:
    lower = html.lower()
    for name, patterns in _CMS_PATTERNS:
        if any(p in lower for p in patterns):
            return name
    m = re.search(
        r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    )
    if m:
        return m.group(1).split()[0].lower()
    return ""


def _detect_google_ads(html: str) -> int:
    lower = html.lower()
    return 1 if any(p in lower for p in _GOOGLE_ADS_PATTERNS) else 0


async def _domain_age(domain: str) -> float | None:
    """RDAP lookup for domain creation date. Returns age in years or None."""
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            resp = await c.get(
                f"https://rdap.org/domain/{domain}",
                headers={"Accept": "application/json"},
            )
        if resp.status_code != 200:
            return None
        for event in resp.json().get("events", []):
            if event.get("eventAction") == "registration":
                dt_str = event.get("eventDate", "")
                if dt_str:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    return round((now - dt).days / 365.25, 1)
    except Exception as e:
        logger.debug("RDAP lookup failed for %s: %s", domain, e)
    return None
