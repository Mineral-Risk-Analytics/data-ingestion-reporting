"""Tests for ``ScoringScope`` and the ``score_company_scoped`` entry point.

Two layers under test:

1. ``ScoringScope`` dataclass invariants (frozen, default-all, ``is_all`` flag).
2. ``score_company_scoped`` orchestration contract:
   * scoped runs NEVER persist a ``CompanyScore`` (regardless of caller intent),
   * scoped runs SKIP supply-chain propagation in v1
     (``signals_used.propagation_skipped_due_to_scope == True``),
   * scoped scope is threaded into every ``evidence_query`` helper as the
     ``scope=`` kwarg,
   * even when ``rescore_company`` is invoked directly with a non-ALL scope and
     ``persist=True``, the orchestrator coerces ``persist=False``.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401  (register ORMs)
from app.db.base import Base
from app.models.company import Company, CompanyScore
from app.services.scoring.orchestrator import (
    rescore_company,
    score_company_scoped,
)
from app.services.scoring.types import ScoringScope


# ---------------------------------------------------------------------------
# Local fixtures (mirror tests/test_orchestrator.py to keep this file
# self-contained — the in-memory engine is short-lived per test).
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


@pytest.fixture()
def fixture_company(session: Session) -> Company:
    c = Company(canonical_name="ScopedTest", headquarters_country="US")
    session.add(c)
    session.flush()
    return c


def _mock_evidence_no_events() -> dict:
    """Mocks for every evidence_query helper consumed by the orchestrator."""
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
        "get_supplier_chain":                 lambda db, cid, **kw: [],
        "get_latest_company_scores":          lambda db, cids, **kw: {},
        "get_chemistry_mix_for_company":      lambda db, cid, aod=None, **kw: None,
        "get_chemistry_material_intensities": lambda db, cids, aod=None, **kw: {},
        "get_latest_chemistry_risk_scores":   lambda db, cids, **kw: {},
        "get_chemistry_slugs":                lambda db, cids: {},
    }


# ---------------------------------------------------------------------------
# ScoringScope invariants
# ---------------------------------------------------------------------------

class TestScoringScopeDataclass:
    def test_default_is_all_no_op(self):
        s = ScoringScope()
        assert s.is_all() is True

    def test_ALL_singleton_is_no_op(self):
        assert ScoringScope.ALL.is_all() is True

    def test_any_filter_makes_non_all(self):
        assert not ScoringScope(material_ids=frozenset({1})).is_all()
        assert not ScoringScope(country_codes=frozenset({"CN"})).is_all()
        assert not ScoringScope(chemistry_ids=frozenset({1})).is_all()
        assert not ScoringScope(regulation_keys=frozenset({"UFLPA"})).is_all()
        assert not ScoringScope(facility_ids=frozenset({uuid.uuid4()})).is_all()
        assert not ScoringScope(supplier_depth_max=1).is_all()

    def test_frozen(self):
        s = ScoringScope(material_ids=frozenset({1}))
        with pytest.raises(Exception):  # FrozenInstanceError or similar
            s.material_ids = frozenset({2})  # type: ignore[misc]


# ---------------------------------------------------------------------------
# score_company_scoped contract
# ---------------------------------------------------------------------------

class TestScoreCompanyScoped:
    def test_scoped_run_does_not_persist(
        self, session: Session, fixture_company: Company
    ):
        scope = ScoringScope(country_codes=frozenset({"CN"}))
        with patch.multiple(
            "app.services.scoring.orchestrator", **_mock_evidence_no_events()
        ):
            row = score_company_scoped(session, fixture_company.id, scope)
        # Sanity: in-memory ORM object exists with no PK assigned.
        assert row.id is None
        # Database confirms zero rows for this company.
        rows = session.scalars(
            select(CompanyScore).where(CompanyScore.company_id == fixture_company.id)
        ).all()
        assert rows == []

    def test_scoped_run_skips_propagation(
        self, session: Session, fixture_company: Company
    ):
        scope = ScoringScope(country_codes=frozenset({"CN"}))
        with patch.multiple(
            "app.services.scoring.orchestrator", **_mock_evidence_no_events()
        ):
            row = score_company_scoped(session, fixture_company.id, scope)
        rj = row.rationale_json
        assert rj["signals_used"]["propagation_skipped_due_to_scope"] is True
        assert rj["signals_used"]["supplier_chain_size"] == 0
        assert rj["components"]["supply_chain_propagation"] is None

    def test_full_scope_via_scored_wrapper_still_skipped(
        self, session: Session, fixture_company: Company
    ):
        """Even with ``ScoringScope.ALL`` via the wrapper, ``persist=False`` is
        forced because ``score_company_scoped`` always sets it."""
        with patch.multiple(
            "app.services.scoring.orchestrator", **_mock_evidence_no_events()
        ):
            row = score_company_scoped(
                session, fixture_company.id, ScoringScope.ALL
            )
        # ALL scope means propagation IS NOT skipped (wrapper passes scope as-is)
        # but persist must still be False.
        assert row.id is None
        assert "propagation_skipped_due_to_scope" not in row.rationale_json[
            "signals_used"
        ]


class TestRescoreScopeCoercion:
    def test_non_all_scope_with_persist_true_is_coerced_off(
        self, session: Session, fixture_company: Company
    ):
        """``rescore_company(... scope=non-ALL, persist=True)`` is the foot-gun
        path; the orchestrator MUST drop persist back to False."""
        scope = ScoringScope(material_ids=frozenset({1}))
        with patch.multiple(
            "app.services.scoring.orchestrator", **_mock_evidence_no_events()
        ):
            row = rescore_company(
                session, fixture_company.id, run_id="r1", scope=scope, persist=True
            )
        assert row.id is None
        rows = session.scalars(
            select(CompanyScore).where(CompanyScore.company_id == fixture_company.id)
        ).all()
        assert rows == []

    def test_full_scope_with_persist_true_does_persist(
        self, session: Session, fixture_company: Company
    ):
        """Sanity contrast: default ``ScoringScope.ALL`` + ``persist=True`` must
        write to the DB so we know scope-coercion is the only reason scoped runs
        skip persistence."""
        with patch.multiple(
            "app.services.scoring.orchestrator", **_mock_evidence_no_events()
        ):
            row = rescore_company(session, fixture_company.id, run_id="r2")
        session.flush()
        assert row.id is not None
        rows = session.scalars(
            select(CompanyScore).where(CompanyScore.company_id == fixture_company.id)
        ).all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Scope threading: scope kwarg actually reaches the query layer
# ---------------------------------------------------------------------------

class TestScopeThreading:
    def test_scope_kwarg_flows_into_evidence_helpers(
        self, session: Session, fixture_company: Company
    ):
        """Verify each evidence_query helper sees the scope object the caller
        provided. Catches any future regression where a new helper is added to
        the orchestrator without forwarding ``scope=``."""
        scope = ScoringScope(country_codes=frozenset({"CN"}))
        observed: dict[str, ScoringScope | None] = {}

        def make_recorder(name: str, return_value):
            def _f(*args, **kw):
                observed[name] = kw.get("scope")
                return return_value
            return _f

        # Build mocks that record scope.
        mocks = {
            "get_company_material_exposure":      make_recorder("get_company_material_exposure", []),
            "get_events_for_company":             make_recorder("get_events_for_company", []),
            "get_filing_signals":                 make_recorder("get_filing_signals", []),
            "get_active_compliance_obligations":  make_recorder("get_active_compliance_obligations", []),
            "get_facilities_for_company":         make_recorder("get_facilities_for_company", []),
            "get_events_for_geographies":         make_recorder("get_events_for_geographies", []),
            "get_events_for_materials":           make_recorder("get_events_for_materials", []),
            "get_regulations_scoping_company":    make_recorder("get_regulations_scoping_company", []),
            "get_events_for_regulations":         make_recorder("get_events_for_regulations", []),
            # supplier_chain / latest_scores / chemistry helpers are not invoked
            # for scoped runs (propagation skipped + no models seeded), but we
            # mock them anyway for safety.
            "get_supplier_chain":                 lambda db, cid, **kw: [],
            "get_latest_company_scores":          lambda db, cids, **kw: {},
            "get_chemistry_mix_for_company":      make_recorder("get_chemistry_mix_for_company", None),
            "get_chemistry_material_intensities": lambda db, cids, aod=None, **kw: {},
            "get_latest_chemistry_risk_scores":   lambda db, cids, **kw: {},
            "get_chemistry_slugs":                lambda db, cids: {},
        }

        with patch.multiple("app.services.scoring.orchestrator", **mocks):
            score_company_scoped(session, fixture_company.id, scope)

        # Every recorded helper must have received the same scope object.
        assert len(observed) > 0
        for name, seen in observed.items():
            assert seen is scope, f"{name} did not receive scope kwarg"
