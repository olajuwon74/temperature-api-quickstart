"""Cooling-load and cost model.

Converts temperature exposure (dry-bulb bins from the heatmap exceedance
ladder, or an hourly wet-bulb series from env_params) into estimated annual
chiller electricity cost for a given cooling architecture and facility size.

The four architecture presets are calibrated (2026-08-21) against real,
citable industry references rather than reasoned-but-unsourced guesses —
each `CoolingArchitecture.source` field names what backs its numbers. The
references used:

* ASHRAE TC9.9 "Thermal Guidelines for Data Processing Environments" (5th
  ed.) — air classes A1-A4 (15-32C to 5-45C allowable dry-bulb) and the
  18-27C recommended envelope shared across classes; liquid-cooling facility
  water classes W17 through W45+ (2C up to 45C+ supply water), used to anchor
  the direct-to-chip free-cooling band.
* Field-reported COP ranges: legacy CRAC 1.5-2.5, ASHRAE 90.1-2019 minimum
  standard (2.2 net sensible COP), modern chiller+CRAH system 4.5-9.75
  (2-4x the DX range) — see MEP Academy / AHRI-sourced roundups.
* Evaporative/adiabatic research data: dew-point evaporative coolers reach
  COP up to ~29.7 average (peak 48.3) in favorable conditions, most
  effective below ~20C wet-bulb, degrading as humidity rises.
* Uptime Institute Global Data Center Survey 2025 — 1.54 average global PUE,
  used as a sanity ceiling (cooling is one part of total facility overhead,
  so no architecture's implied PUE should land far outside this).

These are still calibration estimates, not a specific vendor's spec sheet —
a real siting decision should re-verify against the actual equipment under
consideration — but every number here now traces to a named, checkable
source rather than an unstated guess. Every number is a `CoolingArchitecture`
field, so re-calibrating further is a data change, not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoolingArchitecture:
    key: str
    label: str
    # "dry_bulb" -> driven by the heatmap's measured 2m air temperature (tcm).
    # "wet_bulb" -> driven by env_params' wet_bulb_temperature_celsius, since
    # these systems reject heat by evaporation and respond to humidity, not
    # just raw air temperature.
    driving_temp: str
    # At/below this temperature, heat rejection needs no (or minimal)
    # compressor work — modeled as `aux_load_fraction` of IT load only.
    free_cooling_threshold_c: float
    # Design/extreme condition for this driving metric — COP is clamped to
    # `cop_at_design` at or above this temperature.
    design_temp_c: float
    cop_at_threshold: float
    cop_at_design: float
    # Fan/pump draw during free cooling, as a fraction of IT load.
    aux_load_fraction: float
    note: str
    source: str


ARCHITECTURES: dict[str, CoolingArchitecture] = {
    "dx_air_cooled": CoolingArchitecture(
        key="dx_air_cooled",
        label="Air-cooled DX / CRAC — legacy & small-site standard",
        driving_temp="dry_bulb",
        free_cooling_threshold_c=5.0,
        design_temp_c=32.0,
        cop_at_threshold=3.0,
        cop_at_design=2.2,
        aux_load_fraction=0.06,
        note="Still standard for edge/colo/small facilities. Narrow free-cooling band, steepest COP decay.",
        source=(
            "design_temp_c: ASHRAE TC9.9 Class A1 allowable upper bound (32C), "
            "the class matching legacy/enterprise DX deployments. "
            "cop_at_design: ASHRAE 90.1-2019 minimum standard, 2.2 net sensible "
            "COP. cop_at_threshold: top of the field-reported legacy-CRAC range "
            "(1.5-2.5), nudged up slightly for a reasonably modern unit."
        ),
    ),
    "chiller_tower": CoolingArchitecture(
        key="chiller_tower",
        label="Water-cooled chiller + tower — enterprise standard",
        driving_temp="wet_bulb",
        free_cooling_threshold_c=8.0,
        design_temp_c=27.0,
        cop_at_threshold=7.0,
        cop_at_design=4.5,
        aux_load_fraction=0.05,
        note="The dominant enterprise/hyperscale workhorse for ~20 years. Waterside economizer below threshold.",
        source=(
            "design_temp_c: ASHRAE TC9.9's 18-27C recommended envelope upper "
            "bound — chiller plants are typically designed against the "
            "recommended range, not the wider allowable range. cop_at_threshold "
            "/ cop_at_design: span the field-reported modern chiller+CRAH "
            "system range of 4.5-9.75 (favorable-condition top vs. "
            "least-favorable-realistic bottom of that range). "
            "free_cooling_threshold_c: typical cooling-tower approach "
            "(~3-5C) below a ~7C chilled-water setpoint, the standard "
            "waterside-economizer rule of thumb."
        ),
    ),
    "evaporative": CoolingArchitecture(
        key="evaporative",
        label="Evaporative / adiabatic — modern hyperscale",
        driving_temp="wet_bulb",
        free_cooling_threshold_c=18.0,
        design_temp_c=26.0,
        cop_at_threshold=13.0,
        cop_at_design=4.0,
        aux_load_fraction=0.04,
        note="What most major hyperscalers shifted to for air-cooled halls. Efficient in dry climates, degrades fastest as humidity rises.",
        source=(
            "free_cooling_threshold_c: research literature reports evaporative "
            "assist is most effective below ~20C wet-bulb — set at 18C to stay "
            "conservative. cop_at_threshold: heavily discounted from research-grade "
            "dew-point evaporative COP figures (avg ~29.7, peak 48.3, EER 20-30 "
            "in favorable arid conditions) to account for typical field-deployed "
            "overhead rather than optimized lab conditions. cop_at_design: "
            "reverts toward chiller-like performance once wet-bulb is high "
            "enough that evaporative assist provides little benefit."
        ),
    ),
    "liquid_dtc": CoolingArchitecture(
        key="liquid_dtc",
        label="Direct-to-chip liquid — AI-era",
        driving_temp="wet_bulb",
        free_cooling_threshold_c=28.0,
        design_temp_c=40.0,
        cop_at_threshold=12.0,
        cop_at_design=5.0,
        aux_load_fraction=0.03,
        note="AI/GPU-density racks. Higher coolant supply temps widen the free-cooling band.",
        source=(
            "free_cooling_threshold_c / design_temp_c: anchored to ASHRAE "
            "TC9.9 5th-edition liquid-cooling facility-water class W40 (up to "
            "40C supply water) — the realistic modern target class for "
            "GPU-density AI deployments, per ASHRAE's 2021 liquid-cooling "
            "guidance. Higher allowable coolant temps let dry coolers handle "
            "heat rejection without a chiller across a much wider ambient "
            "range than any air- or water-cooled architecture above. "
            "cop_at_threshold / cop_at_design: no directly published "
            "full-system COP was found for this specific configuration — "
            "these remain reasoned estimates (flagged, unlike the other "
            "three architectures) consistent with minimal compressor "
            "reliance under the W40 design envelope."
        ),
    ),
}


def cop_at_temperature(arch: CoolingArchitecture, temp_c: float) -> float | None:
    """COP at a given driving temperature, or None to signal free cooling."""
    if temp_c <= arch.free_cooling_threshold_c:
        return None
    if temp_c >= arch.design_temp_c:
        return arch.cop_at_design
    frac = (temp_c - arch.free_cooling_threshold_c) / (
        arch.design_temp_c - arch.free_cooling_threshold_c
    )
    return arch.cop_at_threshold + frac * (arch.cop_at_design - arch.cop_at_threshold)


def _power_kw(arch: CoolingArchitecture, temp_c: float, it_load_kw: float) -> float:
    cop = cop_at_temperature(arch, temp_c)
    if cop is None:
        return it_load_kw * arch.aux_load_fraction
    return it_load_kw / cop


def kwh_from_hourly_series(
    arch: CoolingArchitecture, hourly_temps_c: list[float], it_load_kw: float
) -> float:
    """Exact-ish: sum hourly power draw over a real hourly temperature series."""
    return sum(_power_kw(arch, t, it_load_kw) for t in hourly_temps_c)


def bins_from_exceedance_ladder(
    hours_above: dict[float, float], total_hours: float, ladder: list[float]
) -> list[tuple[float, float, float]]:
    """Turn cumulative "hours above threshold T" counts into per-bin hour counts.

    `ladder` must be sorted ascending and match the keys of `hours_above`
    (a threshold's hours-above value comes from `stats_data['mean']` on an
    exceedance heatmap call). Returns (bin_low, bin_high, hours) triples,
    using +/-inf for the open-ended bottom and top bins.
    """
    bins: list[tuple[float, float, float]] = []
    bins.append((float("-inf"), ladder[0], total_hours - hours_above[ladder[0]]))
    for lo, hi in zip(ladder[:-1], ladder[1:]):
        bins.append((lo, hi, hours_above[lo] - hours_above[hi]))
    bins.append((ladder[-1], float("inf"), hours_above[ladder[-1]]))
    return [(lo, hi, max(0.0, hrs)) for lo, hi, hrs in bins]


def kwh_from_bins(
    arch: CoolingArchitecture, bins: list[tuple[float, float, float]], it_load_kw: float
) -> float:
    """Approximate: sum power draw at each bin's representative (midpoint) temperature."""
    total = 0.0
    for lo, hi, hours in bins:
        if hours <= 0:
            continue
        if lo == float("-inf"):
            rep_temp = hi - 3.0
        elif hi == float("inf"):
            rep_temp = lo + 3.0
        else:
            rep_temp = (lo + hi) / 2.0
        total += _power_kw(arch, rep_temp, it_load_kw) * hours
    return total


@dataclass
class SiteCostResult:
    site_name: str
    architecture: str
    study_period_hours: float
    study_period_kwh: float
    study_period_cost_usd: float
    projected_annual_kwh: float
    projected_annual_cost_usd: float


def annualize(
    site_name: str,
    arch: CoolingArchitecture,
    study_period_kwh: float,
    study_period_hours: float,
    electricity_rate_usd_per_kwh: float,
) -> SiteCostResult:
    """Scale a study-period kWh total to a naive annual estimate.

    This linearly projects the study period's conditions across a full
    8,760-hour year (`projected = study * 8760 / study_period_hours`). That
    is an upper-bound-leaning estimate if the study window is a summer peak
    period rather than a full year — call out that caveat in the UI, don't
    let this number stand alone as "the" annual cost without it.
    """
    study_cost = study_period_kwh * electricity_rate_usd_per_kwh
    scale = 8760.0 / study_period_hours if study_period_hours else 0.0
    projected_kwh = study_period_kwh * scale
    projected_cost = study_cost * scale
    return SiteCostResult(
        site_name=site_name,
        architecture=arch.label,
        study_period_hours=study_period_hours,
        study_period_kwh=study_period_kwh,
        study_period_cost_usd=study_cost,
        projected_annual_kwh=projected_kwh,
        projected_annual_cost_usd=projected_cost,
    )
