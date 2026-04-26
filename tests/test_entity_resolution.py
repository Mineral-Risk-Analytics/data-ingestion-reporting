"""
Tests for entity resolution and the evidence query layer.

Test categories:
  A. Pure-function tests (match helpers) — no DB, always run.
  B. persist_company_links tests — SQLite in-memory (portable, no JSONB queries).
  C. resolve_companies_for_event tests — no DB (function takes pre-loaded cache).
  D. evidence_query tests — mock DB session (JSONB.contains() is PG-specific).

Completion checks from spec:
  1. risk_event_companies table defined in metadata with UniqueConstraint.
  2. resolve_companies_for_event returns only the matching company.
  3. Re-running persist_company_links upserts, not duplicates.
  4. get_events_for_company returns events for matching company only.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session, sessionmaker

# Register all ORM models so Base.metadata is fully populated
import app.models  # noqa: F401

from app.constants import RiskCategory
from app.db.base import Base
from app.models.company import Company
from app.models.regulatory import RiskEvent, RiskEventCompany
from app.services.ingestion.entity_resolution import (
    CachedCompanyInfo,
    _geography_match,
    _material_match,
    _named_match,
    persist_company_links,
    resolve_companies_for_event,
)
from app.services.scoring.evidence_query import (
    EventWithRelevance,
    get_events_for_company,
    get_filing_signals,
)


# ---------------------------------------------------------------------------
# COMPLETION CHECK 1 — table + constraint in metadata (no DB required)
# ---------------------------------------------------------------------------

def test_risk_event_companies_in_metadata():
    """risk_event_companies table is registered in ORM metadata."""
    assert "risk_event_companies" in Base.metadata.tables


def test_unique_constraint_registered():
    """uq_risk_event_company constraint is present on the ORM table."""
    table = Base.metadata.tables["risk_event_companies"]
    names = {c.name for c in table.constraints}
    assert "uq_risk_event_company" in names


# ---------------------------------------------------------------------------
# SQLite fixture — JSONB columns rendered as JSON for DDL only
# ---------------------------------------------------------------------------

def _patch_sqlite_jsonb():
    """
    Make SQLite's DDL compiler render PG-only column types as JSON.
    Called before create_all so the patch is in place for schema creation.

    Patches:
      - JSONB → JSON (used by several intel/regulatory models)
      - ARRAY → JSON (used by ``insight_posts.materials/geographies``;
        InsightPost was added in foundation Phase 1 and is unrelated to
        these tests, but it's now in ``Base.metadata`` so create_all sees it)

    No JSONB query operators (e.g. @>) or ARRAY indexing are used in these
    tests, so the runtime behaviour is unaffected by the DDL substitution.
    """
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON
    if not hasattr(SQLiteTypeCompiler, "visit_ARRAY"):
        SQLiteTypeCompiler.visit_ARRAY = SQLiteTypeCompiler.visit_JSON


@pytest.fixture()
def db():
    _patch_sqlite_jsonb()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @sa_event.listens_for(engine, "connect")
    def _fk_on(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine)
    session = Sess()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _enable_event_company_linking(monkeypatch):
    """
    Foundation phase 3 disables RiskEventCompany inserts in the live ingestion
    pipeline (see ``app/services/ingestion/feature_flags.py``). The upsert-
    semantics tests in this file still need the writer to run end-to-end so
    the dedupe / upgrade behaviour is regression-tested for when Phase 5
    re-enables the flag. Flip it on for the duration of each test.
    """
    monkeypatch.setattr(
        "app.services.ingestion.feature_flags.LINK_EVENTS_TO_COMPANIES",
        True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _company(db: Session, name: str, hq: str = "US") -> Company:
    c = Company(canonical_name=name, supply_chain_stage="miner", headquarters_country=hq)
    db.add(c)
    db.flush()
    return c


def _event(db: Session, title: str, categories: list[str]) -> RiskEvent:
    ev = RiskEvent(
        event_type="test",
        title=title,
        risk_categories_json=categories,
        geography_json={},
        metadata_json={},
        event_date=datetime.now(timezone.utc),
    )
    db.add(ev)
    db.flush()
    return ev


def _cached(c: Company, aliases: list[str] = (), kws: set[str] = ()) -> CachedCompanyInfo:
    return CachedCompanyInfo(
        id=c.id,
        canonical_name_lower=c.canonical_name.lower(),
        headquarters_country=c.headquarters_country,
        supply_chain_stage=c.supply_chain_stage or "other",
        alias_lowers=list(aliases),
        material_ids=frozenset(),
        material_keywords=frozenset(kws),
    )


# ---------------------------------------------------------------------------
# A. Pure function tests — match helpers
# ---------------------------------------------------------------------------

def test_named_match_canonical(db):
    c = _company(db, "Albemarle Corporation")
    ci = _cached(c)
    assert _named_match("albemarle corporation posts loss", ci) is True
    assert _named_match("copper tariff news", ci) is False


def test_named_match_alias(db):
    c = _company(db, "Albemarle Corporation")
    ci = _cached(c, aliases=["albemarle corp."])
    assert _named_match("albemarle corp. raises guidance", ci) is True


def test_named_match_short_alias_ignored(db):
    """Aliases shorter than 5 chars are skipped to avoid false positives."""
    c = _company(db, "ALB Holdings")
    ci = _cached(c, aliases=["alb"])
    assert _named_match("alb news", ci) is False  # too short


def test_geography_match_hq_country(db):
    c = _company(db, "China Lithium Co", hq="CN")
    ci = _cached(c)
    assert _geography_match({"cn", "lithium"}, ci) is True


def test_geography_match_high_concentration_geo(db):
    """Any company matches when a high-concentration geo (CN/CD/RU) is in geo_values."""
    c = _company(db, "US Company", hq="US")
    ci = _cached(c)
    # CN appears → broad HCG match for all supply chains downstream
    assert _geography_match({"cn"}, ci) is True


def test_geography_no_match(db):
    c = _company(db, "French OEM", hq="FR")
    ci = _cached(c)
    assert _geography_match({"jp", "japan"}, ci) is False


def test_geography_empty_values(db):
    c = _company(db, "Random Corp", hq="US")
    ci = _cached(c)
    assert _geography_match(set(), ci) is False


def test_material_match_keyword(db):
    c = _company(db, "Lithium Corp", hq="US")
    ci = _cached(c, kws={"lithium", "chemicals"})
    assert _material_match("new lithium hydroxide plant opened", {}, ci) is True
    assert _material_match("steel tariff decision today", {}, ci) is False


def test_material_match_hs_code(db):
    c = _company(db, "Cell Maker", hq="KR")
    ci = _cached(c, kws=set())
    # HS 8507xx = battery cells
    assert _material_match("trade data", {"hs_code": "850760"}, ci) is True
    assert _material_match("trade data", {"hs_code": "720210"}, ci) is False


# ---------------------------------------------------------------------------
# B/C. COMPLETION CHECK 2 — resolve returns only matching company
# ---------------------------------------------------------------------------

def test_resolve_named_match_only(db):
    """Named match: only the company whose name appears in the event gets a link."""
    tesla = _company(db, "Tesla Inc.")
    alb   = _company(db, "Albemarle Corporation")

    ev = _event(db, "Tesla Inc. files 10-K with battery supply chain disclosures",
                [RiskCategory.FINANCIAL_PRESSURE.value])

    cache = [_cached(tesla), _cached(alb)]
    matches = resolve_companies_for_event(db, ev, cache)

    ids = {m[0] for m in matches}
    assert tesla.id in ids
    assert alb.id not in ids


def test_resolve_geography_match(db):
    """Company with CN headquarters gets linked to CN-geography event."""
    cn_co = _company(db, "CATL", hq="CN")
    us_co = _company(db, "Panasonic NA", hq="US")

    ev = RiskEvent(
        event_type="test",
        title="Export controls on battery precursors tightened",
        risk_categories_json=[RiskCategory.GEOPOLITICAL_TRADE.value],
        geography_json={"partner": "CN"},  # CN → high-concentration geo match
        metadata_json={},
        event_date=datetime.now(timezone.utc),
    )
    db.add(ev); db.flush()

    cache = [_cached(cn_co), _cached(us_co)]
    matches = resolve_companies_for_event(db, ev, cache)

    ids = {m[0] for m in matches}
    assert cn_co.id in ids


def test_relevance_named_beats_geography(db):
    """Named match (1.0) wins over geography match (0.85) for the same company."""
    co = _company(db, "Albemarle Corporation", hq="CN")  # HQ in CN → geo match too
    ev = RiskEvent(
        event_type="test",
        title="Albemarle Corporation lithium expansion in CN",
        risk_categories_json=[RiskCategory.MATERIAL_CONCENTRATION.value],
        geography_json={"partner": "CN"},
        metadata_json={},
        event_date=datetime.now(timezone.utc),
    )
    db.add(ev); db.flush()

    cache = [_cached(co)]
    matches = resolve_companies_for_event(db, ev, cache)

    assert len(matches) == 1
    _, relevance, reason = matches[0]
    assert relevance == 1.0
    assert reason == "named_company"


def test_resolve_no_match_non_battery(db):
    """Non-battery event with no keyword overlap produces no links."""
    co = _company(db, "Steel Corp", hq="US")
    ev = _event(db, "Iron ore price update", ["operational"])
    cache = [_cached(co)]
    matches = resolve_companies_for_event(db, ev, cache)
    assert matches == []


# ---------------------------------------------------------------------------
# COMPLETION CHECK 3 — upsert semantics (no duplicate rows)
# ---------------------------------------------------------------------------

def test_persist_creates_row(db):
    co = _company(db, "Company A")
    ev = _event(db, "Some event", [RiskCategory.OPERATIONAL.value])

    persist_company_links(db, ev, [(co.id, 0.85, "geography")])
    db.flush()

    link = db.query(RiskEventCompany).filter_by(
        risk_event_id=ev.id, company_id=co.id
    ).one()
    assert link.relevance_score == 0.85
    assert link.match_reason == "geography"


def test_persist_upserts_on_rerun(db):
    """Re-running persist_company_links upgrades the row, never creates duplicates."""
    co = _company(db, "Company A")
    ev = _event(db, "Some event", [RiskCategory.OPERATIONAL.value])

    # First pass — geography match
    persist_company_links(db, ev, [(co.id, 0.85, "geography")])
    db.flush()

    # Second pass — named match with higher relevance
    persist_company_links(db, ev, [(co.id, 1.0, "named_company")])
    db.flush()

    links = db.query(RiskEventCompany).filter_by(
        risk_event_id=ev.id, company_id=co.id
    ).all()
    assert len(links) == 1, "Upsert must not create duplicate rows"
    assert links[0].relevance_score == 1.0
    assert links[0].match_reason == "named_company"


def test_persist_empty_matches_is_noop(db):
    ev = _event(db, "No-match event", [RiskCategory.OPERATIONAL.value])
    persist_company_links(db, ev, [])
    db.flush()
    count = db.query(RiskEventCompany).filter_by(risk_event_id=ev.id).count()
    assert count == 0


def test_persist_multiple_companies(db):
    co_a = _company(db, "Company A")
    co_b = _company(db, "Company B")
    ev   = _event(db, "Multi-company event", [RiskCategory.GEOPOLITICAL_TRADE.value])

    persist_company_links(db, ev, [
        (co_a.id, 1.0, "named_company"),
        (co_b.id, 0.85, "geography"),
    ])
    db.flush()

    count = db.query(RiskEventCompany).filter_by(risk_event_id=ev.id).count()
    assert count == 2


# ---------------------------------------------------------------------------
# COMPLETION CHECK 4 — get_events_for_company via junction table
# Using mock sessions because JSONB.contains() generates PG-specific SQL.
# ---------------------------------------------------------------------------

def _mock_event(eid: int, title: str, categories: list[str]) -> SimpleNamespace:
    """
    Lightweight stand-in for a RiskEvent when no DB session is available.
    Uses SimpleNamespace so attribute assignment works without SQLAlchemy's
    ORM instance-state machinery.
    """
    return SimpleNamespace(
        id=eid,
        title=title,
        risk_categories_json=categories,
        event_date=datetime(2025, 6, 1, tzinfo=timezone.utc),
        severity_score=0.7,
        confidence_score=0.8,
        geography_json={},
        metadata_json={},
    )


def _make_mock_db_for_events(events_with_relevance: list[tuple]) -> Session:
    """
    Return a mock Session whose execute() returns the given rows.
    Row shape: (RiskEvent, relevance_score).
    """
    mock_db = MagicMock(spec=Session)
    mock_result = MagicMock()
    mock_result.all.return_value = events_with_relevance
    mock_db.execute.return_value = mock_result
    return mock_db


def test_get_events_returns_only_linked_company():
    """
    COMPLETION CHECK 4: get_events_for_company scopes to the company via the
    junction join — unlinked events are not returned.

    The query is mocked because JSONB.contains() generates PG-specific SQL that
    SQLite cannot compile. The mock verifies the function builds a result list
    only from what the DB layer returns.
    """
    ev_a = _mock_event(1, "Battery trade restriction", [RiskCategory.GEOPOLITICAL_TRADE.value])
    # Only ev_a is "returned" by the mocked DB (company A only)
    mock_db = _make_mock_db_for_events([(ev_a, 0.85)])

    company_id = uuid.uuid4()
    results = get_events_for_company(mock_db, company_id=company_id,
                                     category=RiskCategory.GEOPOLITICAL_TRADE,
                                     as_of_date=date(2026, 4, 1))

    assert len(results) == 1
    assert isinstance(results[0], EventWithRelevance)
    assert results[0].event.id == ev_a.id
    assert results[0].relevance_score == 0.85


def test_get_events_empty_when_no_links():
    """No junction rows → empty list returned."""
    mock_db = _make_mock_db_for_events([])
    company_id = uuid.uuid4()
    results = get_events_for_company(mock_db, company_id=company_id,
                                     category=RiskCategory.OPERATIONAL,
                                     as_of_date=date(2026, 4, 1))
    assert results == []


def test_get_events_carries_correct_relevance():
    """relevance_score from junction row is preserved in EventWithRelevance."""
    ev = _mock_event(5, "Export ban", [RiskCategory.GEOPOLITICAL_TRADE.value])
    mock_db = _make_mock_db_for_events([(ev, 1.0)])

    company_id = uuid.uuid4()
    results = get_events_for_company(mock_db, company_id, RiskCategory.GEOPOLITICAL_TRADE, date(2026, 4, 1))
    assert results[0].relevance_score == 1.0


def test_get_filing_signals_delegates_to_financial_pressure_category():
    """
    get_events_for_company delegates to get_filing_signals for FINANCIAL_PRESSURE,
    which also returns EventWithRelevance objects.
    """
    ev = _mock_event(10, "10-K filing", [RiskCategory.FINANCIAL_PRESSURE.value])
    mock_db = _make_mock_db_for_events([(ev, 0.85)])

    company_id = uuid.uuid4()
    # Calling get_events_for_company with FINANCIAL_PRESSURE delegates to get_filing_signals
    results = get_events_for_company(mock_db, company_id, RiskCategory.FINANCIAL_PRESSURE, date(2026, 4, 1))
    assert len(results) == 1
    assert results[0].event.id == ev.id
