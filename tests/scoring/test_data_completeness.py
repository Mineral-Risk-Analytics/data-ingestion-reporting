"""Unit tests for the per-pillar data-completeness diagnostic added in 11.6.

The function is pure: given the sub-input snapshot of a single score
calculation, it returns the per-pillar + overall fraction of inputs that
came from real data vs default fallbacks.  Does NOT participate in the
scoring math; surfaced via rationale_json for partner UI.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.scoring.market_aggregator import (
    MARKET_PILLAR_WEIGHTS,
    _compute_pillar_data_completeness,
)


def _sig(**kwargs):
    """Build a stub MaterialCriticalitySignal with the given field values."""
    defaults = dict(
        criticality_score=None, hhi_score=None, reserve_hhi_score=None,
        reserve_life_index=None, capacity_utilization=None,
        production_yoy_pct=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _call(sig=None, **kwargs):
    """Wrap _compute_pillar_data_completeness with neutral defaults."""
    defaults = dict(
        geo_country_concentration=0.0,  # 4.1 removed the facility-presence floor
        geo_export_restriction=0.0,
        geo_tariff=0.0,
        reg_top_event_count=0,
        reg_scope_obligation_count=0,
        op_structural_dependency=None,  # V1: struct_dep retired (event-only)
        op_event_count=0,
        fin_evidence_count=0,
        fin_company_coverage=0.0,
    )
    defaults.update(kwargs)
    return _compute_pillar_data_completeness(sig, **defaults)


class TestAllRealData:
    """Baseline: every input is real → completeness 1.0 everywhere."""

    def test_overall_is_one(self):
        sig = _sig(
            criticality_score=0.7, hhi_score=4500, reserve_hhi_score=3000,
            reserve_life_index=40, capacity_utilization=0.85,
            production_yoy_pct=0.05,
        )
        r = _call(
            sig,
            geo_country_concentration=0.6, geo_export_restriction=0.4,
            geo_tariff=0.3,
            reg_top_event_count=3, reg_scope_obligation_count=2,
            op_structural_dependency=0.5, op_event_count=2,
            fin_evidence_count=180, fin_company_coverage=0.55,
        )
        assert r["overall"] == 1.0

    def test_every_pillar_is_one(self):
        sig = _sig(
            criticality_score=0.7, hhi_score=4500, reserve_hhi_score=3000,
            reserve_life_index=40, capacity_utilization=0.85,
            production_yoy_pct=0.05,
        )
        r = _call(
            sig,
            geo_country_concentration=0.6, geo_export_restriction=0.4,
            geo_tariff=0.3,
            reg_top_event_count=3, reg_scope_obligation_count=2,
            op_structural_dependency=0.5, op_event_count=2,
            fin_evidence_count=180, fin_company_coverage=0.55,
        )
        for pillar in ("material", "geopolitical", "regulatory", "operational", "financial"):
            assert r[pillar] == 1.0, f"{pillar} expected 1.0, got {r[pillar]}"


class TestAllDefaults:
    """Boundary: no signal anywhere → completeness 0.0 everywhere."""

    def test_overall_is_zero(self):
        assert _call(None)["overall"] == 0.0

    def test_per_pillar_is_zero(self):
        r = _call(None)
        for pillar in ("material", "geopolitical", "regulatory", "operational", "financial"):
            assert r[pillar] == 0.0


class TestMaterialPillar:
    """The Material pillar reads 6 fields from MaterialCriticalitySignal."""

    def test_no_signal_object_is_zero(self):
        assert _call(None)["material"] == 0.0

    def test_empty_signal_object_is_zero(self):
        assert _call(_sig())["material"] == 0.0

    def test_single_field_yields_one_sixth(self):
        r = _call(_sig(hhi_score=4500))
        assert r["material"] == pytest.approx(1/6, abs=0.005)

    def test_half_fields_yields_one_half(self):
        # 3 of 6 fields populated
        r = _call(_sig(
            criticality_score=0.7, hhi_score=4500, capacity_utilization=0.8,
        ))
        assert r["material"] == 0.5


class TestGeopoliticalPillar:
    """3 slots: country_concentration (> 0), exp_rest, tariff."""

    def test_zero_concentration_is_empty_slot(self):
        # 4.1: facility-presence floor removed — no MCS share is 0.0 (not the
        # old 0.02 floor), which is an empty slot.
        r = _call(geo_country_concentration=0.0)
        assert r["geopolitical"] == 0.0

    def test_any_real_concentration_counts(self):
        # Threshold is now "> 0.0": any real MCS share fills the slot.
        r = _call(geo_country_concentration=0.01)
        assert r["geopolitical"] == pytest.approx(1/3, abs=0.005)

    def test_all_three_slots_real(self):
        r = _call(
            geo_country_concentration=0.65,
            geo_export_restriction=0.4,
            geo_tariff=0.3,
        )
        assert r["geopolitical"] == 1.0


class TestRegulatoryPillar:
    """Binary: real iff at least one regulation OR scope obligation."""

    def test_no_regs_no_scope_is_zero(self):
        assert _call(reg_top_event_count=0, reg_scope_obligation_count=0)["regulatory"] == 0.0

    def test_one_regulation_is_one(self):
        assert _call(reg_top_event_count=1)["regulatory"] == 1.0

    def test_one_scope_obligation_is_one(self):
        assert _call(reg_scope_obligation_count=1)["regulatory"] == 1.0


class TestOperationalPillar:
    """2 slots: structural_dependency away from 0.3 floor, event_impacts > 0."""

    def test_floor_and_no_events_is_zero(self):
        r = _call(op_structural_dependency=0.3, op_event_count=0)
        assert r["operational"] == 0.0

    def test_above_floor_is_half(self):
        r = _call(op_structural_dependency=0.55, op_event_count=0)
        assert r["operational"] == 0.5

    def test_events_only_is_half(self):
        # Floor preserved (still 0.3) but events present.
        r = _call(op_structural_dependency=0.3, op_event_count=2)
        assert r["operational"] == 0.5

    def test_both_real_is_one(self):
        r = _call(op_structural_dependency=0.55, op_event_count=2)
        assert r["operational"] == 1.0


class TestFinancialPillar:
    """2 slots: any evidence_count > 0, sec_edgar coverage > 0."""

    def test_no_evidence_and_no_sec_is_zero(self):
        r = _call(fin_evidence_count=0, fin_company_coverage=0.0)
        assert r["financial"] == 0.0

    def test_evidence_only_is_half(self):
        # Captures the "Pink Sheet data but no SEC EDGAR" case (most
        # base-metal materials).
        r = _call(fin_evidence_count=180, fin_company_coverage=0.0)
        assert r["financial"] == 0.5

    def test_sec_only_is_half(self):
        # Captures the "SEC EDGAR producer coverage but no Pink Sheet
        # AND no Fig 10" hypothetical case.
        r = _call(fin_evidence_count=0, fin_company_coverage=0.6)
        assert r["financial"] == 0.5

    def test_both_is_one(self):
        r = _call(fin_evidence_count=180, fin_company_coverage=0.6)
        assert r["financial"] == 1.0


class TestOverallIsWeightedSum:
    """The 'overall' value is the MARKET_PILLAR_WEIGHTS-weighted mean of
    the five per-pillar values.  Sanity-check the math."""

    def test_only_financial_missing_yields_882(self):
        # Every pillar = 1.0 except Financial = 0.0.  Overall should be
        # 1.0 - financial_weight = 1.0 - 0.118 ≈ 0.882.
        sig = _sig(
            criticality_score=0.7, hhi_score=4500, reserve_hhi_score=3000,
            reserve_life_index=40, capacity_utilization=0.85,
            production_yoy_pct=0.05,
        )
        r = _call(
            sig,
            geo_country_concentration=0.6, geo_export_restriction=0.4,
            geo_tariff=0.3,
            reg_top_event_count=3, reg_scope_obligation_count=2,
            op_structural_dependency=0.5, op_event_count=2,
            fin_evidence_count=0, fin_company_coverage=0.0,
        )
        expected = 1.0 - MARKET_PILLAR_WEIGHTS["financial"]
        assert r["overall"] == pytest.approx(expected, abs=0.005)

    def test_financial_and_operational_missing(self):
        # Both bottom-weighted pillars missing → overall should be
        # 1.0 - 0.118 - 0.118 = 0.764.
        sig = _sig(
            criticality_score=0.7, hhi_score=4500, reserve_hhi_score=3000,
            reserve_life_index=40, capacity_utilization=0.85,
            production_yoy_pct=0.05,
        )
        r = _call(
            sig,
            geo_country_concentration=0.6, geo_export_restriction=0.4,
            geo_tariff=0.3,
            reg_top_event_count=3, reg_scope_obligation_count=2,
        )
        expected = 1.0 - MARKET_PILLAR_WEIGHTS["financial"] - MARKET_PILLAR_WEIGHTS["operational"]
        assert r["overall"] == pytest.approx(expected, abs=0.005)


class TestCobaltLikeProfile:
    """Realistic profile: thin Op, GAP Fin — captures the matrix's
    cobalt × DRC pattern.  This is the canonical 'launch material with
    asymmetric data' case the 11.6 diagnostic is designed to surface."""

    def test_overall_well_below_one(self):
        sig = _sig(
            criticality_score=0.85, hhi_score=6200, reserve_hhi_score=5800,
            reserve_life_index=35,
            capacity_utilization=None,  # not in MCS for cobalt
            production_yoy_pct=0.02,
        )
        r = _call(
            sig,
            geo_country_concentration=0.70, geo_export_restriction=0.55,
            geo_tariff=0.0,  # no cobalt-specific tariffs in this period
            reg_top_event_count=4, reg_scope_obligation_count=1,
            op_structural_dependency=0.3, op_event_count=0,
            fin_evidence_count=0, fin_company_coverage=0.0,
        )
        # Material 5/6 = 0.833, Geo 2/3 = 0.667, Reg 1.0, Op 0, Fin 0.
        # Overall recomputed from the LIVE weights (stale literals broke
        # when the pillar weights changed under V1 — 2026-07-27 triage).
        from app.services.scoring.market_aggregator import MARKET_PILLAR_WEIGHTS as W
        assert r["material"] == pytest.approx(0.833, abs=0.005)
        assert r["geopolitical"] == pytest.approx(0.667, abs=0.005)
        assert r["regulatory"] == 1.0
        assert r["operational"] == 0.0
        assert r["financial"] == 0.0
        expected = (W["material"] * r["material"] + W["geopolitical"] * r["geopolitical"]
                    + W["regulatory"] * 1.0)
        assert r["overall"] == pytest.approx(expected, abs=0.005)
