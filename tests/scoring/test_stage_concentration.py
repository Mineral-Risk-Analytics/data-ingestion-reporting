"""V1 concentration pillar acceptance tests (spec §3 + §7b).

The cobalt fixture mirrors the live DB share tables as of 2026-07-17 —
the hand-verified acceptance numbers from the V1 spec review:

    CD 83.23 (driving: intermediate), CN 84.96 (refined; battery-grade
    stale-excluded), ID 36.21 (ore), FI 25.72 (refined), RU 15.15 (ore).

If these move, either the share data changed (fine — update the fixture
from the DB) or the formula changed (not fine without a spec revision).
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.scoring.stage_concentration import (
    GOVERNANCE_AMPLIFIER_BETA,
    apply_governance_amplifier,
    FRESHNESS_YEARS,
    ShareRow,
    compute_stage_concentration,
)

AS_OF = date(2026, 7, 17)


def _cobalt_rows() -> list[ShareRow]:
    """Cobalt share tables incl. real parent/child duplication (2605+260500)."""
    ore_6, ore_4 = 101, 102          # 260500 / 2605
    int_6, int_4 = 201, 202          # 282200 / 2822
    ref_6 = 301                      # 810520
    bat_6 = 401                      # 283329

    ore = {
        "CD": 0.7529, "ID": 0.1440, "RU": 0.0252, "MG": 0.0128,
        "AU": 0.0121, "PH": 0.0121, "CA": 0.0115, "PG": 0.0092,
        "CU": 0.0065, "CN": 0.0065, "TR": 0.0062, "US": 0.0010,
    }
    intermediate = {"CD": 0.76, "ID": 0.12}
    refined = {
        "CN": 0.786, "FI": 0.072, "CA": 0.027, "JP": 0.019,
        "MG": 0.018, "ID": 0.017, "NO": 0.014, "AU": 0.014,
    }

    rows: list[ShareRow] = []
    for cc, s in ore.items():  # duplicated across 6- and 4-digit mappings
        rows.append(ShareRow("ore", cc, s, 2026, 6, ore_6))
        rows.append(ShareRow("ore", cc, s, 2026, 4, ore_4))
    for cc, s in intermediate.items():
        rows.append(ShareRow("intermediate", cc, s, 2024, 6, int_6))
        rows.append(ShareRow("intermediate", cc, s, 2024, 4, int_4))
    for cc, s in refined.items():
        rows.append(ShareRow("refined", cc, s, 2024, 6, ref_6))
    # Battery-grade: single CN row, 2022 -> stale at 2026 as-of (§7b).
    rows.append(ShareRow("battery_grade", "CN", 0.85, 2022, 6, bat_6))
    return rows


@pytest.fixture(scope="module")
def result():
    return compute_stage_concentration(_cobalt_rows(), AS_OF)


class TestCobaltAcceptance:
    """Pinned to the hand-computed V1 spec-review table."""

    def test_stage_hhis(self, result):
        assert result.stages["ore"].hhi_raw == pytest.approx(0.589027, abs=1e-5)
        assert result.stages["intermediate"].hhi_raw == pytest.approx(0.5920, abs=1e-4)
        assert result.stages["refined"].hhi_raw == pytest.approx(0.625075, abs=1e-5)
        for stage in ("ore", "intermediate", "refined"):
            assert result.stages[stage].hhi_cliff > 0.95  # all "extreme" tier

    def test_cd_driven_by_intermediate(self, result):
        cd = result.per_geo["CD"]
        assert cd.score == pytest.approx(83.23, abs=0.02)
        assert cd.driving_stage == "intermediate"
        assert cd.sub_scores["ore"] == pytest.approx(82.81, abs=0.02)

    def test_cn_driven_by_refined_not_stale_battery(self, result):
        cn = result.per_geo["CN"]
        assert cn.score == pytest.approx(84.96, abs=0.02)
        assert cn.driving_stage == "refined"
        assert "battery_grade" not in cn.sub_scores       # §7b exclusion
        assert cn.sub_scores["ore"] == pytest.approx(7.69, abs=0.02)

    def test_battery_grade_reported_stale(self, result):
        assert ("battery_grade", 2022) in result.stale_stages
        assert "battery_grade" not in result.stages

    def test_mid_tier_producers(self, result):
        assert result.per_geo["ID"].score == pytest.approx(36.21, abs=0.02)
        assert result.per_geo["ID"].driving_stage == "ore"
        assert result.per_geo["FI"].score == pytest.approx(25.72, abs=0.02)
        assert result.per_geo["FI"].driving_stage == "refined"
        assert result.per_geo["RU"].score == pytest.approx(15.15, abs=0.02)

    def test_refined_beats_ore_for_dual_stage_producers(self, result):
        # CA: ore 1.15% vs refined 2.7% -> refined drives.
        ca = result.per_geo["CA"]
        assert ca.driving_stage == "refined"
        assert ca.score == pytest.approx(15.75, abs=0.02)

    def test_non_producers_absent(self, result):
        for geo in ("DE", "BY", "SY", "AT"):
            assert geo not in result.per_geo

    def test_parent_child_deduped(self, result):
        ore = result.stages["ore"]
        assert len(ore.shares) == 12          # countries once, not twice
        assert ore.source_mapping_ids == [101]  # 6-digit preferred
        assert ore.conflicts == []


class TestPolicyEdges:
    def test_freshness_boundary(self):
        # Exactly FRESHNESS_YEARS old -> fresh; one year older -> stale.
        fresh = ShareRow("ore", "CD", 0.75, AS_OF.year - FRESHNESS_YEARS, 6, 1)
        stale = ShareRow("refined", "CN", 0.78, AS_OF.year - FRESHNESS_YEARS - 1, 6, 2)
        result = compute_stage_concentration([fresh, stale], AS_OF)
        assert "ore" in result.stages
        assert ("refined", AS_OF.year - FRESHNESS_YEARS - 1) in result.stale_stages

    def test_single_year_snapshot_per_stage(self):
        # An older vintage must not blend into the latest year's HHI.
        rows = [
            ShareRow("ore", "CD", 0.75, 2026, 6, 1),
            ShareRow("ore", "ID", 0.14, 2026, 6, 1),
            ShareRow("ore", "ZM", 0.30, 2025, 6, 1),   # stale vintage row
        ]
        result = compute_stage_concentration(rows, AS_OF)
        assert set(result.stages["ore"].shares) == {"CD", "ID"}
        assert "ZM" not in result.per_geo

    def test_equal_specificity_conflict_keeps_max_and_flags(self):
        rows = [
            ShareRow("ore", "CD", 0.70, 2026, 6, 1),
            ShareRow("ore", "CD", 0.75, 2026, 6, 2),
        ]
        result = compute_stage_concentration(rows, AS_OF)
        assert result.stages["ore"].shares["CD"] == 0.75
        assert len(result.stages["ore"].conflicts) == 1

    def test_zero_and_negative_shares_ignored(self):
        rows = [
            ShareRow("ore", "CD", 0.75, 2026, 6, 1),
            ShareRow("ore", "XX", 0.0, 2026, 6, 1),
        ]
        result = compute_stage_concentration(rows, AS_OF)
        assert "XX" not in result.per_geo

    def test_excluded_stages_ignored(self):
        rows = [
            ShareRow("ore", "CD", 0.75, 2026, 6, 1),
            ShareRow("fabricated", "CN", 0.90, 2026, 6, 9),
        ]
        result = compute_stage_concentration(rows, AS_OF)
        assert "fabricated" not in result.stages
        assert "CN" not in result.per_geo

    def test_empty_input(self):
        result = compute_stage_concentration([], AS_OF)
        assert result.stages == {} and result.per_geo == {}


# ---------------------------------------------------------------------------
# 4.1 governance amplifier — "75% in DRC is worse than 75% in Australia"
# ---------------------------------------------------------------------------

class TestGovernanceAmplifier:
    def test_no_wgi_is_a_pure_noop(self):
        # Without wgi_by_geo the 4.0 acceptance values must be untouched.
        rows = [ShareRow("ore", "CD", 0.7529, 2026, 6, 1)]
        base = compute_stage_concentration(rows, AS_OF)
        with_none = compute_stage_concentration(rows, AS_OF, wgi_by_geo=None)
        assert with_none.per_geo["CD"].score == base.per_geo["CD"].score
        assert with_none.per_geo["CD"].governance is None
        assert with_none.per_geo["CD"].raw_score == base.per_geo["CD"].score

    def test_unstable_geo_amplified_stable_geo_lightly(self):
        rows = [
            ShareRow("ore", "CD", 0.75, 2026, 6, 1),
            ShareRow("ore", "AU", 0.75, 2026, 6, 1),
        ]
        # identical share -> identical raw score; only governance differs
        res = compute_stage_concentration(
            rows, AS_OF, wgi_by_geo={"CD": 27.4, "AU": 83.2}
        )
        cd, au = res.per_geo["CD"], res.per_geo["AU"]
        assert cd.raw_score == pytest.approx(au.raw_score)
        assert cd.score > au.score              # DRC now scores HIGHER
        assert cd.score > cd.raw_score          # amplified upward
        assert au.score > au.raw_score          # mildly (inst 0.168)
        assert cd.governance["applied"] and cd.governance["beta"] == GOVERNANCE_AMPLIFIER_BETA

    def test_geo_missing_from_wgi_map_not_amplified(self):
        rows = [ShareRow("ore", "CD", 0.75, 2026, 6, 1)]
        res = compute_stage_concentration(rows, AS_OF, wgi_by_geo={"XX": 30.0})
        assert res.per_geo["CD"].governance is None
        assert res.per_geo["CD"].score == res.per_geo["CD"].raw_score

    def test_sub_scores_stay_raw(self):
        rows = [ShareRow("ore", "CD", 0.75, 2026, 6, 1)]
        res = compute_stage_concentration(rows, AS_OF, wgi_by_geo={"CD": 27.4})
        raw_sub = res.per_geo["CD"].sub_scores["ore"]
        assert raw_sub == pytest.approx(res.per_geo["CD"].raw_score)
        assert res.per_geo["CD"].score > raw_sub

    # pure-function behaviour ------------------------------------------------
    def test_amplifier_bounds_and_monotonicity(self):
        f = apply_governance_amplifier
        assert f(0.5, 0.0) == 0.5 and f(0.5, None) == 0.5      # stable/unknown no-op
        assert f(0.0, 0.9) == 0.0                              # zero stays zero
        assert f(1.0, 0.9) == 1.0                              # ceiling exact
        assert f(0.99, 0.7) < 1.0                              # no hard pin near top
        assert f(0.83, 0.73) > f(0.83, 0.48) > f(0.83, 0.10)   # monotonic in instability
        assert f(0.9, 0.5) > f(0.8, 0.5) > f(0.5, 0.5)         # monotonic in score

    def test_amplifier_matches_linear_form_at_low_scores(self):
        # For small p the soft-ceiling reduces to the EU linear p*(1+beta*inst).
        p, inst = 0.10, 0.70
        linear = p * (1 + GOVERNANCE_AMPLIFIER_BETA * inst)
        assert apply_governance_amplifier(p, inst) == pytest.approx(linear, rel=0.02)

    def test_cobalt_acceptance_values_4_1(self):
        # Pinned 4.1 acceptance: CD (raw 83.23, WGI 27.4) amplifies to ~90.2
        # and OVERTAKES CN (raw 84.96, WGI 51.7 -> ~90.0) as driving geo.
        cd = apply_governance_amplifier(0.8323, 1 - 0.274) * 100
        cn = apply_governance_amplifier(0.8496, 1 - 0.517) * 100
        assert cd == pytest.approx(90.23, abs=0.05)
        assert cn == pytest.approx(90.01, abs=0.05)
        assert cd > cn
