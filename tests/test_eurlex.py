"""Tests for app/services/ingestion/eurlex.py.

The fetcher tests mock httpx — no real HTTP requests are issued. The
ingest tests use an in-memory SQLite session with only the tables the
ingester touches, sidestepping the Postgres-only ARRAY/JSONB columns
declared elsewhere in the ORM.
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
)
from app.models.source import Source
from app.models.supply import Material
from app.services.ingestion.eurlex import (
    BATTERY_REGULATIONS,
    EURLEX_HTML_URL,
    _strip_html,
    fetch_eurlex_summary,
    ingest_eurlex,
)


# ---------------------------------------------------------------------------
# In-memory SQLite session — only the tables the ingester touches
# ---------------------------------------------------------------------------

def _patch_sqlite_jsonb() -> None:
    """Render JSONB as JSON in SQLite DDL.

    Mirrors the pattern in tests/scoring/conftest.py. Safe to call repeatedly.
    """
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler  # type: ignore[import]

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]


# Tables touched by the EUR-Lex ingester. We create only these to avoid the
# Postgres-specific ARRAY columns elsewhere in the ORM (e.g. InsightPost).
_EURLEX_TABLES = (
    Source.__table__,
    SourceDocument.__table__,
    Material.__table__,
    Regulation.__table__,
    RegulationMaterialScope.__table__,
    RegulationGeographyScope.__table__,
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
def seeded_materials(session: Session) -> dict[str, int]:
    """Insert every material referenced by ``BATTERY_REGULATIONS``."""
    needed = {
        material_name
        for reg in BATTERY_REGULATIONS
        for material_name, _ in reg["material_scopes"]
    }
    out: dict[str, int] = {}
    for name in sorted(needed):
        m = Material(canonical_name=name, category="metal")
        session.add(m)
        session.flush()
        out[name] = m.id
    session.commit()
    return out


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

    def test_truncates_to_800_chars(self):
        long_paragraph = "a" * 5000
        html_body = f"<p>{long_paragraph}</p>"
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.text = html_body
        mock_response.raise_for_status.return_value = None

        with patch("app.services.ingestion.eurlex.httpx.get", return_value=mock_response):
            summary = fetch_eurlex_summary("32023R1542")

        assert summary is not None
        assert len(summary) == 800

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
# ingest_eurlex — first-run inserts
# ---------------------------------------------------------------------------

class TestIngestEurlexFirstRun:
    def test_inserts_all_manifest_regulations(self, session: Session, seeded_materials):
        result = ingest_eurlex(session, fetch_summaries=False)

        assert result["inserted"] == len(BATTERY_REGULATIONS)
        assert result["updated"] == 0
        assert result["skipped"] == 0

        rows = session.scalars(select(Regulation)).all()
        assert len(rows) == len(BATTERY_REGULATIONS)
        keys = {r.regulation_key for r in rows}
        assert keys == {r["regulation_key"] for r in BATTERY_REGULATIONS}

    def test_inserted_regulations_are_marked_verified(
        self, session: Session, seeded_materials
    ):
        ingest_eurlex(session, fetch_summaries=False)

        for r in session.scalars(select(Regulation)).all():
            assert r.verified is True, f"{r.regulation_key} should be verified=True"

    def test_creates_source_row(self, session: Session, seeded_materials):
        ingest_eurlex(session, fetch_summaries=False)

        sources = session.scalars(select(Source)).all()
        assert len(sources) == 1
        assert sources[0].name == "EUR-Lex"
        assert sources[0].source_type == "eurlex"
        assert sources[0].phase == "1"

    def test_creates_source_document_per_regulation(
        self, session: Session, seeded_materials
    ):
        ingest_eurlex(session, fetch_summaries=False)

        docs = session.scalars(select(SourceDocument)).all()
        assert len(docs) == len(BATTERY_REGULATIONS)
        for doc in docs:
            assert doc.external_id.startswith("eurlex_")
            assert doc.document_type == "regulation"
            assert doc.metadata_json is not None
            assert "celex" in doc.metadata_json

    def test_creates_material_scope_rows(self, session: Session, seeded_materials):
        result = ingest_eurlex(session, fetch_summaries=False)

        expected_total = sum(len(r["material_scopes"]) for r in BATTERY_REGULATIONS)
        assert result["material_scopes"] == expected_total
        assert (
            session.scalar(
                select(RegulationMaterialScope).where(
                    RegulationMaterialScope.id.is_not(None)
                )
            )
            is not None
        )

    def test_creates_geography_scope_rows(self, session: Session, seeded_materials):
        result = ingest_eurlex(session, fetch_summaries=False)

        expected_total = sum(len(r["geography_scopes"]) for r in BATTERY_REGULATIONS)
        assert result["geography_scopes"] == expected_total

    def test_summary_is_none_when_fetch_disabled(
        self, session: Session, seeded_materials
    ):
        ingest_eurlex(session, fetch_summaries=False)

        for r in session.scalars(select(Regulation)).all():
            assert r.summary is None


# ---------------------------------------------------------------------------
# ingest_eurlex — idempotency
# ---------------------------------------------------------------------------

class TestIngestEurlexIdempotency:
    def test_second_run_skips_all(self, session: Session, seeded_materials):
        ingest_eurlex(session, fetch_summaries=False)
        result2 = ingest_eurlex(session, fetch_summaries=False)

        assert result2["inserted"] == 0
        assert result2["updated"] == 0
        assert result2["skipped"] == len(BATTERY_REGULATIONS)
        assert result2["material_scopes"] == 0
        assert result2["geography_scopes"] == 0

    def test_second_run_does_not_duplicate_regulations(
        self, session: Session, seeded_materials
    ):
        ingest_eurlex(session, fetch_summaries=False)
        ingest_eurlex(session, fetch_summaries=False)

        assert (
            len(session.scalars(select(Regulation)).all())
            == len(BATTERY_REGULATIONS)
        )

    def test_second_run_does_not_duplicate_source_documents(
        self, session: Session, seeded_materials
    ):
        ingest_eurlex(session, fetch_summaries=False)
        ingest_eurlex(session, fetch_summaries=False)

        assert (
            len(session.scalars(select(SourceDocument)).all())
            == len(BATTERY_REGULATIONS)
        )

    def test_second_run_does_not_duplicate_scopes(
        self, session: Session, seeded_materials
    ):
        ingest_eurlex(session, fetch_summaries=False)
        ingest_eurlex(session, fetch_summaries=False)

        expected_mat = sum(len(r["material_scopes"]) for r in BATTERY_REGULATIONS)
        expected_geo = sum(len(r["geography_scopes"]) for r in BATTERY_REGULATIONS)
        assert (
            len(session.scalars(select(RegulationMaterialScope)).all())
            == expected_mat
        )
        assert (
            len(session.scalars(select(RegulationGeographyScope)).all())
            == expected_geo
        )


# ---------------------------------------------------------------------------
# ingest_eurlex — summary backfill on existing rows
# ---------------------------------------------------------------------------

class TestIngestEurlexSummaryBackfill:
    def test_backfills_missing_summary_when_fetch_enabled(
        self, session: Session, seeded_materials
    ):
        ingest_eurlex(session, fetch_summaries=False)

        with patch(
            "app.services.ingestion.eurlex.fetch_eurlex_summary",
            return_value="Backfilled preamble text.",
        ):
            result = ingest_eurlex(session, fetch_summaries=True)

        assert result["updated"] == len(BATTERY_REGULATIONS)
        assert result["inserted"] == 0
        for r in session.scalars(select(Regulation)).all():
            assert r.summary == "Backfilled preamble text."

    def test_does_not_overwrite_existing_summary(
        self, session: Session, seeded_materials
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

        # No fetch should have been issued since every summary was already populated.
        fetch_mock.assert_not_called()
        assert result["updated"] == 0
        assert result["skipped"] == len(BATTERY_REGULATIONS)
        for r in session.scalars(select(Regulation)).all():
            assert r.summary == "Original preamble."

    def test_failed_summary_fetch_does_not_break_run(
        self, session: Session, seeded_materials
    ):
        with patch(
            "app.services.ingestion.eurlex.fetch_eurlex_summary",
            return_value=None,
        ):
            result = ingest_eurlex(session, fetch_summaries=True)

        assert result["inserted"] == len(BATTERY_REGULATIONS)
        for r in session.scalars(select(Regulation)).all():
            assert r.summary is None


# ---------------------------------------------------------------------------
# Manifest data validation
# ---------------------------------------------------------------------------

class TestManifestData:
    def test_unique_regulation_keys(self):
        keys = [r["regulation_key"] for r in BATTERY_REGULATIONS]
        assert len(keys) == len(set(keys)), "regulation_key values must be unique"

    def test_unique_celex_numbers(self):
        celex = [r["celex"] for r in BATTERY_REGULATIONS]
        assert len(celex) == len(set(celex)), "CELEX numbers must be unique"

    def test_required_keys_present(self):
        required = {
            "regulation_key", "celex", "title", "issuing_body", "geography",
            "status", "publication_date", "effective_date", "policy_theme",
            "material_scopes", "geography_scopes",
        }
        for r in BATTERY_REGULATIONS:
            missing = required - set(r.keys())
            assert not missing, f"{r.get('regulation_key')} missing keys: {missing}"

    def test_publication_date_before_effective_date(self):
        for r in BATTERY_REGULATIONS:
            assert r["publication_date"] <= r["effective_date"], (
                f"{r['regulation_key']}: publication_date must precede effective_date"
            )

    def test_url_template_renders(self):
        for r in BATTERY_REGULATIONS:
            url = EURLEX_HTML_URL.format(celex=r["celex"])
            assert url.startswith("https://eur-lex.europa.eu/")
            assert f"CELEX:{r['celex']}" in url

    def test_scope_types_valid(self):
        valid_material = {"covered", "restricted", "banned", "disclosure_required"}
        valid_geography = {"jurisdiction", "origin_country", "targeted_country"}
        for r in BATTERY_REGULATIONS:
            for _, scope_type in r["material_scopes"]:
                assert scope_type in valid_material, (
                    f"{r['regulation_key']}: invalid material scope_type {scope_type!r}"
                )
            for _, scope_type in r["geography_scopes"]:
                assert scope_type in valid_geography, (
                    f"{r['regulation_key']}: invalid geography scope_type {scope_type!r}"
                )
