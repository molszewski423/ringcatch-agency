import asyncio
import logging
import os
import random
import re

import httpx
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_JUNK_EMAIL_DOMAINS = {"example.com", "youremail.com", "email.com", "domain.com",
                       "sentry.io", "wixpress.com", "squarespace.com"}
_JUNK_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "pdf", "zip", "mp4", "mov"}

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


async def _delay(lo: float, hi: float) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def scrape_google_maps(niche: str, city: str, max_results: int = 20) -> list[dict]:
    query = f"{niche} in {city}"
    results: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
            ],
        )
        ctx = await browser.new_context(user_agent=_UA, locale="en-US")
        page = await ctx.new_page()

        try:
            search_url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
            await page.goto(search_url, timeout=30_000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                await page.wait_for_selector('div[role="feed"]', timeout=10_000)
            except PWTimeout:
                logger.warning("Feed load timeout for '%s' — proceeding anyway", query)
            await _delay(2, 4)

            # Scroll the results feed to trigger lazy loading
            feed_sel = 'div[role="feed"]'
            for _ in range(4):
                try:
                    await page.evaluate(
                        f'document.querySelector(\'{feed_sel}\').scrollTop += 1500'
                    )
                    await _delay(1.5, 3)
                except Exception:
                    break

            # Collect result cards
            cards = await page.locator(f'{feed_sel} > div').all()
            logger.info(f"Found {len(cards)} cards for '{query}'")

            for card in cards[:max_results]:
                try:
                    # Read name from the card itself BEFORE clicking — avoids h1="Results" race condition
                    card_name = ""
                    for sel in [".fontHeadlineSmall", ".qBF1Pd", "span[class*='fontHead']"]:
                        try:
                            el = card.locator(sel).first
                            if await el.count():
                                t = (await el.text_content() or "").strip()
                                if t and len(t) > 2:
                                    card_name = t
                                    break
                        except Exception:
                            pass

                    # Scroll into view before clicking to avoid off-screen timeouts
                    try:
                        await card.scroll_into_view_if_needed(timeout=3_000)
                    except Exception:
                        pass

                    # Try clicking with fallback to JS click
                    clicked = False
                    for attempt in range(2):
                        try:
                            await card.click(timeout=8_000)
                            clicked = True
                            break
                        except PWTimeout:
                            if attempt == 0:
                                try:
                                    await card.evaluate("el => el.click()")
                                    clicked = True
                                    break
                                except Exception:
                                    pass
                    if not clicked:
                        logger.warning("Could not click card for '%s' — skipping", card_name or "unknown")
                        continue

                    await _delay(1.5, 3.5)

                    # Wait for h1, then retry until it's not "Results"
                    name = card_name
                    try:
                        await page.wait_for_selector("h1", timeout=8_000)
                        for _ in range(8):
                            h1 = await _text(page, "h1")
                            if h1 and h1.lower() not in ("results", ""):
                                name = h1
                                break
                            await _delay(0.4, 0.8)
                    except PWTimeout:
                        pass

                    phone = await _text(page, '[data-item-id^="phone"] .Io6YTe')
                    website = await _text(page, '[data-item-id*="authority"] .Io6YTe')
                    address = await _text(page, '[data-item-id="address"] .Io6YTe')

                    if not name:
                        continue

                    # GBP rating and review count
                    gbp_rating = None
                    gbp_review_count = None
                    for r_sel in [".MW4etd", 'span[aria-label*=" star"]']:
                        rt = await _text(page, r_sel)
                        if rt:
                            try:
                                gbp_rating = float(rt.split()[0])
                                break
                            except (ValueError, IndexError):
                                pass
                    for rv_sel in [".UY7F9", 'span[aria-label*=" review"]', 'button[aria-label*=" review"]']:
                        rvt = await _text(page, rv_sel)
                        if rvt:
                            m = re.search(r"[\d,]+", rvt)
                            if m:
                                gbp_review_count = int(m.group().replace(",", ""))
                                break

                    results.append({
                        "business_name": name,
                        "phone": phone,
                        "website": _normalize_url(website),
                        "address": address,
                        "city": city,
                        "niche": niche,
                        "email": "",
                        "domain": "",
                        "gbp_rating": gbp_rating,
                        "gbp_review_count": gbp_review_count,
                    })

                    await _delay(1.5, 4)

                except PWTimeout:
                    logger.warning("Timeout on card '%s' — skipping", card_name or "unknown")
                except Exception as exc:
                    logger.warning("Card extraction error: %s", exc)

        finally:
            await browser.close()

    return results


async def extract_email_from_website(url: str) -> tuple[str, str]:
    if not url:
        return "", ""

    try:
        domain = url.split("//")[1].split("/")[0].removeprefix("www.")
    except IndexError:
        return "", ""

    pages = [url, f"{url.rstrip('/')}/contact", f"{url.rstrip('/')}/about"]

    async with httpx.AsyncClient(follow_redirects=True, timeout=10,
                                  headers={"User-Agent": _UA}) as client:
        for target in pages:
            try:
                resp = await client.get(target)
                for match in EMAIL_RE.findall(resp.text):
                    email_domain = match.split("@")[1].lower()
                    tld = email_domain.rsplit(".", 1)[-1]
                    if (email_domain not in _JUNK_EMAIL_DOMAINS
                            and tld not in _JUNK_TLDS
                            and "." in email_domain):
                        return match.lower(), domain
                await _delay(1, 3)
            except Exception:
                continue

    return "", domain


async def hunter_lookup(domain: str, api_key: str) -> str:
    """Try Hunter.io first, fall back to Prospeo (free tier), then Anymailfinder."""
    if not domain:
        return ""

    # 1. Hunter.io
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.hunter.io/v2/domain-search",
                    params={"domain": domain, "api_key": api_key, "limit": "1"},
                )
            if resp.status_code == 200:
                for entry in resp.json().get("data", {}).get("emails", []):
                    if entry.get("confidence", 0) > 50:
                        return entry["value"]
            elif resp.status_code == 429:
                logger.debug("Hunter rate-limited for %s — trying fallbacks", domain)
        except Exception as e:
            logger.debug("Hunter lookup failed for %s: %s", domain, e)

    return ""


async def _text(page, selector: str) -> str:
    el = page.locator(selector).first
    if await el.count():
        return (await el.text_content() or "").strip()
    return ""


def _normalize_url(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    if raw.startswith("http"):
        return raw
    return f"https://{raw}"
