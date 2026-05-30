import logging

logger = logging.getLogger(__name__)

SCORE_THRESHOLD = 35  # leads below this score are not stored or emailed


def score_lead(lead: dict, enrichment: dict) -> int:
    """Return a 0-100 quality score. Hard filters return 0."""

    # Hard filters
    if not lead.get("website"):
        return 0
    if not lead.get("email"):
        return 0
    age = enrichment.get("domain_age_years")
    if age is not None and age < 0.5:
        return 0  # domain registered < 6 months ago — not yet established

    s = 0

    # Contact completeness
    if lead.get("phone"):    s += 5
    if lead.get("website"):  s += 10
    if lead.get("email"):    s += 5

    # Chatbot situation — no chatbot is our best signal
    chatbot_type = enrichment.get("chatbot_type", "")
    if not enrichment.get("has_chatbot"):
        s += 20  # prime prospect — no chat solution yet
    elif chatbot_type in ("tawk", "tidio"):
        s += 5   # free/basic widget — convertible
    # enterprise chat (intercom, drift, zendesk): +0 — may already be solved

    # GBP rating and review volume
    rating  = lead.get("gbp_rating") or 0
    reviews = lead.get("gbp_review_count") or 0
    if 3.5 <= rating <= 4.7:    s += 10
    elif rating > 4.7:          s += 7   # excellent but may be hard to impress
    if 3 <= reviews <= 200:     s += 15  # lowered floor from 10 → 3 to capture newer SMBs
    elif reviews > 200:         s += 5   # large business — may not be SMB target

    # Domain age — give benefit of the doubt if unknown
    if age is None:
        s += 5   # can't verify but don't penalise
    elif 1 <= age <= 15:
        s += 10
    elif age > 15:
        s += 5

    # CMS — easier embed = better prospect
    cms = enrichment.get("cms", "")
    if cms == "wordpress":                          s += 8
    elif cms in ("wix", "squarespace", "webflow",
                 "shopify"):                        s += 4
    elif cms == "godaddy":                          s += 2

    # Google Ads presence → has marketing budget
    if enrichment.get("has_google_ads"):            s += 10

    # Site response time — fast site = active business
    ms = enrichment.get("site_response_ms") or 9999
    if ms < 1500:   s += 5
    elif ms < 3000: s += 2

    return min(s, 100)
