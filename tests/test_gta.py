"""Tests for app/services/ingestion/gta.py.

The download is fully mocked — no real HTTP requests are issued. The ingest
tests use an in-memory SQLite session containing only the tables the GTA
ingester touches, sidestepping the Postgres-only ARRAY/JSONB columns
declared elsewhere in the ORM.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models.country import Country
from app.models.documents import SourceDocument
from app.models.regulatory import (
    RiskEvent,
    RiskEventGeography,
    RiskEventHsMapping,
    RiskEventMaterial,
)
from app.models.source import Source
from app.models.supply import HsCodeMaterialMapping, Material
from app.services.ingestion.gta import (
    BATTERY_HS_PREFIXES,
    GTA_INTERVENTION_CATEGORY_MAP,
    _build_column_map,
    _content_hash,
    _matches_prefix,
    _parse_date,
    _resolve_country,
    _severity_for,
    _split_hs_codes,
    ingest_gta,
    parse_gta_csv,
)


# ---------------------------------------------------------------------------
# In-memory SQLite session — only the tables the ingester touches
# ---------------------------------------------------------------------------

def _patch_sqlite_jsonb() -> None:
    """Render JSONB as JSON in SQLite DDL. Mirrors tests/scoring/conftest.py."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler  # type: ignore[import]

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]


_GTA_TABLES = (
    Country.__table__,
    Source.__table__,
    SourceDocument.__table__,
    Material.__table__,
    HsCodeMaterialMapping.__table__,
    RiskEvent.__table__,
    RiskEventGeography.__table__,
    RiskEventHsMapping.__table__,
    RiskEventMaterial.__table__,
)


@pytest.fixture()
def session() -> Session:
    _patch_sqlite_jsonb()
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine, tables=list(_GTA_TABLES))
    Session_ = sessionmaker(bind=engine)
    s = Session_()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def seeded_materials_and_hs(session: Session) -> dict[str, int]:
    """Insert materials + HS-code mappings used by the fixture CSV."""
    rows = [
        ("Natural Graphite", "2504"),
        ("Nickel", "2604"),
        ("Lithium", "2836"),
    ]
    out: dict[str, int] = {}
    for name, hs in rows:
        m = Material(canonical_name=name, category="metal")
        session.add(m)
        session.flush()
        out[name] = m.id
        session.add(
            HsCodeMaterialMapping(
                hs_code_prefix=hs,
                material_id=m.id,
                description=f"{name} HS prefix {hs}",
                confidence=1.0,
                digit_count=len(hs),
                market_scope="global",
            )
        )
    session.commit()
    return out


# ---------------------------------------------------------------------------
# CSV fixture builder
# ---------------------------------------------------------------------------

# Five rows exercising every parser branch:
#   1. Red + matching HS (graphite, China export licensing)         → KEPT
#   2. Red + matching HS (nickel, Indonesia export ban)             → KEPT
#   3. Red + non-matching HS (cotton, China)                        → SKIPPED
#   4. Amber + matching HS (lithium, Australia)                     → SKIPPED (Amber)
#   5. Red + matching HS but year 2010                              → SKIPPED (since_year)
#
# Plus a sixth row with an unmappable country to validate the
# unresolved-country path (still kept but ``implementing_iso2`` is None).
_GTA_CSV_FIXTURE = """\
id,title,date_announced,date_implemented,date_removed,implementing_jurisdiction,gta_evaluation,intervention_type,affected_hs_codes,description,in_force
1001,China graphite export licensing,2023-10-20,2023-12-01,,China,Red,Export licensing requirements,250410;250490,China requires export licences for natural and synthetic graphite anodes used in EV batteries.,true
1002,Indonesia nickel ore export ban,2019-09-01,2020-01-01,,Indonesia,Red,Export bans,260400,Indonesia bans the export of unprocessed nickel ore to compel domestic refining.,true
1003,China cotton import quota,2021-05-10,2021-06-01,,China,Red,Import quota,520100,Tariff-rate quota on raw cotton imports — irrelevant for batteries.,true
1004,Australia lithium royalty review,2024-02-10,,,Australia,Amber,Export taxes,283691,Pending royalty review on lithium carbonate exports — not implemented.,false
1005,DRC cobalt historical export tax,2010-04-01,2010-07-01,2012-01-01,Democratic Republic of the Congo,Red,Export taxes,810520,Historic export tax on cobalt — pre-window.,false
1006,Atlantis lithium tariff,2022-03-01,2022-04-15,,Atlantis,Red,Export taxes,283691,Tariff on lithium carbonate exports from a country not in our ISO2 map.,true
"""


def _csv_bytes() -> bytes:
    return _GTA_CSV_FIXTURE.encode("utf-8")


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------

class TestParseDate:
    def test_full_date(self):
        d = _parse_date("2023-10-20")
        assert d is not None
        assert d.year == 2023 and d.month == 10 and d.day == 20
        assert d.tzinfo is not None

    def test_year_month_only(self):
        d = _parse_date("2023-10")
        assert d is not None
        assert d.year == 2023 and d.month == 10 and d.day == 1

    def test_slash_separator(self):
        d = _parse_date("2023/10/20")
        assert d is not None
        assert d.month == 10

    def test_empty_returns_none(self):
        assert _parse_date("") is None
        assert _parse_date("   ") is None

    def test_garbage_returns_none(self):
        assert _parse_date("not a date") is None


class TestSplitHsCodes:
    def test_semicolon_separator(self):
        assert _split_hs_codes("250410;260400") == ["250410", "260400"]

    def test_comma_separator(self):
        assert _split_hs_codes("250410,260400") == ["250410", "260400"]

    def test_drops_short_codes_as_cpc_not_hs(self):
        """Fewer than 6 digits is treated as CPC/malformed — not zero-padded to HS-6."""
        assert _split_hs_codes("2504") == []

    def test_strips_dots_and_keeps_digits(self):
        assert _split_hs_codes("26.04.00") == ["260400"]

    def test_empty(self):
        assert _split_hs_codes("") == []


class TestMatchesPrefix:
    def test_match(self):
        assert _matches_prefix("250410", BATTERY_HS_PREFIXES) is True

    def test_no_match(self):
        assert _matches_prefix("520100", BATTERY_HS_PREFIXES) is False


class TestResolveCountry:
    def test_iso2_passthrough(self):
        assert _resolve_country("CN") == "CN"
        assert _resolve_country("cn") == "CN"

    def test_full_name_mapped(self):
        assert _resolve_country("China") == "CN"
        assert _resolve_country("Indonesia") == "ID"
        assert _resolve_country("Democratic Republic of the Congo") == "CD"

    def test_unknown_returns_none(self):
        assert _resolve_country("Atlantis") is None

    def test_empty_returns_none(self):
        assert _resolve_country("") is None
        assert _resolve_country("   ") is None


class TestSeverityFor:
    def test_export_ban_high(self):
        assert _severity_for("Export bans", in_force=True) == 0.9

    def test_active_non_ban(self):
        assert _severity_for("Export taxes", in_force=True) == 0.7

    def test_inactive(self):
        assert _severity_for("Export taxes", in_force=False) == 0.3


class TestContentHash:
    def test_deterministic(self):
        from datetime import datetime, timezone

        d = datetime(2023, 12, 1, tzinfo=timezone.utc)
        h1 = _content_hash("title", "summary", d)
        h2 = _content_hash("title", "summary", d)
        assert h1 == h2
        assert len(h1) == 64

    def test_changes_with_inputs(self):
        from datetime import datetime, timezone

        d = datetime(2023, 12, 1, tzinfo=timezone.utc)
        assert _content_hash("a", "b", d) != _content_hash("a", "c", d)


class TestBuildColumnMap:
    def test_resolves_canonical_columns(self):
        headers = [
            "id",
            "title",
            "date_announced",
            "date_implemented",
            "date_removed",
            "implementing_jurisdiction",
            "gta_evaluation",
            "intervention_type",
            "affected_hs_codes",
            "description",
            "in_force",
        ]
        col = _build_column_map(headers)
        for k in (
            "id",
            "title",
            "implementing_jurisdiction",
            "gta_evaluation",
            "intervention_type",
            "affected_hs_codes",
        ):
            assert col[k] == k

    def test_resolves_alias_columns(self):
        headers = [
            "intervention_id",
            "intervention_title",
            "implementing_country",
            "evaluation",
            "instrument",
            "hs_codes",
        ]
        col = _build_column_map(headers)
        assert col["id"] == "intervention_id"
        assert col["title"] == "intervention_title"
        assert col["implementing_jurisdiction"] == "implementing_country"
        assert col["gta_evaluation"] == "evaluation"
        assert col["intervention_type"] == "instrument"
        assert col["affected_hs_codes"] == "hs_codes"

    def test_raises_on_missing_required(self):
        headers = ["id", "title"]  # missing several required columns
        with pytest.raises(ValueError) as exc_info:
            _build_column_map(headers)
        assert "missing required column" in str(exc_info.value)


# ---------------------------------------------------------------------------
# parse_gta_csv
# ---------------------------------------------------------------------------

class TestParseGtaCsv:
    def test_keeps_only_red_and_matching_hs(self):
        results = parse_gta_csv(_csv_bytes(), since_year=2018)
        kept_ids = {r["gta_id"] for r in results}
        # 1001 (graphite), 1002 (nickel), 1006 (lithium, unknown country) — all Red+match+within window.
        # 1003 cotton skipped (no HS match), 1004 Amber skipped, 1005 too old skipped.
        assert kept_ids == {1001, 1002, 1006}

    def test_export_ban_maps_to_geopolitical_trade(self):
        results = parse_gta_csv(_csv_bytes(), since_year=2018)
        nickel = next(r for r in results if r["gta_id"] == 1002)
        assert nickel["intervention_type"] == "Export bans"
        assert nickel["risk_category"] == "geopolitical_trade"
        # Sanity check the mapping table itself.
        assert GTA_INTERVENTION_CATEGORY_MAP["Export bans"] == "geopolitical_trade"

    def test_country_resolved_for_known_jurisdictions(self):
        results = parse_gta_csv(_csv_bytes(), since_year=2018)
        china = next(r for r in results if r["gta_id"] == 1001)
        indonesia = next(r for r in results if r["gta_id"] == 1002)
        assert china["implementing_iso2"] == "CN"
        assert indonesia["implementing_iso2"] == "ID"

    def test_unresolvable_country_returns_none_without_failing(self):
        results = parse_gta_csv(_csv_bytes(), since_year=2018)
        atlantis = next(r for r in results if r["gta_id"] == 1006)
        assert atlantis["implementing_iso2"] is None

    def test_matched_hs_codes_filtered_to_prefixes(self):
        results = parse_gta_csv(_csv_bytes(), since_year=2018)
        graphite = next(r for r in results if r["gta_id"] == 1001)
        # Both 250410 and 250490 begin with 2504 → both kept.
        assert sorted(graphite["matched_hs_codes"]) == ["250410", "250490"]
        nickel = next(r for r in results if r["gta_id"] == 1002)
        assert nickel["matched_hs_codes"] == ["260400"]

    def test_event_date_prefers_implemented_over_announced(self):
        results = parse_gta_csv(_csv_bytes(), since_year=2018)
        graphite = next(r for r in results if r["gta_id"] == 1001)
        # Implementation date is 2023-12-01.
        assert graphite["event_date"].year == 2023
        assert graphite["event_date"].month == 12
        assert graphite["event_date"].day == 1

    def test_in_force_parsed(self):
        results = parse_gta_csv(_csv_bytes(), since_year=2018)
        graphite = next(r for r in results if r["gta_id"] == 1001)
        atlantis = next(r for r in results if r["gta_id"] == 1006)
        assert graphite["in_force"] is True
        assert atlantis["in_force"] is True

    def test_long_summary_survives_parse(self):
        # 2026-07-30: the old 500-char cap silently destroyed source content
        # (2,497 of 2,575 production GTA events were sliced mid-word).  A
        # realistic long description must now survive intact; only the 8000
        # sanity bound truncates.
        long_desc = "x" * 1000
        csv_text = (
            "id,title,date_announced,date_implemented,date_removed,"
            "implementing_jurisdiction,gta_evaluation,intervention_type,"
            "affected_hs_codes,description,in_force\n"
            f"2000,Long desc,2023-01-01,2023-02-01,,China,Red,Export bans,260400,{long_desc},true\n"
        )
        results = parse_gta_csv(csv_text.encode(), since_year=2018)
        assert len(results) == 1
        assert len(results[0]["summary"]) == 1000

    def test_summary_sanity_bound_at_8000(self):
        huge_desc = "y" * 9000
        csv_text = (
            "id,title,date_announced,date_implemented,date_removed,"
            "implementing_jurisdiction,gta_evaluation,intervention_type,"
            "affected_hs_codes,description,in_force\n"
            f"2001,Huge desc,2023-01-01,2023-02-01,,China,Red,Export bans,260400,{huge_desc},true\n"
        )
        results = parse_gta_csv(csv_text.encode(), since_year=2018)
        assert len(results) == 1
        assert len(results[0]["summary"]) == 8000

    def test_future_implementation_reanchored_to_announcement(self):
        # 2026-07-30: a future implementation date must not become event_date
        # (trailing evidence windows would admit it forever, at max decay
        # weight).  With a past announcement, the event is dated to the
        # announcement and the forward date is preserved.
        csv_text = (
            "id,title,date_announced,date_implemented,date_removed,"
            "implementing_jurisdiction,gta_evaluation,intervention_type,"
            "affected_hs_codes,description,in_force\n"
            "2002,Announced future ban,2024-01-15,2099-01-01,,China,Red,Export bans,260400,Ban takes effect 2099.,false\n"
        )
        results = parse_gta_csv(csv_text.encode(), since_year=2018)
        assert len(results) == 1
        r = results[0]
        assert r["event_date"].year == 2024
        assert r["scheduled_implementation_date"] is not None
        assert r["scheduled_implementation_date"].year == 2099

    def test_entirely_future_row_skipped(self):
        # Announced AND implemented in the future → nothing has happened yet;
        # the row must not enter the database at all.
        csv_text = (
            "id,title,date_announced,date_implemented,date_removed,"
            "implementing_jurisdiction,gta_evaluation,intervention_type,"
            "affected_hs_codes,description,in_force\n"
            "2003,Entirely future ban,2098-06-01,2099-01-01,,China,Red,Export bans,260400,Nothing yet.,false\n"
        )
        results = parse_gta_csv(csv_text.encode(), since_year=2018)
        assert results == []


# ---------------------------------------------------------------------------
# ingest_gta — end-to-end with mocked download
# ---------------------------------------------------------------------------

class TestIngestGta:
    def test_inserts_red_matching_events(self, session, seeded_materials_and_hs):
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=_csv_bytes(),
        ):
            result = ingest_gta(session, since_year=2018)

        # 1001, 1002 → 2 events. Row 1006 (Atlantis) has no resolved country and
        # is skipped (see ``skipped_unknown_country``).
        assert result["inserted"] == 2
        assert result["downloaded_rows"] == 3
        assert result["skipped_existing"] == 0
        assert result["skipped_unknown_country"] == 1

        events = session.scalars(select(RiskEvent)).all()
        assert len(events) == 2
        for e in events:
            assert e.verified is False
            assert e.confidence_score == 0.9
            assert e.risk_categories_json == ["geopolitical_trade"]
            assert e.metadata_json is not None
            assert e.metadata_json.get("source_url")
            assert "gta_hs_codes" in e.metadata_json

    def test_export_ban_severity_higher(self, session, seeded_materials_and_hs):
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=_csv_bytes(),
        ):
            ingest_gta(session, since_year=2018)

        nickel_event = session.scalar(
            select(RiskEvent).where(
                RiskEvent.metadata_json["gta_id"].as_string() == "1002"
            )
        )
        # SQLite JSONB ``->>`` may not work; fall back to scanning if needed.
        if nickel_event is None:
            nickel_event = next(
                e
                for e in session.scalars(select(RiskEvent))
                if (e.metadata_json or {}).get("gta_id") == 1002
            )
        assert nickel_event.severity_score == 0.9

    def test_geography_links_only_for_resolved_countries(
        self, session, seeded_materials_and_hs
    ):
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=_csv_bytes(),
        ):
            result = ingest_gta(session, since_year=2018)

        # China (1001) + Indonesia (1002) → 2 geography links. Atlantis (1006)
        # is unresolved and contributes no geography row.
        assert result["geography_links"] == 2
        assert result["skipped_unknown_country"] == 1

        geos = session.scalars(select(RiskEventGeography)).all()
        assert {g.country_code for g in geos} == {"CN", "ID"}
        for g in geos:
            assert g.geography_context == "primary"
            assert g.relevance_score == 1.0

    def test_material_links_via_hs_code(self, session, seeded_materials_and_hs):
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=_csv_bytes(),
        ):
            result = ingest_gta(session, since_year=2018)

        # graphite (250410+250490 → both match prefix 2504 → 1 material link)
        # nickel  (260400 → 1 material link)
        # Atlantis lithium row is not ingested (unresolved jurisdiction).
        assert result["material_links"] == 2

        links = session.scalars(select(RiskEventMaterial)).all()
        for link in links:
            assert link.match_reason == "hs_code"
            assert link.relevance_score == 0.9

    def test_creates_source_and_per_intervention_documents(
        self, session, seeded_materials_and_hs
    ):
        # 2026-07-30: one per-run bulk document PLUS one document per
        # inserted intervention, each carrying its permalink.  Before this,
        # every event pointed at the run document (url=NULL) and rendered
        # with no source link.
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=_csv_bytes(),
        ):
            ingest_gta(session, since_year=2018)

        sources = session.scalars(select(Source)).all()
        assert len(sources) == 1
        assert sources[0].name == "Global Trade Alert"
        assert sources[0].source_type == "gta"

        docs = session.scalars(select(SourceDocument)).all()
        run_docs = [d for d in docs if d.external_id.startswith("gta_bulk_csv_")]
        intervention_docs = [
            d for d in docs if d.external_id.startswith("gta_intervention_")
        ]
        assert len(run_docs) == 1
        assert run_docs[0].document_type == "trade_policy_database"
        # 2 inserted events → 2 intervention documents.
        assert len(intervention_docs) == 2
        for d in intervention_docs:
            assert d.url is not None
            assert d.url.startswith("https://globaltradealert.org/intervention/")
            assert d.document_type == "trade_intervention"

    def test_events_point_at_permalink_documents(
        self, session, seeded_materials_and_hs
    ):
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=_csv_bytes(),
        ):
            ingest_gta(session, since_year=2018)

        events = session.scalars(select(RiskEvent)).all()
        assert len(events) == 2
        for e in events:
            doc = session.get(SourceDocument, e.source_document_id)
            gta_id = (e.metadata_json or {}).get("gta_id")
            assert doc.url == f"https://globaltradealert.org/intervention/{gta_id}"
            assert (e.metadata_json or {}).get("permalink") == doc.url
            # source_url now records the event's own permalink, with the bulk
            # download URL preserved separately.
            assert (e.metadata_json or {}).get("source_url") == doc.url
            assert "bulk_source_url" in (e.metadata_json or {})

    def test_idempotent_second_run_skips_all(
        self, session, seeded_materials_and_hs
    ):
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=_csv_bytes(),
        ):
            ingest_gta(session, since_year=2018)
            result2 = ingest_gta(session, since_year=2018)

        assert result2["inserted"] == 0
        assert result2["skipped_existing"] + result2["refreshed_existing"] == 2
        assert result2["material_links"] == 0
        assert result2["geography_links"] == 0

        # No duplication.
        assert len(session.scalars(select(RiskEvent)).all()) == 2
        assert len(session.scalars(select(RiskEventMaterial)).all()) == 2
        assert len(session.scalars(select(RiskEventGeography)).all()) == 2

    def test_reingest_repairs_truncated_summary_without_duplicating(
        self, session, seeded_materials_and_hs
    ):
        # 2026-07-30: the repair path.  First ingest stores a short (e.g.
        # previously truncated) summary; a later ingest carrying the full
        # text must UPDATE the same row, not insert a duplicate — identity
        # is the GTA intervention id, not a hash of the text.
        short_desc = "Short version."
        long_desc = "Short version. " + "Much longer restored text. " * 30
        header = (
            "id,title,date_announced,date_implemented,date_removed,"
            "implementing_jurisdiction,gta_evaluation,intervention_type,"
            "affected_hs_codes,description,in_force\n"
        )
        row = "3000,Repairable event,2023-01-01,2023-02-01,,China,Red,Export bans,260400,{desc},true\n"
        csv_short = (header + row.format(desc=short_desc)).encode()
        csv_long = (header + row.format(desc=long_desc)).encode()

        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=csv_short,
        ):
            r1 = ingest_gta(session, since_year=2018)
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=csv_long,
        ):
            r2 = ingest_gta(session, since_year=2018)

        assert r1["inserted"] == 1
        assert r2["inserted"] == 0
        assert r2["refreshed_existing"] == 1
        assert r2["refresh_field_counts"].get("summary") == 1

        events = session.scalars(select(RiskEvent)).all()
        assert len(events) == 1
        assert events[0].summary == long_desc.strip()

        # And a regressed (shorter) feed must NOT destroy the longer text.
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=csv_short,
        ):
            r3 = ingest_gta(session, since_year=2018)
        assert r3["inserted"] == 0
        events = session.scalars(select(RiskEvent)).all()
        assert len(events) == 1
        assert events[0].summary == long_desc.strip()

    def test_refresh_never_touches_curated_fields(
        self, session, seeded_materials_and_hs
    ):
        # A human-set verified flag / category / severity must survive a
        # repair re-ingest untouched.
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=_csv_bytes(),
        ):
            ingest_gta(session, since_year=2018)

        ev = session.scalars(select(RiskEvent)).first()
        ev.verified = True
        ev.severity_score = 0.123
        ev.confidence_score = 0.456
        session.commit()
        ev_id = ev.id

        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=_csv_bytes(),
        ):
            ingest_gta(session, since_year=2018)

        ev = session.get(RiskEvent, ev_id)
        assert ev.verified is True
        assert ev.severity_score == 0.123
        assert ev.confidence_score == 0.456

    def test_dry_run_writes_nothing(self, session, seeded_materials_and_hs):
        # 2026-07-30: --dry-run reports the blast radius and rolls back.
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=_csv_bytes(),
        ):
            result = ingest_gta(session, since_year=2018, dry_run=True)

        assert result["dry_run"] == 1
        assert result["inserted"] == 2  # would-be inserts, still counted
        assert session.scalars(select(RiskEvent)).all() == []
        assert session.scalars(select(RiskEventMaterial)).all() == []
        assert session.scalars(select(SourceDocument)).all() == []

    def test_dry_run_reports_refreshes_without_writing(
        self, session, seeded_materials_and_hs
    ):
        short_desc = "Short version."
        long_desc = "Short version. " + "Restored. " * 40
        header = (
            "id,title,date_announced,date_implemented,date_removed,"
            "implementing_jurisdiction,gta_evaluation,intervention_type,"
            "affected_hs_codes,description,in_force\n"
        )
        row = "3001,Dry-run repair probe,2023-01-01,2023-02-01,,China,Red,Export bans,260400,{desc},true\n"

        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=(header + row.format(desc=short_desc)).encode(),
        ):
            ingest_gta(session, since_year=2018)
        stored = session.scalars(select(RiskEvent)).first().summary

        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=(header + row.format(desc=long_desc)).encode(),
        ):
            result = ingest_gta(session, since_year=2018, dry_run=True)

        assert result["dry_run"] == 1
        assert result["refreshed_existing"] == 1
        assert result["refresh_field_counts"].get("summary") == 1
        # Nothing actually changed.
        assert session.scalars(select(RiskEvent)).first().summary == stored

    def test_since_year_filter_applied(self, session, seeded_materials_and_hs):
        # since_year=2024 excludes everything in the fixture.
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=_csv_bytes(),
        ):
            result = ingest_gta(session, since_year=2024)

        assert result["inserted"] == 0
        assert result["downloaded_rows"] == 0


# ---------------------------------------------------------------------------
# --api-raw-cache (2026-07-31)
# ---------------------------------------------------------------------------

class TestApiRawCache:
    def _api_row(self):
        return {
            "intervention_id":         77001,
            "state_act_id":            88001,
            "state_act_title":         "China: cached export ban on graphite",
            "intervention_description": [{"text": "<p>Cached description</p>"}],
            "gta_evaluation":           "Red",
            "implementing_jurisdictions": [{"id": 156, "name": "China", "iso": "CHN"}],
            "affected_jurisdictions":   [],
            "intervention_type":        "Export ban",
            "mast_chapter":             "P1",
            "affected_sectors":         [],
            "affected_products":        [{"product_id": 250410, "name": "Graphite"}],
            "date_announced":           "2023-06-01",
            "date_implemented":         "2023-07-01",
            "is_in_force":              True,
            "is_official_source":       True,
        }

    def test_cache_hit_skips_api_and_needs_no_key(
        self, session, seeded_materials_and_hs, tmp_path, monkeypatch
    ):
        """A pre-existing cache file must be used INSTEAD of calling the API.

        GTA meters by records pulled per month; the cache exists so a
        dry-run + real-run sequence pulls once, and so a failed parse does
        not throw away a completed pull.  On a cache hit no API key is
        required at all.
        """
        import json as _json

        from app.services.ingestion import gta as gta_mod

        cache = tmp_path / "gta_raw.json"
        cache.write_text(_json.dumps([self._api_row()]))

        def _must_not_fetch(*a, **k):
            raise AssertionError("API was called despite a cache hit")

        monkeypatch.setattr(gta_mod, "fetch_gta_interventions_api", _must_not_fetch)

        result = ingest_gta(
            session, since_year=2018, use_api=True, api_raw_cache=str(cache)
        )
        assert result["inserted"] == 1

    def test_cache_written_after_successful_fetch(
        self, session, seeded_materials_and_hs, tmp_path, monkeypatch
    ):
        import json as _json

        from app.services.ingestion import gta as gta_mod

        cache = tmp_path / "gta_raw.json"
        assert not cache.exists()

        monkeypatch.setattr(
            gta_mod, "fetch_gta_interventions_api", lambda **k: [self._api_row()]
        )

        class _FakeSettings:
            gta_api_key = "test-key"

        monkeypatch.setattr(
            "app.core.config.get_settings", lambda: _FakeSettings()
        )

        result = ingest_gta(
            session, since_year=2018, use_api=True, api_raw_cache=str(cache)
        )
        assert result["inserted"] == 1
        assert cache.exists()
        assert _json.loads(cache.read_text())[0]["intervention_id"] == 77001


# ---------------------------------------------------------------------------
# Suggestion inversion (2026-07-31, triage plan Phase 1)
# ---------------------------------------------------------------------------

class TestSuggestionInversion:
    def test_events_land_pending_with_suggestions_not_assignments(
        self, session, seeded_materials_and_hs
    ):
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=_csv_bytes(),
        ):
            ingest_gta(session, since_year=2018)

        events = session.scalars(select(RiskEvent)).all()
        assert len(events) == 2
        for e in events:
            # The machine proposes...
            assert e.suggested_category == "geopolitical_trade"
            assert e.direction == "restrictive"
            # ...and does NOT dispose: nothing scores until a human confirms.
            assert e.primary_category is None
            assert e.triage_status == "pending_triage"
            assert e.metadata_json.get("category_mapping") == "explicit"
            assert e.metadata_json.get("n_materials") == 1

    def test_supportive_subsidy_lands_display_only(
        self, session, seeded_materials_and_hs
    ):
        # Audit F1 (decided 2026-07-31): the production-subsidy family is
        # supply-SUPPORTIVE from a buyer's perspective and must not queue as
        # risk — it lands display_only (visible, reviewable, promotable).
        csv_text = (
            "id,title,date_announced,date_implemented,date_removed,"
            "implementing_jurisdiction,gta_evaluation,intervention_type,"
            "affected_hs_codes,description,in_force\n"
            "4000,US grant to graphite anode plant,2024-01-01,2024-02-01,,"
            "United States of America,Red,Financial grant,250410,"
            "Grant supporting domestic anode capacity.,true\n"
        )
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=csv_text.encode(),
        ):
            ingest_gta(session, since_year=2018)

        ev = session.scalars(select(RiskEvent)).one()
        assert ev.direction == "supportive"
        assert ev.suggested_category == "financial_pressure"
        assert ev.triage_status == "display_only"
        assert ev.primary_category is None
        assert ev.metadata_json.get("triage_route") == "auto_display_only_supportive"

    def test_material_links_are_suggested(self, session, seeded_materials_and_hs):
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=_csv_bytes(),
        ):
            ingest_gta(session, since_year=2018)

        links = session.scalars(select(RiskEventMaterial)).all()
        assert links
        for link in links:
            assert link.status == "suggested"

    def test_unknown_intervention_type_marked_fallback(
        self, session, seeded_materials_and_hs
    ):
        csv_text = (
            "id,title,date_announced,date_implemented,date_removed,"
            "implementing_jurisdiction,gta_evaluation,intervention_type,"
            "affected_hs_codes,description,in_force\n"
            "4001,Novel measure,2024-01-01,2024-02-01,,China,Red,"
            "Some brand new measure type,260400,Unmapped type.,true\n"
        )
        with patch(
            "app.services.ingestion.gta.download_gta_csv",
            return_value=csv_text.encode(),
        ):
            ingest_gta(session, since_year=2018)

        ev = session.scalars(select(RiskEvent)).one()
        # Falls back to the default category — but says so.
        assert ev.suggested_category == "geopolitical_trade"
        assert ev.metadata_json.get("category_mapping") == "fallback"
        # Unknown type → no subtype family → neutral direction, stays pending.
        assert ev.direction == "neutral"
        assert ev.triage_status == "pending_triage"
