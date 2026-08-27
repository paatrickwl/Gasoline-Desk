"""
app.py -- Autonomous Indonesia Energy Intelligence & Swarm Prediction Desk

Streamlit multi-page dashboard wiring together:
  * engine.py            -> Subsidy Gap Tracker (institutional pricing)
  * simulation_engine.py -> Macro Shock Sandbox (multi-agent swarm)
  * forecast_engine.py   -> Oil Price Forecast & Drivers
  * osint_scraper.py     -> OSINT / Simulation intelligence feed

Guardrail: every page wraps its data calls in try/except and surfaces a
clear "source: live | mock | baseline_fallback | cached_fallback" badge so
the user always knows whether they're looking at fresh data or the cached
baseline -- never a silently invented number.
"""

import json
from datetime import datetime, timezone
from typing import Optional

import plotly.graph_objects as go
import streamlit as st

from engine import PricingEngine
from forecast_engine import ForecastEngine
from fuel_price_scraper import get_live_fuel_prices
from margin_optimizer import AdministeredFuelError, get_competitor_prices, optimize_price
from osint_scraper import get_osint_feed, summarize_risk_signals
from simulation_engine import ShockScenario, SimulationEngine

st.set_page_config(page_title="Indonesia Energy Intelligence Desk", layout="wide")

# Bain & Company-style palette: signature red, flat black/white/gray, no gradients.
RED = "#C8102E"
BLACK = "#1A1A1A"
GRAY_DARK = "#4D4D4D"
GRAY_MID = "#8C8C8C"
GRAY_LINE = "#D9D9D9"
GRAY_BG = "#F7F7F7"

BENCHMARK_LABELS = {
    "icp_usd_bbl": "ICP",
    "mops92_usd_bbl": "MOPS 92",
    "mops95_usd_bbl": "MOPS 95",
    "mops97_usd_bbl": "MOPS 97",
    "mops_gasoil_usd_bbl": "Gasoil CN48",
    "mops_gasoil_cn51_usd_bbl": "Gasoil CN51",
    "brent_usd_bbl": "Brent",
}
# ICP/Brent are CRUDE oil benchmarks -- Kepmen 245/2022 prices BBM Umum off
# refined-PRODUCT benchmarks (MOPS/Argus Gasoline & Gas Oil) only. Selecting
# a crude benchmark for a fuel is never regulation-compliant; kept in the
# selector purely for macro what-if exploration, not as a real cost basis.
CRUDE_BENCHMARK_KEYS = {"icp_usd_bbl", "brent_usd_bbl"}


@st.cache_resource
def get_pricing_engine() -> PricingEngine:
    return PricingEngine()


def get_fuel_catalog() -> dict:
    """fuel_key -> fuel dict (label, brand, price_idr_liter, is_administered,
    benchmark_key), straight from the baseline -- single source of truth so
    adding a SKU only means editing data/baseline_parameters.json."""
    return get_pricing_engine().baseline.get("fuels", {})


@st.cache_resource
def get_simulation_engine() -> SimulationEngine:
    return SimulationEngine()


@st.cache_resource
def get_forecast_engine() -> ForecastEngine:
    return ForecastEngine(get_pricing_engine().baseline)


def source_badge(source: str) -> None:
    colors = {
        "live": "🟢 live",
        "mock": "🟡 mock (demo)",
        "baseline_fallback": "🟠 baseline fallback (cached)",
        "cached_fallback": "🟠 cached fallback (live feed unavailable)",
    }
    st.caption(colors.get(source, f"⚪ {source}"))


def inject_bain_style() -> None:
    """Global type + control-bar styling for the exec-facing pages.
    Barlow Condensed for headline numerals, Inter for body -- flat
    black/white/red, no shadows, no rounded cards."""
    st.markdown(
        f"""
        <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=Inter:wght@400;500;600;700&display=swap">
        <style>
        html, body, [class*="css"] {{ font-family: 'Inter', sans-serif; }}
        .head {{ font-family: 'Barlow Condensed', 'Arial Narrow', sans-serif; font-variant-numeric: tabular-nums; }}
        div[role="radiogroup"] {{ gap: 22px; }}
        div[role="radiogroup"] label {{
            border-bottom: 3px solid transparent;
            padding-bottom: 6px;
            margin-right: 0 !important;
        }}
        div[role="radiogroup"] label[data-checked="true"] {{ border-bottom: 3px solid {RED}; }}
        .block-container {{ padding-top: 1.2rem !important; padding-bottom: 1rem !important; max-width: 100% !important; }}
        div[data-testid="stVerticalBlock"] {{ gap: 0.35rem; }}

        /* Sidebar ~25% narrower than Streamlit's default 21rem, but with
        looser vertical rhythm than the main content -- the tight 0.35rem
        gap above is right for the dense main dashboard, not for a list of
        distinct status lines that need visual breathing room. */
        section[data-testid="stSidebar"] {{ width: 268px !important; min-width: 268px !important; max-width: 268px !important; }}
        section[data-testid="stSidebar"] div[data-testid="stVerticalBlock"] {{ gap: 0.85rem; }}
        section[data-testid="stSidebar"] .stCaption p {{ line-height: 1.5; margin-bottom: 0; }}
        section[data-testid="stSidebar"] hr {{ margin: 0.4rem 0; }}

        </style>
        """,
        unsafe_allow_html=True,
    )


def _bain_layout(fig: go.Figure, height: int = 220) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=10, r=30, t=40, b=10),
        plot_bgcolor="white",
        paper_bgcolor="white",
        showlegend=False,
        font=dict(family="Inter, sans-serif", color=BLACK, size=14),
    )
    return fig


def _position_vs_cost_chart(market_price: float, admin_price: float, retail_label: str = "Administered") -> go.Figure:
    """Bullet-style: black bar = market-clearing/cost-to-serve price, red marker = retail price."""
    scale_max = max(market_price, admin_price) * 1.3
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=[market_price], y=["Price"], orientation="h",
        marker_color=BLACK, width=0.85,
        text=[f"Market-clearing<br>Rp {market_price:,.0f}"], textposition="outside",
        insidetextanchor="start", textfont=dict(size=14), cliponaxis=False,
    ))
    fig.add_shape(
        type="line", x0=admin_price, x1=admin_price, y0=-0.5, y1=0.5,
        line=dict(color=RED, width=5),
    )
    fig.add_annotation(
        x=admin_price, y=0.65, showarrow=False, yanchor="bottom",
        text=f"{retail_label}  Rp {admin_price:,.0f}", font=dict(color=RED, size=14, family="Inter"),
    )
    fig.update_xaxes(range=[0, scale_max], showgrid=False, zeroline=False, visible=False)
    fig.update_yaxes(visible=False, range=[-0.9, 0.9])
    return _bain_layout(fig, height=200)


def _tax_waterfall_chart(pricing: dict) -> go.Figure:
    """Hand-built floating waterfall via go.Bar(base=...) -- go.Waterfall has
    no per-point marker.color, and Base/Final need distinct colors from the
    middle tax layers, so a plain Bar trace with explicit bases gives full
    control over which segment gets which color."""
    mops = pricing["mops_idr_liter"]
    konstanta = pricing["konstanta_idr_liter"]
    margin = pricing["margin_idr_liter"]
    pbbkb = pricing["pbbkb_idr_liter"]
    ppn = pricing["ppn_idr_liter"]
    final = pricing["final_price_idr_liter"]

    categories = ["MOPS", "+Konstanta", "+Margin 10/90", "+PBBKB", "+PPN", "Final Price"]
    running = [mops, mops + konstanta, mops + konstanta + margin]
    running.append(running[-1] + pbbkb)
    bases = [0, mops, running[0], running[1], running[2], 0]
    heights = [mops, konstanta, margin, pbbkb, ppn, final]
    colors = [BLACK, GRAY_MID, GRAY_MID, GRAY_MID, GRAY_MID, RED]
    text = [f"{mops:,.0f}", f"+{konstanta:,.0f}", f"+{margin:,.0f}", f"+{pbbkb:,.0f}", f"+{ppn:,.0f}", f"{final:,.0f}"]
    tops = [b + h for b, h in zip(bases, heights)]

    fig = go.Figure(go.Bar(
        x=categories, y=heights, base=bases,
        marker_color=colors, text=text, textposition="outside",
        width=0.7, textfont=dict(size=14),
    ))
    for i in range(len(categories) - 2):
        fig.add_shape(
            type="line", x0=i + 0.25, x1=i + 1 - 0.25, y0=tops[i], y1=tops[i],
            line=dict(color=GRAY_LINE, width=1, dash="dot"),
        )
    fig.update_xaxes(showgrid=False, tickfont=dict(size=12))
    fig.update_yaxes(showgrid=False, visible=False, range=[0, final * 1.25])
    return _bain_layout(fig, height=260)


def _build_gap_treemap(engine: PricingEngine, fuels: dict, live_fuel_data: Optional[dict] = None,
                        province: str = "Prov. DKI Jakarta") -> go.Figure:
    """Portfolio-wide view: every fuel in the catalog, grouped by brand, sized
    by |gap| (Rp/L) and colored by SIGNED gap. The sign convention is the
    same across administered and commercial fuels (compute_subsidy_gap =
    market_clearing - retail price), so one diverging color scale reads
    correctly for both: positive/red = price below cost (state absorbs, or
    retailer sells at a loss); negative/green = price above cost.

    Each fuel is priced on ITS OWN catalog benchmark_key -- not whichever
    benchmark happens to be selected in the page's control bar -- since
    gasoline-class and diesel-class fuels track different benchmarks.

    Uses live scraped retail prices where available (same source as the
    hero numbers above), falling back to the baseline catalog per fuel --
    so this never silently mixes a live number for the selected fuel with
    a stale placeholder for everyone else."""
    labels, parents, values, colors, hover = [], [], [], [], []

    brands = sorted({f["brand"] for f in fuels.values()})
    for b in brands:
        labels.append(b)
        parents.append("")
        values.append(0)
        colors.append(None)
        hover.append("")

    for fuel_key, f in fuels.items():
        live_price = _extract_live_price(live_fuel_data, fuel_key, province) if live_fuel_data else None
        try:
            result = engine.price_fuel(f["benchmark_key"], fuel_key, live_retail_price=live_price)
        except Exception:
            continue
        gap_dict = result.get("subsidy_gap")
        gap = gap_dict["subsidy_gap_idr_liter"] if gap_dict else 0.0
        kind = "Subsidy Gap" if f.get("is_administered") else "Price Gap"
        freshness = "live" if result.get("retail_price_source") == "live" else "baseline"

        labels.append(f["label"])
        parents.append(f["brand"])
        values.append(abs(gap) + 1)  # +1 floor so a near-zero gap still renders a visible box
        colors.append(gap)
        hover.append(f"{kind}: Rp {gap:,.0f}/L ({freshness})")

    fig = go.Figure(go.Treemap(
        labels=labels,
        parents=parents,
        values=values,
        branchvalues="remainder",  # brand nodes get value=0 (no "own" value beyond their fuels);
                                    # Plotly then sizes each brand box as the sum of its fuel children.
        marker=dict(
            colors=colors,
            colorscale=[[0, "#1f9d55"], [0.5, "#f2f2f2"], [1, RED]],
            cmid=0,
            showscale=True,
            colorbar=dict(title="Gap<br>Rp/L", thickness=14, len=0.8),
        ),
        text=hover,
        texttemplate="<b>%{label}</b><br>%{text}",
        textfont=dict(size=12, family="Inter"),
        hovertemplate="%{label}<br>%{text}<extra></extra>",
    ))
    fig.update_layout(
        height=420,
        margin=dict(l=4, r=4, t=4, b=4),
        font=dict(family="Inter, sans-serif", color=BLACK),
    )
    return fig


def _build_insight_copy(fuel_label: str, gap: dict, margin_pct: float, is_administered: bool) -> dict:
    """Compute the action-title / key-message / implications copy from the
    live numbers -- nothing here is a fixed string, all of it tracks
    whatever gap/margin the current selector combination produces.

    Administered fuels (Pertalite, Solar Subsidi) get the APBN/fiscal-exposure
    framing. Commercial/market-priced fuels (Pertamax variants, Dex, and all
    competitor brands) are NOT government-subsidized, so they get a retailer
    margin framing instead -- calling a market price a "subsidy gap" would be
    a real mischaracterization, not just a copy nit."""
    is_subsidized = gap["is_subsidized"]
    abs_gap = abs(gap["subsidy_gap_idr_liter"])
    retail_word = "administered price" if is_administered else "retail price"
    gap_noun = "Subsidy Gap" if is_administered else "Price Gap"

    if is_administered:
        if is_subsidized:
            title = f"{fuel_label} is priced below cost recovery — the state is absorbing the difference"
            key_message = (
                f"True market-clearing cost for {fuel_label} (Rp {gap['market_clearing_price_idr_liter']:,.0f}/L) sits "
                f"Rp {abs_gap:,.0f}/L above the {retail_word} (Rp {gap['administered_price_idr_liter']:,.0f}/L). "
                f"This is active fiscal exposure — the state budget absorbs this gap on every liter sold."
            )
            fiscal_bullet = (
                f"<strong>Active fiscal exposure</strong> — APBN absorbs Rp {abs_gap:,.0f}/L on every liter of "
                f"{fuel_label} sold at the {retail_word}; scale with volume to size the budget line."
            )
        else:
            title = f"{fuel_label} is priced above cost recovery — the state is over-collecting, not subsidizing"
            key_message = (
                f"True market-clearing cost for {fuel_label} (Rp {gap['market_clearing_price_idr_liter']:,.0f}/L) sits "
                f"Rp {abs_gap:,.0f}/L below the {retail_word} (Rp {gap['administered_price_idr_liter']:,.0f}/L). "
                f"No fiscal exposure on this SKU today — but the margin is thin and benchmark-sensitive."
            )
            fiscal_bullet = (
                f"<strong>No fiscal exposure today</strong> on {fuel_label} — the {retail_word} already covers "
                f"cost, so APBN is not absorbing a per-liter loss on this SKU."
            )
    else:
        # Commercial fuel: no government subsidy involved, frame as retailer
        # margin vs. estimated cost-to-serve instead of fiscal exposure.
        if is_subsidized:
            title = f"{fuel_label} is retailing below estimated cost-to-serve"
            key_message = (
                f"Estimated cost-to-serve for {fuel_label} (Rp {gap['market_clearing_price_idr_liter']:,.0f}/L) sits "
                f"Rp {abs_gap:,.0f}/L above the retail price (Rp {gap['administered_price_idr_liter']:,.0f}/L). "
                f"This is a commercial (non-subsidized) fuel — a negative retailer margin here is a pricing/competitive "
                f"signal, not a state fiscal exposure."
            )
            fiscal_bullet = (
                f"<strong>Thin or negative retailer margin</strong> on {fuel_label} — Rp {abs_gap:,.0f}/L below "
                f"estimated cost-to-serve; likely a competitive pricing move rather than a cost change."
            )
        else:
            title = f"{fuel_label} is retailing above estimated cost-to-serve"
            key_message = (
                f"Estimated cost-to-serve for {fuel_label} (Rp {gap['market_clearing_price_idr_liter']:,.0f}/L) sits "
                f"Rp {abs_gap:,.0f}/L below the retail price (Rp {gap['administered_price_idr_liter']:,.0f}/L). "
                f"This is a commercial (non-subsidized) fuel, so this reads as normal retailer margin, not a "
                f"government subsidy gap."
            )
            fiscal_bullet = (
                f"<strong>Normal retailer margin</strong> on {fuel_label} — Rp {abs_gap:,.0f}/L above estimated "
                f"cost-to-serve; not a state fiscal matter since this SKU isn't government-administered."
            )

    margin_pct_abs = abs(margin_pct)
    margin_bullet = (
        f"<strong>Margin is thin ({margin_pct_abs:.1f}%)</strong> — a moderate benchmark or Rupiah move within "
        f"recent trailing ranges could flip the sign of this gap."
        if margin_pct_abs < 5
        else f"<strong>Margin currently {margin_pct_abs:.1f}%</strong> — comfortable buffer against near-term "
             f"benchmark or FX swings, but revisit if trends persist."
    )

    cross_sku_bullet = (
        f"<strong>Check other SKUs separately</strong> — other fuels run on different benchmarks, brands, and "
        f"administered/commercial status; this read does not extend beyond {fuel_label}."
    )

    return {
        "title": title,
        "key_message": key_message,
        "bullets": [fiscal_bullet, margin_bullet, cross_sku_bullet],
        "is_subsidized": is_subsidized,
        "is_administered": is_administered,
        "gap_noun": gap_noun,
        "retail_word": retail_word,
    }


def page_subsidy_gap_tracker():
    try:
        engine = get_pricing_engine()
    except Exception as e:
        st.error(f"Pricing engine failed to initialize: {e}")
        return

    # -- Eyebrow ---------------------------------------------------------
    st.markdown(
        "<div style='font-size:11px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;"
        "border-bottom:2px solid #1A1A1A;padding-bottom:8px;margin-bottom:12px;'>"
        "Indonesia Energy Desk &nbsp;/&nbsp; Subsidy Gap Tracker</div>",
        unsafe_allow_html=True,
    )

    fuels = get_fuel_catalog()

    # -- Live retail prices (Pertamina/Shell official + news cross-check for
    # BP-AKR/Vivo). Never lets a scrape failure break the page -- falls back
    # to the static baseline catalog price, same pattern as OSINT. ----------
    try:
        live_fuel_data = _cached_live_fuel_prices()
    except Exception:
        live_fuel_data = None

    provinces = []
    if live_fuel_data and live_fuel_data.get("pertamina"):
        any_fuel_prices = next(iter(live_fuel_data["pertamina"]["prices_by_province"].values()), {})
        provinces = sorted(any_fuel_prices.keys())

    # -- Control bar. Benchmark stays a horizontal radio (5 options fit one
    # row); Fuel is a brand-grouped dropdown -- 14+ SKUs across 4 brands
    # won't fit as horizontal pills without wrapping badly. ------------------
    ctrl1, ctrl2 = st.columns([5, 3])
    with ctrl1:
        benchmark_key = st.radio(
            "Benchmark", list(BENCHMARK_LABELS.keys()),
            format_func=lambda k: BENCHMARK_LABELS[k], horizontal=True, index=0,
        )
    with ctrl2:
        retail_key = st.selectbox(
            "Fuel", list(fuels.keys()),
            format_func=lambda k: f"{fuels[k]['brand']} — {fuels[k]['label']}", index=0,
        )

    province = "Prov. DKI Jakarta"
    if provinces:
        default_idx = provinces.index("Prov. DKI Jakarta") if "Prov. DKI Jakarta" in provinces else 0
        province = st.selectbox(
            "Region (Pertamina live pricing only -- other brands show one national reference price)",
            provinces, index=default_idx,
        )

    live_price = _extract_live_price(live_fuel_data, retail_key, province) if live_fuel_data else None

    try:
        result = engine.price_fuel(benchmark_key, retail_key, live_retail_price=live_price)
    except Exception as e:
        st.error(f"Pricing calculation failed, and baseline fallback also failed: {e}")
        return

    pricing = result["pricing"]
    gap = result["subsidy_gap"]
    fuel_label = result["fuel_label"]
    is_administered = result["is_administered"]

    if not gap:
        st.info("No retail price mapped for this fuel selection.")
        return

    if not result.get("is_regulation_compliant_benchmark", True):
        st.info(
            f"ℹ️ {fuel_label} is regulated on **{BENCHMARK_LABELS.get(result['mandated_benchmark_key'], result['mandated_benchmark_key'])}** "
            f"per Kepmen ESDM 245/2022 (RON/CN class: {result.get('regulatory_class', 'n/a')}) -- you've selected a different benchmark, "
            f"so this is a what-if scenario, not the regulation-compliant cost reading."
        )

    margin_pct = (gap["subsidy_gap_idr_liter"] / gap["market_clearing_price_idr_liter"]) * 100
    copy = _build_insight_copy(fuel_label, gap, margin_pct, is_administered)

    # -- Action title ------------------------------------------------------
    st.markdown(
        f"""
        <div class='head' style='font-size:26px;font-weight:700;line-height:1.15;color:#1A1A1A;margin-top:8px;'>
            {copy['title']}
        </div>
        <div style='width:56px;height:4px;background:{RED};margin:8px 0 10px 0;'></div>
        """,
        unsafe_allow_html=True,
    )

    # -- Key message (BLUF) -------------------------------------------------
    st.markdown(
        f"""
        <div style='border-left:4px solid {RED};background:{GRAY_BG};padding:12px 18px;margin-bottom:12px;'>
            <div style='font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:{RED};'>Key message</div>
            <div style='font-size:13.5px;font-weight:500;color:#202020;line-height:1.45;'>{copy['key_message']}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    fresh_label = "🟢 live" if result["benchmark_source"] == "live" else "🟠 baseline fallback (cached)"
    retail_fresh_label = "🟢 live retail price" if result["retail_price_source"] == "live" else "🟠 placeholder retail price"
    st.caption(
        f"{fresh_label} benchmark ({BENCHMARK_LABELS[benchmark_key]}) · {retail_fresh_label} · "
        f"FX Rp {result['fx_rate']:,.0f}/USD (JISDOR)"
    )
    if live_fuel_data and live_fuel_data.get("cross_check", {}).get(retail_key, {}).get("status") == "MISMATCH":
        cc = live_fuel_data["cross_check"][retail_key]
        st.warning(f"⚠️ Source disagreement for {fuel_label}: official Rp {cc['official']:,.0f} vs news cross-check Rp {cc['news']:,.0f} -- verify manually.")

    if live_fuel_data:
        news_consensus = live_fuel_data.get("news_whitelist", {}).get("consensus", {}) if live_fuel_data.get("news_whitelist") else {}
        agreement = news_consensus.get(retail_key)
        if agreement:
            outlets = ", ".join(agreement["votes"].keys())
            if agreement["agreement"] == "unanimous":
                st.caption(f"✓ Confirmed unanimous across {len(agreement['votes'])} news outlets: {outlets}")
            elif agreement["agreement"] == "DISAGREEMENT":
                st.warning(f"⚠️ News outlets disagree on {fuel_label}: {agreement['votes']}")
            else:
                st.caption(f"Single news source (no second outlet to cross-check): {outlets}")

    # -- Stat row (hairline separated) --------------------------------------
    gap_color = RED
    s1, s2, s3 = st.columns([1.2, 1, 1])
    with s1:
        st.markdown(
            f"""
            <div style='font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:{GRAY_MID};'>{copy['gap_noun']}</div>
            <div class='head' style='font-size:44px;font-weight:700;color:{gap_color};line-height:.95;'>
                Rp {gap['subsidy_gap_idr_liter']:,.0f}<span style='font-size:16px;font-weight:600;color:{GRAY_DARK};'> /L</span>
            </div>
            <div style='font-size:12px;color:{GRAY_DARK};'>{
                ("State absorbing cost" if copy["is_subsidized"] else "State over-collecting vs. true cost")
                if is_administered else
                ("Below estimated cost-to-serve" if copy["is_subsidized"] else "Above estimated cost-to-serve")
            }</div>
            """,
            unsafe_allow_html=True,
        )
    with s2:
        st.markdown(
            f"""
            <div style='border-left:1px solid {GRAY_LINE};padding-left:24px;'>
            <div style='font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:{GRAY_MID};'>Market-Clearing Price</div>
            <div class='head' style='font-size:30px;font-weight:700;color:#1A1A1A;line-height:.95;'>
                Rp {gap['market_clearing_price_idr_liter']:,.0f}<span style='font-size:13px;font-weight:600;color:{GRAY_MID};'> /L</span>
            </div>
            <div style='font-size:12px;color:{GRAY_DARK};'>{BENCHMARK_LABELS[benchmark_key]} + JISDOR FX + tax stack</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with s3:
        st.markdown(
            f"""
            <div style='border-left:1px solid {GRAY_LINE};padding-left:24px;'>
            <div style='font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:{GRAY_MID};'>{"Administered Price" if is_administered else "Retail Price"}</div>
            <div class='head' style='font-size:30px;font-weight:700;color:#1A1A1A;line-height:.95;'>
                Rp {gap['administered_price_idr_liter']:,.0f}<span style='font-size:13px;font-weight:600;color:{GRAY_MID};'> /L</span>
            </div>
            <div style='font-size:12px;color:{GRAY_DARK};'>{"Government-set pump price" if is_administered else f"{result['fuel_brand']} commercial retail price"}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown(f"<div style='border-top:1px solid {GRAY_LINE};margin:14px 0 22px 0;'></div>", unsafe_allow_html=True)

    # -- Charts ---------------------------------------------------------------
    chart1, chart2 = st.columns(2)
    with chart1:
        st.markdown("<div style='font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;line-height:1.4;'>Price Position vs. Cost</div>", unsafe_allow_html=True)
        st.plotly_chart(
            _position_vs_cost_chart(
                gap["market_clearing_price_idr_liter"], gap["administered_price_idr_liter"],
                retail_label="Administered" if is_administered else "Retail",
            ),
            use_container_width=True, config={"displayModeBar": False},
        )
    with chart2:
        st.markdown("<div style='font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;line-height:1.4;'>Tax &amp; Logistics Stack (Rp/L)</div>", unsafe_allow_html=True)
        st.plotly_chart(_tax_waterfall_chart(pricing), use_container_width=True, config={"displayModeBar": False})

    # -- Portfolio-wide gap treemap ---------------------------------------------
    st.markdown(f"<div style='border-top:1px solid {GRAY_LINE};margin:18px 0 10px 0;'></div>", unsafe_allow_html=True)
    st.markdown(
        "<div style='font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;margin-bottom:2px;'>"
        "Gap Across All Fuels</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Box size = magnitude of the gap (Rp/L), NOT sales volume -- we have no real consumption data per SKU. "
        "Color: red = price sits below cost (state absorbing / retailer selling at a loss), "
        "green = price sits above cost (over-collecting / retailer margin positive). "
        "Each fuel priced on its own catalog benchmark, independent of the selector above. "
        f"Uses live retail prices where available (region: {province})."
    )
    try:
        treemap_fig = _build_gap_treemap(engine, fuels, live_fuel_data, province)
        st.plotly_chart(treemap_fig, use_container_width=True, config={"displayModeBar": False})
    except Exception as e:
        st.error(f"Could not build the gap treemap: {e}")

    # -- Implications -----------------------------------------------------------
    st.markdown(f"<div style='border-top:2px solid #1A1A1A;margin:4px 0 8px 0;padding-top:8px;'>"
                f"<div style='font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;'>Implications for Management</div></div>",
                unsafe_allow_html=True)
    b1, b2, b3 = st.columns(3)
    for col, bullet in zip((b1, b2, b3), copy["bullets"]):
        with col:
            st.markdown(
                f"""
                <div style='display:flex;gap:8px;'>
                    <div style='width:8px;height:8px;background:{RED};margin-top:5px;flex-shrink:0;'></div>
                    <div style='font-size:13px;line-height:1.45;color:#252525;'>{bullet}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown(
        f"<div style='font-size:10px;color:{GRAY_MID};margin-top:10px;'>"
        f"Cost formula: {result.get('esdm_regulation', 'Kepmen ESDM No. 245.K/MG.01/MEM.M/2022')} · "
        f"Sources: {BENCHMARK_LABELS[benchmark_key]} benchmark, BI JISDOR · cached baseline used where live feeds are unavailable. "
        f"Conversion: 159 L/bbl (official)</div>",
        unsafe_allow_html=True,
    )


def page_macro_shock_sandbox():
    st.header("Macro Shock Sandbox")
    st.write("Inject a benchmark price shock and watch multi-agent swarm propagation (refiners, traders, consumers, policymakers).")

    try:
        sim_engine = get_simulation_engine()
    except Exception as e:
        st.error(f"Simulation engine failed to initialize: {e}")
        return

    col1, col2, col3 = st.columns(3)
    with col1:
        shock_pct = st.slider("Initial shock (%)", -30, 30, 15) / 100.0
    with col2:
        steps = st.slider("Propagation steps", 1, 15, 5)
    with col3:
        seed = st.number_input("Random seed (reproducibility)", value=42, step=1)

    scenario_name = st.text_input("Scenario name", value="Custom macro shock")

    if st.button("Run Simulation", type="primary"):
        try:
            scenario = ShockScenario(name=scenario_name, initial_shock_pct=shock_pct, steps=steps, seed=int(seed))
            result = sim_engine.run(scenario)
        except Exception as e:
            st.error(f"Simulation failed: {e}")
            return

        st.success(result.summary)

        agents = list(result.steps[0]["agent_contributions"].keys())
        fig = go.Figure()
        for agent in agents:
            fig.add_trace(go.Scatter(
                x=[s["step"] for s in result.steps],
                y=[s["agent_contributions"][agent] for s in result.steps],
                mode="lines+markers",
                name=agent,
            ))
        fig.add_trace(go.Scatter(
            x=[s["step"] for s in result.steps],
            y=[s["net_shock_pct"] for s in result.steps],
            mode="lines+markers",
            name="Net Shock",
            line=dict(width=4, dash="dash"),
        ))
        fig.update_layout(title="Agent Reaction Propagation", xaxis_title="Step", yaxis_title="Deviation (%)")
        st.plotly_chart(fig, use_container_width=True)

        with st.expander("Raw step log"):
            st.json(result.steps)


OSINT_FETCH_TTL_SECONDS = 3600  # 1 hour -- safe polling interval for public RSS feeds,
                                 # well below anything that would look like abuse (these
                                 # feeds themselves usually refresh every 15-60 min anyway).


@st.cache_data(ttl=OSINT_FETCH_TTL_SECONDS, show_spinner="Refreshing OSINT feed (live)...")
def _cached_live_osint_feed() -> dict:
    """Cached wrapper around the live fetch. Streamlit reruns the whole
    script on every widget interaction; without this cache, every click
    anywhere in the app would re-trigger fresh HTTP requests to the RSS
    feeds. This pins live fetches to once per OSINT_FETCH_TTL_SECONDS."""
    return get_osint_feed(use_live=True)


FUEL_PRICE_FETCH_TTL_SECONDS = 86400  # daily -- retail fuel prices change on official
                                        # adjustment dates (roughly monthly), not hourly,
                                        # so daily polling is already generous.


@st.cache_data(ttl=FUEL_PRICE_FETCH_TTL_SECONDS, show_spinner="Refreshing live fuel prices...")
def _cached_live_fuel_prices() -> dict:
    """Cached wrapper -- see _cached_live_osint_feed for why this matters.
    Real failure modes (site down, page structure changed) are handled
    inside get_live_fuel_prices() itself via cached_fallback."""
    return get_live_fuel_prices()


def _extract_live_price(live_data: dict, fuel_key: str, province: str = "Prov. DKI Jakarta") -> Optional[float]:
    """Pulls one fuel's live price out of the combined scrape result --
    Pertamina prices are keyed by province, Shell/BP-AKR/Vivo are flat
    (single reference price, no per-province data available). Returns
    None if this fuel wasn't found in any live source (falls back to
    baseline catalog price at the call site)."""
    if not live_data:
        return None
    pertamina = live_data.get("pertamina")
    if pertamina and fuel_key in pertamina.get("prices_by_province", {}):
        return pertamina["prices_by_province"][fuel_key].get(province)
    shell = live_data.get("shell")
    if shell and fuel_key in shell.get("prices", {}):
        return shell["prices"][fuel_key]
    news = live_data.get("news_whitelist")
    if news and fuel_key in news.get("prices", {}):
        return news["prices"][fuel_key]
    return None


def page_price_forecast():
    st.header("Oil Price Forecast & Drivers")
    st.write("What actually moves the benchmark price we use, plus an honest attempt at a forward projection.")

    try:
        forecast_engine = get_forecast_engine()
    except Exception as e:
        st.error(f"Forecast engine failed to initialize: {e}")
        return

    benchmark_key = st.radio(
        "Benchmark", list(BENCHMARK_LABELS.keys()),
        format_func=lambda k: BENCHMARK_LABELS[k], horizontal=True, index=0,
    )
    horizon_days = st.slider("Forecast horizon (days)", min_value=7, max_value=90, value=30, step=7)

    # Pull the same real, hourly-cached OSINT signal used elsewhere so the
    # "current read" on choke-point/policy drivers is grounded in the actual
    # headlines fetched, not a description written in a vacuum.
    try:
        feed = _cached_live_osint_feed()
        risk_summary = summarize_risk_signals(feed)
    except Exception:
        risk_summary = None

    st.subheader("What moves this benchmark")
    drivers = forecast_engine.explain_drivers(risk_summary)
    for d in drivers:
        with st.container(border=True):
            st.markdown(f"**{d['factor']}**  ·  _{d['category'].replace('_', ' ')}_")
            st.caption(d["description"])
            st.markdown(f"Typical effect: {d['typical_effect']}")
            if d.get("current_read"):
                st.info(d["current_read"], icon="📡")

    st.divider()
    st.subheader(f"Forecast: {BENCHMARK_LABELS[benchmark_key]}, {horizon_days} days out")

    try:
        result = forecast_engine.forecast(benchmark_key, horizon_days=horizon_days)
    except Exception as e:
        st.error(f"Forecast failed: {e}")
        return

    if result.method == "flat_carry_forward_no_history":
        st.warning(
            "**This is not a real forecast.** " + result.confidence_note,
            icon="⚠️",
        )
    else:
        st.success("Real trend-based projection (see caveat below).", icon="📈")

    c1, c2, c3 = st.columns(3)
    c1.metric("Current value (USD/bbl)", f"{result.current_value:.2f}")
    c2.metric(f"Projected in {horizon_days}d (USD/bbl)", f"{result.projected_value:.2f}",
              delta=f"{result.projected_value - result.current_value:+.2f}")
    c3.metric("Historical points used", result.history_points_used)

    st.caption(result.confidence_note)

    if result.method == "flat_carry_forward_no_history":
        with st.expander("How to enable a real forecast"):
            st.write(
                "Drop a CSV at `data/historical_prices.csv` with columns "
                "`date,benchmark_key,value` (see `data/historical_prices_template.csv` for the format) "
                "and at least 10 data points for this benchmark. The engine will then fit an actual "
                "linear trend instead of holding the baseline flat."
            )


def page_margin_optimizer():
    st.header("Margin Optimizer (Commercial Fuels)")
    st.write(
        "Finds the retail price that maximizes margin x volume for commercial (non-subsidized) fuels. "
        "**Deliberately excludes Pertalite and Solar Subsidi** -- optimizing the gap on a state-administered "
        "fuel would mean recommending a subsidy cut, not a business pricing move."
    )

    try:
        engine = get_pricing_engine()
    except Exception as e:
        st.error(f"Pricing engine failed to initialize: {e}")
        return

    fuels = get_fuel_catalog()
    commercial_fuels = {k: v for k, v in fuels.items() if not v.get("is_administered", False)}

    ctrl1, ctrl2 = st.columns([5, 3])
    with ctrl1:
        benchmark_key = st.radio(
            "Benchmark", list(BENCHMARK_LABELS.keys()),
            format_func=lambda k: BENCHMARK_LABELS[k], horizontal=True, index=0, key="margin_benchmark",
        )
    with ctrl2:
        fuel_key = st.selectbox(
            "Fuel (commercial only)", list(commercial_fuels.keys()),
            format_func=lambda k: f"{commercial_fuels[k]['brand']} — {commercial_fuels[k]['label']}",
            index=0, key="margin_fuel",
        )

    # -- Real-data overrides: replace the illustrative assumptions whenever
    # the user has actual numbers, without touching the baseline JSON. -----
    elasticity_override = None
    competitor_prices_override = None

    with st.expander("📥 Use real data instead of assumptions (optional)"):
        st.markdown("**Price/volume history** — estimates real elasticity via log-log regression.")
        st.caption("CSV with columns: `price_idr_liter,volume_liter` — at least 4 rows, different price points.")
        uploaded = st.file_uploader("Upload CSV", type="csv", key="elasticity_csv")
        if uploaded is not None:
            try:
                import csv
                import io
                text = io.TextIOWrapper(uploaded, encoding="utf-8")
                reader = csv.DictReader(text)
                pairs = [(float(row["price_idr_liter"]), float(row["volume_liter"])) for row in reader]
                estimate = estimate_elasticity_from_data(pairs)
                elasticity_override = estimate.elasticity
                st.success(f"Estimated elasticity: {estimate.elasticity} (R²={estimate.r_squared}, n={estimate.n_points})")
                if estimate.r_squared < 0.5:
                    st.warning("Low R² — this fit is weak; treat the estimate with caution.")
            except Exception as e:
                st.error(f"Could not estimate elasticity from this file: {e}")

        st.markdown("**Real competitor prices** (Rp/L, comma-separated)")
        competitor_input = st.text_input(
            "e.g. 13100, 13050, 12980", value="", key="competitor_prices_input",
            help="Current observed competitor prices for this octane class -- overrides the catalog placeholder.",
        )
        if competitor_input.strip():
            try:
                competitor_prices_override = [float(x.strip()) for x in competitor_input.split(",") if x.strip()]
                st.success(f"Using {len(competitor_prices_override)} real competitor price(s) you entered.")
            except ValueError:
                st.error("Could not parse competitor prices -- use comma-separated numbers.")

    # -- Auto-pull live prices (Pertamina/Shell official + news cross-check)
    # as the default, unless the user typed a manual competitor override
    # above -- manual input always wins since it's the more current word
    # from the user in this session. --------------------------------------
    try:
        live_fuel_data = _cached_live_fuel_prices()
    except Exception:
        live_fuel_data = None

    live_current_price = _extract_live_price(live_fuel_data, fuel_key) if live_fuel_data else None

    if competitor_prices_override is None and live_fuel_data:
        same_class_competitors = get_competitor_prices(fuel_key, engine.baseline)
        live_competitor_prices = [
            p for p in (_extract_live_price(live_fuel_data, c["fuel_key"]) for c in same_class_competitors)
            if p is not None
        ]
        if live_competitor_prices:
            competitor_prices_override = live_competitor_prices
            st.caption(f"Auto-using {len(live_competitor_prices)} live competitor price(s) for this octane class.")

    try:
        result = optimize_price(
            fuel_key, engine, benchmark_key,
            elasticity_override=elasticity_override,
            competitor_prices_override=competitor_prices_override,
            current_price_override=live_current_price,
        )
    except AdministeredFuelError as e:
        st.error(str(e))
        return
    except Exception as e:
        st.error(f"Optimization failed: {e}")
        return

    delta_pct = (result.recommended_price_idr_liter / result.current_price_idr_liter - 1) * 100
    direction = "raise" if delta_pct > 0.05 else ("cut" if delta_pct < -0.05 else "hold")

    constraint_labels = {
        "elasticity_optimum": "demand elasticity (profit peaks before hitting any ceiling)",
        "competitor_ceiling": "competitor price ceiling",
        "wtp_ceiling": "willingness-to-pay ceiling",
        "current_price": "no feasible improvement found",
    }

    st.markdown(
        f"""
        <div style='border-left:4px solid {RED};background:{GRAY_BG};padding:12px 18px;margin:8px 0 16px 0;'>
            <div style='font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:{RED};'>Recommendation</div>
            <div style='font-size:15px;font-weight:500;color:#202020;line-height:1.5;'>
                {"Hold" if direction == "hold" else ("Raise" if direction == "raise" else "Cut")} {result.fuel_label} from
                Rp {result.current_price_idr_liter:,.0f}/L to <strong>Rp {result.recommended_price_idr_liter:,.0f}/L</strong>
                ({delta_pct:+.1f}%), lifting margin from Rp {result.current_margin_idr_liter:,.0f}/L to
                Rp {result.recommended_margin_idr_liter:,.0f}/L. Binding constraint:
                <strong>{constraint_labels.get(result.binding_constraint, result.binding_constraint)}</strong>.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("COGS (Rp/L)", f"{result.cogs_idr_liter:,.0f}")
    c2.metric("Current price", f"{result.current_price_idr_liter:,.0f}")
    c3.metric("Recommended price", f"{result.recommended_price_idr_liter:,.0f}", delta=f"{delta_pct:+.1f}%")
    c4.metric("Recommended margin", f"{result.recommended_margin_idr_liter:,.0f}")
    st.caption("🟢 Current price is live" if live_current_price else "🟠 Current price is the baseline placeholder (no live price found for this fuel)")

    if result.competitor_reference_price:
        st.caption(
            f"Competitor reference (avg, same octane class): Rp {result.competitor_reference_price:,.0f}/L · "
            f"competitor ceiling Rp {result.competitor_ceiling:,.0f}/L · WTP ceiling Rp {result.wtp_ceiling:,.0f}/L"
        )
    else:
        st.caption(f"No same-class competitor in catalog · WTP ceiling Rp {result.wtp_ceiling:,.0f}/L")

    st.subheader("Sensitivity: price vs. profit index")
    table = result.sensitivity_table
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[p["price_idr_liter"] for p in table],
        y=[p["profit_index"] for p in table],
        mode="lines+markers",
        line=dict(color=BLACK, width=2),
        marker=dict(color=[RED if p["price_idr_liter"] == result.recommended_price_idr_liter else
                            (GRAY_MID if not p["within_ceiling"] else BLACK) for p in table], size=8),
        name="Profit index",
    ))
    fig.add_vline(x=result.current_price_idr_liter, line=dict(color=GRAY_LINE, dash="dot"),
                  annotation_text="Current", annotation_position="top")
    fig.update_layout(
        height=320, plot_bgcolor="white", paper_bgcolor="white", showlegend=False,
        xaxis_title="Price (Rp/L)", yaxis_title="Profit index (margin x volume index)",
        font=dict(family="Inter, sans-serif", color=BLACK, size=13),
        margin=dict(l=10, r=10, t=30, b=10),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    st.caption("Gray markers = beyond the competitor/WTP ceiling (excluded from recommendation). Red = recommended point.")

    with st.expander("Full sensitivity table"):
        st.dataframe(table, use_container_width=True)

    st.subheader("Caveats")
    for c in result.caveats:
        st.markdown(f"- {c}")


def page_osint_simulation_feed():
    st.header("OSINT / Simulation Intelligence Feed")
    st.write("Open-source headlines, logistics/choke-point indicators, and an OSINT-derived shock scenario.")

    # Always live, auto-refreshed at most once per OSINT_FETCH_TTL_SECONDS via
    # st.cache_data -- no manual toggle needed. Any failure still degrades
    # cleanly to cache/mock inside get_osint_feed().
    try:
        feed = _cached_live_osint_feed()
    except Exception as e:
        st.error(f"OSINT feed failed entirely (including fallback): {e}")
        return

    try:
        fetched_dt = datetime.fromisoformat(feed["fetched_at"])
        age_min = (datetime.now(timezone.utc) - fetched_dt).total_seconds() / 60
        age_label = f"{age_min:.0f} min ago" if age_min >= 1 else "just now"
    except Exception:
        age_label = "unknown"

    st.markdown(
        f"""
        <div style='border-left:4px solid {RED};background:{GRAY_BG};padding:10px 16px;margin-bottom:10px;'>
            <div style='font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:{RED};'>Last fetched</div>
            <div style='font-size:15px;font-weight:600;color:#1A1A1A;'>{feed['fetched_at']} &nbsp;
                <span style='font-weight:500;color:{GRAY_DARK};'>({age_label} · auto-refreshes every {OSINT_FETCH_TTL_SECONDS // 60} min)</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    source_badge(feed["source"])
    if feed.get("feed_errors"):
        with st.expander("Feed errors (partial live data)"):
            for err in feed["feed_errors"]:
                st.caption(err)

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Headlines")
        for h in feed.get("headlines", []):
            st.markdown(f"**[{h['category']}]** {h['headline']}  \n*{h['source']} — {h['published_at']}*")

    with col2:
        st.subheader("Choke-Point Logistics")
        for c in feed.get("logistics", []):
            st.markdown(f"**{c['choke_point']}** — congestion `{c['congestion_index']}` ({c['status']})")

    st.divider()
    st.subheader("Derived Risk Summary -> Simulation Bridge")

    try:
        risk_summary = summarize_risk_signals(feed)
    except Exception as e:
        st.error(f"Risk summarization failed: {e}")
        return

    st.json(risk_summary)

    if st.button("Run Simulation from OSINT Signal"):
        try:
            sim_engine = get_simulation_engine()
            result = sim_engine.run_from_osint(risk_summary)
        except Exception as e:
            st.error(f"OSINT-derived simulation failed: {e}")
            return
        st.success(result.summary)
        st.json(result.steps)


def main():
    inject_bain_style()  # applies to sidebar + every page, not just Subsidy Gap Tracker

    st.sidebar.markdown(
        "<div style='font-size:18px;font-weight:700;line-height:1.25;color:#1A1A1A;'>"
        "Indonesia Energy Intelligence Desk</div>",
        unsafe_allow_html=True,
    )
    st.sidebar.caption("Autonomous, three-layer predictive energy desk")
    page = st.sidebar.radio(
        "Navigate",
        ["Subsidy Gap Tracker", "Margin Optimizer", "Macro Shock Sandbox", "Oil Price Forecast", "OSINT / Simulation Feed"],
    )

    st.sidebar.divider()
    st.sidebar.markdown(
        "<div style='font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#8C8C8C;margin-bottom:6px;'>Desk status</div>",
        unsafe_allow_html=True,
    )

    try:
        baseline = get_pricing_engine().baseline
        st.sidebar.markdown(
            f"""
            <div style='background:#F7F7F7;border-radius:6px;padding:10px 12px;font-size:12px;line-height:1.7;color:#252525;'>
                Baseline as of {baseline.get('as_of', 'n/a')}<br>
                JISDOR: Rp {baseline['fx']['jisdor_idr_usd']:,.0f}/USD<br>
                ICP: {baseline['benchmarks']['icp_usd_bbl']:.2f} USD/bbl
            </div>
            <div style='font-size:11px;color:#8C8C8C;margin-top:4px;'>⚠️ Static — no free real-time ICP/JISDOR feed exists.</div>
            """,
            unsafe_allow_html=True,
        )
    except Exception:
        st.sidebar.caption("Baseline unavailable")

    try:
        live_fuel_data = _cached_live_fuel_prices()
        n_pertamina = len(live_fuel_data["pertamina"]["prices_by_province"]) if live_fuel_data.get("pertamina") else 0
        n_shell = len(live_fuel_data["shell"]["prices"]) if live_fuel_data.get("shell") else 0
        n_news = len(live_fuel_data["news_whitelist"]["prices"]) if live_fuel_data.get("news_whitelist") else 0
        fetched_label = live_fuel_data["fetched_at"][:16].replace("T", " ")
        st.sidebar.markdown(
            f"""
            <div style='background:#F7F7F7;border-radius:6px;padding:10px 12px;margin-top:10px;font-size:12px;line-height:1.7;color:#252525;'>
                🟢 Live retail prices<br>
                {n_pertamina} Pertamina + {n_shell} Shell + {n_news} BP-AKR/Vivo<br>
                <span style='color:#8C8C8C;'>Fetched {fetched_label} UTC</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    except Exception:
        st.sidebar.caption("🟠 Live retail prices unavailable — using baseline placeholders")

    st.sidebar.divider()
    st.sidebar.markdown(
        "<div style='font-size:12px;line-height:1.9;color:#4D4D4D;'>"
        "<b>Layer 1</b> — Claude data/logic<br>"
        "<b>Layer 2</b> — OSINT signal parser<br>"
        "<b>Layer 3</b> — Multi-agent swarm sim</div>",
        unsafe_allow_html=True,
    )

    if page == "Subsidy Gap Tracker":
        page_subsidy_gap_tracker()
    elif page == "Margin Optimizer":
        page_margin_optimizer()
    elif page == "Macro Shock Sandbox":
        page_macro_shock_sandbox()
    elif page == "Oil Price Forecast":
        page_price_forecast()
    else:
        page_osint_simulation_feed()


if __name__ == "__main__":
    main()
