"""Tests for app/services/ingestion/eurlex.py.

The fetcher tests mock httpx — no real HTTP requests are issued. The
ingest tests use an in-memory SQLite session with only the tables the
ingester touches, sidestepping the Postgres-only ARRAY/JSONB columns
declared elsewhere in the ORM.

EUR-Lex ingest (post 2026-05) is alias-driven: regulations and scopes must
already exist in the DB; ``ingest_eurlex`` walks ``regulation_aliases`` rows
for ``source_system='eurlex_celex'`` and attaches documents / summaries /
risk events.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models.documents import SourceDocument
from app.models.regulatory import (
    Regulation,
    RegulationGeographyScope,
    RegulationMaterialScope,
    RegulationSourceAlias,
    RiskEvent,
    RiskEventGeography,
    RiskEventMaterial,
    RiskEventRegulation,
)
from app.models.source import Source
from app.models.supply import Material
from app.services.ingestion.eurlex import (
    EURLEX_HTML_URL,
    SEVERITY_BY_STATUS,
    _strip_html,
    fetch_eurlex_summary,
    ingest_eurlex,
)

# ---------------------------------------------------------------------------
# Minimal CELEX fixture — mirrors production shape without importing seed data
# ---------------------------------------------------------------------------

_EURLEX_CELEX_ROWS: list[dict] = [
    {
        "celex": "32023R1542",
        "regulation_key": "EU_TEST_BATTERY_2023",
        "title": "Test Battery Regulation",
        "status": "effective",
        "effective_date": date(2023, 8, 17),
        "material_name": "Lithium",
    },
    {
        "celex": "32019R1753",
        "regulation_key": "EU_TEST_CRITICAL_2019",
        "title": "Test Critical Raw Materials Regulation",
        "status": "proposed",
        "effective_date": date(2024, 1, 1),
        "material_name": "Cobalt",
    },
]


def _patch_sqlite_jsonb() -> None:
    """Render JSONB as JSON in SQLite DDL.

    Mirrors the pattern in tests/scoring/conftest.py. Safe to call repeatedly.
    """
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler  # type: ignore[import]

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]


_EURLEX_TABLES = (
    Source.__table__,
    SourceDocument.__table__,
    Material.__table__,
    Regulation.__table__,
    RegulationMaterialScope.__table__,
    RegulationGeographyScope.__table__,
    RegulationSourceAlias.__table__,
    RiskEvent.__table__,
    RiskEventRegulation.__table__,
    RiskEventMaterial.__table__,
    RiskEventGeography.__table__,
)


@pytest.fixture()
def session() -> Session:
    _patch_sqlite_jsonb()
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine, tables=list(_EURLEX_TABLES))
    Session_ = sessionmaker(bind=engine)
    s = Session_()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def seeded_eurlex_context(session: Session) -> None:
    """Insert materials, regulations, scopes, and CELEX aliases for ingest tests."""
    for row in _EURLEX_CELEX_ROWS:
        m = Material(canonical_name=row["material_name"], category="metal")
        session.add(m)
        session.flush()

        reg = Regulation(
            regulation_key=row["regulation_key"],
            title=row["title"],
            issuing_body="European Union",
            geography="EU",
            policy_theme="battery_supply_chain",
            status=row["status"],
            publication_date=date(2023, 1, 1),
            effective_date=row["effective_date"],
            summary=None,
            metadata_json={},
            verified=True,
        )
        session.add(reg)
        session.flush()

        session.add(
            RegulationMaterialScope(
                regulation_id=reg.id,
                material_id=m.id,
                scope_type="covered",
            )
        )
        session.add(
            RegulationGeographyScope(
                regulation_id=reg.id,
                country_code="EU",
                scope_type="jurisdiction",
            )
        )
        session.add(
            RegulationSourceAlias(
                source_system="eurlex_celex",
                source_key=row["celex"],
                regulation_id=reg.id,
                is_skipped=False,
            )
        )
    session.commit()


# ---------------------------------------------------------------------------
# _strip_html
# ---------------------------------------------------------------------------


class TestStripHtml:
    def test_strips_tags(self):
        assert _strip_html("<p>Hello <b>World</b></p>") == "Hello World"

    def test_decodes_entities(self):
        assert _strip_html("<p>caf&eacute; &amp; tea</p>") == "café & tea"

    def test_collapses_whitespace(self):
        assert _strip_html("  a\n\nb\t  c  ") == "a b c"

    def test_drops_script_and_style_blocks(self):
        raw = (
            "<style>body { color: red; }</style>"
            "<p>Visible</p>"
            "<script>alert('hi');</script>"
        )
        result = _strip_html(raw)
        assert "Visible" in result
        assert "alert" not in result
        assert "color" not in result


# ---------------------------------------------------------------------------
# fetch_eurlex_summary
# ---------------------------------------------------------------------------


class TestFetchEurlexSummary:
    def test_returns_stripped_text_on_success(self):
        html_body = (
            "<html><body><p>Recital 1 of the regulation introduces the scope.</p>"
            "<p>Recital 2 details the targeted materials.</p></body></html>"
        )
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.text = html_body
        mock_response.raise_for_status.return_value = None

        with patch("app.services.ingestion.eurlex.httpx.get", return_value=mock_response) as m:
            summary = fetch_eurlex_summary("32023R1542")

        assert summary is not None
        assert "Recital 1" in summary
        assert "Recital 2" in summary
        assert "<p>" not in summary
        m.assert_called_once()
        called_url = m.call_args.args[0]
        assert "CELEX:32023R1542" in called_url

    def test_truncates_to_summary_max_chars(self):
        # 2026-05-17: cap bumped 800 → 2500.  Test imports the constant
        # instead of hardcoding the value so future bumps don't break it.
        from app.services.ingestion.eurlex import _SUMMARY_MAX_CHARS

        long_paragraph = "a" * (_SUMMARY_MAX_CHARS * 2)
        html_body = f"<p>{long_paragraph}</p>"
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.text = html_body
        mock_response.raise_for_status.return_value = None

        with patch("app.services.ingestion.eurlex.httpx.get", return_value=mock_response):
            summary = fetch_eurlex_summary("32023R1542")

        assert summary is not None
        assert len(summary) == _SUMMARY_MAX_CHARS

    def test_returns_none_on_request_error(self):
        with patch(
            "app.services.ingestion.eurlex.httpx.get",
            side_effect=httpx.RequestError("boom", request=MagicMock()),
        ):
            assert fetch_eurlex_summary("32023R1542") is None

    def test_returns_none_on_http_status_error(self):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock(status_code=404)
        )
        with patch("app.services.ingestion.eurlex.httpx.get", return_value=mock_response):
            assert fetch_eurlex_summary("32023R1542") is None

    def test_returns_none_on_empty_body(self):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.text = "   <p></p>  "
        mock_response.raise_for_status.return_value = None
        with patch("app.services.ingestion.eurlex.httpx.get", return_value=mock_response):
            assert fetch_eurlex_summary("32023R1542") is None


# ---------------------------------------------------------------------------
# ingest_eurlex — alias-driven refresh
# ---------------------------------------------------------------------------


class TestIngestEurlexFirstRun:
    def test_processes_all_seeded_celex_aliases(
        self, session: Session, seeded_eurlex_context: None
    ):
        result = ingest_eurlex(session, fetch_summaries=False)

        n = len(_EURLEX_CELEX_ROWS)
        assert result["celex_processed"] == n
        assert result["risk_events_created"] == n
        assert result["source_documents_attached"] == n
        assert result["summary_skipped"] == n
        assert result["summary_backfilled"] == 0
        assert result["unknown_celex"] == 0

        assert len(session.scalars(select(Regulation)).all()) == n
        assert len(session.scalars(select(SourceDocument)).all()) == n

    def test_regulations_remain_verified(
        self, session: Session, seeded_eurlex_context: None
    ):
        ingest_eurlex(session, fetch_summaries=False)

        for r in session.scalars(select(Regulation)).all():
            assert r.verified is True

    def test_creates_source_row(self, session: Session, seeded_eurlex_context: None):
        ingest_eurlex(session, fetch_summaries=False)

        sources = session.scalars(select(Source)).all()
        assert len(sources) == 1
        assert sources[0].name == "EUR-Lex"
        assert sources[0].source_type == "eurlex"
        assert sources[0].phase == "1"

    def test_creates_source_document_per_celex(
        self, session: Session, seeded_eurlex_context: None
    ):
        ingest_eurlex(session, fetch_summaries=False)

        docs = session.scalars(select(SourceDocument)).all()
        assert len(docs) == len(_EURLEX_CELEX_ROWS)
        for doc in docs:
            assert doc.external_id.startswith("eurlex_")
            assert doc.document_type == "regulation"
            assert doc.metadata_json is not None
            assert "celex" in doc.metadata_json

    def test_propagates_material_links_to_risk_events(
        self, session: Session, seeded_eurlex_context: None
    ):
        result = ingest_eurlex(session, fetch_summaries=False)

        assert result["material_links_created"] >= len(_EURLEX_CELEX_ROWS)
        links = session.scalars(select(RiskEventMaterial)).all()
        assert len(links) >= len(_EURLEX_CELEX_ROWS)

    def test_summary_is_none_when_fetch_disabled(
        self, session: Session, seeded_eurlex_context: None
    ):
        ingest_eurlex(session, fetch_summaries=False)

        for r in session.scalars(select(Regulation)).all():
            assert r.summary is None


# ---------------------------------------------------------------------------
# ingest_eurlex — idempotency
# ---------------------------------------------------------------------------


class TestIngestEurlexIdempotency:
    def test_second_run_does_not_duplicate_risk_events_or_documents(
        self, session: Session, seeded_eurlex_context: None
    ):
        ingest_eurlex(session, fetch_summaries=False)
        result2 = ingest_eurlex(session, fetch_summaries=False)

        assert result2["risk_events_created"] == 0
        assert result2["source_documents_attached"] == 0
        n = len(_EURLEX_CELEX_ROWS)
        assert len(session.scalars(select(RiskEvent)).all()) == n
        assert len(session.scalars(select(RiskEventRegulation)).all()) == n
        assert len(session.scalars(select(SourceDocument)).all()) == n

    def test_second_run_does_not_duplicate_regulations(
        self, session: Session, seeded_eurlex_context: None
    ):
        ingest_eurlex(session, fetch_summaries=False)
        ingest_eurlex(session, fetch_summaries=False)

        assert len(session.scalars(select(Regulation)).all()) == len(_EURLEX_CELEX_ROWS)


# ---------------------------------------------------------------------------
# ingest_eurlex — summary backfill
# ---------------------------------------------------------------------------


class TestIngestEurlexSummaryBackfill:
    def test_backfills_missing_summary_when_fetch_enabled(
        self, session: Session, seeded_eurlex_context: None
    ):
        ingest_eurlex(session, fetch_summaries=False)

        with patch(
            "app.services.ingestion.eurlex.fetch_eurlex_summary",
            return_value="Backfilled preamble text.",
        ):
            result = ingest_eurlex(session, fetch_summaries=True)

        assert result["summary_backfilled"] == len(_EURLEX_CELEX_ROWS)
        for r in session.scalars(select(Regulation)).all():
            assert r.summary == "Backfilled preamble text."

    def test_does_not_overwrite_existing_summary(
        self, session: Session, seeded_eurlex_context: None
    ):
        with patch(
            "app.services.ingestion.eurlex.fetch_eurlex_summary",
            return_value="Original preamble.",
        ):
            ingest_eurlex(session, fetch_summaries=True)

        with patch(
            "app.services.ingestion.eurlex.fetch_eurlex_summary",
            return_value="Replacement preamble.",
        ) as fetch_mock:
            result = ingest_eurlex(session, fetch_summaries=True)

        fetch_mock.assert_not_called()
        assert result["summary_backfilled"] == 0
        for r in session.scalars(select(Regulation)).all():
            assert r.summary == "Original preamble."

    def test_failed_summary_fetch_does_not_break_run(
        self, session: Session, seeded_eurlex_context: None
    ):
        with patch(
            "app.services.ingestion.eurlex.fetch_eurlex_summary",
            return_value=None,
        ):
            result = ingest_eurlex(session, fetch_summaries=True)

        assert result["summary_backfilled"] == 0
        for r in session.scalars(select(Regulation)).all():
            assert r.summary is None


# ---------------------------------------------------------------------------
# Fixture data validation
# ---------------------------------------------------------------------------


class TestEurlexFixtureData:
    def test_unique_regulation_keys(self):
        keys = [r["regulation_key"] for r in _EURLEX_CELEX_ROWS]
        assert len(keys) == len(set(keys))

    def test_unique_celex_numbers(self):
        celex = [r["celex"] for r in _EURLEX_CELEX_ROWS]
        assert len(celex) == len(set(celex))

    def test_url_template_renders(self):
        for r in _EURLEX_CELEX_ROWS:
            url = EURLEX_HTML_URL.format(celex=r["celex"])
            assert url.startswith("https://eur-lex.europa.eu/")
            assert f"CELEX:{r['celex']}" in url


# ---------------------------------------------------------------------------
# ingest_eurlex — risk event generation
# ---------------------------------------------------------------------------


class TestIngestEurlexRiskEvents:
    def test_creates_one_risk_event_per_regulation(
        self, session: Session, seeded_eurlex_context: None
    ):
        result = ingest_eurlex(session, fetch_summaries=False)

        assert result["risk_events_created"] == len(_EURLEX_CELEX_ROWS)
        events = session.scalars(select(RiskEvent)).all()
        assert len(events) == len(_EURLEX_CELEX_ROWS)

    def test_risk_event_fields(self, session: Session, seeded_eurlex_context: None):
        ingest_eurlex(session, fetch_summaries=False)

        events = session.scalars(select(RiskEvent)).all()
        for ev in events:
            # 2026-05-17: event_type renamed REGULATORY_IMPLEMENTATION
            # → eurlex_regulation for consistency with other parsers'
            # lowercase mechanism-based naming.
            assert ev.event_type == "eurlex_regulation"
            assert ev.event_date is None
            # 2026-05-17: confidence recalibrated 1.0 → 0.9 to reflect
            # that the soft side of regulation-event confidence is our
            # interpretation of scope (materials, geography), not the
            # existence of the regulation itself.
            assert ev.confidence_score == 0.9
            assert ev.verified is True
            assert ev.risk_categories_json == ["regulatory_compliance"]
            assert ev.severity_score is not None
            # 0.0 allowed because superseded status maps to severity 0.0
            # (the fixture doesn't use superseded but we accept it).
            assert 0.0 <= ev.severity_score <= 1.0
            assert ev.content_hash is not None and len(ev.content_hash) == 64
            assert ev.metadata_json is not None
            assert "regulation_key" in ev.metadata_json
            assert "celex" in ev.metadata_json

    def test_severity_calibrated_by_status(self, session: Session, seeded_eurlex_context: None):
        ingest_eurlex(session, fetch_summaries=False)

        events = {ev.metadata_json["regulation_key"]: ev for ev in session.scalars(select(RiskEvent)).all()}
        for row in _EURLEX_CELEX_ROWS:
            ev = events[row["regulation_key"]]
            expected = SEVERITY_BY_STATUS.get(row["status"], 0.35)
            assert ev.severity_score == pytest.approx(expected)

    def test_risk_event_linked_to_correct_regulation(
        self, session: Session, seeded_eurlex_context: None
    ):
        ingest_eurlex(session, fetch_summaries=False)

        junctions = session.scalars(select(RiskEventRegulation)).all()
        assert len(junctions) == len(_EURLEX_CELEX_ROWS)

        for junction in junctions:
            assert junction.relevance_score == pytest.approx(1.0)
            reg = session.get(Regulation, junction.regulation_id)
            assert reg is not None
            # 2026-05-17: match_reason now varies by status —
            # "regulation_effective" / "regulation_enacted" /
            # "regulation_proposed" / "regulation_superseded".  Verify
            # the value matches the regulation's own status.
            assert junction.match_reason == f"regulation_{reg.status}"
            ev = session.get(RiskEvent, junction.risk_event_id)
            assert ev is not None

    def test_effective_date_in_metadata(self, session: Session, seeded_eurlex_context: None):
        ingest_eurlex(session, fetch_summaries=False)

        events_by_key = {
            ev.metadata_json["regulation_key"]: ev for ev in session.scalars(select(RiskEvent)).all()
        }
        for row in _EURLEX_CELEX_ROWS:
            ev = events_by_key[row["regulation_key"]]
            assert "effective_date" in ev.metadata_json
            assert ev.metadata_json["effective_date"] == row["effective_date"].isoformat()

    def test_second_run_does_not_duplicate_risk_events(
        self, session: Session, seeded_eurlex_context: None
    ):
        ingest_eurlex(session, fetch_summaries=False)
        ingest_eurlex(session, fetch_summaries=False)

        assert len(session.scalars(select(RiskEvent)).all()) == len(_EURLEX_CELEX_ROWS)
        assert len(session.scalars(select(RiskEventRegulation)).all()) == len(_EURLEX_CELEX_ROWS)

    def test_content_hash_unique_across_regulations(
        self, session: Session, seeded_eurlex_context: None
    ):
        ingest_eurlex(session, fetch_summaries=False)

        hashes = [ev.content_hash for ev in session.scalars(select(RiskEvent)).all()]
        assert len(hashes) == len(set(hashes))


class TestIngestEurlexEmptyAliases:
    def test_no_aliases_returns_zero_counts(self, session: Session) -> None:
        result = ingest_eurlex(session, fetch_summaries=False)
        assert result["celex_processed"] == 0
        assert result["risk_events_created"] == 0
