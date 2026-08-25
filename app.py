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


def _launch() -> None:
    st.session_state.view = "app"


if st.session_state.view == "landing":
    bg_b64 = _b64(HERO_BG)
    logo_b64 = _b64(LOGO)

    st.markdown(
        f"""
        <style>
        @keyframes dc-fade-up {{
            from {{ opacity: 0; transform: translateY(18px); }}
            to   {{ opacity: 1; transform: translateY(0); }}
        }}
        @keyframes dc-pan {{
            0%   {{ background-position: 50% 15%; }}
            100% {{ background-position: 50% 45%; }}
        }}
        @keyframes dc-glow {{
            0%, 100% {{ opacity: .35; transform: scale(1); }}
            50%      {{ opacity: .65; transform: scale(1.08); }}
        }}
        @keyframes dc-float {{
            0%, 100% {{ transform: translateY(0); }}
            50%      {{ transform: translateY(-6px); }}
        }}
        .dc-hero {{
            position: relative;
            min-height: 74vh;
            border-radius: 20px;
            overflow: hidden;
            padding: 3.2vw 3.6vw 5.5vw 3.6vw;
            display: flex;
            flex-direction: column;
            justify-content: center;
            color: #fdfdfd;
            background-image:
                linear-gradient(120deg, rgba(5,16,32,.90) 0%, rgba(8,28,54,.62) 48%, rgba(5,16,32,.92) 100%),
                url('data:image/jpeg;base64,{bg_b64}');
            background-size: cover;
            background-position: 50% 25%;
            animation: dc-pan 22s ease-in-out infinite alternate;
        }}
        .dc-glow-blob {{
            position: absolute;
            width: 480px; height: 480px;
            left: -160px; top: -140px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(255,107,53,.55), transparent 70%);
            filter: blur(50px);
            animation: dc-glow 7s ease-in-out infinite;
            pointer-events: none;
        }}
        .dc-hero img.dc-logo {{
            height: 22px; width: auto; opacity: .92; margin: 0 0 22px 0 !important;
            display: block !important; align-self: flex-start !important; float: none !important;
            animation: dc-fade-up .7s ease both;
        }}
        .dc-eyebrow {{
            font-size: .82rem; letter-spacing: .12em; text-transform: uppercase;
            color: #ffb38a; font-weight: 600; margin-bottom: 14px;
            animation: dc-fade-up .7s ease both; animation-delay: .05s;
        }}
        .dc-hero h1 {{
            font-size: clamp(2.1rem, 4vw, 3.4rem); line-height: 1.08; font-weight: 800;
            margin: 0 0 18px 0; max-width: 780px; letter-spacing: -0.01em;
            animation: dc-fade-up .8s ease both; animation-delay: .12s;
        }}
        .dc-hero h2 {{
            font-size: clamp(1.05rem, 1.6vw, 1.35rem); font-weight: 500; line-height: 1.4;
            margin: 0 0 14px 0; max-width: 620px; color: #eef3fa;
            animation: dc-fade-up .8s ease both; animation-delay: .22s;
        }}
        .dc-hero p {{
            font-size: .98rem; line-height: 1.55; max-width: 560px; color: #c7d3e0;
            animation: dc-fade-up .8s ease both; animation-delay: .32s;
        }}
        .dc-badges {{
            position: absolute; right: 4vw; top: 50%; transform: translateY(-50%);
            display: flex; flex-direction: column; gap: 20px; z-index: 2;
        }}
        .dc-badge {{
            width: 54px; height: 54px; border-radius: 50%;
            border: 1.5px solid rgba(255,255,255,.32);
            background: rgba(255,255,255,.06);
            display: flex; align-items: center; justify-content: center;
            animation: dc-fade-up .7s ease both, dc-float 4.5s ease-in-out infinite;
        }}
        .dc-badge svg {{ width: 22px; height: 22px; }}
        @media (max-width: 900px) {{ .dc-badges {{ display: none; }} }}
        </style>

        <div class="dc-hero">
          <div class="dc-glow-blob"></div>
          <img class="dc-logo" src="data:image/png;base64,{logo_b64}" />
          <div class="dc-eyebrow">FortyGuard Hackathon '26</div>
          <h1>Data-Centre Siting &amp; Cooling-Cost Engine</h1>
          <h2>Know which candidate site costs more to cool — in real dollars — before you break ground.</h2>
          <p>
            Built on FortyGuard's hyperlocal, 2m-measured temperature data —
            turning site-to-site microclimate differences into an annual
            cooling-cost comparison, not just a temperature map.
          </p>
          <div class="dc-badges">
            <div class="dc-badge" style="animation-delay:.15s,0s" title="Hyperlocal temperature">
              <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                <rect x="10" y="3" width="4" height="12" rx="2"/><circle cx="12" cy="18" r="3.4"/>
              </svg>
            </div>
            <div class="dc-badge" style="animation-delay:.3s,.4s" title="Candidate sites">
              <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 21s7-7.58 7-12A7 7 0 1 0 5 9c0 4.42 7 12 7 12z"/><circle cx="12" cy="9" r="2.3"/>
              </svg>
            </div>
            <div class="dc-badge" style="animation-delay:.45s,.8s" title="Annual cost">
              <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                <line x1="12" y1="2" x2="12" y2="22"/><path d="M17 6.8c0-1.9-2.2-2.9-5-2.9s-5 1.1-5 2.9 2.2 2.5 5 2.9 5 1.1 5 2.9-2.2 2.9-5 2.9-5-1-5-2.9"/>
              </svg>
            </div>
            <div class="dc-badge" style="animation-delay:.6s,1.2s" title="Data-centre cooling">
              <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                <rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/>
                <circle cx="7" cy="7" r=".9" fill="#fff" stroke="none"/><circle cx="7" cy="17" r=".9" fill="#fff" stroke="none"/>
              </svg>
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <style>
        div[data-testid='stButton']{ margin: -5.4rem 0 0 3.2vw; max-width: 260px; }
        div[data-testid='stButton'] button{
            font-size: 1.1rem !important; font-weight: 700 !important;
            padding: .9rem 1.8rem !important; border-radius: 10px !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.button("Launch the tool →", type="primary", on_click=_launch)
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
        data=build_pdf_bytes(df, headline_text, facility_mw, rate, methodology_notes),
        file_name="dc_cooling_cost_report.pdf",
        mime="application/pdf",
    )

# Site map
st.subheader("Candidate sites")
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
