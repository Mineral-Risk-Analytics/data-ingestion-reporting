"""Pure-math tests for global_rollup helpers (F-GR-1/2 fixes, 2026-06-10).

The full ``score_material_global_rollup`` requires a Session + Postgres-
specific ``pg_insert``.  The math fixes were extracted into three pure
helpers so they can be tested directly without DB plumbing:

    - compute_per_pillar_weighted_average  (F-GR-1)
    - compute_overall_from_pillars         (F-GR-1 overall composition)
    - compute_data_quality_score           (F-GR-2 publishability gate)
"""

from __future__ import annotations

import pytest

from app.services.scoring.global_rollup import (
    MEANINGFUL_SIGNAL_THRESHOLD,
    _PILLAR_COLS,
    compute_data_quality_score,
    compute_meaningful_pillar_count,
    compute_overall_from_pillars,
    compute_per_pillar_weighted_average,
    compute_production_weighted_pillar,
)


# Convenient short names for the test scenarios — keep the imports
# self-documenting.
MATERIAL  = "material_concentration_score"
GEO       = "geopolitical_trade_score"
REG       = "regulatory_compliance_score"
OPERATIONAL = "operational_score"
FIN       = "financial_pressure_score"


# ---------------------------------------------------------------------------
# compute_per_pillar_weighted_average — F-GR-1
# ---------------------------------------------------------------------------

class TestPerPillarWeightedAverage:
    def test_all_pillars_populated_yields_simple_weighted_avg(self):
        # CN weight 0.7 → material=80, AU weight 0.3 → material=70
        # Expected: 0.7×80 + 0.3×70 = 77 (divided by total weight 1.0)
        per_geo = [
            ("CN", 0.7, {c: 80.0 for c in _PILLAR_COLS}),
            ("AU", 0.3, {c: 70.0 for c in _PILLAR_COLS}),
        ]
        values, weight, geos, _meaningful = compute_per_pillar_weighted_average(per_geo, _PILLAR_COLS)
        for col in _PILLAR_COLS:
            assert values[col] == pytest.approx(77.0)
            assert weight[col] == pytest.approx(1.0)
            assert geos[col] == 2

    def test_sparse_pillar_normalised_by_contributing_weights_only(self):
        # F-GR-1 CORE BUG SCENARIO:
        # CN weight 0.7, AU weight 0.3.  Material is populated on both,
        # regulatory only on AU.
        #
        # Pre-fix behaviour (BROKEN): regulatory would normalise to
        # 0×0.7 + 0.3×50 = 15 — under-counts AU's contribution because
        # CN's NULL spent its 0.7 share.
        #
        # Post-fix (CORRECT): regulatory normalises against AU's weight
        # only → 50.
        per_geo = [
            ("CN", 0.7, {MATERIAL: 80.0, GEO: 60.0, REG: None, OPERATIONAL: 40.0, FIN: None}),
            ("AU", 0.3, {MATERIAL: 70.0, GEO: 50.0, REG: 50.0, OPERATIONAL: 30.0, FIN: 20.0}),
        ]
        values, weight, geos, _meaningful = compute_per_pillar_weighted_average(per_geo, _PILLAR_COLS)
        # Material: both → 0.7×80 + 0.3×70 = 77, divisor=1.0
        assert values[MATERIAL] == pytest.approx(77.0)
        assert weight[MATERIAL] == pytest.approx(1.0)
        assert geos[MATERIAL] == 2
        # Regulatory: AU only → 0.3×50 / 0.3 = 50 (NOT 15)
        assert values[REG] == pytest.approx(50.0)
        assert weight[REG] == pytest.approx(0.3)
        assert geos[REG] == 1
        # Financial: AU only → 0.3×20 / 0.3 = 20 (NOT 6)
        assert values[FIN] == pytest.approx(20.0)
        assert weight[FIN] == pytest.approx(0.3)
        assert geos[FIN] == 1

    def test_pillar_with_no_data_returns_none(self):
        per_geo = [
            ("CN", 0.7, {MATERIAL: 80.0, GEO: None, REG: None, OPERATIONAL: None, FIN: None}),
            ("AU", 0.3, {MATERIAL: 70.0, GEO: None, REG: None, OPERATIONAL: None, FIN: None}),
        ]
        values, weight, geos, _meaningful = compute_per_pillar_weighted_average(per_geo, _PILLAR_COLS)
        assert values[MATERIAL] == pytest.approx(77.0)
        for col in (GEO, REG, OPERATIONAL, FIN):
            assert values[col] is None
            assert weight[col] == 0.0
            assert geos[col] == 0

    def test_zero_or_negative_weight_geo_ignored(self):
        # Equal-weight fallback (1.0) gets crushed in practice; verify
        # zero/negative weights are skipped outright.
        per_geo = [
            ("CN", 100.0, {c: 80.0 for c in _PILLAR_COLS}),
            ("XX", 0.0,   {c: 10.0 for c in _PILLAR_COLS}),
            ("YY", -1.0,  {c: 10.0 for c in _PILLAR_COLS}),
        ]
        values, _, geos, _m = compute_per_pillar_weighted_average(per_geo, _PILLAR_COLS)
        assert values[MATERIAL] == pytest.approx(80.0)
        assert geos[MATERIAL] == 1   # only CN counted

    def test_empty_geo_list_returns_all_none(self):
        values, weight, geos, _m = compute_per_pillar_weighted_average([], _PILLAR_COLS)
        for col in _PILLAR_COLS:
            assert values[col] is None
            assert weight[col] == 0.0
            assert geos[col] == 0


# ---------------------------------------------------------------------------
# compute_overall_from_pillars — F-GR-1 overall composition
# ---------------------------------------------------------------------------

PILLAR_WEIGHTS = {
    MATERIAL:    0.294,
    GEO:         0.235,
    REG:         0.235,
    OPERATIONAL: 0.118,
    FIN:         0.118,
}


class TestOverallFromPillars:
    def test_all_pillars_populated_uses_market_weights(self):
        pillar_values = {col: 50.0 for col in _PILLAR_COLS}
        overall = compute_overall_from_pillars(pillar_values, PILLAR_WEIGHTS)
        # All pillars at 50 → overall 50 regardless of weighting
        assert overall == pytest.approx(50.0)

    def test_missing_pillars_rescale_remaining_weights(self):
        # Only material + geopolitical populated.
        # Weights: 0.294 + 0.235 = 0.529.
        # Rescaled: material = 0.294/0.529 ≈ 0.556, geo = 0.235/0.529 ≈ 0.444
        # Pillars: material=80, geo=60 → 0.556×80 + 0.444×60 ≈ 71.1
        pillar_values = {
            MATERIAL: 80.0, GEO: 60.0,
            REG: None, OPERATIONAL: None, FIN: None,
        }
        overall = compute_overall_from_pillars(pillar_values, PILLAR_WEIGHTS)
        expected = (0.294 * 80.0 + 0.235 * 60.0) / (0.294 + 0.235)
        assert overall == pytest.approx(expected, rel=1e-3)

    def test_no_pillars_populated_returns_none(self):
        pillar_values = {col: None for col in _PILLAR_COLS}
        assert compute_overall_from_pillars(pillar_values, PILLAR_WEIGHTS) is None


# ---------------------------------------------------------------------------
# compute_data_quality_score — F-GR-2 publishability gate
# ---------------------------------------------------------------------------

class TestDataQualityScore:
    def test_fully_populated_returns_one(self):
        # 5 pillars × 5 geos = 25 cells, all populated → 1.0
        contributing_geos = {col: 5 for col in _PILLAR_COLS}
        q = compute_data_quality_score(contributing_geos, 5, PILLAR_WEIGHTS)
        assert q == pytest.approx(1.0)

    def test_half_populated_pillars_return_pillar_weighted_half(self):
        # All 5 pillars populated for half the geos
        contributing_geos = {col: 2 for col in _PILLAR_COLS}
        q = compute_data_quality_score(contributing_geos, 4, PILLAR_WEIGHTS)
        assert q == pytest.approx(0.5)

    def test_pillar_weight_dominates_score(self):
        # Material pillar weight = 0.294 (largest).
        # 5 geos total.  Material populated for all 5, others zero.
        # Score = (0.294 × 5 + 0 + 0 + 0 + 0) / (Σ weights × 5)
        #       = 1.470 / (1.000 × 5) = 0.294
        contributing_geos = {MATERIAL: 5, GEO: 0, REG: 0, OPERATIONAL: 0, FIN: 0}
        q = compute_data_quality_score(contributing_geos, 5, PILLAR_WEIGHTS)
        assert q == pytest.approx(0.294, rel=1e-2)

    def test_zero_geos_returns_zero(self):
        q = compute_data_quality_score({}, 0, PILLAR_WEIGHTS)
        assert q == 0.0


# ---------------------------------------------------------------------------
# compute_meaningful_pillar_count — F-GR-6 publishability gate refinement
# ---------------------------------------------------------------------------

class TestMeaningfulPillarCount:
    def test_per_pillar_average_tracks_meaningful_geos(self):
        """The 4th return value of compute_per_pillar_weighted_average
        counts geos whose value was ≥ MEANINGFUL_SIGNAL_THRESHOLD (5/100)."""
        per_geo = [
            ("CN", 100.0, {MATERIAL: 80.0, GEO: 60.0, REG: 0.0, OPERATIONAL: 0.5, FIN: 50.0}),
            ("AU", 100.0, {MATERIAL: 70.0, GEO: 0.0,  REG: 4.0, OPERATIONAL: 30.0, FIN: 45.0}),
            ("US", 100.0, {MATERIAL: 65.0, GEO: 0.0,  REG: 0.0, OPERATIONAL: 0.0, FIN: 60.0}),
        ]
        _, _, contributing, meaningful = compute_per_pillar_weighted_average(per_geo, _PILLAR_COLS)
        # Material: all 3 geos meaningful (80, 70, 65 — all ≥5)
        assert meaningful[MATERIAL] == 3
        assert contributing[MATERIAL] == 3
        # Geo: only CN ≥5
        assert meaningful[GEO] == 1
        # Reg: AU at 4 is below 5; CN and US at 0 below 5 — none meaningful
        assert meaningful[REG] == 0
        # Op: AU at 30 ≥5; CN at 0.5 and US at 0 below 5
        assert meaningful[OPERATIONAL] == 1
        # Fin: all 3 ≥5
        assert meaningful[FIN] == 3

    def test_meaningful_pillar_count_uses_30pct_threshold(self):
        # 5 pillars, each with 10 contributing geos.
        # Material: 4/10 meaningful = 40% (above 30%) → counts
        # Geo:      3/10 = 30% (exactly at threshold) → counts
        # Reg:      2/10 = 20% (below threshold) → doesn't count
        # Op:       0/10 = 0% → doesn't count
        # Fin:      10/10 = 100% → counts
        pillar_meaningful = {MATERIAL: 4, GEO: 3, REG: 2, OPERATIONAL: 0, FIN: 10}
        pillar_contributing = {MATERIAL: 10, GEO: 10, REG: 10, OPERATIONAL: 10, FIN: 10}
        n = compute_meaningful_pillar_count(pillar_meaningful, pillar_contributing)
        assert n == 3

    def test_zero_contributing_geos_pillar_skipped(self):
        # Pillar with 0 contributing geos shouldn't divide by zero
        pillar_meaningful = {MATERIAL: 5, GEO: 0, REG: 0, OPERATIONAL: 0, FIN: 0}
        pillar_contributing = {MATERIAL: 10, GEO: 0, REG: 0, OPERATIONAL: 0, FIN: 0}
        n = compute_meaningful_pillar_count(pillar_meaningful, pillar_contributing)
        assert n == 1

    def test_custom_fraction(self):
        pillar_meaningful = {MATERIAL: 6, GEO: 4, REG: 2, OPERATIONAL: 1, FIN: 0}
        pillar_contributing = {MATERIAL: 10, GEO: 10, REG: 10, OPERATIONAL: 10, FIN: 10}
        # At 50%: Material (60%) only → 1
        assert compute_meaningful_pillar_count(pillar_meaningful, pillar_contributing, 0.50) == 1
        # At 20%: Material + Geo + Reg → 3
        assert compute_meaningful_pillar_count(pillar_meaningful, pillar_contributing, 0.20) == 3

    def test_threshold_constant_value(self):
        # Locks in the threshold value so any future change is intentional.
        assert MEANINGFUL_SIGNAL_THRESHOLD == 5.0


# ---------------------------------------------------------------------------
# compute_production_weighted_pillar — geopolitical supply-origin weighting
# (2026-07-18): geopolitical rolls up by MINE-stage production share, not
# trade value, and non-producers must not move it.
# ---------------------------------------------------------------------------

class TestProductionWeightedPillar:
    def test_weights_by_production_share(self):
        # CD 75% at geopol 41.1 dominates; ID 14.4% at 16.7 next.
        ore = {"CD": 0.75, "ID": 0.144, "RU": 0.025}
        vals = {"CD": 41.1, "ID": 16.7, "RU": 18.0}
        got = compute_production_weighted_pillar(ore, vals)
        exp = (0.75*41.1 + 0.144*16.7 + 0.025*18.0) / (0.75 + 0.144 + 0.025)
        assert got == pytest.approx(exp)

    def test_non_producers_excluded_even_if_high_risk(self):
        # Syria/Belarus/Iran are politically unstable but produce ZERO cobalt,
        # so they are absent from ore_weights and cannot move the result — the
        # whole point of the fix.  A trade-weighted average WOULD include them.
        ore = {"CD": 0.75, "ID": 0.144}
        vals = {"CD": 41.1, "ID": 16.7, "SY": 28.1, "BY": 28.0, "IR": 23.0}
        got = compute_production_weighted_pillar(ore, vals)
        exp = (0.75*41.1 + 0.144*16.7) / (0.75 + 0.144)
        assert got == pytest.approx(exp)
        # And the dominant producer anchors it well above the trade-avg (~25.6)
        assert got > 35.0

    def test_missing_pillar_value_skipped(self):
        # A producer with no geopolitical score contributes nothing (not a 0).
        ore = {"CD": 0.75, "ID": 0.25}
        vals = {"CD": 40.0, "ID": None}
        assert compute_production_weighted_pillar(ore, vals) == pytest.approx(40.0)

    def test_zero_and_negative_weights_ignored(self):
        ore = {"CD": 0.75, "XX": 0.0, "YY": -0.3}
        vals = {"CD": 40.0, "XX": 99.0, "YY": 99.0}
        assert compute_production_weighted_pillar(ore, vals) == pytest.approx(40.0)

    def test_no_producer_contributes_returns_none(self):
        # No overlap between producers and scored geos → None so the caller
        # keeps its existing (trade-weighted) value.
        ore = {"CD": 0.75}
        vals = {"US": 20.0}
        assert compute_production_weighted_pillar(ore, vals) is None
        assert compute_production_weighted_pillar({}, {"CD": 40.0}) is None
