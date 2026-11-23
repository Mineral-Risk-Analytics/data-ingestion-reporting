"""Unit tests for the 11.4-Fin audit:

* ``score_financial_pressure`` parameter renamed ``filing_count`` →
  ``evidence_count`` (11.4-Fin-B).
* ``_derive_market_financial_inputs`` returns a 6-tuple including
  ``sub_input_diagnostic`` (11.4-Fin-A) with per-tier source attribution,
  sparse-evidence cap state, Tier 3 15-pt cap firing flag, and coverage
  counts.
* ``_classify_fin_source`` pure helper returns single-string source label.

The Financial Pressure pillar has the cleanest defaults of all 5 — no
0.5 midpoints anywhere — so the diagnostic IS the deliverable.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.scoring.financial_pressure import score_financial_pressure
from app.services.scoring.market_aggregator import (
    _classify_fin_source,
    _derive_market_financial_inputs,
)


AS_OF = date(2026, 6, 4)


# get_events_for_material uses PostgreSQL JSONB @> which SQLite can't parse.
# _derive_company_financial_signal hits company tables that we don't want
# to seed for these tests.  Both are patched out — these tests cover the
# diagnostic shape, not the event / company path.
def _patch_market_helpers():
    return patch.multiple(
        "app.services.scoring.market_aggregator",
        get_events_for_material=lambda *a, **kw: [],
        _derive_company_financial_signal=lambda *a, **kw: (0.0, 0.0, None),
    )


# ---------------------------------------------------------------------------
# 11.4-Fin-B: score_financial_pressure parameter rename
# ---------------------------------------------------------------------------

class TestEvidenceCountRename:
    """The parameter that was called ``filing_count`` is now
    ``evidence_count``.  Behaviour preserved end-to-end."""

    def test_evidence_count_two_or_more_full_score(self):
        # 2+ evidence points → no sparse cap; raw sum returned (or 100).
        score = score_financial_pressure(
            base_filing_signal=20.0, leverage_warning_bonus=15.0,
            liquidity_stress_bonus=10.0, evidence_count=2,
        )
        assert score == 45.0

    def test_evidence_count_one_halves(self):
        score = score_financial_pressure(
            base_filing_signal=20.0, leverage_warning_bonus=10.0,
            liquidity_stress_bonus=10.0, evidence_count=1,
        )
        # raw 40 × 0.5 = 20
        assert abs(score - 20.0) < 0.01

    def test_evidence_count_zero_zeros(self):
        score = score_financial_pressure(
            base_filing_signal=40.0, leverage_warning_bonus=30.0,
            liquidity_stress_bonus=30.0, evidence_count=0,
        )
        assert score == 0.0


# ---------------------------------------------------------------------------
# _classify_fin_source pure helper
# ---------------------------------------------------------------------------

class TestClassifyFinSource:
    def test_no_tier_returns_none(self):
        assert _classify_fin_source() == "none"
        assert _classify_fin_source(pink_sheet=0.0, fig10=0.0) == "none"

    def test_single_tier_returns_tier_name(self):
        assert _classify_fin_source(pink_sheet=10.0) == "pink_sheet"
        assert _classify_fin_source(fig10=10.0) == "fig10"
        assert _classify_fin_source(events=10.0) == "events"
        assert _classify_fin_source(company=10.0) == "company"

    def test_two_tiers_returns_multiple(self):
        assert _classify_fin_source(pink_sheet=5.0, fig10=10.0) == "multiple"
        assert _classify_fin_source(events=5.0, company=10.0) == "multiple"

    def test_three_or_four_tiers_returns_multiple(self):
        assert _classify_fin_source(
            pink_sheet=1.0, fig10=2.0, events=3.0,
        ) == "multiple"
        assert _classify_fin_source(
            pink_sheet=1.0, fig10=2.0, events=3.0, company=4.0,
        ) == "multiple"


# ---------------------------------------------------------------------------
# sub_input_diagnostic shape stability
# ---------------------------------------------------------------------------

class TestDiagnosticShape:
    """Top-level + inner key contract — partner UI relies on this."""

    def test_top_level_keys(self, sqlite_session):
        with _patch_market_helpers():
            result = _derive_market_financial_inputs(
                sqlite_session, material_id=1, geography_code="CD",
                as_of_date=AS_OF,
            )
        diag = result[5]
        assert set(diag.keys()) == {
            "base_filing_signal",
            "leverage_warning_bonus",
            "liquidity_stress_bonus",
            "sparse_evidence_cap",
            "coverage",
        }

    def test_base_filing_signal_inner_keys(self, sqlite_session):
        with _patch_market_helpers():
            result = _derive_market_financial_inputs(
                sqlite_session, material_id=1, geography_code="CD",
                as_of_date=AS_OF,
            )
        diag = result[5]
        assert set(diag["base_filing_signal"].keys()) == {
            "data_backed", "source",
            "pink_sheet_contribution", "fig10_contribution",
            "events_contribution", "company_contribution",
            "tier_3_capped_at_15",
        }

    def test_leverage_warning_inner_keys(self, sqlite_session):
        with _patch_market_helpers():
            result = _derive_market_financial_inputs(
                sqlite_session, material_id=1, geography_code="CD",
                as_of_date=AS_OF,
            )
        diag = result[5]
        assert set(diag["leverage_warning_bonus"].keys()) == {
            "data_backed", "source",
            "pink_sheet_contribution", "fig10_contribution",
            "events_contribution",
        }

    def test_liquidity_stress_inner_keys(self, sqlite_session):
        with _patch_market_helpers():
            result = _derive_market_financial_inputs(
                sqlite_session, material_id=1, geography_code="CD",
                as_of_date=AS_OF,
            )
        diag = result[5]
        assert set(diag["liquidity_stress_bonus"].keys()) == {
            "data_backed", "source",
            "pink_sheet_contribution", "fig10_contribution",
            "events_contribution",
        }

    def test_sparse_evidence_cap_inner_keys(self, sqlite_session):
        with _patch_market_helpers():
            result = _derive_market_financial_inputs(
                sqlite_session, material_id=1, geography_code="CD",
                as_of_date=AS_OF,
            )
        diag = result[5]
        assert set(diag["sparse_evidence_cap"].keys()) == {
            "applied", "factor", "evidence_count",
        }

    def test_coverage_inner_keys(self, sqlite_session):
        with _patch_market_helpers():
            result = _derive_market_financial_inputs(
                sqlite_session, material_id=1, geography_code="CD",
                as_of_date=AS_OF,
            )
        diag = result[5]
        assert set(diag["coverage"].keys()) == {
            "pink_sheet_price_points",
            "fig10_signal_present",
            "fin_event_count",
            "sec_edgar_coverage_weight",
            "sec_edgar_company_count",
        }


# ---------------------------------------------------------------------------
# Empty DB baseline
# ---------------------------------------------------------------------------

class TestEmptyDatabase:
    """No prices, no events, no SEC EDGAR: every signal 0, every
    data_backed False, sparse cap fires hard."""

    def test_all_sub_inputs_zero(self, sqlite_session):
        with _patch_market_helpers():
            result = _derive_market_financial_inputs(
                sqlite_session, material_id=1, geography_code="CD",
                as_of_date=AS_OF,
            )
        base, lev, liq, ev_count, _, diag = result
        assert base == 0.0
        assert lev == 0.0
        assert liq == 0.0
        # ev_count: 0 price points + 0 events + Fig 10 absent = 0
        assert ev_count == 0
        assert diag["base_filing_signal"]["data_backed"] is False
        assert diag["base_filing_signal"]["source"] == "none"
        assert diag["leverage_warning_bonus"]["data_backed"] is False
        assert diag["leverage_warning_bonus"]["source"] == "none"
        assert diag["liquidity_stress_bonus"]["data_backed"] is False
        assert diag["liquidity_stress_bonus"]["source"] == "none"

    def test_sparse_evidence_cap_fires(self, sqlite_session):
        """evidence_count=0 → cap factor 0.0, applied=True."""
        with _patch_market_helpers():
            result = _derive_market_financial_inputs(
                sqlite_session, material_id=1, geography_code="CD",
                as_of_date=AS_OF,
            )
        diag = result[5]
        assert diag["sparse_evidence_cap"]["applied"] is True
        assert diag["sparse_evidence_cap"]["factor"] == 0.0
        assert diag["sparse_evidence_cap"]["evidence_count"] == 0

    def test_coverage_all_zero(self, sqlite_session):
        with _patch_market_helpers():
            result = _derive_market_financial_inputs(
                sqlite_session, material_id=1, geography_code="CD",
                as_of_date=AS_OF,
            )
        diag = result[5]
        assert diag["coverage"]["pink_sheet_price_points"] == 0
        assert diag["coverage"]["fig10_signal_present"] is False
        assert diag["coverage"]["fin_event_count"] == 0
        assert diag["coverage"]["sec_edgar_coverage_weight"] == 0.0
        assert diag["coverage"]["sec_edgar_company_count"] == 0


# ---------------------------------------------------------------------------
# Pink Sheet path (Tier 1)
# ---------------------------------------------------------------------------

class TestPinkSheetTier:
    """When only Pink Sheet has data, source labels reflect that."""

    def _seed_prices(self, session, prices: list[float], material_id: int = 1):
        from app.models.supply import CommodityPrice
        # Spread prices across the price window (default 90 days).
        from datetime import timedelta
        for i, p in enumerate(prices):
            session.add(CommodityPrice(
                material_id=material_id,
                price_date=AS_OF - timedelta(days=i * 7),
                price_usd=p,
                price_unit="USD/MT",  # NOT NULL constraint
                source="test_seed",
            ))
        session.commit()

    def test_pink_sheet_volatility_only(self, sqlite_session):
        # High CV → base_filing_signal > 0.
        self._seed_prices(sqlite_session, [100.0, 200.0, 100.0, 200.0])
        with _patch_market_helpers():
            result = _derive_market_financial_inputs(
                sqlite_session, material_id=1, geography_code="CN",
                as_of_date=AS_OF,
            )
        base, _, _, _, _, diag = result
        assert base > 0.0
        assert diag["base_filing_signal"]["pink_sheet_contribution"] > 0.0
        # No other tiers active.
        assert diag["base_filing_signal"]["source"] == "pink_sheet"
        assert diag["coverage"]["pink_sheet_price_points"] == 4

    def test_pink_sheet_spike(self, sqlite_session):
        # Last > First by enough to trigger leverage_warning_bonus.
        # Prices descending (most recent first by date) means earliest=100, latest=300 → 200% spike.
        self._seed_prices(sqlite_session, [300.0, 250.0, 200.0, 100.0])
        with _patch_market_helpers():
            result = _derive_market_financial_inputs(
                sqlite_session, material_id=1, geography_code="CN",
                as_of_date=AS_OF,
            )
        _, lev, liq, _, _, diag = result
        assert lev > 0.0
        assert liq == 0.0
        assert diag["leverage_warning_bonus"]["source"] == "pink_sheet"
        assert diag["liquidity_stress_bonus"]["source"] == "none"

    def test_sparse_cap_not_applied_with_two_prices(self, sqlite_session):
        # 2 points = exactly the minimum, sparse cap NOT applied.
        self._seed_prices(sqlite_session, [100.0, 200.0])
        with _patch_market_helpers():
            result = _derive_market_financial_inputs(
                sqlite_session, material_id=1, geography_code="CN",
                as_of_date=AS_OF,
            )
        diag = result[5]
        assert diag["sparse_evidence_cap"]["applied"] is False
        assert diag["sparse_evidence_cap"]["factor"] == 1.0
        assert diag["sparse_evidence_cap"]["evidence_count"] == 2
