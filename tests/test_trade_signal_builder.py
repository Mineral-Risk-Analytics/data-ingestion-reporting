"""Unit tests for ``trade_signal_builder`` — currently scoped to the
pure-function helpers added/changed in the 2026-06 Section 9.1 audit:

  * :func:`_concentration_severity` — 5-bucket calibration including
    the new ``>=0.95 → 1.00`` ceiling.
  * :func:`_herfindahl_hirschman_index` — HHI computation and the
    edge-case ``world_total == 0``.

End-to-end ``build_trade_signals`` integration testing requires a real
database session + seeded trade_flows; that is out of scope here and is
covered operationally by the daily Inngest job + the run-summary
authoritative count fix from 9.1's sibling work.
"""

from __future__ import annotations

import math

import pytest

from app.services.ingestion.trade_signal_builder import (
    _BASE_CONFIDENCE_SCORE,
    _COMPANY_DATA_CONFIDENCE_DEFAULT,
    _COVERAGE_THIN_WORLD_TOTAL,
    _COVERAGE_WARNING_CONFIDENCE_FACTOR,
    _DROP_THRESHOLD,
    _IMPORT_DECLINE_CONFIDENCE_SCORE,
    _MIN_PER_COUNTRY_TRADE_USD,
    _MIN_REPORTERS_FOR_FULL_CONFIDENCE,
    _MIN_TRADE_VALUE_USD,
    _MIN_WORLD_TOTAL_USD,
    _compute_company_relevance,
    _concentration_severity,
    _drop_severity,
    _herfindahl_hirschman_index,
)


class TestConcentrationSeverity:
    """Cover every bucket boundary in :func:`_concentration_severity`."""

    @pytest.mark.parametrize(
        "share, expected",
        [
            # Bottom bucket: 60-69%
            (0.60, 0.55),
            (0.65, 0.55),
            (0.6999, 0.55),
            # 70-79%
            (0.70, 0.70),
            (0.79, 0.70),
            # 80-89%
            (0.80, 0.82),
            (0.85, 0.82),
            (0.8999, 0.82),
            # 90-94% — pre-9.1.C this was 0.90 ceiling for >=0.90.
            (0.90, 0.90),
            (0.94, 0.90),
            # >=95% — new bucket added 9.1.C, was 0.90, now 1.00.
            (0.95, 1.00),
            (0.99, 1.00),
            (1.00, 1.00),
        ],
    )
    def test_buckets(self, share: float, expected: float) -> None:
        assert _concentration_severity(share) == expected

    def test_below_threshold_returns_floor_bucket(self) -> None:
        # Caller is expected to gate on ``share >= _CONCENTRATION_THRESHOLD``
        # before calling, but the function should still return the floor
        # bucket value (not raise) if a sub-threshold share leaks through.
        assert _concentration_severity(0.59) == 0.55
        assert _concentration_severity(0.0) == 0.55

    def test_monopoly_severity_is_one(self) -> None:
        # Headline post-9.1.C behaviour: true monopoly hits the system
        # ceiling, not 0.90.
        assert _concentration_severity(1.00) == 1.00


class TestHerfindahlHirschmanIndex:
    """Cover :func:`_herfindahl_hirschman_index` semantics."""

    def test_pure_monopoly_is_ten_thousand(self) -> None:
        # Single country at 100% → HHI = 1.0^2 * 10000 = 10000.
        hhi = _herfindahl_hirschman_index({"CN": 1_000_000.0})
        assert hhi == pytest.approx(10_000.0)

    def test_even_two_way_split(self) -> None:
        # 50/50 → HHI = 0.5^2 + 0.5^2 = 0.5 → 5000.  DOJ "highly
        # concentrated" lower bound is 2500.
        hhi = _herfindahl_hirschman_index({"A": 500.0, "B": 500.0})
        assert hhi == pytest.approx(5_000.0)

    def test_top_with_diversified_rest(self) -> None:
        # 60% top + 10 equally-small (4% each) others.
        # HHI = 0.6^2 + 10 * 0.04^2 = 0.36 + 0.016 = 0.376 → 3760.
        country_totals = {"A": 600.0}
        for i in range(10):
            country_totals[f"C{i}"] = 40.0
        hhi = _herfindahl_hirschman_index(country_totals)
        assert hhi == pytest.approx(3_760.0, abs=0.5)

    def test_top_with_concentrated_competitor(self) -> None:
        # 60% top + 35% second + 5% third.
        # HHI = 0.36 + 0.1225 + 0.0025 = 0.485 → 4850.
        country_totals = {"A": 600.0, "B": 350.0, "C": 50.0}
        hhi = _herfindahl_hirschman_index(country_totals)
        assert hhi == pytest.approx(4_850.0, abs=0.5)

    def test_diversified_vs_concentrated_distinguishable(self) -> None:
        # The whole reason 9.1.B added HHI: raw top-share treats these
        # identically, HHI doesn't.
        diversified = {"A": 600.0, "B": 100.0, "C": 100.0, "D": 100.0, "E": 100.0}
        concentrated = {"A": 600.0, "B": 400.0}
        hhi_div = _herfindahl_hirschman_index(diversified)
        hhi_conc = _herfindahl_hirschman_index(concentrated)
        # Both have 60% top-share but HHI separates them clearly.
        assert hhi_conc > hhi_div
        assert (hhi_conc - hhi_div) > 1000  # meaningful spread

    def test_empty_dict_returns_zero(self) -> None:
        assert _herfindahl_hirschman_index({}) == 0.0

    def test_zero_world_total_returns_zero(self) -> None:
        # Defensive edge case — caller is supposed to have filtered
        # zero-total groups already, but the function should not divide
        # by zero if a row sneaks through.
        assert _herfindahl_hirschman_index({"A": 0.0, "B": 0.0}) == 0.0

    def test_hhi_bounded_by_zero_and_ten_thousand(self) -> None:
        # Property: HHI is always on [0, 10000] regardless of inputs.
        # Sample a few random-ish distributions.
        cases = [
            {"A": 1.0},
            {"A": 1.0, "B": 1.0, "C": 1.0},
            {"A": 5.0, "B": 2.0, "C": 2.0, "D": 1.0},
            {"A": 1_000_000.0, "B": 1.0},
        ]
        for c in cases:
            hhi = _herfindahl_hirschman_index(c)
            assert 0.0 <= hhi <= 10_000.0, c
            assert not math.isnan(hhi)


class TestDropSeverity:
    """Cover every bucket boundary in :func:`_drop_severity` post-9.2.B + 9.2.C."""

    @pytest.mark.parametrize(
        "drop_fraction, expected",
        [
            # Bottom bucket: 25-29% (post-9.2.B threshold bump)
            (0.25, 0.50),
            (0.27, 0.50),
            (0.2999, 0.50),
            # 30-39%
            (0.30, 0.65),
            (0.39, 0.65),
            # 40-49%
            (0.40, 0.78),
            (0.49, 0.78),
            # 50-59%
            (0.50, 0.88),
            (0.59, 0.88),
            # >=60% — new bucket added 9.2.C, was 0.88, now 1.00.
            (0.60, 1.00),
            (0.70, 1.00),
            (1.00, 1.00),
        ],
    )
    def test_buckets(self, drop_fraction: float, expected: float) -> None:
        assert _drop_severity(drop_fraction) == expected

    def test_below_threshold_returns_floor_bucket(self) -> None:
        # Caller is expected to gate on ``drop_fraction >= _DROP_THRESHOLD``
        # but the function should still return the floor bucket value
        # (not raise) if a sub-threshold drop leaks through.
        assert _drop_severity(0.10) == 0.50
        assert _drop_severity(0.0) == 0.50

    def test_catastrophic_drop_hits_system_ceiling(self) -> None:
        # Headline post-9.2.C behavior: catastrophic drops (think
        # Indonesia 2014 nickel ore export ban, ~70% drop) get the
        # full system ceiling, not the previously-capped 0.88.
        assert _drop_severity(0.70) == 1.00


class TestDropThreshold:
    """The threshold itself moved from 0.20 to 0.25 in 9.2.B."""

    def test_threshold_is_twenty_five_percent(self) -> None:
        # If anyone reverts to 0.20 without a documented reason, this
        # test fails so the calibration drift is caught.  The decision
        # is rooted in trade-shock literature (IMF / academic trade
        # economics) using 25-30% as the "unusual decline" threshold.
        assert _DROP_THRESHOLD == 0.25


class TestCoverageGuardConstants:
    """Sanity-check the 9.1.A constants are internally consistent.

    These are not behaviour tests — they catch refactors that
    accidentally break the chain of reasoning behind the magic numbers.
    """

    def test_coverage_warning_factor_drops_below_high_severity_floor(self) -> None:
        # 9.1.A claim: 0.6 * 0.75 = 0.45 < 0.60 (the high-severity
        # confidence floor in compute_effective_confidence).  If anyone
        # bumps _COVERAGE_WARNING_CONFIDENCE_FACTOR up, this assertion
        # fails so the methodology drift is caught at test time.
        thinned = _BASE_CONFIDENCE_SCORE * _COVERAGE_WARNING_CONFIDENCE_FACTOR
        assert thinned < 0.60

    def test_min_reporters_is_at_least_two(self) -> None:
        # If the threshold ever gets set to 1, the soft guard becomes a
        # no-op (every group has at least 1 reporter or wouldn't be in
        # the dict).  Guard against accidental relaxation.
        assert _MIN_REPORTERS_FOR_FULL_CONFIDENCE >= 2

    def test_thin_world_total_at_or_above_per_country_floor(self) -> None:
        # 9.4 (2026-06): per-group hard floor and the coverage-warning
        # "thin" threshold are now equal ($5M).  The world_total
        # branch of the coverage warning is dead code under current
        # constants — only the reporter_count branch fires the warning.
        # Constant retained at $5M for documentation + to make a future
        # Phase 1.5 change that lowers _MIN_WORLD_TOTAL_USD trivial.
        assert _COVERAGE_THIN_WORLD_TOTAL >= _MIN_PER_COUNTRY_TRADE_USD


class TestNoiseFloorConstants:
    """9.4 split: per-country flow floor vs per-group meaningfulness floor."""

    def test_per_country_floor_matches_backcompat_alias(self) -> None:
        # The pre-9.4 single constant ``_MIN_TRADE_VALUE_USD`` is now an
        # alias to the per-country floor.  This guard catches anyone
        # breaking the backcompat by changing the alias to point at
        # _MIN_WORLD_TOTAL_USD instead.
        assert _MIN_TRADE_VALUE_USD == _MIN_PER_COUNTRY_TRADE_USD

    def test_world_total_floor_strictly_above_per_country(self) -> None:
        # Semantic invariant: it should never make sense for a group's
        # hard floor to be at or below the per-country floor.  A group
        # built from a single country at the per-country floor would
        # trivially clear the group floor under that configuration.
        assert _MIN_WORLD_TOTAL_USD > _MIN_PER_COUNTRY_TRADE_USD

    def test_world_total_floor_at_five_million(self) -> None:
        # 9.4 calibrated this at exactly $5M (5x the per-country floor)
        # to align with the existing coverage-warning band.  If anyone
        # bumps this without re-thinking the coverage warning, the
        # test fails to surface the dependency.
        assert _MIN_WORLD_TOTAL_USD == 5_000_000


class TestComputeCompanyRelevance:
    """9.6 — relevance derived from exposure_score × data_confidence."""

    def test_max_exposure_max_confidence(self) -> None:
        # Headline case: fully-exposed, fully-confident company maps to
        # relevance 1.0, which feeds the 1.30x impact multiplier (max).
        assert _compute_company_relevance(1.0, 1.0) == 1.0

    def test_zero_exposure_yields_zero(self) -> None:
        # exposure=0 should never produce a non-zero relevance.
        # Companies with zero exposure should be skipped at the caller
        # layer, but the helper itself must not silently boost them.
        assert _compute_company_relevance(0.0, 1.0) == 0.0

    def test_zero_confidence_yields_zero(self) -> None:
        # confidence=0 means the partner has no faith in the exposure
        # value at all — relevance collapses to 0 regardless of the
        # nominal exposure.
        assert _compute_company_relevance(1.0, 0.0) == 0.0

    def test_null_confidence_uses_default(self) -> None:
        # When data_confidence is NULL (partner hasn't graded the row),
        # _COMPANY_DATA_CONFIDENCE_DEFAULT (0.8) kicks in.
        result = _compute_company_relevance(0.5, None)
        assert result == pytest.approx(0.5 * _COMPANY_DATA_CONFIDENCE_DEFAULT)

    def test_partial_exposure_partial_confidence(self) -> None:
        # Worked example from the audit doc: exposure=0.5, confidence=0.8
        # → relevance=0.40 (mapped to ~0.94x impact multiplier — i.e.
        # slight discount rather than the 1.21x boost the pre-9.6 0.85
        # constant would have given this company).
        assert _compute_company_relevance(0.5, 0.8) == pytest.approx(0.40)

    def test_relevance_clamped_to_unit_interval(self) -> None:
        # Defensive: should never exceed 1.0 or go below 0.0, even with
        # malformed inputs (e.g. caller bug passing a score > 1.0).
        assert _compute_company_relevance(1.5, 1.5) == 1.0
        assert _compute_company_relevance(-0.5, 1.0) == 0.0

    def test_default_confidence_is_moderate(self) -> None:
        # 9.6 chose 0.8 as the fallback when data_confidence is NULL.
        # This guard catches accidental change to a non-moderate value
        # (e.g. 1.0 = assume confident, or 0.5 = assume uncertain).
        assert _COMPANY_DATA_CONFIDENCE_DEFAULT == 0.8


class TestImportDeclineConfidence:
    """The 9.3.A confidence-asymmetry constant for IMPORT_DECLINE."""

    def test_import_below_export(self) -> None:
        # Headline 9.3.A claim: import-side declines are noisier than
        # export-side declines, so their inherent confidence is lower.
        assert _IMPORT_DECLINE_CONFIDENCE_SCORE < _BASE_CONFIDENCE_SCORE

    def test_import_confidence_below_high_severity_floor(self) -> None:
        # 9.3.A intent: IMPORT_DECLINE events must never benefit from
        # the high-severity (>=0.80) confidence floor in
        # compute_effective_confidence (which floors at 0.60).  The
        # configured constant must be strictly less than 0.60 for that
        # to hold.
        assert _IMPORT_DECLINE_CONFIDENCE_SCORE < 0.60

    def test_import_confidence_above_coverage_warning_floor(self) -> None:
        # Sanity: the inherent IMPORT_DECLINE confidence should be at
        # least as high as the coverage-warning floor.  If someone
        # bumps _IMPORT_DECLINE_CONFIDENCE_SCORE below the coverage
        # floor, IMPORT_DECLINE events would never differ between
        # coverage-good and coverage-warning states.  Whatever
        # calibration the IMPORT base value carries, it should still be
        # >= the noise-floored value.
        coverage_floor = _BASE_CONFIDENCE_SCORE * _COVERAGE_WARNING_CONFIDENCE_FACTOR
        assert _IMPORT_DECLINE_CONFIDENCE_SCORE >= coverage_floor
