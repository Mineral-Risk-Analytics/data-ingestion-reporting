"""Unit tests for the 11.4 Material Concentration default-fix + diagnostic.

The audit (11.4.A + 11.4.B + 11.4.C) changes the asymmetric defaults in
``_derive_market_material_inputs``:

  * criticality_score absent  : pre 0.5 / post 0.0
  * reserve_life_index absent : pre 0.5 / post 0.0
  * hhi_score absent          : pre 0.5 / post 0.0
  * reserve_hhi_score absent  : pre 0.5 / post 0.0
  * capacity_utilization absent: pre 0.5 / post 0.0

The supply_trend default was already 0.0 pre-11.4 and is preserved.

It also adds a ``sub_input_diagnostic`` dict — the 4th tuple element —
which mirrors the 11.6 data-completeness pattern at the sub-input level.

These tests cover the pure-function path: empty DB session, no MCS share
row, no facility row, so the producer_signal is forced to its terminal
``no_data`` branch.  Whenever a numeric output is asserted, the test
also asserts the matching diagnostic flag is False so the two stay in
lock-step.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.services.scoring.market_aggregator import (
    _derive_market_material_inputs,
)


AS_OF = date(2026, 6, 4)


def _sig(**kwargs):
    """Build a stub MaterialCriticalitySignal."""
    defaults = dict(
        criticality_score=None,
        hhi_score=None,
        reserve_hhi_score=None,
        reserve_life_index=None,
        capacity_utilization=None,
        production_yoy_pct=None,
        source="USGS_MCS_2026",
        reference_year=2024,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# 11.4.A + 11.4.B — defaults flipped from 0.5 → 0.0
# ---------------------------------------------------------------------------

class TestNoSignalReturnsZero:
    """When the criticality signal is absent entirely, every numeric output
    is 0.0 (was 0.5 / 0.5 / 0.3 pre-11.4)."""

    def test_no_signal_no_events(self, sqlite_session):
        crit, conc, trade_vol, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=None,
            geography_code="CD",  # no MCS share, no facility in empty DB
            trade_events=[],
            as_of_date=AS_OF,
        )
        # criticality: 0.70 × 0.0 + 0.30 × 0.0 = 0.0  (was 0.5 pre-11.4)
        assert crit == 0.0
        # concentration: all five components 0 → 0.0  (was ~0.5 pre-11.4)
        assert conc == 0.0
        # trade_volatility: still 0.3 when no events (this was NOT in scope of 11.4)
        assert trade_vol == 0.3

    def test_no_signal_diagnostic_all_false(self, sqlite_session):
        _, _, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=None,
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        assert diag["criticality"]["criticality_score_data_backed"] is False
        assert diag["criticality"]["reserve_life_index_data_backed"] is False
        assert diag["concentration"]["production_hhi_data_backed"] is False
        assert diag["concentration"]["reserve_hhi_data_backed"] is False
        assert diag["concentration"]["capacity_stress_data_backed"] is False
        assert diag["concentration"]["supply_trend_data_backed"] is False
        assert diag["concentration"]["producer_signal_source"] == "no_data"
        assert diag["trade_volatility"]["trade_event_data_backed"] is False
        assert diag["trade_volatility"]["trade_event_count"] == 0

    def test_empty_signal_object_returns_zero(self, sqlite_session):
        """An object exists but every field is None — still 0.0 / False."""
        crit, conc, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=_sig(),  # all defaults None
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        assert crit == 0.0
        assert conc == 0.0
        assert diag["concentration"]["production_hhi_data_backed"] is False
        assert diag["concentration"]["reserve_hhi_data_backed"] is False
        assert diag["concentration"]["capacity_stress_data_backed"] is False


class TestPartialSignal:
    """When some fields are populated and others are not, only the populated
    components contribute to concentration.  Verifies 11.4 default isn't
    a free-pass — the math still adds up component-wise."""

    def test_only_prod_hhi_populated(self, sqlite_session):
        # hhi_score = 1.0 (max), everything else None → concentration = 0.45
        _, conc, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=_sig(hhi_score=1.0),
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        assert conc == pytest.approx(0.45)
        assert diag["concentration"]["production_hhi_data_backed"] is True
        assert diag["concentration"]["reserve_hhi_data_backed"] is False
        assert diag["concentration"]["capacity_stress_data_backed"] is False

    def test_only_reserve_hhi_populated(self, sqlite_session):
        _, conc, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=_sig(reserve_hhi_score=1.0),
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        # 0.15 weight on reserve_hhi
        assert conc == pytest.approx(0.15)
        assert diag["concentration"]["reserve_hhi_data_backed"] is True
        assert diag["concentration"]["production_hhi_data_backed"] is False

    def test_only_capacity_stress_populated(self, sqlite_session):
        # cap_util = 0.90 → cap_stress = 1.0 → contribution = 0.10
        _, conc, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=_sig(capacity_utilization=0.90),
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        assert conc == pytest.approx(0.10)
        assert diag["concentration"]["capacity_stress_data_backed"] is True
        assert diag["concentration"]["production_hhi_data_backed"] is False

    def test_capacity_below_floor_still_data_backed(self, sqlite_session):
        # cap_util = 0.50 (the low threshold) → cap_stress = 0.0 BUT the data
        # was provided.  The diagnostic should still report data_backed=True
        # — the value is genuinely 0, not a default.
        _, conc, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=_sig(capacity_utilization=0.50),
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        assert conc == 0.0
        assert diag["concentration"]["capacity_stress_data_backed"] is True


class TestCriticalitySignalComponents:
    """The criticality blend = 0.70 × hhi_criticality + 0.30 × scarcity_signal."""

    def test_only_criticality_score(self, sqlite_session):
        crit, _, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=_sig(criticality_score=1.0),
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        # 0.70 × 1.0 + 0.30 × 0.0 = 0.70
        assert crit == pytest.approx(0.70)
        assert diag["criticality"]["criticality_score_data_backed"] is True
        assert diag["criticality"]["reserve_life_index_data_backed"] is False

    def test_only_reserve_life_index_low(self, sqlite_session):
        # RLI = 20 → scarcity_signal = 1.0 → 0.70 × 0.0 + 0.30 × 1.0 = 0.30
        crit, _, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=_sig(reserve_life_index=20),
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        assert crit == pytest.approx(0.30)
        assert diag["criticality"]["criticality_score_data_backed"] is False
        assert diag["criticality"]["reserve_life_index_data_backed"] is True

    def test_high_rli_data_backed_but_signal_zero(self, sqlite_session):
        # RLI = 80 → scarcity_signal clipped to 0.0; data still data-backed.
        crit, _, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=_sig(reserve_life_index=80),
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        assert crit == 0.0
        assert diag["criticality"]["reserve_life_index_data_backed"] is True


# ---------------------------------------------------------------------------
# 11.4.C — diagnostic dict shape and producer_signal_source semantics
# ---------------------------------------------------------------------------

class TestDiagnosticShape:
    """The diagnostic always returns the same three top-level keys with the
    same inner keys — partner UI relies on this stability."""

    def test_top_level_keys(self, sqlite_session):
        _, _, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=None,
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        assert set(diag.keys()) == {"criticality", "concentration", "trade_volatility"}

    def test_criticality_inner_keys(self, sqlite_session):
        _, _, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=None,
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        assert set(diag["criticality"].keys()) == {
            "criticality_score_data_backed",
            "reserve_life_index_data_backed",
        }

    def test_concentration_inner_keys(self, sqlite_session):
        _, _, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=None,
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        assert set(diag["concentration"].keys()) == {
            "production_hhi_data_backed",
            "reserve_hhi_data_backed",
            "producer_signal_source",
            "capacity_stress_data_backed",
            "supply_trend_data_backed",
        }

    def test_trade_volatility_inner_keys(self, sqlite_session):
        _, _, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=None,
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        assert set(diag["trade_volatility"].keys()) == {
            "trade_event_data_backed",
            "trade_event_count",
        }


class TestProducerSignalSource:
    """``producer_signal_source`` is a 3-way string, NOT a bool, so partner
    UI can distinguish 'MCS share' from 'facility-floor fallback' from
    'truly no data'."""

    def test_no_data_when_empty_db(self, sqlite_session):
        _, _, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=None,
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        assert diag["concentration"]["producer_signal_source"] == "no_data"

    def test_value_is_one_of_three_strings(self, sqlite_session):
        _, _, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=None,
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        assert diag["concentration"]["producer_signal_source"] in (
            "mcs_share", "facility_floor", "no_data",
        )


# ---------------------------------------------------------------------------
# Realistic launch-mineral profiles — captures the matrix's worst-offender
# patterns the 11.4 default fix is designed to correct.
# ---------------------------------------------------------------------------

class TestThinDataProfiles:
    """These tests verify the 11.4 fix removes the score-inflation pattern
    the audit identified.  Pre-11.4 each of these would have returned
    concentration close to 0.5 (the midpoint) even though MCS has no
    real signal for the material.  Post-11.4 they correctly return 0.0."""

    def test_cobalt_like_no_hhi_no_capacity(self, sqlite_session):
        # Cobalt has criticality_score but no hhi_score / reserve_hhi /
        # capacity_utilization — pre-11.4 these three would default to
        # 0.5 each → 0.45×0.5 + 0.15×0.5 + 0.10×0.5 = 0.35 of concentration
        # from pure defaults.  Post-11.4 = 0.0.
        crit, conc, _, _ = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=_sig(criticality_score=0.85),
            geography_code="CD",
            trade_events=[],
            as_of_date=AS_OF,
        )
        assert conc == 0.0
        assert crit == pytest.approx(0.595)  # 0.70 × 0.85

    def test_lithium_like_partial_data(self, sqlite_session):
        # Lithium typically has hhi but not reserve_hhi or capacity_utilization.
        # Concentration should reflect ONLY the hhi component, not borrow
        # 0.5 midpoints for the other slots.
        #
        # Step 1 update (2026-06-15): the raw hhi_score now flows through
        # ``hhi_concentration_risk`` (DOJ-aligned cliff mapping) before
        # weighting.  Raw 0.6 → cliff value in the "very highly concentrated"
        # band.  Expected formula: 0.45 × hhi_concentration_risk(0.6).
        from app.services.scoring.material_risk import hhi_concentration_risk
        _, conc, _, diag = _derive_market_material_inputs(
            sqlite_session,
            material_id=1,
            criticality_signal=_sig(criticality_score=0.7, hhi_score=0.6),
            geography_code="AU",
            trade_events=[],
            as_of_date=AS_OF,
        )
        # Only hhi contributes: 0.45 × cliff(0.6)
        expected_conc = 0.45 * hhi_concentration_risk(0.6)
        assert conc == pytest.approx(expected_conc, abs=1e-4)
        # Diagnostic confirms only one of the five components was real.
        real_flags = [
            diag["concentration"]["production_hhi_data_backed"],
            diag["concentration"]["reserve_hhi_data_backed"],
            diag["concentration"]["capacity_stress_data_backed"],
            diag["concentration"]["supply_trend_data_backed"],
        ]
        assert sum(real_flags) == 1  # only prod_hhi
