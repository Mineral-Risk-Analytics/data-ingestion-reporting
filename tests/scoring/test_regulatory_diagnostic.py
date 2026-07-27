"""Unit tests for the 11.4-Reg audit:

* ``_resolve_compliance_weight`` NULL/empty fallback flipped 0.50 → 0.0
  (math change — uncurated regulations contribute 0 to obligation_score)
* ``_derive_market_regulatory_inputs`` returns a 4-tuple including
  ``sub_input_diagnostic`` with obligations/events/proximity visibility
  for the pillar's three silent score-shaping behaviours.

The diagnostic surface is the entire load-bearing visibility upgrade:
partner UI now sees how many regulations were uncurated, whether the
top-3 event truncation kicked in, whether the 40-pt obligation cap
fired, and whether any events had effective_date metadata.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.scoring.market_aggregator import (
    _derive_market_regulatory_inputs,
    _resolve_compliance_weight,
)


AS_OF = date(2026, 6, 4)


# Patch helper: get_events_for_regulations uses a PostgreSQL JSONB ``@>``
# operator that SQLite can't parse.  The Regulatory diagnostic tests that
# exercise the scope-obligation path don't need real events; we patch
# the helper to return [].  Tests that DO need events live elsewhere
# (the orchestrator-level integration tests use postgres).
def _patch_events():
    return patch(
        "app.services.scoring.market_aggregator.get_events_for_regulations",
        return_value=[],
    )


# ---------------------------------------------------------------------------
# _resolve_compliance_weight: 11.4-Reg-A math change
# ---------------------------------------------------------------------------

class TestResolveComplianceWeightDefault:
    """The NULL/empty fallback path.

    11.4-Reg-A REVERT (2026-06-06): briefly flipped to 0.0 then reverted
    after discovering all 8 partner-tier regulations have NULL
    geography_compliance_weights.  These tests lock in the reverted
    0.50 behaviour so a future re-flip can't silently zero scores
    again without a curation gate.
    """

    def test_null_jsonb_returns_half(self):
        assert _resolve_compliance_weight(None, "US") == 0.50

    def test_empty_dict_returns_half(self):
        assert _resolve_compliance_weight({}, "US") == 0.50

    def test_dict_missing_geo_and_default_returns_half(self):
        """If JSONB has SOME entries but neither the target country nor a
        DEFAULT key, the implicit fallback returns 0.50."""
        assert _resolve_compliance_weight({"CN": 1.0}, "US") == 0.50

    def test_explicit_default_in_dict_preserved(self):
        """Partner-curated DEFAULT key is honoured."""
        assert _resolve_compliance_weight({"DEFAULT": 0.30}, "US") == 0.30


class TestResolveComplianceWeightHits:
    """Curated lookups still work the same way."""

    def test_exact_iso2_match(self):
        assert _resolve_compliance_weight(
            {"CN": 1.0, "US": 0.05}, "CN",
        ) == 1.0

    def test_default_fallback(self):
        assert _resolve_compliance_weight(
            {"CN": 1.0, "DEFAULT": 0.30}, "JP",
        ) == 0.30


# ---------------------------------------------------------------------------
# sub_input_diagnostic shape stability
# ---------------------------------------------------------------------------

class TestDiagnosticShape:
    def test_top_level_keys(self, sqlite_session):
        _, _, _, _pts, diag = _derive_market_regulatory_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            as_of_date=AS_OF,
        )
        assert set(diag.keys()) == {"obligations", "events", "proximity"}

    def test_obligations_inner_keys(self, sqlite_session):
        _, _, _, _pts, diag = _derive_market_regulatory_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            as_of_date=AS_OF,
        )
        assert set(diag["obligations"].keys()) == {
            "data_backed",
            "total_count",
            "curated_weight_count",
            "default_weight_count",
            "raw_obligation_score",
            "capped_at_40",
        }

    def test_events_inner_keys(self, sqlite_session):
        _, _, _, _pts, diag = _derive_market_regulatory_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            as_of_date=AS_OF,
        )
        assert set(diag["events"].keys()) == {
            "data_backed", "total_count", "top_3_used_count",
        }

    def test_proximity_inner_keys(self, sqlite_session):
        _, _, _, _pts, diag = _derive_market_regulatory_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            as_of_date=AS_OF,
        )
        assert set(diag["proximity"].keys()) == {
            "adjustment_active", "events_with_effective_date_count",
        }


# ---------------------------------------------------------------------------
# Empty DB baseline
# ---------------------------------------------------------------------------

class TestEmptyDatabase:
    """No regulations, no events: all zero, all data_backed=False."""

    def test_no_scope_obligations(self, sqlite_session):
        impacts, obligations, prox, _pts, diag = _derive_market_regulatory_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            as_of_date=AS_OF,
        )
        assert impacts == []
        assert obligations == []
        assert prox == 1.0
        assert diag["obligations"]["total_count"] == 0
        assert diag["obligations"]["curated_weight_count"] == 0
        assert diag["obligations"]["default_weight_count"] == 0
        assert diag["obligations"]["raw_obligation_score"] == 0.0
        assert diag["obligations"]["capped_at_40"] is False
        assert diag["obligations"]["data_backed"] is False
        assert diag["events"]["data_backed"] is False
        assert diag["events"]["total_count"] == 0
        assert diag["events"]["top_3_used_count"] == 0
        assert diag["proximity"]["adjustment_active"] is False
        assert diag["proximity"]["events_with_effective_date_count"] == 0


# ---------------------------------------------------------------------------
# Realistic curated vs default scenarios
# ---------------------------------------------------------------------------

class TestObligationsCurationCount:
    """The 11.4-Reg-A partner-visible flag: how many of the scoped
    regulations had real curated weights vs the 0.0 fallback."""

    def test_curated_regulation_counts_as_curated(self, sqlite_session):
        from app.models.regulatory import (
            Regulation, RegulationMaterialScope,
        )
        from app.models.supply import Material

        m = Material(canonical_name="TestMat")
        sqlite_session.add(m)
        sqlite_session.flush()

        # Curated weights table: this regulation's per-country scope is
        # known.  Partner has actively decided what UFLPA-like rule
        # applies to CN vs US.
        reg = Regulation(
            regulation_key="TEST_REG",
            title="Test regulation",
            geography_compliance_weights={"CN": 1.0, "US": 0.05},
        )
        sqlite_session.add(reg)
        sqlite_session.flush()

        sqlite_session.add(RegulationMaterialScope(
            regulation_id=reg.id, material_id=m.id,
        ))
        sqlite_session.commit()

        with _patch_events():
            _, _, _, _pts, diag = _derive_market_regulatory_inputs(
                sqlite_session, material_id=m.id, geography_code="CN",
                as_of_date=AS_OF,
            )
        assert diag["obligations"]["total_count"] == 1
        assert diag["obligations"]["curated_weight_count"] == 1
        assert diag["obligations"]["default_weight_count"] == 0
        assert diag["obligations"]["data_backed"] is True

    def test_uncurated_regulation_counts_as_default(self, sqlite_session):
        from app.models.regulatory import (
            Regulation, RegulationMaterialScope,
        )
        from app.models.supply import Material

        m = Material(canonical_name="TestMat")
        sqlite_session.add(m)
        sqlite_session.flush()

        # NULL geography_compliance_weights: the regulation has been
        # seeded but its per-country weights haven't been curated yet.
        # Post-11.4-Reg-A this contributes 0.0 weight, NOT 0.50.
        reg = Regulation(
            regulation_key="UNCURATED_REG",
            title="Uncurated regulation",
            geography_compliance_weights=None,
        )
        sqlite_session.add(reg)
        sqlite_session.flush()

        sqlite_session.add(RegulationMaterialScope(
            regulation_id=reg.id, material_id=m.id,
        ))
        sqlite_session.commit()

        with _patch_events():
            _, obligations, _, _pts, diag = _derive_market_regulatory_inputs(
                sqlite_session, material_id=m.id, geography_code="CN",
                as_of_date=AS_OF,
            )
        # 11.4-Reg-A REVERT: uncurated NULL JSONB returns 0.50 (was
        # briefly 0.0 in the original 11.4-Reg-A flip).
        assert obligations == [("UNCURATED_REG", 0.50)]
        assert diag["obligations"]["total_count"] == 1
        assert diag["obligations"]["curated_weight_count"] == 0
        assert diag["obligations"]["default_weight_count"] == 1
        # data_backed=False because no obligation has a curated weight
        # JSONB.  The 0.50 default is a placeholder, not partner-curated.
        assert diag["obligations"]["data_backed"] is False
        # raw_obligation_score = base_pts × 0.50.  UNCURATED_REG has no
        # obligation_points set (NULL → coalesced to 0), so raw = 0.0
        # despite the 0.50 weight.
        assert diag["obligations"]["raw_obligation_score"] == 0.0


class TestObligationRawAndCapped:
    """40-point cap visibility — capped_at_40 surfaces when raw > 40."""

    def test_below_cap(self, sqlite_session):
        from app.models.regulatory import (
            Regulation, RegulationMaterialScope,
        )
        from app.models.supply import Material

        m = Material(canonical_name="TestMat")
        sqlite_session.add(m)
        sqlite_session.flush()

        # CBAM is worth 5 base points (DB-driven via obligation_points);
        # weight 1.0 → 5.0 raw.
        reg = Regulation(
            regulation_key="EU_CBAM",
            title="EU CBAM",
            geography_compliance_weights={"CN": 1.0},
            is_obligation=True,
            obligation_points=5,
        )
        sqlite_session.add(reg)
        sqlite_session.flush()

        sqlite_session.add(RegulationMaterialScope(
            regulation_id=reg.id, material_id=m.id,
        ))
        sqlite_session.commit()

        with _patch_events():
            _, _, _, _pts, diag = _derive_market_regulatory_inputs(
                sqlite_session, material_id=m.id, geography_code="CN",
                as_of_date=AS_OF,
            )
        assert diag["obligations"]["raw_obligation_score"] == 5.0
        assert diag["obligations"]["capped_at_40"] is False

    def test_above_cap(self, sqlite_session):
        """UFLPA + EU_BATTERY_REG_2023 + CRMA_2024 + IRA_DOMESTIC + EU_CSDDD
        at full weight = 25 + 20 + 15 + 15 + 10 = 85 raw points.  Cap fires."""
        from app.models.regulatory import (
            Regulation, RegulationMaterialScope,
        )
        from app.models.supply import Material

        m = Material(canonical_name="TestMat")
        sqlite_session.add(m)
        sqlite_session.flush()

        for key, pts in (
            ("UFLPA", 25), ("EU_BATTERY_REG_2023", 20), ("CRMA_2024", 15),
            ("IRA_DOMESTIC", 15), ("EU_CSDDD", 10),
        ):
            reg = Regulation(
                regulation_key=key,
                title=f"{key} test",
                geography_compliance_weights={"CN": 1.0},
                is_obligation=True,
                obligation_points=pts,
            )
            sqlite_session.add(reg)
            sqlite_session.flush()
            sqlite_session.add(RegulationMaterialScope(
                regulation_id=reg.id, material_id=m.id,
            ))
        sqlite_session.commit()

        with _patch_events():
            _, _, _, _pts, diag = _derive_market_regulatory_inputs(
                sqlite_session, material_id=m.id, geography_code="CN",
                as_of_date=AS_OF,
            )
        assert diag["obligations"]["raw_obligation_score"] == 85.0
        assert diag["obligations"]["capped_at_40"] is True
        # The scorer will cap to 40; the diagnostic exposes the raw 85.


# ---------------------------------------------------------------------------
# Region-alias weight resolution (2026-07-23)
# ---------------------------------------------------------------------------

class TestRegionAliasWeights:
    """"EU" key in geography_compliance_weights applies to member states."""

    def test_region_key_resolves_for_member(self):
        from app.services.scoring.market_aggregator import _resolve_compliance_weight
        w = {"EU": 0.0, "DEFAULT": 0.3}
        assert _resolve_compliance_weight(w, "FI") == 0.0   # CBAM intra-EU exempt
        assert _resolve_compliance_weight(w, "DE") == 0.0
        assert _resolve_compliance_weight(w, "CN") == 0.3   # non-member → DEFAULT

    def test_exact_country_beats_region(self):
        from app.services.scoring.market_aggregator import _resolve_compliance_weight
        w = {"EU": 0.2, "FR": 0.9, "DEFAULT": 0.5}
        assert _resolve_compliance_weight(w, "FR") == 0.9
        assert _resolve_compliance_weight(w, "DE") == 0.2

    def test_region_absent_falls_through(self):
        from app.services.scoring.market_aggregator import _resolve_compliance_weight
        assert _resolve_compliance_weight({"DEFAULT": 0.4}, "FI") == 0.4
        assert _resolve_compliance_weight(None, "FI") == 0.50

    def test_region_membership_is_complete_eu27(self):
        from app.constants import REGION_MEMBERS
        assert len(REGION_MEMBERS["EU"]) == 27
        assert "GB" not in REGION_MEMBERS["EU"]   # Brexit


# ---------------------------------------------------------------------------
# applies_all_materials gate (migration 064, 2026-07-23)
# ---------------------------------------------------------------------------

class TestAllMaterialsGate:
    def _material(self, sqlite_session, name="GateMat"):
        from app.models.supply import Material
        m = Material(canonical_name=name)
        sqlite_session.add(m)
        sqlite_session.flush()
        return m

    def test_all_goods_reg_enters_unlisted_material(self, sqlite_session):
        """UFLPA-shaped rule: no material scope rows, flag set -> scores."""
        from app.models.regulatory import Regulation
        m = self._material(sqlite_session)
        sqlite_session.add(Regulation(
            regulation_key="ALL_GOODS_REG", title="All goods",
            is_obligation=True, obligation_points=25,
            applies_all_materials=True,
            geography_compliance_weights={"CN": 1.0, "DEFAULT": 0.0},
        ))
        sqlite_session.commit()
        with _patch_events():
            _, obligations, _, _pts, diag = _derive_market_regulatory_inputs(
                sqlite_session, material_id=m.id, geography_code="CN",
                as_of_date=AS_OF,
            )
        assert obligations == [("ALL_GOODS_REG", 1.0)]
        assert diag["obligations"]["raw_obligation_score"] == 25.0

    def test_geo_scope_alone_no_longer_gates(self, sqlite_session):
        """CRMA-shaped leak: targeted_country row on a material-scoped reg
        must NOT pull it into an unlisted material's scoring."""
        from app.models.regulatory import Regulation, RegulationGeographyScope
        m = self._material(sqlite_session)
        reg = Regulation(
            regulation_key="SCOPED_REG", title="Material-scoped",
            is_obligation=True, obligation_points=15,
            geography_compliance_weights={"CN": 1.0},
        )
        sqlite_session.add(reg)
        sqlite_session.flush()
        sqlite_session.add(RegulationGeographyScope(
            regulation_id=reg.id, country_code="CN",
            scope_type="targeted_country",
        ))
        sqlite_session.commit()
        with _patch_events():
            _, obligations, _, _pts, diag = _derive_market_regulatory_inputs(
                sqlite_session, material_id=m.id, geography_code="CN",
                as_of_date=AS_OF,
            )
        assert obligations == []   # pre-064 this leaked in via the geo row
        assert diag["obligations"]["total_count"] == 0
