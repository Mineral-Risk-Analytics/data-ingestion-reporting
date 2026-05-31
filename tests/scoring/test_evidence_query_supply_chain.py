"""Tests for the supply-chain rollup helpers in evidence_query.py.

The event-fetch helpers ``get_events_for_*`` use ``JSONB.contains([category])``
which SQLite cannot compile (no ``@>`` operator), so those queries are tested
only at the orchestrator level via mocks. The helpers exercised here are pure
SQL on relational tables (``CompanySupplyRelationship``, ``Facility``,
``CompanyScore``, ``Regulation*Scope``) and run cleanly on in-memory SQLite.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.models.company import (
    Company,
    CompanyMaterialExposure,
    CompanyScore,
    CompanySupplyRelationship,
)
from app.models.facility import CompanyFacility, Facility
from app.models.regulatory import (
    CompanyRegulationExposure,
    Regulation,
    RegulationGeographyScope,
    RegulationMaterialScope,
)
from app.models.supply import Material
from app.services.scoring.evidence_query import (
    SupplierEdge,
    get_facilities_for_company,
    get_latest_company_scores,
    get_regulations_scoping_company,
    get_supplier_chain,
)
from app.services.scoring.types import ScoringScope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_company(session, name: str) -> Company:
    c = Company(canonical_name=name, headquarters_country="US")
    session.add(c)
    session.flush()
    return c


def _make_edge(
    session,
    buyer: Company,
    supplier: Company,
    *,
    volume_share: float | None = None,
    material_id: int | None = None,
) -> CompanySupplyRelationship:
    rel = CompanySupplyRelationship(
        buyer_id=buyer.id,
        supplier_id=supplier.id,
        material_id=material_id,
        volume_share_pct=volume_share,
    )
    session.add(rel)
    session.flush()
    return rel


# ---------------------------------------------------------------------------
# get_supplier_chain
# ---------------------------------------------------------------------------

class TestGetSupplierChain:
    def test_empty_when_no_edges(self, sqlite_session):
        root = _make_company(sqlite_session, "Root")
        assert get_supplier_chain(sqlite_session, root.id) == []

    def test_single_tier_returns_direct_suppliers(self, sqlite_session):
        root = _make_company(sqlite_session, "Buyer")
        sup = _make_company(sqlite_session, "Supplier")
        _make_edge(sqlite_session, root, sup, volume_share=0.4)

        chain = get_supplier_chain(sqlite_session, root.id, max_depth=1)
        assert len(chain) == 1
        edge = chain[0]
        assert edge.supplier_id == sup.id
        assert edge.depth == 1
        assert edge.cumulative_volume_share == pytest.approx(0.4)

    def test_two_tiers_carry_cumulative_share(self, sqlite_session):
        root = _make_company(sqlite_session, "Buyer")
        tier1 = _make_company(sqlite_session, "Tier1")
        tier2 = _make_company(sqlite_session, "Tier2")
        _make_edge(sqlite_session, root, tier1, volume_share=0.5)
        _make_edge(sqlite_session, tier1, tier2, volume_share=0.6)

        chain = get_supplier_chain(sqlite_session, root.id, max_depth=2)
        edges_by_id = {e.supplier_id: e for e in chain}
        assert tier1.id in edges_by_id and tier2.id in edges_by_id
        assert edges_by_id[tier2.id].depth == 2
        assert edges_by_id[tier2.id].cumulative_volume_share == pytest.approx(0.3)

    def test_depth_cap_truncates_walk(self, sqlite_session):
        root = _make_company(sqlite_session, "Buyer")
        t1 = _make_company(sqlite_session, "T1")
        t2 = _make_company(sqlite_session, "T2")
        _make_edge(sqlite_session, root, t1, volume_share=1.0)
        _make_edge(sqlite_session, t1, t2, volume_share=1.0)

        chain1 = get_supplier_chain(sqlite_session, root.id, max_depth=1)
        chain2 = get_supplier_chain(sqlite_session, root.id, max_depth=2)
        assert {e.supplier_id for e in chain1} == {t1.id}
        assert {e.supplier_id for e in chain2} == {t1.id, t2.id}

    def test_cycle_is_skipped(self, sqlite_session):
        a = _make_company(sqlite_session, "A")
        b = _make_company(sqlite_session, "B")
        # A -> B -> A would loop forever without cycle guard
        _make_edge(sqlite_session, a, b, volume_share=1.0)
        _make_edge(sqlite_session, b, a, volume_share=1.0)

        chain = get_supplier_chain(sqlite_session, a.id, max_depth=5)
        # Root (A) is never re-emitted as its own supplier.
        assert all(e.supplier_id != a.id for e in chain)
        assert {e.supplier_id for e in chain} == {b.id}

    def test_unknown_volume_propagates_as_none(self, sqlite_session):
        root = _make_company(sqlite_session, "Buyer")
        sup = _make_company(sqlite_session, "Supplier")
        _make_edge(sqlite_session, root, sup, volume_share=None)

        chain = get_supplier_chain(sqlite_session, root.id)
        assert chain[0].cumulative_volume_share is None

    def test_max_visited_caps_BFS(self, sqlite_session):
        root = _make_company(sqlite_session, "Buyer")
        for i in range(10):
            sup = _make_company(sqlite_session, f"Sup{i}")
            _make_edge(sqlite_session, root, sup, volume_share=0.1)

        chain = get_supplier_chain(
            sqlite_session, root.id, max_depth=2, max_visited=5
        )
        assert len(chain) <= 5

    def test_scope_supplier_depth_max_clamps(self, sqlite_session):
        root = _make_company(sqlite_session, "Buyer")
        t1 = _make_company(sqlite_session, "T1")
        t2 = _make_company(sqlite_session, "T2")
        _make_edge(sqlite_session, root, t1, volume_share=1.0)
        _make_edge(sqlite_session, t1, t2, volume_share=1.0)

        chain = get_supplier_chain(
            sqlite_session,
            root.id,
            max_depth=3,
            scope=ScoringScope(supplier_depth_max=1),
        )
        assert {e.supplier_id for e in chain} == {t1.id}


# ---------------------------------------------------------------------------
# get_latest_company_scores
# ---------------------------------------------------------------------------

class TestGetLatestCompanyScores:
    def test_returns_most_recent_per_company(self, sqlite_session):
        c = _make_company(sqlite_session, "X")
        sqlite_session.add_all(
            [
                CompanyScore(
                    company_id=c.id,
                    as_of_date=date(2024, 1, 1),
                    overall_risk_score=20.0,
                    scoring_version="2.0",
                ),
                CompanyScore(
                    company_id=c.id,
                    as_of_date=date(2025, 6, 1),
                    overall_risk_score=70.0,
                    scoring_version="3.0",
                ),
            ]
        )
        sqlite_session.flush()

        out = get_latest_company_scores(sqlite_session, [c.id])
        assert out[c.id].overall_risk_score == 70.0

    def test_empty_input(self, sqlite_session):
        assert get_latest_company_scores(sqlite_session, []) == {}


# ---------------------------------------------------------------------------
# get_facilities_for_company
# ---------------------------------------------------------------------------

class TestGetFacilitiesForCompany:
    def test_returns_facilities_owned_by_company(self, sqlite_session):
        c = _make_company(sqlite_session, "FacCo")
        f1 = Facility(facility_type="mine", country="CN")
        f2 = Facility(facility_type="refinery", country="US")
        sqlite_session.add_all([f1, f2])
        sqlite_session.flush()
        sqlite_session.add_all(
            [
                CompanyFacility(company_id=c.id, facility_id=f1.id),
                CompanyFacility(company_id=c.id, facility_id=f2.id),
            ]
        )
        sqlite_session.flush()

        facs = get_facilities_for_company(sqlite_session, c.id)
        assert {f.country for f in facs} == {"CN", "US"}

    def test_scope_country_filter(self, sqlite_session):
        c = _make_company(sqlite_session, "FacCo")
        f_cn = Facility(facility_type="mine", country="CN")
        f_us = Facility(facility_type="refinery", country="US")
        sqlite_session.add_all([f_cn, f_us])
        sqlite_session.flush()
        sqlite_session.add_all(
            [
                CompanyFacility(company_id=c.id, facility_id=f_cn.id),
                CompanyFacility(company_id=c.id, facility_id=f_us.id),
            ]
        )
        sqlite_session.flush()

        facs = get_facilities_for_company(
            sqlite_session, c.id, scope=ScoringScope(country_codes=frozenset({"CN"}))
        )
        assert {f.country for f in facs} == {"CN"}


# ---------------------------------------------------------------------------
# get_regulations_scoping_company (UNION semantics — no event JSONB needed)
# ---------------------------------------------------------------------------

class TestGetRegulationsScopingCompany:
    def _seed(self, session):
        c = _make_company(session, "OEM")
        m_li = Material(canonical_name="Lithium-test", category="cathode_active")
        m_co = Material(canonical_name="Cobalt-test", category="cathode_active")
        session.add_all([m_li, m_co])
        session.flush()

        # Reg 1: structured exposure (status weighting)
        r1 = Regulation(
            regulation_key="UFLPA-test", title="UFLPA", verified=True,
        )
        # Reg 2: material-scope only
        r2 = Regulation(
            regulation_key="LI-RULE", title="Lithium rule", verified=True,
        )
        # Reg 3: geography-scope only
        r3 = Regulation(
            regulation_key="CN-RULE", title="China origin rule", verified=True,
        )
        session.add_all([r1, r2, r3])
        session.flush()

        session.add(
            CompanyRegulationExposure(
                company_id=c.id,
                regulation_id=r1.id,
                compliance_status="non_compliant",
            )
        )
        session.add(RegulationMaterialScope(regulation_id=r2.id, material_id=m_li.id))
        session.add(
            RegulationGeographyScope(regulation_id=r3.id, country_code="CN")
        )
        session.flush()
        return c, m_li, m_co

    def test_union_of_three_sources(self, sqlite_session):
        c, m_li, _ = self._seed(sqlite_session)
        out = dict(
            get_regulations_scoping_company(
                sqlite_session,
                c.id,
                material_ids={m_li.id},
                country_codes={"CN"},
            )
        )
        assert out["UFLPA-test"] == 1.0    # non_compliant
        assert out["LI-RULE"] == 0.4       # material-scope ``covered`` → _SCOPE_TYPE_WEIGHT
        assert out["CN-RULE"] == 0.5       # geography-scope only

    def test_dedup_keeps_max_weight(self, sqlite_session):
        """A reg that scopes a material AND has a non_compliant exposure must
        keep the higher weight (1.0), not the scope-derived 0.5."""
        c = _make_company(sqlite_session, "OEM2")
        m = Material(canonical_name="Nickel-test")
        sqlite_session.add(m)
        sqlite_session.flush()
        r = Regulation(regulation_key="NI-RULE", title="Nickel rule", verified=True)
        sqlite_session.add(r)
        sqlite_session.flush()
        sqlite_session.add(
            CompanyRegulationExposure(
                company_id=c.id,
                regulation_id=r.id,
                compliance_status="non_compliant",
            )
        )
        sqlite_session.add(
            RegulationMaterialScope(regulation_id=r.id, material_id=m.id)
        )
        sqlite_session.flush()

        out = dict(
            get_regulations_scoping_company(
                sqlite_session, c.id, material_ids={m.id}, country_codes=set()
            )
        )
        assert out == {"NI-RULE": 1.0}

    def test_scope_regulation_keys_filters_results(self, sqlite_session):
        c, m_li, _ = self._seed(sqlite_session)
        # Include UFLPA-test in scope so the fallback text-scan path (which
        # uses JSONB.contains and is unsupported by SQLite) is not exercised.
        out = dict(
            get_regulations_scoping_company(
                sqlite_session,
                c.id,
                material_ids={m_li.id},
                country_codes={"CN"},
                scope=ScoringScope(
                    regulation_keys=frozenset({"LI-RULE", "UFLPA-test"})
                ),
            )
        )
        assert set(out.keys()) == {"LI-RULE", "UFLPA-test"}
        assert "CN-RULE" not in out
