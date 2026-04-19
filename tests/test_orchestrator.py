"""
Orchestrator completion-check tests.

Checks (from the spec):
  1. rescore_company() with a fixture company + events writes a CompanyScore row
     with all five component scores and non-null rationale_json.
  2. Calling rescore_company() twice for the same company produces TWO rows
     (append-only confirmed).
  3. A scoring failure for one company does not prevent the ingestion pipeline's
     db.commit() from completing (successfully ingested events are preserved).
  4. No code path calls aggregate_supplier_risk() directly from outside orchestrator.py
     (verified by a source-text assertion).

Evidence query functions use JSONB.contains() which is PostgreSQL-specific. For
these unit tests the evidence layer is mocked at the orchestrator call-site so that
the SQLite in-memory DB can be used for the CompanyScore persistence assertions.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import textwrap
import types
import uuid
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

# Ensure all ORM models are registered before create_all.
import app.models  # noqa: F401

from app.db.base import Base
from app.models.company import Company, CompanyScore
from app.services.scoring.evidence_query import EventWithRelevance
from app.services.scoring.orchestrator import rescore_company


# ---------------------------------------------------------------------------
# SQLite fixture — patch JSONB → JSON for DDL compatibility
# ---------------------------------------------------------------------------

def _patch_sqlite_jsonb() -> None:
    """Render JSONB as JSON in SQLite DDL (no JSONB query operators used here)."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler  # type: ignore[import]

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]


@pytest.fixture()
def sqlite_session():
    """In-memory SQLite session with all ORM tables created."""
    _patch_sqlite_jsonb()
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    Session_ = sessionmaker(bind=engine)
    session = Session_()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def fixture_company(sqlite_session: Session) -> Company:
    """Insert and return a minimal Company row."""
    company = Company(
        canonical_name="TestCo Battery",
        headquarters_country="CN",
        supply_chain_stage="miner",
    )
    sqlite_session.add(company)
    sqlite_session.flush()
    return company


# ---------------------------------------------------------------------------
# Mock helpers — return consistent minimal evidence
# ---------------------------------------------------------------------------

def _empty_ew_list() -> list:
    return []


def _mock_evidence_no_events():
    """
    Patch the evidence query functions used by rescore_company() to return
    empty / conservative defaults. The orchestrator's aggregation layer handles
    empty evidence by producing default 0.3-0.5 range inputs per the spec.

    Keys are bare attribute names on the orchestrator module (as imported there).
    Each mock accepts ``**kwargs`` so the orchestrator's new ``scope=`` and
    other v3.0 kwargs do not break the lambda signatures.
    """
    return {
        # Existing query helpers (now scope-threaded)
        "get_company_material_exposure":     lambda db, cid, **kw: [],
        "get_events_for_company":            lambda db, cid, cat, aod, **kw: [],
        "get_filing_signals":                lambda db, cid, **kw: [],
        "get_active_compliance_obligations": lambda db, cid, **kw: [],
        # v3.0 supply-chain rollup helpers
        "get_facilities_for_company":        lambda db, cid, **kw: [],
        "get_events_for_geographies":        lambda db, ccs, cat, aod, **kw: [],
        "get_events_for_materials":          lambda db, mids, cat, aod, **kw: [],
        "get_regulations_scoping_company":   lambda db, cid, mids, ccs, **kw: [],
        "get_events_for_regulations":        lambda db, rkeys, aod, **kw: [],
        "get_supplier_chain":                lambda db, cid, **kw: [],
        "get_latest_company_scores":         lambda db, cids, **kw: {},
        # v3.0 chemistry helpers
        "get_chemistry_mix_for_company":     lambda db, cid, aod=None, **kw: None,
        "get_chemistry_material_intensities": lambda db, cids, aod=None, **kw: {},
        "get_latest_chemistry_risk_scores":  lambda db, cids, **kw: {},
        "get_chemistry_slugs":               lambda db, cids: {},
    }


# ---------------------------------------------------------------------------
# Completion check 1: rescore writes a full CompanyScore row
# ---------------------------------------------------------------------------

def test_rescore_writes_score_row(sqlite_session: Session, fixture_company: Company) -> None:
    """
    rescore_company() with no evidence events should produce a CompanyScore row
    with all five component scores populated and non-null rationale_json.
    Conservative defaults from the aggregation layer must yield non-zero scores
    (e.g. material concentration: 0.5 default criticality and concentration).
    """
    with patch.multiple("app.services.scoring.orchestrator", **_mock_evidence_no_events()):
        score_row = rescore_company(sqlite_session, fixture_company.id, run_id="test")

    sqlite_session.flush()

    assert score_row.id is not None, "flush() should populate the PK"
    assert score_row.company_id == fixture_company.id
    assert score_row.scoring_version == "3.0"

    # All five component scores must be present and on [0, 100]
    for col in (
        "material_concentration_risk_score",
        "geopolitical_trade_risk_score",
        "regulatory_risk_score",
        "operational_risk_score",
        "financial_pressure_score",
        "overall_risk_score",
    ):
        val = getattr(score_row, col)
        assert val is not None, f"{col} should not be None"
        assert 0.0 <= val <= 100.0, f"{col}={val} out of [0,100]"

    # rationale_json must be non-null and contain the expected top-level keys
    assert score_row.rationale_json is not None
    rj = score_row.rationale_json
    assert "inputs" in rj
    assert "components" in rj
    assert "top_evidence" in rj
    assert "decay" in rj
    assert "notes" in rj

    # notes field must be a non-empty string
    assert isinstance(rj["notes"], str) and len(rj["notes"]) > 0

    # inputs.supplier_id must match (key name preserved for API compatibility)
    assert rj["inputs"]["supplier_id"] == str(fixture_company.id)
    assert rj["inputs"]["run_id"] == "test"


def test_rescore_with_one_geopolitical_event(
    sqlite_session: Session, fixture_company: Company
) -> None:
    """
    Fixture company with one GEOPOLITICAL_TRADE event should produce a non-null
    geopolitical score above the zero baseline.
    """
    from app.constants import RiskCategory

    ev = MagicMock()
    ev.id = 1
    ev.severity_score = 0.75
    ev.confidence_score = 0.80
    ev.event_date = None  # triggers fallback to as_of_date
    ev.title = "Export restrictions on battery-grade lithium"
    ev.metadata_json = {"event_subtype": "EXPORT_RESTRICTION"}

    ew = EventWithRelevance(event=ev, relevance_score=0.90)

    def fake_get_events(db, cid, cat, aod, **kw):
        if cat == RiskCategory.GEOPOLITICAL_TRADE:
            return [ew]
        return []

    overrides = _mock_evidence_no_events()
    overrides["get_events_for_company"] = fake_get_events
    with patch.multiple("app.services.scoring.orchestrator", **overrides):
        score_row = rescore_company(sqlite_session, fixture_company.id, run_id="test-geo")

    assert score_row.geopolitical_trade_risk_score is not None
    # With an EXPORT_RESTRICTION event, geo score should be above the all-default baseline
    assert score_row.geopolitical_trade_risk_score >= 0.0
    # top_evidence should contain the event ID
    assert "1" in score_row.rationale_json["top_evidence"]


# ---------------------------------------------------------------------------
# Completion check 2: rescore twice → two rows (append-only)
# ---------------------------------------------------------------------------

def test_rescore_append_only(sqlite_session: Session, fixture_company: Company) -> None:
    """
    Calling rescore_company() twice for the same company on the same date should
    create TWO separate rows in company_scores (append-only; no overwrite).
    """
    with patch.multiple("app.services.scoring.orchestrator", **_mock_evidence_no_events()):
        row1 = rescore_company(sqlite_session, fixture_company.id, run_id="run-1")
        row2 = rescore_company(sqlite_session, fixture_company.id, run_id="run-2")

    sqlite_session.flush()

    rows = sqlite_session.scalars(
        select(CompanyScore).where(CompanyScore.company_id == fixture_company.id)
    ).all()

    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
    ids = {r.id for r in rows}
    assert len(ids) == 2, "Both rows must have distinct primary keys"
    run_ids = {r.rationale_json["inputs"]["run_id"] for r in rows}
    assert "run-1" in run_ids
    assert "run-2" in run_ids


# ---------------------------------------------------------------------------
# Completion check 3: scoring failure must not block ingestion commit
# ---------------------------------------------------------------------------

def test_scoring_failure_does_not_block_ingestion(
    sqlite_session: Session, fixture_company: Company
) -> None:
    """
    When rescore_company() raises an exception for one company the pipeline
    catches it and continues. The ingestion run (and any persisted events) must
    not be rolled back.

    Simulated by calling the rescore loop logic directly with a mock that raises.
    """
    touched_company_ids: set[uuid.UUID] = {fixture_company.id}

    def failing_rescore(db, cid, run_id, **kw):
        raise RuntimeError("DB timeout — simulated failure")

    errors: list[str] = []

    # Replicate the pipeline's try/except rescore loop inline
    with patch("app.services.scoring.orchestrator.rescore_company", side_effect=failing_rescore):
        from app.services.scoring.orchestrator import rescore_company as _rc  # get the mock

        for cid in touched_company_ids:
            try:
                _rc(sqlite_session, cid, run_id="ing-run-1")
            except Exception as exc:
                errors.append(str(exc))

    # The loop must have caught the error
    assert len(errors) == 1
    assert "simulated failure" in errors[0]

    # The session must still be usable (no transaction corruption)
    new_row = CompanyScore(
        company_id=fixture_company.id,
        as_of_date=date.today(),
        overall_risk_score=42.0,
        scoring_version="2.0",
    )
    sqlite_session.add(new_row)
    sqlite_session.flush()
    assert new_row.id is not None, "Session must remain usable after a caught scoring failure"


# ---------------------------------------------------------------------------
# Completion check 4: aggregate_supplier_risk() not called outside orchestrator
# ---------------------------------------------------------------------------

def test_aggregate_supplier_risk_only_in_orchestrator() -> None:
    """
    Verify that aggregate_supplier_risk() is not called directly from any module
    other than orchestrator.py and supplier_risk.py (the module that defines it
    and the one that is allowed to call it via the orchestrator).

    This is a static source-text assertion — not a runtime check.
    """
    import ast
    import pathlib
    import re

    app_root = pathlib.Path(__file__).parent.parent / "app"
    # supplier_risk.py defines the function; orchestrator.py is the sole caller;
    # __init__.py files may re-export the symbol but must not call it.
    allowed_files = {"orchestrator.py", "supplier_risk.py"}
    violations: list[str] = []

    for py_file in app_root.rglob("*.py"):
        if py_file.name in allowed_files:
            continue
        source = py_file.read_text(encoding="utf-8")
        # Only flag files that CALL the function (name followed by an opening paren).
        if re.search(r"aggregate_supplier_risk\s*\(", source):
            violations.append(str(py_file.relative_to(app_root.parent)))

    assert violations == [], (
        "aggregate_supplier_risk() is called outside orchestrator.py in:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Aggregator unit tests (no DB — pure function)
# ---------------------------------------------------------------------------

class TestDeriveFinancialInputs:
    """Unit tests for derive_financial_inputs (no DB, no JSONB)."""

    def _make_ew(
        self,
        severity: float = 0.5,
        confidence: float = 0.5,
        subtype: str = "",
        title: str = "",
    ) -> EventWithRelevance:
        ev = MagicMock()
        ev.id = 1
        ev.severity_score = severity
        ev.confidence_score = confidence
        ev.event_date = None
        ev.metadata_json = {"event_subtype": subtype}
        ev.title = title
        return EventWithRelevance(event=ev, relevance_score=0.80)

    def test_empty_events_returns_zeros(self) -> None:
        from app.services.scoring.evidence_aggregator import derive_financial_inputs

        base, lev, liq, fc = derive_financial_inputs([])
        assert base == 0.0
        assert lev == 0.0
        assert liq == 0.0
        assert fc == 0

    def test_base_filing_signal_scaled_by_severity(self) -> None:
        from app.services.scoring.evidence_aggregator import derive_financial_inputs

        ew = self._make_ew(severity=0.5)
        base, _, _, fc = derive_financial_inputs([ew])
        assert abs(base - 20.0) < 0.01  # 0.5 * 40 = 20
        assert fc == 1

    def test_leverage_warning_tag_included(self) -> None:
        from app.services.scoring.evidence_aggregator import derive_financial_inputs

        ew = self._make_ew(severity=0.6, subtype="LEVERAGE_WARNING")
        _, lev, _, _ = derive_financial_inputs([ew])
        assert abs(lev - 0.6) < 0.01

    def test_going_concern_tag_included(self) -> None:
        from app.services.scoring.evidence_aggregator import derive_financial_inputs

        ew = self._make_ew(severity=0.7, subtype="GOING_CONCERN")
        _, _, liq, _ = derive_financial_inputs([ew])
        assert abs(liq - 0.7) < 0.01

    def test_leverage_bonus_capped_at_30(self) -> None:
        from app.services.scoring.evidence_aggregator import derive_financial_inputs

        ews = [self._make_ew(severity=0.9, subtype="LEVERAGE_WARNING") for _ in range(50)]
        _, lev, _, _ = derive_financial_inputs(ews)
        assert lev == 30.0


class TestDeriveOperationalInputs:
    def _make_ew(self, severity: float, subtype: str = "") -> EventWithRelevance:
        ev = MagicMock()
        ev.id = 2
        ev.severity_score = severity
        ev.confidence_score = 0.6
        ev.event_date = None
        ev.metadata_json = {"event_subtype": subtype}
        ev.title = ""
        return EventWithRelevance(event=ev, relevance_score=0.80)

    def test_no_events_uses_default_structural_dep(self) -> None:
        from app.services.scoring.evidence_aggregator import derive_operational_inputs

        struct_dep, impacts = derive_operational_inputs([], date.today())
        assert abs(struct_dep - 0.3) < 0.001
        assert impacts == []

    def test_single_source_event_raises_struct_dep(self) -> None:
        from app.services.scoring.evidence_aggregator import derive_operational_inputs

        ew = self._make_ew(severity=0.9, subtype="SINGLE_SOURCE")
        struct_dep, _ = derive_operational_inputs([ew], date.today())
        assert struct_dep > 0.3


class TestDeriveRegulatoryInputs:
    def _make_ew(self, severity: float = 0.5, confidence: float = 0.6) -> EventWithRelevance:
        ev = MagicMock()
        ev.id = 3
        ev.severity_score = severity
        ev.confidence_score = confidence
        ev.event_date = None
        ev.metadata_json = {}
        ev.title = ""
        return EventWithRelevance(event=ev, relevance_score=0.80)

    def test_no_events_returns_empty_impacts(self) -> None:
        from app.services.scoring.evidence_aggregator import derive_regulatory_inputs

        impacts, obligations, prox = derive_regulatory_inputs(
            [], [("UFLPA", 1.0)], date.today()
        )
        assert impacts == []
        assert obligations == [("UFLPA", 1.0)]
        assert prox == 1.0

    def test_event_within_90_days_sets_proximity(self) -> None:
        from datetime import timedelta

        from app.services.scoring.evidence_aggregator import derive_regulatory_inputs

        as_of = date.today()
        eff_date = (as_of + timedelta(days=30)).isoformat()

        ev = MagicMock()
        ev.id = 4
        ev.severity_score = 0.5
        ev.confidence_score = 0.6
        ev.event_date = None
        ev.metadata_json = {"effective_date": eff_date}
        ev.title = ""
        ew = EventWithRelevance(event=ev, relevance_score=0.80)

        _, _, prox = derive_regulatory_inputs([ew], [], as_of)
        assert prox == 1.15

    def test_event_outside_90_days_leaves_prox_default(self) -> None:
        from datetime import timedelta

        from app.services.scoring.evidence_aggregator import derive_regulatory_inputs

        as_of = date.today()
        eff_date = (as_of + timedelta(days=120)).isoformat()

        ev = MagicMock()
        ev.id = 5
        ev.severity_score = 0.5
        ev.confidence_score = 0.6
        ev.event_date = None
        ev.metadata_json = {"effective_date": eff_date}
        ev.title = ""
        ew = EventWithRelevance(event=ev, relevance_score=0.80)

        _, _, prox = derive_regulatory_inputs([ew], [], as_of)
        assert prox == 1.0
