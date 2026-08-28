"""Data-Centre Siting & Cooling-Cost Engine — Streamlit app.

Compares candidate data-centre sites on projected annual cooling cost, using
FortyGuard's hyperlocal temperature layer (dry-bulb exceedance ladder) and
environmental-parameters layer (hourly wet-bulb series) run through a
selectable cooling-architecture model. See `dc_siting/cooling_cost.py` for
the model itself and its calibration caveats.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from fortyguard import FortyGuardClient
from fortyguard.exceptions import FortyGuardError

from dc_siting.cooling_cost import (
    ARCHITECTURES,
    bins_from_exceedance_ladder,
    kwh_from_bins,
    kwh_from_hourly_series,
    annualize,
    annualize_seasonal,
)
from dc_siting.data import (
    DRY_BULB_LADDER_C,
    buffered_aoi,
    cache_key,
    geocode_us_address,
    point_footprint,
    pull_dry_bulb_bins,
    pull_wet_bulb_series,
)
from dc_siting.export import build_csv_bytes, build_pdf_bytes

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

ROOT = Path(__file__).parent
CACHE_DIR = ROOT / "data" / "dc_siting_cache"
DEMO_GEOJSON = ROOT / "data" / "dc_candidate_sites_loudoun_va_sample.geojson"
HERO_BG = ROOT / "assets" / "_web" / "cover_bg_web.jpg"
LOGO = ROOT / "assets" / "fortyguard_logo.png"


@st.cache_data(show_spinner=False)
def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()

# Fixed categorical order — never cycled — matching dc_siting.cooling_cost.ARCHITECTURES.
ARCH_ORDER = ["dx_air_cooled", "chiller_tower", "evaporative", "liquid_dtc"]
ARCH_COLORS = {
    "dx_air_cooled": "#2a78d6",  # categorical slot 1 (blue)
    "chiller_tower": "#eb6834",  # slot 2 (orange)
    "evaporative": "#1baf7a",    # slot 3 (aqua)
    "liquid_dtc": "#eda100",     # slot 4 (yellow)
}
SEQUENTIAL_BLUE = "#184f95"

st.set_page_config(page_title="DC Siting & Cooling-Cost Engine", layout="wide")


@st.cache_resource(show_spinner=False)
def get_client() -> FortyGuardClient | None:
    try:
        return FortyGuardClient()
    except FortyGuardError:
        return None


@st.cache_data(show_spinner=False)
def load_demo_sites() -> list[dict]:
    fc = json.loads(DEMO_GEOJSON.read_text())
    sites = []
    for feat in fc["features"]:
        props = feat.get("properties", {})
        sites.append(
            {
                "id": props.get("parcel_id") or props.get("name") or "site",
                "name": props.get("name", "Unnamed site"),
                "geometry": feat["geometry"],
                "source": "DATS site (cached)",
            }
        )
    return sites


def load_uploaded_sites(upload) -> list[dict]:
    fc = json.loads(upload.read())
    sites = []
    for i, feat in enumerate(fc["features"]):
        props = feat.get("properties", {})
        sites.append(
            {
                "id": props.get("parcel_id") or props.get("id") or f"site_{i}",
                "name": props.get("name", f"Site {i + 1}"),
                "geometry": feat["geometry"],
                "source": "GeoJSON upload",
            }
        )
    return sites


def _pull_window(client, site, start_date, end_date, refresh, status_box):
    """One site, one date window -> (bins, wb_series, total_hours, lat, lon)."""
    aoi, lat, lon = buffered_aoi(site["geometry"])

    def on_status(msg: str, _name=site["name"], _w=f"{start_date}..{end_date}") -> None:
        status_box.write(f"**{_name}** ({_w}) — {msg}")

    hours_above, total_hours = pull_dry_bulb_bins(
        client, CACHE_DIR, site["id"], aoi, start_date, end_date, refresh, on_status
    )
    bins = bins_from_exceedance_ladder(hours_above, total_hours, DRY_BULB_LADDER_C)
    wb_series = pull_wet_bulb_series(
        client, CACHE_DIR, site["id"], lat, lon, start_date, end_date, refresh, on_status
    )
    return bins, wb_series, total_hours, lat, lon


def run_pipeline(
    client: FortyGuardClient,
    sites: list[dict],
    start_date: str,
    end_date: str,
    it_load_kw: float,
    rate: float,
    arch_keys: list[str],
    refresh: bool,
    status_box,
    seasonal: bool = False,
    winter_start: str | None = None,
    winter_end: str | None = None,
) -> tuple[pd.DataFrame, dict, dict]:
    rows = []
    bin_data: dict[str, list] = {}
    wb_data: dict[str, list] = {}

    for site in sites:
        summer_bins, summer_wb, summer_hours, lat, lon = _pull_window(
            client, site, start_date, end_date, refresh, status_box
        )
        bin_data[site["id"]] = summer_bins
        wb_data[site["id"]] = summer_wb

        if seasonal:
            winter_bins, winter_wb, winter_hours, _, _ = _pull_window(
                client, site, winter_start, winter_end, refresh, status_box
            )

        for key in arch_keys:
            arch = ARCHITECTURES[key]

            def _kwh(bins, wb):
                return kwh_from_bins(arch, bins, it_load_kw) if arch.driving_temp == "dry_bulb" else kwh_from_hourly_series(arch, wb, it_load_kw)

            if seasonal:
                windows = [
                    ("summer", _kwh(summer_bins, summer_wb), summer_hours),
                    ("winter", _kwh(winter_bins, winter_wb), winter_hours),
                ]
                result = annualize_seasonal(site["name"], arch, windows, rate)
                study_hours = summer_hours + winter_hours
            else:
                kwh = _kwh(summer_bins, summer_wb)
                result = annualize(site["name"], arch, kwh, summer_hours, rate)
                study_hours = summer_hours

            rows.append(
                {
                    "site_id": site["id"],
                    "site": site["name"],
                    "architecture_key": key,
                    "architecture": arch.label,
                    "annual_cost_usd": result.projected_annual_cost_usd,
                    "annual_kwh": result.projected_annual_kwh,
                    "study_period_hours": study_hours,
                    "lat": lat,
                    "lon": lon,
                }
            )
    return pd.DataFrame(rows), bin_data, wb_data


# ── landing view ─────────────────────────────────────────────────────────
st.session_state.setdefault("view", "landing")

# Real sites/window this draws from — same as the pre-seeded cache, so this
# reads live off disk (zero network calls) rather than a frozen snapshot
# baked into copy that could drift from the actual calibrated model.
_LANDING_SITES = [
    ("xai-colossus-memphis", "xAI Colossus", "Memphis, TN"),
    ("aws-northern-virginia", "AWS", "Northern Virginia"),
    ("aws-pennsylvania-nuclear", "AWS Cumulus", "Berwick, PA"),
    ("google-berkeley-county", "Google", "Berkeley County, SC"),
    ("microsoft-san-antonio", "Microsoft", "San Antonio, TX"),
    ("meta-forest-city", "Meta", "Forest City, NC"),
]
_LANDING_WINDOW = ("2025-07-01", "2025-07-31")
_LANDING_IT_LOAD_KW = 5000.0
_LANDING_RATE = 0.16


@st.cache_data(show_spinner=False)
def _landing_stats() -> dict | None:
    """Real 6-site chiller+tower comparison + real 4-architecture comparison
    at one named site, computed live from the pre-seeded cache. Returns None
    (landing page skips the data section) if that cache isn't present, e.g.
    a fresh clone without the committed cache files.
    """
    start, end = _LANDING_WINDOW
    total_hours = 31 * 24.0
    site_costs = []
    va_arch_costs = None

    for site_id, name, place in _LANDING_SITES:
        hours_above = {}
        for t in DRY_BULB_LADDER_C:
            p = CACHE_DIR / f"{cache_key('exceedance', site_id, start, end, t)}.json"
            if not p.exists():
                return None
            hours_above[t] = json.loads(p.read_text())["mean"]
        bins = bins_from_exceedance_ladder(hours_above, total_hours, DRY_BULB_LADDER_C)

        wb_path = CACHE_DIR / f"{cache_key('wet_bulb', site_id, start, end)}.json"
        if not wb_path.exists():
            return None
        wb = json.loads(wb_path.read_text())

        ct = ARCHITECTURES["chiller_tower"]
        kwh = kwh_from_hourly_series(ct, wb, _LANDING_IT_LOAD_KW)
        cost = annualize(name, ct, kwh, total_hours, _LANDING_RATE).projected_annual_cost_usd
        site_costs.append((name, place, cost))

        if site_id == "aws-northern-virginia":
            va_arch_costs = []
            for key in ARCH_ORDER:
                a = ARCHITECTURES[key]
                kwh2 = (
                    kwh_from_bins(a, bins, _LANDING_IT_LOAD_KW)
                    if a.driving_temp == "dry_bulb"
                    else kwh_from_hourly_series(a, wb, _LANDING_IT_LOAD_KW)
                )
                cost2 = annualize(name, a, kwh2, total_hours, _LANDING_RATE).projected_annual_cost_usd
                va_arch_costs.append((a.label.split(" — ")[0], cost2))

    site_costs.sort(key=lambda r: r[2])
    return {"sites": site_costs, "arch": va_arch_costs}


def _launch() -> None:
    st.session_state.view = "app"


def _flatten(html: str) -> str:
    """Strip leading whitespace from every line. A blank line followed by an
    indented line inside st.markdown(unsafe_allow_html=True) gets misread as
    a Markdown indented code block instead of raw HTML — confirmed live,
    this rendered as visible literal tag text instead of applying as markup.
    Dedent alone doesn't fully fix it once content nests past the common
    prefix, so every line is stripped individually instead.
    """
    return "\n".join(line.lstrip() for line in html.split("\n"))


if st.session_state.view == "landing":
    bg_b64 = _b64(HERO_BG)
    stats = _landing_stats()

    def _bar_row(name: str, sub: str, value: float, maxval: float, kind: str = "site") -> str:
        # Single line, no leading indentation — a blank line followed by an
        # indented line inside an st.markdown call gets misread as a Markdown
        # indented code block instead of raw HTML (confirmed live: this was
        # rendering as visible literal tag text, not applying as markup).
        pct = round(value / maxval * 100, 1) if maxval else 0
        return (
            f'<div class="bar-row"><div class="bar-label"><span class="bl-name">{name}</span>'
            f'<span class="bl-sub">{sub}</span></div><div class="bar-track">'
            f'<div class="bar-fill {kind}" style="width:{pct}%"></div></div>'
            f'<div class="bar-value">${value:,.0f}</div></div>'
        )

    landing_css = """
    <style>
    @keyframes dc-fade-up { from { opacity:0; transform:translateY(18px);} to { opacity:1; transform:translateY(0);} }
    @keyframes dc-pan { 0% { background-position:50% 15%;} 100% { background-position:50% 45%;} }
    .dc-page {
        font-family:-apple-system,"Segoe UI",system-ui,sans-serif;
        background:#ffffff; color:#171717; padding-bottom:8px;
    }
    .st-key-hero_wrap {
        position:relative; z-index:0; border-radius:20px; overflow:hidden;
        padding:3.2vw 3.6vw 2.6vw 3.6vw; margin-bottom:0; min-height:46vh;
        display:flex; flex-direction:column; justify-content:center;
    }
    /* Streamlit gives every element's own wrapper `position:relative`, which
       would otherwise become the containing block for the absolutely
       positioned bg layer below and collapse it to 0 height. Neutralise it
       on just this first wrapper so .dc-hero-bg sizes against .st-key-hero_wrap
       instead (confirmed live via getComputedStyle — this was the real bug). */
    .st-key-hero_wrap .stElementContainer:first-child { position:static; }
    .dc-hero-bg {
        position:absolute; top:0; left:0; width:100%; height:100%; z-index:-1;
        background-image: linear-gradient(120deg, rgba(5,16,32,.92) 0%, rgba(8,28,54,.68) 48%, rgba(5,16,32,.94) 100%), url('data:image/jpeg;base64,__BG__');
        background-size:cover; background-position:50% 25%; animation: dc-pan 22s ease-in-out infinite alternate;
    }
    .dc-hero-text { font-family:-apple-system,"Segoe UI",system-ui,sans-serif; color:#fdfdfd; }
    .dc-hero-text h1 { font-size:clamp(1.9rem, 3.6vw, 3rem); line-height:1.1; font-weight:800; margin:0 0 16px 0; max-width:18ch; letter-spacing:-0.01em; animation: dc-fade-up .7s ease both; }
    .dc-hero-text h1 em { font-style:normal; color:#ff8a5c; }
    .dc-hero-text p.sub { font-size:.98rem; line-height:1.55; max-width:52ch; color:#c7d3e0; margin:0 0 8px 0; animation: dc-fade-up .8s ease both .28s; }
    .dc-quiet { font-size:.78rem; color:#8a8a8a; margin:20px 0 0 0; }
    .dc-quiet b { color:#171717; }
    .dc-section { padding: 56px 0 0 0; }
    .dc-kicker { font-size:.72rem; letter-spacing:.13em; text-transform:uppercase; color:#e0633f; font-weight:700; margin:0 0 12px 0; }
    .dc-section h2 { font-size:clamp(1.3rem, 2.2vw, 1.7rem); font-weight:700; letter-spacing:-0.01em; margin:0 0 14px 0; max-width:32ch; color:#171717; }
    .dc-lede { font-size:.95rem; color:#4b4b4b; max-width:60ch; line-height:1.6; margin:0 0 32px 0; }
    .dc-lede b { color:#171717; }
    .bars { display:flex; flex-direction:column; gap:12px; margin-bottom: 8px; }
    .bar-row { display:grid; grid-template-columns: 170px 1fr 100px; align-items:center; gap:14px; }
    .bar-label { display:flex; flex-direction:column; }
    .bl-name { font-size:13px; font-weight:600; color:#171717; }
    .bl-sub { font-size:11px; color:#8a8a8a; }
    .bar-track { height:9px; background:#eeeeee; border-radius:5px; overflow:hidden; }
    .bar-fill { height:100%; border-radius:5px; background:linear-gradient(90deg,#c94a2c,#ff6b3d); }
    .bar-fill.arch { background:linear-gradient(90deg,#1a5f8a,#3fa9f5); }
    .bar-value { font-size:12.5px; text-align:right; color:#4b4b4b; font-variant-numeric:tabular-nums; }
    .finding-box { margin-top:24px; padding:18px 22px; background:#faf4f1; border-left:3px solid #e0633f; font-size:14px; color:#4b4b4b; line-height:1.6; max-width:60ch; border-radius: 0 8px 8px 0;}
    .finding-box b { color:#171717; }
    .dc-cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:1px; background:#e4e4e4; border:1px solid #e4e4e4; border-radius:10px; overflow:hidden; margin-top:8px; }
    .dc-card { background:#fff; padding:20px 20px; }
    .dc-card .n { font-size:10.5px; color:#a0a0a0; margin-bottom:8px; }
    .dc-card h3 { font-size:14.5px; font-weight:700; margin:0 0 6px 0; color:#171717; }
    .dc-card p { font-size:12.5px; color:#6b6b6b; line-height:1.5; margin:0; }
    .dc-sources { display:flex; flex-direction:column; border-top:1px solid #e4e4e4; margin-top:8px; }
    .dc-source { display:grid; grid-template-columns:210px 1fr; gap:20px; padding:16px 0; border-bottom:1px solid #e4e4e4; }
    .dc-source .name { font-size:13.5px; font-weight:700; color:#171717; }
    .dc-source .desc { font-size:12.5px; color:#6b6b6b; line-height:1.5; }
    .dc-footer { padding: 32px 0 8px 0; display:flex; justify-content:space-between; flex-wrap:wrap; gap:10px; font-size:11px; color:#9a9a9a; border-top:1px solid #e4e4e4; margin-top:40px; }
    @media (max-width: 680px) { .bar-row, .dc-source { grid-template-columns:1fr; gap:6px; } .bar-value { text-align:left; } }
    </style>
    """.replace("__BG__", bg_b64)

    hero_text_html = """
    <div class="dc-hero-text">
      <h1>FortyGuard found a 45-point thermal gap between two AWS sites. <em>We priced it.</em></h1>
      <p class="sub">
        The Data-Centre Siting &amp; Cooling-Cost Engine turns FortyGuard's hyperlocal
        temperature data into the real annual dollar difference between candidate sites
        and cooling designs. It's the figure their own DATS report stopped short of computing.
      </p>
    </div>
    """
    st.markdown(
        "<style>"
        "div[data-testid='stButton'] button{font-size:1rem !important; font-weight:700 !important;"
        "padding:.85rem 1.7rem !important; border-radius:10px !important;}"
        ".st-key-hero_cta{margin:0; max-width:220px;}"
        ".st-key-closing_cta{margin-top:.8rem; max-width:220px;}"
        "</style>",
        unsafe_allow_html=True,
    )
    # The hero card is a real st.container so the button renders as an
    # in-flow child right after the paragraph, inside the card. Splitting
    # the background into its own absolutely-positioned layer (z-index:-1)
    # instead of a min-height div means the container's height still hugs
    # its actual content (text + button), rather than the earlier fixed
    # 70vh box that left a dead gap before the button rendered outside it.
    with st.container(key="hero_wrap"):
        st.markdown(_flatten(landing_css + '<div class="dc-hero-bg"></div>'), unsafe_allow_html=True)
        st.markdown(_flatten(hero_text_html), unsafe_allow_html=True)
        st.button("Launch tool →", type="primary", key="hero_cta", on_click=_launch)

    # Self-contained fragment with its own background/text-color wrapper —
    # an unclosed <div> here can't inherit styling from hero_html's div,
    # since each st.markdown call is parsed as its own independent HTML
    # fragment (confirmed live: that's why the white background never
    # actually reached this content before).
    mid_html = '<div class="dc-page">'
    if stats:
        cheapest, priciest = stats["sites"][0], stats["sites"][-1]
        delta = priciest[2] - cheapest[2]
        mid_html += f"""
        <p class="dc-quiet">6 real named AI data centres &middot; <b>${delta:,.0f}/yr</b> separates cheapest from priciest</p>
        """

    if stats:
        site_rows = "".join(_bar_row(n, p, v, stats["sites"][-1][2]) for n, p, v in stats["sites"])
        max_arch = max(v for _, v in stats["arch"])
        arch_rows = "".join(_bar_row(n, "", v, max_arch, "arch") for n, v in stats["arch"])
        cheapest, priciest = stats["sites"][0], stats["sites"][-1]
        delta = priciest[2] - cheapest[2]
        arch_cheap, arch_rich = stats["arch"][-1], stats["arch"][0]

        mid_html += f"""
        <div class="dc-section">
          <div class="dc-kicker">The comparison</div>
          <h2>Same architecture, same facility size, six real sites.</h2>
          <p class="dc-lede">Every site below is one of the 36 real, named US AI data centres FortyGuard scored in its
          own DATS 2025 Baseline report, run through the same 5&nbsp;MW water-cooled chiller model, same
          electricity rate, same month of real hyperlocal temperature data.</p>
          <div class="bars">{site_rows}</div>
          <div class="finding-box"><b>${delta:,.0f}/yr</b> separates {cheapest[0]} ({cheapest[1]}) from {priciest[0]}
          ({priciest[1]}), a real, moderate, defensible gap between real facilities, not a hypothetical.</div>
        </div>

        <div class="dc-section">
          <div class="dc-kicker">The bigger lever</div>
          <h2>Cooling architecture often moves the number more than location does.</h2>
          <p class="dc-lede">Same real site, AWS's Northern Virginia facility, run through all four
          cooling designs the tool supports.</p>
          <div class="bars">{arch_rows}</div>
        </div>
        """

    mid_html += """
    <div class="dc-section">
      <div class="dc-kicker">How it works</div>
      <h2>Two live FortyGuard endpoints, one calibrated model.</h2>
      <p class="dc-lede">A threshold ladder against the heatmap endpoint builds a dry-bulb exposure profile; an
      hourly wet-bulb series from env_params drives the three evaporation-dependent architectures.</p>
      <div class="dc-cards">
        <div class="dc-card"><div class="n">01</div><h3>Air-cooled DX / CRAC</h3><p>Legacy &amp; small-site standard. Narrowest free-cooling band.</p></div>
        <div class="dc-card"><div class="n">02</div><h3>Chiller + tower</h3><p>Enterprise standard for two decades. Waterside economizer below threshold.</p></div>
        <div class="dc-card"><div class="n">03</div><h3>Evaporative / adiabatic</h3><p>Modern hyperscale choice. Degrades fastest as humidity rises.</p></div>
        <div class="dc-card"><div class="n">04</div><h3>Direct-to-chip liquid</h3><p>AI-era, GPU-density racks. Widest free-cooling band.</p></div>
      </div>
    </div>

    <div class="dc-section">
      <div class="dc-kicker">Calibrated, not guessed</div>
      <h2>Every efficiency number traces to a named source.</h2>
      <div class="dc-sources">
        <div class="dc-source"><div class="name">ASHRAE TC9.9 (5th ed.)</div><div class="desc">Air classes A1&ndash;A4, the 18&ndash;27&deg;C recommended envelope, and liquid-cooling facility-water classes W17&ndash;W45+.</div></div>
        <div class="dc-source"><div class="name">Field-reported COP data</div><div class="desc">Legacy CRAC 1.5&ndash;2.5, ASHRAE 90.1-2019 minimum (2.2), modern chiller+CRAH systems (4.5&ndash;9.75).</div></div>
        <div class="dc-source"><div class="name">Evaporative-cooling research</div><div class="desc">Dew-point COP up to ~29.7 average (peak 48.3), most effective below ~20&deg;C wet-bulb.</div></div>
        <div class="dc-source"><div class="name">Uptime Institute 2025 Survey</div><div class="desc">1.54 average global PUE, used as a sanity ceiling against every architecture's implied overhead.</div></div>
      </div>
    </div>

    <div class="dc-section" style="border-top:1px solid #e4e4e4; margin-top:16px;">
      <div class="dc-kicker">Try it</div>
      <h2>Compare your own candidate sites.</h2>
      <p class="dc-lede">Use the six real DATS sites, type in a planned address, or upload your own site boundaries,
      and get the real number back.</p>
    """
    mid_html += "</div>"  # close this call's own .dc-page wrapper
    st.markdown(_flatten(mid_html), unsafe_allow_html=True)

    st.button("Launch tool →", type="primary", key="closing_cta", on_click=_launch)

    st.markdown(
        _flatten(
            """
            <div class="dc-page">
            <div class="dc-footer">
              <span>Data-Centre Siting &amp; Cooling-Cost Engine &middot; FortyGuard Hackathon '26</span>
              <span>Extends FortyGuard's DATS 2025 Baseline report</span>
            </div>
            </div>
            """
        ),
        unsafe_allow_html=True,
    )
    st.stop()

# ── sidebar ──────────────────────────────────────────────────────────────
back_col, title_col = st.columns([1, 9])
with back_col:
    st.button("← Back", on_click=lambda: st.session_state.update(view="landing"))
with title_col:
    st.title("Data-Centre Siting & Cooling-Cost Engine")
st.caption(
    "Hyperlocal temperature intelligence, from FortyGuard's 2m-measured layer, "
    "turned into an annual cooling-cost comparison across candidate sites."
)

client = get_client()
if client is None:
    st.error("No FortyGuard API key found. Add FORTYGUARD_API_KEY to your .env file.")
    st.stop()

with st.sidebar:
    st.header("Candidate sites")
    st.caption("Mix and match — pick known sites, add your own by address, and/or upload a GeoJSON.")
    sites: list[dict] = []

    with st.expander("Known real AI data centers (FortyGuard DATS baseline)", expanded=True):
        all_sites = load_demo_sites()
        names = [s["name"] for s in all_sites]
        default_names = [
            "xAI Colossus (Memphis, TN)",
            "AWS (Northern Virginia)",
            "AWS Cumulus (Berwick, PA — nuclear-adjacent)",
        ]
        chosen = st.multiselect(
            "Sites to compare", names, default=[n for n in default_names if n in names]
        )
        sites += [s for s in all_sites if s["name"] in chosen]
        st.caption(
            "6 of the 36 US AI data centers named in FortyGuard's own DATS "
            "2025 Baseline report — that report scored thermal exposure only "
            "(explicitly no cost/PUE figure); this tool picks up from there. "
            "Default selection recreates DATS's own headline example: AWS's "
            "Virginia site (score 77) vs. its Pennsylvania site (score 32) — "
            "a 45-point gap inside one operator's own portfolio. "
            "Footprints are illustrative ~300m squares on the publicly "
            "reported facility address, not exact parcel boundaries."
        )

    with st.expander("Add a planned/candidate site by address", expanded=False):
        st.session_state.setdefault("manual_sites", [])
        state_abbr = st.selectbox(
            "State", options=list(US_STATES.keys()), format_func=lambda k: US_STATES[k]
        )
        addr = st.text_input("City or street address (optional)", placeholder="e.g. Ashburn, or 123 Main St")
        label = st.text_input("Label for this site (optional)", placeholder="e.g. Candidate Site A")
        if st.button("Add site"):
            query = f"{addr}, {US_STATES[state_abbr]}, USA" if addr else f"{US_STATES[state_abbr]}, USA"
            with st.spinner(f"Looking up '{query}'…"):
                coords = geocode_us_address(query)
            if coords is None:
                st.error(f"Couldn't find a location for '{query}'. Try a more specific address.")
            else:
                lat, lon = coords
                name = label.strip() or f"{addr or US_STATES[state_abbr]} ({state_abbr})"
                st.session_state.manual_sites.append(
                    {
                        "id": f"manual-{len(st.session_state.manual_sites)}-{state_abbr}",
                        "name": name,
                        "geometry": point_footprint(lat, lon),
                        "source": "Address (live geocoded)",
                    }
                )
                st.success(f'Added "{name}" at {lat:.4f}, {lon:.4f}')

        if st.session_state.manual_sites:
            st.caption("Your added sites:")
            keep = []
            for i, s in enumerate(st.session_state.manual_sites):
                c1, c2 = st.columns([4, 1])
                c1.write(f"• {s['name']}")
                if not c2.button("✕", key=f"remove_manual_{i}"):
                    keep.append(s)
            st.session_state.manual_sites = keep
        st.caption(
            "Geocoded via OpenStreetMap Nominatim (free, best-effort — not a "
            "verified parcel lookup). Footprint is an illustrative ~300m "
            "square centered on the matched point."
        )
        sites += st.session_state.manual_sites

    with st.expander("Upload GeoJSON", expanded=False):
        upload = st.file_uploader("Multi-feature polygon GeoJSON", type=["geojson", "json"])
        if upload:
            sites += load_uploaded_sites(upload)

    st.header("Facility")
    facility_mw = st.number_input("Planned IT load (MW)", min_value=0.1, value=5.0, step=0.5)
    rate = st.number_input("Electricity rate ($/kWh)", min_value=0.01, value=0.16, step=0.01)

    st.header("Cooling architecture")
    arch_labels = {k: ARCHITECTURES[k].label for k in ARCH_ORDER}
    chosen_archs = st.multiselect(
        "Architectures to compare",
        options=ARCH_ORDER,
        default=ARCH_ORDER,
        format_func=lambda k: arch_labels[k],
    )
    headline_arch = st.selectbox(
        "Primary architecture (headline comparison)",
        options=chosen_archs or ARCH_ORDER,
        index=0 if not chosen_archs else min(1, len(chosen_archs) - 1),
        format_func=lambda k: arch_labels[k],
    )
    st.caption(
        "The four represent a legacy-to-AI-era spectrum, not arbitrary "
        "options — all four are in real use today at different facility "
        "scales and climates. Compare all four, or pick one as primary."
    )

    st.header("Study window")
    seasonal = st.checkbox(
        "Seasonal model (blend summer + winter — more honest annual estimate, ~2x the live calls)",
        value=False,
    )
    start_date = st.date_input(
        "Summer window start" if seasonal else "Start date", value=pd.Timestamp("2025-07-01")
    ).isoformat()
    end_date = st.date_input(
        "Summer window end" if seasonal else "End date", value=pd.Timestamp("2025-07-31")
    ).isoformat()
    winter_start = winter_end = None
    if seasonal:
        winter_start = st.date_input("Winter window start", value=pd.Timestamp("2025-01-01")).isoformat()
        winter_end = st.date_input("Winter window end", value=pd.Timestamp("2025-01-31")).isoformat()
        st.caption(
            "Each window's average power draw is weighted evenly across the "
            "year (half from summer, half from winter) instead of scaling a "
            "single month to the whole year — still a simplification, but a "
            "meaningfully more honest one."
        )
    else:
        st.caption(
            "Cost is projected to a full year by linearly scaling this window's "
            "conditions across 8,760 hours — an upper-bound-leaning estimate if "
            "this window is a summer peak rather than a full year. Turn on the "
            "seasonal model above for a less one-sided estimate."
        )
    st.caption(
        "Keep each window to 31 days — both the heatmap and env_params "
        "endpoints share a range limit above roughly a month. It isn't one "
        "clean cutoff: 31 days is reliably fast (~45s), 32 days got accepted "
        "but sat processing without completing, and 34+ days is rejected "
        "outright. 31 is the only window confirmed both fast and reliable."
    )
    refresh = st.checkbox("Force refresh (bypass cache, re-bill)", value=False)

    run = st.button("Run comparison", type="primary", disabled=not sites or not chosen_archs)

# ── main ─────────────────────────────────────────────────────────────────
if not sites:
    st.info("Pick at least one candidate site in the sidebar to get started.")
    st.stop()

if "results" not in st.session_state:
    st.session_state["results"] = None

if run:
    it_load_kw = facility_mw * 1000
    status_box = st.status("Pulling FortyGuard data…", expanded=True)
    df, bin_data, wb_data = run_pipeline(
        client, sites, start_date, end_date, it_load_kw, rate, chosen_archs, refresh, status_box,
        seasonal=seasonal, winter_start=winter_start, winter_end=winter_end,
    )
    status_box.update(label="Done.", state="complete", expanded=False)
    st.session_state["results"] = (df, bin_data, wb_data)

if st.session_state["results"] is None:
    st.info("Configure your comparison in the sidebar, then click **Run comparison**.")
    st.stop()

df, bin_data, wb_data = st.session_state["results"]

# Cost is kWh × rate, and kWh doesn't depend on rate — so re-ranking at a
# different rate is pure local math on the results already pulled, no new
# API calls. This lets the story go beyond one fixed number: does the
# ranking hold at a different market's electricity price?
display_rate = st.slider(
    "Explore a different electricity rate ($/kWh)",
    min_value=0.05,
    max_value=0.40,
    value=float(rate),
    step=0.01,
    help="Recomputes every cost instantly from the kWh figures already pulled — no new API calls.",
)
if display_rate != rate:
    df = df.copy()
    df["annual_cost_usd"] = df["annual_kwh"] * display_rate
    st.caption(f"Showing costs at ${display_rate:.2f}/kWh — live re-ranked, zero new API calls.")
else:
    st.caption(f"Showing costs at ${display_rate:.2f}/kWh, the rate set in the sidebar.")

# Headline callout
headline_df = df[df["architecture_key"] == headline_arch].sort_values("annual_cost_usd")
headline_text = ""
if len(headline_df) >= 2:
    cheapest = headline_df.iloc[0]
    priciest = headline_df.iloc[-1]
    delta = priciest["annual_cost_usd"] - cheapest["annual_cost_usd"]
    headline_text = (
        f"With {arch_labels[headline_arch]}: {priciest['site']} costs an estimated "
        f"${delta:,.0f}/yr more to cool than {cheapest['site']} "
        f"(${cheapest['annual_cost_usd']:,.0f}/yr vs. ${priciest['annual_cost_usd']:,.0f}/yr)."
    )
    st.subheader(f"With {arch_labels[headline_arch]}:")
    st.markdown(
        f"### 🔥 **{priciest['site']}** costs an estimated **${delta:,.0f}/yr** more "
        f"to cool than **{cheapest['site']}**"
    )
    c1, c2, c3 = st.columns(3)
    c1.metric(cheapest["site"], f"${cheapest['annual_cost_usd']:,.0f}/yr")
    c2.metric(priciest["site"], f"${priciest['annual_cost_usd']:,.0f}/yr")
    c3.metric("Delta", f"${delta:,.0f}/yr", delta=f"{delta / max(cheapest['annual_cost_usd'], 1):.0%}")
elif len(headline_df) == 1:
    only = headline_df.iloc[0]
    headline_text = f"{only['site']} — {arch_labels[headline_arch]}: ${only['annual_cost_usd']:,.0f}/yr"
    st.metric(f"{only['site']} — {arch_labels[headline_arch]}", f"${only['annual_cost_usd']:,.0f}/yr")

st.divider()

# Comparison bar chart — fixed categorical order/colors, one axis, direct labels.
st.subheader("Projected annual cooling cost by site and architecture")
fig = px.bar(
    df,
    x="site",
    y="annual_cost_usd",
    color="architecture_key",
    barmode="group",
    color_discrete_map=ARCH_COLORS,
    category_orders={"architecture_key": ARCH_ORDER},
    labels={"annual_cost_usd": "Annual cooling cost (USD)", "site": "", "architecture_key": "Architecture"},
)
fig.for_each_trace(lambda t: t.update(name=arch_labels.get(t.name, t.name)))
fig.update_layout(
    plot_bgcolor="#fcfcfb",
    paper_bgcolor="#fcfcfb",
    font_color="#0b0b0b",
    legend_title_text="Cooling architecture",
    yaxis_tickprefix="$",
    yaxis_gridcolor="#e1e0d9",
)
st.plotly_chart(fig, use_container_width=True)

# Pairwise delta matrix — every site-pair's cost gap at the headline
# architecture, not just the cheapest-vs-priciest extremes the headline
# callout already surfaces. Diverging colorscale (two hues + a neutral
# zero midpoint), one axis, direct $ labels on every cell.
pairwise_df = df[df["architecture_key"] == headline_arch].sort_values("annual_cost_usd")
if len(pairwise_df) >= 2:
    st.subheader(f"Pairwise site comparison — {arch_labels[headline_arch]}")
    st.caption("Row → column: how much more (+) or less (−) the column site costs to cool than the row site.")
    site_order = pairwise_df["site"].tolist()
    costs = pairwise_df.set_index("site")["annual_cost_usd"]
    matrix = pd.DataFrame(
        [[costs[col] - costs[row] for col in site_order] for row in site_order],
        index=site_order,
        columns=site_order,
    )
    fig_matrix = px.imshow(
        matrix,
        text_auto="$,.0f",
        color_continuous_scale="RdBu_r",
        color_continuous_midpoint=0,
        aspect="auto",
        labels=dict(color="Δ annual cost (USD)"),
    )
    fig_matrix.update_layout(
        plot_bgcolor="#fcfcfb",
        paper_bgcolor="#fcfcfb",
        font_color="#0b0b0b",
        xaxis_title="",
        yaxis_title="",
    )
    st.plotly_chart(fig_matrix, use_container_width=True)

# Ranked table
st.subheader("Full comparison")
table = df.pivot_table(index="site", columns="architecture", values="annual_cost_usd").reindex(
    columns=[ARCHITECTURES[k].label for k in ARCH_ORDER if k in chosen_archs]
)
st.dataframe(table.style.format("${:,.0f}"), use_container_width=True)

exp_col1, exp_col2 = st.columns(2)
with exp_col1:
    st.download_button(
        "Download CSV",
        data=build_csv_bytes(df),
        file_name="dc_cooling_cost_comparison.csv",
        mime="text/csv",
    )
with exp_col2:
    methodology_notes = [(arch.label, arch.source) for arch in ARCHITECTURES.values()]
    st.download_button(
        "Download PDF report",
        data=build_pdf_bytes(df, headline_text, facility_mw, display_rate, methodology_notes),
        file_name="dc_cooling_cost_report.pdf",
        mime="application/pdf",
    )

# Site map
st.subheader("Candidate sites")
source_df = pd.DataFrame(
    [{"Site": s["name"], "Source": s.get("source", "—")} for s in sites]
)
st.dataframe(source_df, use_container_width=True, hide_index=True)

map_df = df[df["architecture_key"] == headline_arch][["site", "lat", "lon", "annual_cost_usd"]]
if not map_df.empty:
    fig_map = go.Figure(
        go.Scattermap(
            lat=map_df["lat"],
            lon=map_df["lon"],
            mode="markers+text",
            marker=dict(size=16, color=SEQUENTIAL_BLUE),
            text=map_df["site"],
            textposition="top center",
            hovertext=[f"{r.site}<br>${r.annual_cost_usd:,.0f}/yr" for r in map_df.itertuples()],
            hoverinfo="text",
        )
    )
    fig_map.update_layout(
        map=dict(style="open-street-map", center=dict(lat=map_df["lat"].mean(), lon=map_df["lon"].mean()), zoom=11),
        margin=dict(l=0, r=0, t=0, b=0),
        height=420,
    )
    st.plotly_chart(fig_map, use_container_width=True)

# Temperature-exposure detail for one site
st.subheader("Temperature exposure detail")
site_pick = st.selectbox("Site", options=[s["name"] for s in sites])
site_id = next(s["id"] for s in sites if s["name"] == site_pick)
col1, col2 = st.columns(2)
with col1:
    st.caption("Dry-bulb exposure (hours in each band, study window)")
    bins = bin_data.get(site_id, [])
    bin_rows = [
        {"band": ("< 5°C" if lo == float("-inf") else f"> 40°C" if hi == float("inf") else f"{lo:.0f}–{hi:.0f}°C"), "hours": hrs}
        for lo, hi, hrs in bins
    ]
    bin_df = pd.DataFrame(bin_rows)
    fig_bins = px.bar(bin_df, x="band", y="hours", labels={"band": "", "hours": "Hours"})
    fig_bins.update_traces(marker_color=SEQUENTIAL_BLUE)
    fig_bins.update_layout(plot_bgcolor="#fcfcfb", paper_bgcolor="#fcfcfb", font_color="#0b0b0b")
    st.plotly_chart(fig_bins, use_container_width=True)
with col2:
    st.caption("Wet-bulb hourly series (study window)")
    wb = wb_data.get(site_id, [])
    fig_wb = px.histogram(x=wb, nbins=20, labels={"x": "Wet-bulb temperature (°C)"})
    fig_wb.update_traces(marker_color=SEQUENTIAL_BLUE)
    fig_wb.update_layout(plot_bgcolor="#fcfcfb", paper_bgcolor="#fcfcfb", font_color="#0b0b0b", yaxis_title="Hours")
    st.plotly_chart(fig_wb, use_container_width=True)

with st.expander("Methodology & sources — cooling-architecture assumptions"):
    st.caption(
        "Each architecture's efficiency curve is calibrated against a named, "
        "checkable source, not an unstated guess — see `dc_siting/cooling_cost.py` "
        "for the full detail behind each number."
    )
    for arch in ARCHITECTURES.values():
        st.markdown(f"**{arch.label}**")
        st.caption(arch.source)
    st.caption(
        "Sanity-check ceiling: Uptime Institute's Global Data Center Survey "
        "2025 reports a 1.54 average global PUE — cooling is one part of "
        "total facility overhead, so no architecture here should imply a "
        "PUE far outside that. These remain calibration estimates, not a "
        "specific vendor's spec sheet — re-verify against the actual "
        "equipment under consideration before a real financial decision."
    )
