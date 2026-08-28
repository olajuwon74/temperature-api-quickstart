# Data-Centre Siting & Cooling-Cost Engine

Turns FortyGuard's hyperlocal temperature data into the dollar figure their
own [DATS 2025 Baseline report](https://www.fortyguard.com/post/data-center-ambient-thermal-screen-across-us)
stopped short of computing: which candidate data-centre site — and which
cooling architecture — actually costs less to run.

**Live app:** `app.py` (Streamlit) · **Branch:** `feat/dc-siting-cooling-cost-engine`

---

## Why this exists

FortyGuard's DATS report scored the thermal exposure of 36 real US AI data
centres and found dramatic site-to-site variation — including a 45-point gap
between AWS's own Northern Virginia and Pennsylvania facilities, inside one
operator's portfolio. The report explicitly stopped there: *"never a
facility energy, water, or cooling-cost figure, and no site's PUE is
computed."*

This tool picks up exactly where that report stopped — same underlying
hyperlocal temperature data, extended through a cooling-architecture cost
model into an actual annual dollar comparison.

## What it does

1. **Pick candidate sites** — from FortyGuard's own named DATS sites, a typed
   address (geocoded), or an uploaded GeoJSON boundary. All three are
   mixable in one comparison.
2. **Pick a facility size and cooling architecture** (or compare all four:
   air-cooled DX, water-cooled chiller+tower, evaporative/adiabatic,
   direct-to-chip liquid).
3. **Get back** a headline cost delta, a full site-by-architecture cost
   table, a comparison chart, a site map, and a temperature-exposure
   breakdown per site — plus CSV/PDF export.

## How the numbers are built

Two FortyGuard endpoints feed the model, per candidate site:

- **`POST /v1/heatmap`** (`analytic_type=exceedance`) — queried across a
  threshold ladder (5–40°C) over a buffered AOI, building an hours-above-
  threshold dry-bulb exposure profile. This drives the air-cooled/DX
  architecture, since FortyGuard's 2m-measured surface layer is the most
  hyperlocal signal available.
- **`POST /v1/env_params`** — an hourly wet-bulb time series at the site
  centroid, driving the three evaporation-dependent architectures
  (chiller+tower, evaporative, direct-to-chip liquid).

Both endpoints share an undocumented range limit: 31 days is reliably fast
(~45s), 32 days can get accepted but never complete, and 34+ days is
rejected outright. Keep any study window to 31 days.

Each cooling architecture's efficiency curve is calibrated against a named,
checkable source — not an unstated guess — visible in the app's
"Methodology & sources" panel and in `dc_siting/cooling_cost.py`:

- **ASHRAE TC9.9** (5th ed.) — air classes A1–A4, the 18–27°C recommended
  envelope, and liquid-cooling facility-water classes W17–W45+
- **Field-reported COP ranges** — legacy CRAC 1.5–2.5, ASHRAE 90.1-2019
  minimum (2.2), modern chiller+CRAH systems (4.5–9.75)
- **Evaporative-cooling research data** — dew-point COP up to ~29.7 avg
  (peak 48.3), most effective below ~20°C wet-bulb
- **Uptime Institute's 2025 Global Data Center Survey** — 1.54 average
  global PUE, used as a sanity ceiling

The one exception: direct-to-chip liquid cooling's COP figures remain
reasoned estimates — no published full-system number exists for that exact
configuration. This is disclosed in the app, not hidden.

### Annualizing a partial-year sample

By default, a single study window (e.g. one July) is linearly scaled to a
full year — an upper-bound-leaning estimate if that window happens to be a
summer peak. Turning on **"Seasonal model"** instead blends a summer and a
winter window's average power draw evenly across the year — a more honest
estimate, at roughly double the live API calls.

## A real API call

One actual call this project made against the live FortyGuard API — the
`exceedance` heatmap rung at 33°C for AWS's Northern Virginia site, over the
July 2025 study window. The request is exactly what `pull_dry_bulb_bins` in
`dc_siting/data.py` sends (the AOI is the site's real ~1km buffered
footprint, built by `buffered_aoi()`); the response is the real payload
FortyGuard returned, byte-for-byte from the cache file this project
committed (`data/dc_siting_cache/5f70b1d188d04397.json`).

Request — `client.create_heatmap(...)`:

```python
polygon_aoi = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature", "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-77.480204, 39.025951], [-77.469796, 39.025951],
                [-77.469796, 39.034049], [-77.480204, 39.034049],
                [-77.480204, 39.025951],
            ]],
        },
    }],
}

response = client.create_heatmap(
    polygon_aoi=polygon_aoi,
    start_date="2025-07-01",
    end_date="2025-07-31",
    filter_type=4,          # range of days
    granularity=100,
    analytic_type="exceedance",
    threshold=33.0,          # °C
    direction="above",
)
```

Response — `response["stats_data"]` (real, unedited):

```json
{
  "activity_id": "721ffc06-45b9-4e1e-bed6-69bed105e95e",
  "analytic_type": "exceedance",
  "units": "hour",
  "n_cells": 81,
  "min": 85.0,
  "max": 85.0,
  "mean": 85.0
}
```

Read literally: across the 81 tiles covering AWS's Northern Virginia
footprint, every tile spent exactly 85 of July's 744 hours above 33°C dry
bulb — the flat min/max/mean is expected here, since a ~1km AOI over one
site is thermally uniform enough that tile-to-tile variance is negligible
at this granularity. This one `mean` value becomes one rung of the
site's exceedance ladder (`DRY_BULB_LADDER_C`), which `bins_from_exceedance_ladder()`
turns into an hours-per-temperature-bin histogram for the cost model.

## Site sources

- **Known real AI data centres** — 6 of the 36 sites named in FortyGuard's
  DATS report (xAI Colossus/Memphis, AWS Northern Virginia, AWS
  Cumulus/Pennsylvania, Google/Berkeley County SC, Microsoft/San Antonio,
  Meta/Forest City NC). All 6 ship with pre-warmed cached responses, so any
  combination renders instantly rather than triggering a live wait — see
  `data/dc_siting_cache/`. The other 30 DATS sites aren't publicly named;
  the full roster requires contacting FortyGuard directly.
- **Type an address** — state + city/address, geocoded via OpenStreetMap
  Nominatim (free, best-effort, no API key).
- **Upload GeoJSON** — a multi-feature polygon FeatureCollection, same
  shape the repo's other use-case notebooks use.

## Getting started

```bash
git clone https://github.com/olajuwon74/temperature-api-quickstart
cd temperature-api-quickstart
git checkout feat/dc-siting-cooling-cost-engine

python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env       # add your FORTYGUARD_API_KEY

streamlit run app.py       # opens http://localhost:8501
```

The 6 named DATS sites ship with pre-warmed cached API responses (see
`data/dc_siting_cache/`), so the default comparison renders instantly on
first run — a live key is only actually called if you add a new site
(address or GeoJSON), pick a new study window, or tick the sidebar's
"Force refresh (bypass cache, re-bill)" checkbox.

Deployed on Streamlit Community Cloud from this branch — the
`FORTYGUARD_API_KEY` secret has to be entered in **TOML** format there
(`KEY = "value"`, quotes required), which is a different, stricter format
than the local `.env` file uses (no quotes needed).

## Testing

```bash
pytest tests/
```

24 tests cover the pure cost-model math in `dc_siting/cooling_cost.py` —
the COP curve, bin construction, both annualization paths, and the
DX-worst/liquid-best architecture ordering the product's story rests on.
No network involved, runs in well under a second. There's no integration
test suite against the live API — reliability there was validated through
extensive manual live runs during development instead (see the retry logic
in `dc_siting/data.py`, hardened against three real failure modes hit along
the way: transient 403s, mid-poll network drops, and task timeouts/failures
requiring resubmission).

## Known limitations

- The ~31-day API range limit means the default annual estimate is
  extrapolated from a partial-year sample (mitigated, not eliminated, by
  the optional seasonal model).
- Only 6 of the 36 real DATS sites are publicly named.
- Direct-to-chip liquid cooling's COP figures are reasoned estimates, not
  a cited source.
- No capital-cost, water-cost, or demand-charge electricity pricing
  modeling — this is an operating-cooling-cost comparison only.
- Cooling cost is realistically a tie-breaker in real site selection, not
  the primary driver (power availability and incentives usually dominate) —
  this tool gives a partial, not complete, view of that decision, by design.