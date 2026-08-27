"""FortyGuard data pulls for a candidate site: dry-bulb exceedance ladder
(heatmap) and hourly wet-bulb series (env_params), both disk-cached.

Same submit/poll/cache shape as `notebooks/use_cases/parcel_portfolio_heat_screening.ipynb`
(the SDK's async task model doesn't change here) — reused rather than reinvented.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests
from shapely.geometry import Polygon, shape, mapping

from fortyguard import FortyGuardClient
from fortyguard.exceptions import FortyGuardError, TaskFailedError, TaskTimeoutError

# Bracket the free-cooling/design range of every dry-bulb architecture (only
# dx_air_cooled today) with a fixed, shared ladder so one set of heatmap
# calls per site serves any dry-bulb architecture we add later.
DRY_BULB_LADDER_C: list[float] = [5.0, 12.0, 19.0, 26.0, 33.0, 40.0]

_M_PER_DEG_LAT = 111_132.0


def _m_per_deg_lon(lat: float) -> float:
    return 111_320.0 * math.cos(math.radians(lat))


def _to_local_m(poly: Polygon, lat0: float, lon0: float) -> Polygon:
    k = _m_per_deg_lon(lat0)
    return Polygon([((x - lon0) * k, (y - lat0) * _M_PER_DEG_LAT) for x, y in poly.exterior.coords])


def _to_wgs84(poly: Polygon, lat0: float, lon0: float) -> Polygon:
    k = _m_per_deg_lon(lat0)
    return Polygon([(lon0 + x / k, lat0 + y / _M_PER_DEG_LAT) for x, y in poly.exterior.coords])


def buffered_aoi(site_geom: dict, buffer_m: float = 300.0) -> tuple[dict, float, float]:
    """Site polygon (GeoJSON geometry) -> buffered square AOI FeatureCollection.

    A raw single-parcel polygon is often smaller than one heatmap tile and
    returns zero cells (confirmed against the live API before writing this).
    Buffering guarantees enough tiles to average over. Returns the AOI plus
    the site centroid (lat, lon) used for the env_params point pull.
    """
    geom = shape(site_geom)
    cy, cx = geom.centroid.y, geom.centroid.x
    aoi_m = _to_local_m(geom, cy, cx).buffer(buffer_m, join_style=2).envelope
    aoi = _to_wgs84(aoi_m, cy, cx)
    fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": mapping(aoi)}]}
    return fc, cy, cx


def point_footprint(lat: float, lon: float, half_side_m: float = 150.0) -> dict:
    """A small square GeoJSON Polygon centered on a point — same footprint
    shape used for the bundled demo sites, for a user-entered address."""
    dlat = half_side_m / _M_PER_DEG_LAT
    dlon = half_side_m / _m_per_deg_lon(lat)
    coords = [[
        [lon - dlon, lat - dlat],
        [lon + dlon, lat - dlat],
        [lon + dlon, lat + dlat],
        [lon - dlon, lat + dlat],
        [lon - dlon, lat - dlat],
    ]]
    return {"type": "Polygon", "coordinates": coords}


def geocode_us_address(query: str) -> tuple[float, float] | None:
    """Best-effort geocode via OpenStreetMap Nominatim — free, no API key.

    Returns (lat, lon), or None if nothing matched. Nominatim's usage policy
    caps the public instance at ~1 request/second; this is only ever called
    from a human-paced "Add site" button click, so no extra throttling.
    """
    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": query, "format": "json", "limit": 1, "countrycodes": "us"},
        headers={"User-Agent": "dc-siting-cooling-cost-engine/1.0 (FortyGuard hackathon26 demo)"},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


def cache_key(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _submit_and_wait_quiet(
    client: FortyGuardClient,
    method_name: str,
    label: str,
    on_status,
    *,
    resubmit_attempts: int = 3,
    poll_attempts: int = 6,
    poll_timeout: float = 480.0,
    **kwargs,
) -> dict:
    """Submit an async task and poll it to completion, tolerating the three
    transient failure modes actually observed against the live API under
    sustained load: a still-processing task exceeding one poll budget
    (`TaskTimeoutError`), a mid-poll network drop (`requests` exceptions),
    and a transient 403. Those three are retried against the *same*
    activity_id. A task that reaches a genuine terminal `Failed` status is
    different — polling it again can't help — so that's handled by an outer
    loop that submits a fresh activity_id instead. Same two-loop shape as
    `submit_and_wait_quiet` in the parcel-portfolio notebook.
    """
    method = getattr(client, method_name)

    for submit_attempt in range(resubmit_attempts):
        activity_id = method(wait=False, **kwargs)
        on_status(f"submitted {label} -> {activity_id}")
        time.sleep(2.0)

        for poll_attempt in range(poll_attempts):
            try:
                result = client.wait_for(activity_id, poll_interval=5.0, timeout=poll_timeout)
                on_status(f"done: {label}")
                return result
            except TaskTimeoutError:
                if poll_attempt < poll_attempts - 1:
                    on_status(f"{label}: still processing, continuing to poll ({poll_attempt + 2}/{poll_attempts})")
                    continue
                break  # exhausted polling this activity — resubmit
            except TaskFailedError:
                if submit_attempt < resubmit_attempts - 1:
                    back_off = 5 * (submit_attempt + 1)
                    on_status(f"{label}: task failed, resubmitting in {back_off}s")
                    time.sleep(back_off)
                break  # resubmit
            except FortyGuardError as exc:
                msg = str(exc)
                if "-> 403" in msg and poll_attempt < poll_attempts - 1:
                    back_off = 5 * (poll_attempt + 1)
                    on_status(f"{label}: transient 403, retry in {back_off}s")
                    time.sleep(back_off)
                    continue
                raise
            except requests.exceptions.RequestException as exc:
                # Network-level hiccups (connection reset, read timeout) over
                # a long polling loop — same activity_id, just keep asking.
                if poll_attempt < poll_attempts - 1:
                    back_off = 5 * (poll_attempt + 1)
                    on_status(f"{label}: network error ({type(exc).__name__}), retry in {back_off}s")
                    time.sleep(back_off)
                    continue
                raise

    raise TaskFailedError(f"{label}: exhausted all {resubmit_attempts} submit attempts")


def pull_dry_bulb_bins(
    client: FortyGuardClient,
    cache_dir: Path,
    site_id: str,
    aoi: dict,
    start_date: str,
    end_date: str,
    refresh: bool = False,
    on_status=lambda msg: None,
) -> tuple[dict[float, float], float]:
    """Hours-above-threshold (AOI mean) at each rung of DRY_BULB_LADDER_C.

    Returns (hours_above dict keyed by threshold, total_hours_in_window).
    One heatmap call per rung; each is disk-cached independently so adding a
    rung later doesn't re-bill the ones already fetched.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    hours_above: dict[float, float] = {}
    for threshold in DRY_BULB_LADDER_C:
        key = cache_key("exceedance", site_id, start_date, end_date, threshold)
        path = cache_dir / f"{key}.json"
        if not refresh and path.exists():
            on_status(f"cached: exceedance @ {threshold:.0f}C")
            hours_above[threshold] = json.loads(path.read_text())["mean"]
            continue
        result = _submit_and_wait_quiet(
            client,
            "create_heatmap",
            f"exceedance @ {threshold:.0f}C",
            on_status,
            polygon_aoi=aoi,
            start_date=start_date,
            end_date=end_date,
            filter_type=4,
            granularity=100,
            analytic_type="exceedance",
            threshold=threshold,
            direction="above",
        )
        stats = result["stats_data"]
        mean_hours = stats["mean"] if stats.get("n_cells", 0) > 0 else 0.0
        path.write_text(json.dumps({"mean": mean_hours, "raw_stats": stats}))
        hours_above[threshold] = mean_hours

    n_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
    total_hours = n_days * 24.0
    return hours_above, total_hours


def pull_wet_bulb_series(
    client: FortyGuardClient,
    cache_dir: Path,
    site_id: str,
    lat: float,
    lon: float,
    start_date: str,
    end_date: str,
    refresh: bool = False,
    on_status=lambda msg: None,
) -> list[float]:
    """Hourly wet-bulb series (°C) at the site centroid over the study window."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key("wet_bulb", site_id, start_date, end_date)
    path = cache_dir / f"{key}.json"
    if not refresh and path.exists():
        on_status("cached: wet-bulb series")
        return json.loads(path.read_text())

    result = _submit_and_wait_quiet(
        client,
        "environmental_parameters",
        "wet-bulb series",
        on_status,
        latitude=lat,
        longitude=lon,
        temperature=25.0,
        start_date=start_date,
        end_date=end_date,
        filter_type=4,
    )
    series = result["locations"][0]["parameters"]["wet_bulb_temperature_celsius"]
    path.write_text(json.dumps(series))
    return series
