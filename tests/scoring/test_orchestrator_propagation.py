"""Orchestrator-level integration tests for the sixth (propagation) pillar.

These tests run the full ``rescore_company`` pipeline against an in-memory
SQLite DB with mocked event-fetch helpers. The goal is to verify that:

* When supplier-chain BFS returns nothing, the propagation pillar score is
  ``None`` and ``aggregate_supplier_risk`` re-normalises the remaining five
  pillar weights so ``overall_risk_score`` is unaffected.
* When suppliers have persisted scores, the propagation pillar IS computed
  and feeds into the overall score.
* ``rationale_json.propagation_chain`` and ``signals_used.supplier_chain_size``
  reflect what the BFS actually visited.
* The persisted ``CompanyScore.propagation_depth_used`` matches the deepest
  tier with a usable supplier score.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.db.base import Base
from app.models.company import Company, CompanyScore, CompanySupplyRelationship
from app.services.scoring.orchestrator import rescore_company


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _patch_sqlite_jsonb() -> None:
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler  # type: ignore[import]

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]


@pytest.fixture()
def session() -> Session:
    _patch_sqlite_jsonb()
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    Session_ = sessionmaker(bind=engine)
    s = Session_()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _make_company(s: Session, name: str) -> Company:
    c = Company(canonical_name=name, headquarters_country="US")
    s.add(c); s.flush()
    return c


def _evidence_mocks() -> dict:
    return {
        "get_company_material_exposure":      lambda db, cid, **kw: [],
        "get_events_for_company":             lambda db, cid, cat, aod, **kw: [],
        "get_filing_signals":                 lambda db, cid, **kw: [],
        "get_active_compliance_obligations":  lambda db, cid, **kw: [],
        "get_facilities_for_company":         lambda db, cid, **kw: [],
        "get_events_for_geographies":         lambda db, ccs, cat, aod, **kw: [],
        "get_events_for_materials":           lambda db, mids, cat, aod, **kw: [],
        "get_regulations_scoping_company":    lambda db, cid, mids, ccs, **kw: [],
        "get_events_for_regulations":         lambda db, rkeys, aod, **kw: [],
        "get_chemistry_mix_for_company":      lambda db, cid, aod=None, **kw: None,
        "get_chemistry_material_intensities": lambda db, cids, aod=None, **kw: {},
        "get_latest_chemistry_risk_scores":   lambda db, cids, **kw: {},
        "get_chemistry_slugs":                lambda db, cids: {},
        # supplier_chain / latest_scores intentionally NOT mocked —
        # tests use real DB rows so they exercise the BFS + lookup.
    }


# ---------------------------------------------------------------------------
# No-supplier baseline (regression guard for the renormalisation path)
# ---------------------------------------------------------------------------

class TestNoSupplierBaseline:
    def test_no_suppliers_propagation_is_none(self, session: Session):
        c = _make_company(session, "OrphanCo")
        with patch.multiple("app.services.scoring.orchestrator", **_evidence_mocks()):
            row = rescore_company(session, c.id, run_id="orphan")

        assert row.supply_chain_propagation_score is None
        assert row.propagation_depth_used is None
        assert row.rationale_json["components"]["supply_chain_propagation"] is None
        assert row.rationale_json["signals_used"]["supplier_chain_size"] == 0
        assert row.rationale_json["signals_used"]["supplier_scores_used"] == 0
        # propagation_chain block must be empty (or absent), not a stale array.
        prop = row.rationale_json.get("propagation_chain")
        assert not prop  # falsy: None, [], or {} all OK

    def test_overall_score_matches_renormalised_five_pillar(self, session: Session):
        """When propagation is None, overall must equal the renormalised
        five-pillar aggregate. We don't compute the literal expected value
        here — we just verify the score is on [0, 100] and stable across two
        runs (deterministic given identical inputs)."""
        c = _make_company(session, "DeterminismCo")
        with patch.multiple("app.services.scoring.orchestrator", **_evidence_mocks()):
            row1 = rescore_company(session, c.id, run_id="r1")
            row2 = rescore_company(session, c.id, run_id="r2")
        assert row1.overall_risk_score == pytest.approx(row2.overall_risk_score)
        assert 0.0 <= row1.overall_risk_score <= 100.0


# ---------------------------------------------------------------------------
# Tier-1 supplier with persisted score
# ---------------------------------------------------------------------------

class TestTier1Propagation:
    def test_single_tier1_supplier_score_propagates(self, session: Session):
        buyer = _make_company(session, "BuyerCo")
        sup = _make_company(session, "SupCo")
        session.add(
            CompanySupplyRelationship(
                buyer_id=buyer.id,
                supplier_id=sup.id,
                volume_share_pct=1.0,
            )
        )
        # Persist a CompanyScore for the supplier so the propagation pillar
        # has something to consume.
        session.add(
            CompanyScore(
                company_id=sup.id,
                as_of_date=date(2025, 6, 1),
                overall_risk_score=80.0,
                scoring_version="3.0",
            )
        )
        session.flush()

        with patch.multiple("app.services.scoring.orchestrator", **_evidence_mocks()):
            row = rescore_company(session, buyer.id, run_id="tier1")

        assert row.supply_chain_propagation_score is not None
        # Single tier-1 supplier with full volume share → its score IS the
        # propagation score.
        assert row.supply_chain_propagation_score == pytest.approx(80.0)
        assert row.propagation_depth_used == 1
        assert row.rationale_json["signals_used"]["supplier_chain_size"] == 1
        assert row.rationale_json["signals_used"]["supplier_scores_used"] == 1

    def test_supplier_without_persisted_score_is_skipped(self, session: Session):
        """Supplier exists in the chain but has no CompanyScore row yet:
        ``signals_used.supplier_chain_size == 1`` (BFS visited),
        ``signals_used.supplier_scores_used == 0`` (none usable),
        propagation pillar collapses to ``None``."""
        buyer = _make_company(session, "BuyerCo")
        sup = _make_company(session, "NewSupCo")
        session.add(
            CompanySupplyRelationship(
                buyer_id=buyer.id, supplier_id=sup.id, volume_share_pct=1.0
            )
        )
        session.flush()

        with patch.multiple("app.services.scoring.orchestrator", **_evidence_mocks()):
            row = rescore_company(session, buyer.id, run_id="orphan-sup")

        assert row.rationale_json["signals_used"]["supplier_chain_size"] == 1
        assert row.rationale_json["signals_used"]["supplier_scores_used"] == 0
        assert row.supply_chain_propagation_score is None


# ---------------------------------------------------------------------------
# Tier-2 propagation
# ---------------------------------------------------------------------------

class TestTier2Propagation:
    def test_tier2_supplier_records_depth_2(self, session: Session):
        buyer = _make_company(session, "OEM")
        tier1 = _make_company(session, "Tier1")
        tier2 = _make_company(session, "Tier2")
        session.add_all([
            CompanySupplyRelationship(
                buyer_id=buyer.id, supplier_id=tier1.id, volume_share_pct=1.0
            ),
            CompanySupplyRelationship(
                buyer_id=tier1.id, supplier_id=tier2.id, volume_share_pct=1.0
            ),
            # Persist scores for BOTH tiers so propagation_depth_used == 2.
            CompanyScore(
                company_id=tier1.id,
                as_of_date=date(2025, 6, 1),
                overall_risk_score=50.0,
                scoring_version="3.0",
            ),
            CompanyScore(
                company_id=tier2.id,
                as_of_date=date(2025, 6, 1),
                overall_risk_score=90.0,
                scoring_version="3.0",
            ),
        ])
        session.flush()

        with patch.multiple("app.services.scoring.orchestrator", **_evidence_mocks()):
            row = rescore_company(
                session, buyer.id, run_id="t2", propagation_max_depth=2
            )

        assert row.propagation_depth_used == 2
        assert row.rationale_json["signals_used"]["supplier_chain_size"] == 2
        assert row.rationale_json["signals_used"]["supplier_scores_used"] == 2
        # Tier-1 weight 1.0, tier-2 weight 0.4 (DEFAULT_DEPTH_WEIGHTS):
        # propagation = (50*1.0 + 90*0.4) / (1.0 + 0.4) = 86/1.4 ≈ 61.43
        assert row.supply_chain_propagation_score == pytest.approx(
            (50 * 1.0 + 90 * 0.4) / (1.0 + 0.4)
        )

    def test_max_depth_cap_truncates_propagation(self, session: Session):
        """Setting ``propagation_max_depth=1`` excludes tier-2 entirely."""
        buyer = _make_company(session, "OEM")
        tier1 = _make_company(session, "Tier1")
        tier2 = _make_company(session, "Tier2")
        session.add_all([
            CompanySupplyRelationship(
                buyer_id=buyer.id, supplier_id=tier1.id, volume_share_pct=1.0
            ),
            CompanySupplyRelationship(
                buyer_id=tier1.id, supplier_id=tier2.id, volume_share_pct=1.0
            ),
            CompanyScore(
                company_id=tier1.id,
                as_of_date=date(2025, 6, 1),
                overall_risk_score=50.0,
                scoring_version="3.0",
            ),
            CompanyScore(
                company_id=tier2.id,
                as_of_date=date(2025, 6, 1),
                overall_risk_score=90.0,
                scoring_version="3.0",
            ),
        ])
        session.flush()

        with patch.multiple("app.services.scoring.orchestrator", **_evidence_mocks()):
            row = rescore_company(
                session, buyer.id, run_id="t1cap", propagation_max_depth=1
            )

        # Only tier-1 is considered → propagation == tier-1 score.
        assert row.propagation_depth_used == 1
        assert row.supply_chain_propagation_score == pytest.approx(50.0)
        assert row.rationale_json["signals_used"]["supplier_chain_size"] == 1
