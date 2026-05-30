import os
import sqlite3
import time
from pathlib import Path

import httpx
import pandas as pd
import streamlit as st

DATA_DIR = Path("/data")
DB_PATH = DATA_DIR / "agency.db"
SCRAPER_URL  = os.environ.get("SCRAPER_URL",  "http://localhost:8079")
OUTREACH_URL = os.environ.get("OUTREACH_URL", "http://localhost:8080")
SUPPORT_URL  = os.environ.get("SUPPORT_URL",  "http://localhost:8104")

st.set_page_config(
    page_title="RingCatch Dashboard",
    page_icon="🔔",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&display=swap');

  html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

  /* Hide default Streamlit chrome */
  #MainMenu, footer, header { visibility: hidden; }
  .block-container { padding: 0 2rem 2rem; max-width: 100%; }

  /* Brand header */
  .rc-header {
    background: linear-gradient(135deg, #0b0b14 0%, #111122 100%);
    border-bottom: 1px solid #1c1c30;
    padding: 18px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin: -2rem -2rem 2rem;
  }
  .rc-logo { font-size: 1.4rem; font-weight: 900; letter-spacing: -0.5px; color: #e2e8f0; }
  .rc-logo span { color: #22d3ee; }
  .rc-tagline { font-size: 0.75rem; color: #64748b; margin-top: 2px; }

  /* Metric cards */
  .metric-row { display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
  .metric-card {
    flex: 1 1 130px;
    background: #111122;
    border: 1px solid #1c1c30;
    border-radius: 12px;
    padding: 20px 18px;
    transition: border-color 0.2s;
  }
  .metric-card:hover { border-color: #22d3ee; }
  .metric-label { font-size: 0.68rem; font-weight: 700; text-transform: uppercase;
                  letter-spacing: 1.5px; color: #64748b; margin-bottom: 8px; }
  .metric-value { font-size: 2rem; font-weight: 900; color: #e2e8f0; line-height: 1; }
  .metric-sub { font-size: 0.75rem; color: #22d3ee; margin-top: 6px; font-weight: 600; }
  .metric-rev .metric-value { color: #4ade80; }

  /* Section headers */
  .section-title {
    font-size: 0.7rem; font-weight: 800; text-transform: uppercase;
    letter-spacing: 2px; color: #22d3ee; margin-bottom: 12px; margin-top: 8px;
  }

  /* Control panel */
  .ctrl-panel {
    background: #111122;
    border: 1px solid #1c1c30;
    border-radius: 12px;
    padding: 20px 22px;
    margin-bottom: 20px;
  }
  .ctrl-title { font-size: 0.9rem; font-weight: 800; margin-bottom: 14px; color: #e2e8f0; }

  /* Table styling */
  .stDataFrame { border-radius: 10px; overflow: hidden; }
  .stDataFrame thead th {
    background: #111122 !important;
    color: #64748b !important;
    font-size: 0.72rem !important;
    text-transform: uppercase !important;
    letter-spacing: 1px !important;
  }

  /* Tab styling */
  .stTabs [data-baseweb="tab-list"] {
    background: #111122;
    border-radius: 10px 10px 0 0;
    padding: 4px;
    gap: 4px;
    border: 1px solid #1c1c30;
    border-bottom: none;
  }
  .stTabs [data-baseweb="tab"] {
    background: transparent;
    color: #64748b;
    font-size: 0.78rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1px;
    border-radius: 6px;
    padding: 8px 18px;
  }
  .stTabs [aria-selected="true"] {
    background: #22d3ee !important;
    color: #0b0b14 !important;
  }
  .stTabs [data-baseweb="tab-panel"] {
    background: #111122;
    border: 1px solid #1c1c30;
    border-radius: 0 10px 10px 10px;
    padding: 16px;
  }

  /* Buttons */
  .stButton > button {
    background: #22d3ee;
    color: #0b0b14;
    font-weight: 800;
    font-size: 0.85rem;
    border: none;
    border-radius: 8px;
    padding: 10px 22px;
    transition: all 0.15s;
    letter-spacing: 0.3px;
  }
  .stButton > button:hover {
    background: #0891b2;
    color: #fff;
    transform: translateY(-1px);
    box-shadow: 0 4px 16px rgba(34,211,238,0.25);
  }
  .stButton > button:focus { box-shadow: 0 0 0 3px rgba(34,211,238,0.3); }

  /* Inputs */
  .stTextInput > div > div > input,
  .stNumberInput > div > div > input {
    background: #0b0b14 !important;
    border: 1px solid #1c1c30 !important;
    color: #e2e8f0 !important;
    border-radius: 7px !important;
  }
  .stTextInput > div > div > input:focus,
  .stNumberInput > div > div > input:focus {
    border-color: #22d3ee !important;
    box-shadow: 0 0 0 2px rgba(34,211,238,0.15) !important;
  }

  /* Success/error messages */
  .stSuccess { background: rgba(74,222,128,0.1) !important; border: 1px solid #4ade80 !important; border-radius: 8px !important; }
  .stError   { background: rgba(239,68,68,0.1)  !important; border: 1px solid #ef4444 !important; border-radius: 8px !important; }

  /* Divider */
  hr { border-color: #1c1c30 !important; }

  /* Status dot */
  .dot-green { display:inline-block; width:8px; height:8px; background:#4ade80; border-radius:50%; margin-right:6px; }
  .dot-red   { display:inline-block; width:8px; height:8px; background:#ef4444; border-radius:50%; margin-right:6px; }

  /* Auto-refresh toggle */
  .stCheckbox { color: #64748b; font-size: 0.82rem; }
</style>
""", unsafe_allow_html=True)


def get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def _count(db: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        return db.execute(sql, params).fetchone()[0]
    except Exception:
        return 0


def _df(db: sqlite3.Connection, sql: str, cols: list[str]) -> pd.DataFrame:
    try:
        rows = db.execute(sql).fetchall()
        return pd.DataFrame(rows, columns=cols)
    except Exception:
        return pd.DataFrame(columns=cols)


# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="rc-header">
  <div>
    <div class="rc-logo">RING<span>CATCH</span></div>
    <div class="rc-tagline">Operations Dashboard</div>
  </div>
  <div style="font-size:0.75rem; color:#64748b;">AI chatbot agency · automated pipeline</div>
</div>
""", unsafe_allow_html=True)

db = get_conn()

# ── Metrics ───────────────────────────────────────────────────────────────────
leads_n    = _count(db, "SELECT COUNT(*) FROM leads")
scored_n   = _count(db, "SELECT COUNT(*) FROM leads WHERE score > 0")
avg_score_row = db.execute("SELECT ROUND(AVG(score),0) FROM leads WHERE score > 0").fetchone()
avg_score  = int(avg_score_row[0] or 0) if avg_score_row else 0
sent_n     = _count(db, "SELECT COUNT(*) FROM outreach")
replied_n  = _count(db, "SELECT COUNT(*) FROM outreach WHERE replied=1")
booking_n  = _count(db, "SELECT COUNT(*) FROM bookings")
paid_n     = _count(db, "SELECT COUNT(*) FROM payments WHERE status='paid'")
done_n     = _count(db, "SELECT COUNT(*) FROM deliveries WHERE status='delivered'")
revenue_c  = _count(db, "SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid'")
reply_pct  = int(replied_n / max(sent_n, 1) * 100)

st.markdown(f"""
<div class="metric-row">
  <div class="metric-card">
    <div class="metric-label">Leads</div>
    <div class="metric-value">{leads_n:,}</div>
    <div class="metric-sub">{scored_n} scored</div>
  </div>
  <div class="metric-card">
    <div class="metric-label">Avg Score</div>
    <div class="metric-value">{avg_score}</div>
    <div class="metric-sub">Lead quality /100</div>
  </div>
  <div class="metric-card">
    <div class="metric-label">Emails Sent</div>
    <div class="metric-value">{sent_n:,}</div>
    <div class="metric-sub">Outreach total</div>
  </div>
  <div class="metric-card">
    <div class="metric-label">Replies</div>
    <div class="metric-value">{replied_n:,}</div>
    <div class="metric-sub">{reply_pct}% reply rate</div>
  </div>
  <div class="metric-card">
    <div class="metric-label">Bookings</div>
    <div class="metric-value">{booking_n:,}</div>
    <div class="metric-sub">Calls booked</div>
  </div>
  <div class="metric-card">
    <div class="metric-label">Paid</div>
    <div class="metric-value">{paid_n:,}</div>
    <div class="metric-sub">Clients closed</div>
  </div>
  <div class="metric-card">
    <div class="metric-label">Delivered</div>
    <div class="metric-value">{done_n:,}</div>
    <div class="metric-sub">Bots live</div>
  </div>
  <div class="metric-card metric-rev">
    <div class="metric-label">Revenue</div>
    <div class="metric-value">${revenue_c / 100:,.0f}</div>
    <div class="metric-sub">Total collected</div>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Controls ──────────────────────────────────────────────────────────────────
st.markdown('<div class="section-title">Pipeline Controls</div>', unsafe_allow_html=True)

col_a, col_b, col_c = st.columns([1, 1, 1])

with col_a:
    st.markdown('<div class="ctrl-panel"><div class="ctrl-title">🗺 Scraper</div>', unsafe_allow_html=True)
    if st.button("Run scraper (targets.yaml)", key="run_scraper", use_container_width=True):
        with st.spinner("Triggering scraper..."):
            try:
                r = httpx.post(f"{SCRAPER_URL}/scrape", timeout=10)
                st.success(f"✓ {r.json()}")
            except Exception as e:
                st.error(f"✗ {e}")
    st.markdown('</div>', unsafe_allow_html=True)

with col_b:
    st.markdown('<div class="ctrl-panel"><div class="ctrl-title">🎯 Custom Scrape</div>', unsafe_allow_html=True)
    niche_in = st.text_input("Niche", "HVAC", key="niche_in", label_visibility="collapsed",
                              placeholder="Niche (e.g. HVAC)")
    city_in  = st.text_input("City",  "Houston, TX", key="city_in", label_visibility="collapsed",
                              placeholder="City (e.g. Houston, TX)")
    if st.button("Scrape now", key="custom_scrape", use_container_width=True):
        with st.spinner(f"Scraping {niche_in} in {city_in}..."):
            try:
                r = httpx.post(f"{SCRAPER_URL}/scrape",
                               params={"niche": niche_in, "city": city_in}, timeout=10)
                st.success(f"✓ {r.json()}")
            except Exception as e:
                st.error(f"✗ {e}")
    st.markdown('</div>', unsafe_allow_html=True)

with col_c:
    st.markdown('<div class="ctrl-panel"><div class="ctrl-title">✉️ Outreach</div>', unsafe_allow_html=True)
    if st.button("Send next email batch", key="send_outreach", use_container_width=True):
        with st.spinner("Triggering outreach agent..."):
            try:
                r = httpx.post(f"{OUTREACH_URL}/send", timeout=15)
                st.success(f"✓ {r.json()}")
            except Exception as e:
                st.error(f"✗ {e}")
    if st.button("Advance follow-up sequence", key="advance_seq", use_container_width=True):
        with st.spinner("Advancing sequence..."):
            try:
                r = httpx.post(f"{OUTREACH_URL}/advance", timeout=15)
                st.success(f"✓ {r.json()}")
            except Exception as e:
                st.error(f"✗ {e}")
    st.markdown('</div>', unsafe_allow_html=True)

# ── Data Tabs ─────────────────────────────────────────────────────────────────
st.markdown('<div class="section-title" style="margin-top:16px;">Pipeline Data</div>', unsafe_allow_html=True)

tab1, tab2, tab3, tab4, tab5 = st.tabs(["Leads", "Outreach", "Payments", "Deliveries", "System Health"])

with tab1:
    col_search, col_filter = st.columns([3, 1])
    with col_search:
        search = st.text_input("Search leads", placeholder="Filter by name, email, city…",
                               key="lead_search", label_visibility="collapsed")
    with col_filter:
        niche_filter = st.text_input("Niche filter", placeholder="e.g. HVAC",
                                     key="lead_niche", label_visibility="collapsed")

    df = _df(db, """
        SELECT business_name, email, city, niche, score,
               CASE has_chatbot WHEN 1 THEN chatbot_type ELSE '—' END AS chatbot,
               COALESCE(cms, '?') AS cms,
               ROUND(gbp_rating, 1) AS rating, gbp_review_count AS reviews,
               scraped_date
        FROM leads ORDER BY score DESC, id DESC LIMIT 500
    """, ["Business", "Email", "City", "Niche", "Score", "Chatbot", "CMS", "Rating", "Reviews", "Date"])

    if search:
        mask = df.apply(lambda row: row.astype(str).str.contains(search, case=False).any(), axis=1)
        df = df[mask]
    if niche_filter:
        df = df[df["Niche"].str.contains(niche_filter, case=False, na=False)]

    st.caption(f"{len(df)} leads shown · sorted by quality score")
    st.dataframe(df, use_container_width=True, height=400)

with tab2:
    df = _df(db, """
        SELECT l.business_name, l.email, o.sequence_step, o.sent_at, o.replied
        FROM outreach o JOIN leads l ON l.id = o.lead_id
        ORDER BY o.sent_at DESC LIMIT 500
    """, ["Business", "Email", "Step", "Sent At", "Replied"])
    st.dataframe(df, use_container_width=True, height=320)

    st.markdown("---")
    c1, c2 = st.columns([1, 2])
    with c1:
        lead_id_in = st.number_input("Lead ID", min_value=1, step=1, key="lead_id_mark",
                                      label_visibility="visible")
    with c2:
        st.write("")
        st.write("")
        if st.button("Mark as replied", key="mark_replied"):
            db.execute("UPDATE outreach SET replied=1 WHERE lead_id=?", (int(lead_id_in),))
            db.commit()
            st.success(f"✓ Lead {int(lead_id_in)} marked as replied")

with tab3:
    df = _df(db, """
        SELECT client_name, ROUND(amount / 100.0, 2), status, created_at
        FROM payments ORDER BY created_at DESC
    """, ["Client", "Amount ($)", "Status", "Date"])
    st.dataframe(df, use_container_width=True, height=380)

with tab4:
    df = _df(db, """
        SELECT client_id, client_name, niche, status, created_at
        FROM deliveries ORDER BY created_at DESC
    """, ["ID", "Client", "Niche", "Status", "Date"])
    st.dataframe(df, use_container_width=True, height=380)

with tab5:
    h1, h2 = st.columns([1, 1])

    with h1:
        st.markdown("**Agent Heartbeats**")
        df_agents = _df(db, """
            SELECT agent_name, status, last_heartbeat, last_action, actions_today, alerts_active
            FROM agent_status ORDER BY last_heartbeat DESC
        """, ["Agent", "Status", "Last Heartbeat", "Last Action", "Actions Today", "Active Alerts"])
        if not df_agents.empty:
            def _color_status(val):
                color = "#4ade80" if val == "online" else "#ef4444"
                return f"color: {color}; font-weight: 700"
            st.dataframe(
                df_agents.style.applymap(_color_status, subset=["Status"]),
                use_container_width=True, height=300
            )
        else:
            st.info("No heartbeat data yet")

        st.markdown("**Recent Alerts**")
        df_alerts = _df(db, """
            SELECT timestamp, agent, severity, message, acknowledged
            FROM alerts ORDER BY timestamp DESC LIMIT 20
        """, ["Time", "Agent", "Severity", "Message", "Ack"])
        st.dataframe(df_alerts, use_container_width=True, height=220)

    with h2:
        st.markdown("**Recent Incidents (24h)**")
        df_inc = _df(db, """
            SELECT timestamp, service, event_type, details,
                   CASE resolved WHEN 1 THEN 'resolved' ELSE 'open' END AS state
            FROM incidents
            WHERE timestamp >= datetime('now', '-24 hours')
            ORDER BY timestamp DESC
        """, ["Time", "Service", "Event", "Details", "State"])
        if not df_inc.empty:
            st.dataframe(df_inc, use_container_width=True, height=300)
        else:
            st.success("No incidents in the last 24 hours")

        st.markdown("**Live Support Status**")
        try:
            resp = httpx.get(f"{SUPPORT_URL}/status", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                svc_status = data.get("service_status", {})
                rows = []
                for name, info in svc_status.items():
                    rows.append({
                        "Service": name,
                        "Status": info.get("status", "?"),
                        "Failures": info.get("failures", 0),
                        "Last Check": (info.get("last_check") or "")[:16],
                    })
                if rows:
                    df_svc = pd.DataFrame(rows)
                    st.dataframe(df_svc, use_container_width=True, height=300)
                else:
                    st.info("Support agent initialising...")
            else:
                st.warning(f"Support agent returned {resp.status_code}")
        except Exception as e:
            st.warning(f"Support agent unreachable: {e}")

# ── Auto-refresh ──────────────────────────────────────────────────────────────
st.markdown("---")
auto_refresh = st.checkbox("Auto-refresh every 60s", key="auto_refresh")
if auto_refresh:
    time.sleep(60)
    st.rerun()
