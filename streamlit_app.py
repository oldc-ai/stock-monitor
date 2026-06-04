"""
Put Selling Opportunity Scanner — Streamlit web UI
"""

import streamlit as st
import pandas as pd
from pathlib import Path
import yaml

# Import scanner logic from put_scanner.py in the same directory
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from put_scanner import scan_ticker, TARGET_DTE_MIN, TARGET_DTE_MAX, TARGET_DELTA
import put_scanner as _ps

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def load_config_symbols() -> list[str]:
    if CONFIG_PATH.exists():
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        return [s.upper().strip() for s in data.get("symbols", [])]
    return []


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Put Scanner",
    page_icon="📉",
    layout="wide",
)

st.title("📉 Put Selling Opportunity Scanner")
st.caption("Ranks tickers by attractiveness for selling cash-secured puts at 1–2 week expiration.")

# ---------------------------------------------------------------------------
# Sidebar — settings
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Settings")

    default_symbols = load_config_symbols()
    default_text = "\n".join(default_symbols)

    ticker_input = st.text_area(
        "Tickers (one per line)",
        value=default_text,
        height=300,
        help="Edit the list freely. Defaults to symbols in config.yaml.",
    )

    delta = st.slider(
        "Target delta",
        min_value=0.10,
        max_value=0.40,
        value=0.25,
        step=0.05,
        help="Put delta magnitude to target. 0.25–0.30 is most common.",
    )

    col1, col2 = st.columns(2)
    with col1:
        dte_min = st.number_input("DTE min", min_value=1, max_value=60, value=7)
    with col2:
        dte_max = st.number_input("DTE max", min_value=1, max_value=60, value=21)

    rate = st.number_input(
        "Risk-free rate", min_value=0.0, max_value=0.20, value=0.05, step=0.005, format="%.3f"
    )

    run = st.button("Run Scan", type="primary", use_container_width=True)

# ---------------------------------------------------------------------------
# Main panel — results
# ---------------------------------------------------------------------------
if not run:
    st.info("Configure your settings in the sidebar, then click **Run Scan**.")
    st.stop()

tickers = [t.strip().upper() for t in ticker_input.splitlines() if t.strip() and not t.strip().startswith("#")]
if not tickers:
    st.error("No tickers entered.")
    st.stop()

# Patch module globals for DTE/delta
_ps.TARGET_DTE_MIN = int(dte_min)
_ps.TARGET_DTE_MAX = int(dte_max)
_ps.TARGET_DELTA   = float(delta)

st.subheader(f"Scanning {len(tickers)} tickers — DTE {dte_min}–{dte_max}, ~{delta:.0%} delta")
progress = st.progress(0)
status   = st.empty()

results = []
for i, ticker in enumerate(tickers):
    status.text(f"Fetching {ticker}… ({i+1}/{len(tickers)})")
    r = scan_ticker(ticker, risk_free_rate=float(rate))
    if r:
        results.append(r)
    progress.progress((i + 1) / len(tickers))

progress.empty()
status.empty()

if not results:
    st.error("No results returned. Check your tickers or try again.")
    st.stop()

# Sort by composite score
results.sort(key=lambda x: x["composite_score"], reverse=True)

# ---------------------------------------------------------------------------
# Build display dataframe
# ---------------------------------------------------------------------------
rows = []
for r in results:
    flags = []
    if r["iv_rank"] and r["iv_rank"] >= 50:
        flags.append("HIGH-IVR")
    if r["ann_yield_pct"] >= 50:
        flags.append("FAT-PREM")
    if r["trend_score"] >= 75:
        flags.append("STRONG-TREND")
    if r["otm_pct"] < 3:
        flags.append("CLOSE-TO-MONEY")

    rows.append({
        "Ticker":      r["ticker"],
        "Price":       r["price"],
        "Expiration":  r["expiration"],
        "DTE":         r["dte"],
        "Strike":      r["strike"],
        "Delta":       r["delta"],
        "Premium $":   r["premium"],
        "OTM %":       r["otm_pct"],
        "IV %":        r["iv_current"],
        "IVR":         r["iv_rank"] or 0,
        "Ann Yield %": r["ann_yield_pct"],
        "Trend":       r["trend_score"],
        "Score":       r["composite_score"],
        "Flags":       ", ".join(flags),
        "Prem Source": r.get("prem_source", ""),
    })

df = pd.DataFrame(rows)

# ---------------------------------------------------------------------------
# Colour-code Score column
# ---------------------------------------------------------------------------
def colour_score(val):
    if val >= 80:
        return "background-color: #1a7a1a; color: white"
    elif val >= 65:
        return "background-color: #4a9e4a; color: white"
    elif val >= 50:
        return "background-color: #8ab88a"
    else:
        return "background-color: #c0392b; color: white"

def colour_ivr(val):
    if val >= 75:
        return "color: #27ae60; font-weight: bold"
    elif val >= 50:
        return "color: #f39c12"
    return ""

styled = (
    df.style
    .applymap(colour_score, subset=["Score"])
    .applymap(colour_ivr,   subset=["IVR"])
    .format({
        "Price":       "${:.2f}",
        "Strike":      "${:.2f}",
        "Premium $":   "${:.2f}",
        "OTM %":       "{:.1f}%",
        "IV %":        "{:.0f}%",
        "IVR":         "{:.0f}%",
        "Ann Yield %": "{:.1f}%",
        "Score":       "{:.1f}",
        "Delta":       "{:.2f}",
    })
)

st.dataframe(styled, use_container_width=True, height=600)

# ---------------------------------------------------------------------------
# Summary callouts for top 3
# ---------------------------------------------------------------------------
st.markdown("---")
st.subheader("Top 3 picks")
cols = st.columns(3)
for i, r in enumerate(results[:3]):
    with cols[i]:
        st.metric(
            label=f"#{i+1} {r['ticker']}",
            value=f"Score {r['composite_score']}",
            delta=f"Ann {r['ann_yield_pct']:.0f}% | IVR {r['iv_rank'] or 0:.0f}%",
        )
        st.markdown(
            f"**Strike** ${r['strike']:.2f} &nbsp;|&nbsp; "
            f"**Prem** ${r['premium']:.2f} &nbsp;|&nbsp; "
            f"**DTE** {r['dte']}d &nbsp;|&nbsp; "
            f"**OTM** {r['otm_pct']:.1f}%"
        )

# ---------------------------------------------------------------------------
# Column legend
# ---------------------------------------------------------------------------
with st.expander("Column guide"):
    st.markdown("""
| Column | Meaning |
|---|---|
| **DTE** | Days to expiration |
| **Delta** | Put delta magnitude (~0.25–0.30 target) |
| **Premium $** | Option premium per share |
| **OTM %** | How far the strike is below current price |
| **IV %** | Implied/realised volatility annualised |
| **IVR** | IV Rank — where current IV sits in its 52-week range (higher = more elevated, better to sell) |
| **Ann Yield %** | Annualised premium yield on collateral held |
| **Trend** | Trend health score: price vs SMA20/50/200 + momentum (0–100) |
| **Score** | Composite opportunity score (higher = better) |
""")
