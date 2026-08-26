"""Tests for dc_siting/cooling_cost.py — pure math, no network involved."""

from __future__ import annotations

from dataclasses import replace

import pytest

from dc_siting.cooling_cost import (
    ARCHITECTURES,
    annualize,
    annualize_seasonal,
    bins_from_exceedance_ladder,
    cop_at_temperature,
    kwh_from_bins,
    kwh_from_hourly_series,
)

ARCH = ARCHITECTURES["chiller_tower"]  # free_cooling=8C, design=27C, cop 7.0->4.5


class TestCopAtTemperature:
    def test_below_threshold_is_free_cooling(self):
        assert cop_at_temperature(ARCH, ARCH.free_cooling_threshold_c - 1) is None

    def test_at_threshold_is_free_cooling(self):
        # <= threshold, so exactly at the boundary still counts as free cooling.
        assert cop_at_temperature(ARCH, ARCH.free_cooling_threshold_c) is None

    def test_at_design_temp_clamps_to_design_cop(self):
        assert cop_at_temperature(ARCH, ARCH.design_temp_c) == ARCH.cop_at_design

    def test_above_design_temp_stays_clamped(self):
        assert cop_at_temperature(ARCH, ARCH.design_temp_c + 20) == ARCH.cop_at_design

    def test_midpoint_interpolates_linearly(self):
        mid = (ARCH.free_cooling_threshold_c + ARCH.design_temp_c) / 2
        expected = (ARCH.cop_at_threshold + ARCH.cop_at_design) / 2
        assert cop_at_temperature(ARCH, mid) == pytest.approx(expected)

    def test_just_above_threshold_is_near_threshold_cop(self):
        just_above = ARCH.free_cooling_threshold_c + 0.01
        cop = cop_at_temperature(ARCH, just_above)
        assert cop == pytest.approx(ARCH.cop_at_threshold, abs=0.01)


class TestKwhFromHourlySeries:
    def test_all_hours_below_threshold_uses_aux_load_only(self):
        arch = ARCH
        series = [arch.free_cooling_threshold_c - 5] * 10
        it_load_kw = 1000.0
        kwh = kwh_from_hourly_series(arch, series, it_load_kw)
        assert kwh == pytest.approx(it_load_kw * arch.aux_load_fraction * 10)

    def test_all_hours_at_design_temp_uses_design_cop(self):
        arch = ARCH
        series = [arch.design_temp_c] * 5
        it_load_kw = 1000.0
        kwh = kwh_from_hourly_series(arch, series, it_load_kw)
        assert kwh == pytest.approx((it_load_kw / arch.cop_at_design) * 5)

    def test_empty_series_is_zero(self):
        assert kwh_from_hourly_series(ARCH, [], 1000.0) == 0.0

    def test_more_it_load_means_more_kwh(self):
        series = [20.0] * 24
        low = kwh_from_hourly_series(ARCH, series, 1000.0)
        high = kwh_from_hourly_series(ARCH, series, 2000.0)
        assert high == pytest.approx(low * 2)


class TestBinsFromExceedanceLadder:
    def test_bins_sum_to_total_hours(self):
        ladder = [5.0, 12.0, 19.0, 26.0, 33.0, 40.0]
        hours_above = {5.0: 190.0, 12.0: 150.0, 19.0: 90.0, 26.0: 40.0, 33.0: 10.0, 40.0: 2.0}
        total_hours = 200.0
        bins = bins_from_exceedance_ladder(hours_above, total_hours, ladder)
        assert sum(hrs for _, _, hrs in bins) == pytest.approx(total_hours)

    def test_bottom_bin_is_total_minus_lowest_threshold(self):
        ladder = [5.0, 12.0]
        hours_above = {5.0: 190.0, 12.0: 150.0}
        bins = bins_from_exceedance_ladder(hours_above, 200.0, ladder)
        bottom = bins[0]
        assert bottom[0] == float("-inf")
        assert bottom[2] == pytest.approx(10.0)  # 200 - 190

    def test_top_bin_equals_highest_threshold_hours(self):
        ladder = [5.0, 12.0]
        hours_above = {5.0: 190.0, 12.0: 150.0}
        bins = bins_from_exceedance_ladder(hours_above, 200.0, ladder)
        top = bins[-1]
        assert top[1] == float("inf")
        assert top[2] == pytest.approx(150.0)

    def test_never_returns_negative_hours(self):
        # A noisy/inconsistent hours_above (non-monotonic) shouldn't produce
        # a negative bin — clamped to zero instead.
        ladder = [5.0, 12.0]
        hours_above = {5.0: 100.0, 12.0: 120.0}  # inconsistent: "above 12" > "above 5"
        bins = bins_from_exceedance_ladder(hours_above, 200.0, ladder)
        assert all(hrs >= 0 for _, _, hrs in bins)


class TestKwhFromBins:
    def test_matches_manual_calculation_for_single_bin(self):
        arch = ARCH
        bins = [(20.0, 24.0, 10.0)]  # midpoint 22C, 10 hours
        it_load_kw = 1000.0
        expected_cop = cop_at_temperature(arch, 22.0)
        expected_kwh = (it_load_kw / expected_cop) * 10.0
        assert kwh_from_bins(arch, bins, it_load_kw) == pytest.approx(expected_kwh)

    def test_zero_hour_bins_contribute_nothing(self):
        arch = ARCH
        bins = [(20.0, 24.0, 0.0), (24.0, 28.0, 5.0)]
        it_load_kw = 1000.0
        with_zero = kwh_from_bins(arch, bins, it_load_kw)
        without_zero = kwh_from_bins(arch, [(24.0, 28.0, 5.0)], it_load_kw)
        assert with_zero == pytest.approx(without_zero)


class TestAnnualize:
    def test_scales_linearly_to_8760_hours(self):
        result = annualize("Site", ARCH, study_period_kwh=1000.0, study_period_hours=744.0, electricity_rate_usd_per_kwh=0.16)
        expected_scale = 8760.0 / 744.0
        assert result.projected_annual_kwh == pytest.approx(1000.0 * expected_scale)
        assert result.projected_annual_cost_usd == pytest.approx(result.projected_annual_kwh * 0.16)

    def test_zero_study_hours_does_not_raise(self):
        result = annualize("Site", ARCH, study_period_kwh=0.0, study_period_hours=0.0, electricity_rate_usd_per_kwh=0.16)
        assert result.projected_annual_kwh == 0.0


class TestAnnualizeSeasonal:
    def test_two_equal_windows_average_evenly(self):
        # Same avg kW in both windows -> seasonal blend should match a plain
        # annualize() of that same steady rate.
        windows = [("summer", 744.0, 744.0), ("winter", 744.0, 744.0)]  # 1 kWh/hr both windows
        result = annualize_seasonal("Site", ARCH, windows, electricity_rate_usd_per_kwh=0.16)
        assert result.projected_annual_kwh == pytest.approx(8760.0)  # 1 kW average * 8760h

    def test_hotter_summer_pulls_annual_estimate_up(self):
        # Summer draws more power per hour than winter -> annual kWh should
        # sit between the two windows' average rates, not equal either alone.
        windows = [("summer", 2000.0, 744.0), ("winter", 500.0, 744.0)]
        result = annualize_seasonal("Site", ARCH, windows, electricity_rate_usd_per_kwh=0.16)
        summer_only = annualize("Site", ARCH, 2000.0, 744.0, 0.16).projected_annual_kwh
        winter_only = annualize("Site", ARCH, 500.0, 744.0, 0.16).projected_annual_kwh
        assert winter_only < result.projected_annual_kwh < summer_only

    def test_window_breakdown_has_one_entry_per_window(self):
        windows = [("summer", 1000.0, 744.0), ("winter", 400.0, 744.0)]
        result = annualize_seasonal("Site", ARCH, windows, electricity_rate_usd_per_kwh=0.16)
        assert len(result.window_breakdown) == 2
        assert {w["label"] for w in result.window_breakdown} == {"summer", "winter"}


class TestArchitectureOrdering:
    """The relative-efficiency story the whole product rests on: DX worst,
    liquid best, in both free-cooling range and design-condition COP."""

    def test_dx_has_narrowest_free_cooling_band(self):
        dx = ARCHITECTURES["dx_air_cooled"]
        for key in ("chiller_tower", "evaporative", "liquid_dtc"):
            assert dx.free_cooling_threshold_c <= ARCHITECTURES[key].free_cooling_threshold_c

    def test_liquid_has_widest_free_cooling_band(self):
        liquid = ARCHITECTURES["liquid_dtc"]
        for key in ("dx_air_cooled", "chiller_tower", "evaporative"):
            assert liquid.free_cooling_threshold_c >= ARCHITECTURES[key].free_cooling_threshold_c

    def test_dx_has_lowest_design_cop(self):
        dx = ARCHITECTURES["dx_air_cooled"]
        for key in ("chiller_tower", "evaporative", "liquid_dtc"):
            assert dx.cop_at_design <= ARCHITECTURES[key].cop_at_design
