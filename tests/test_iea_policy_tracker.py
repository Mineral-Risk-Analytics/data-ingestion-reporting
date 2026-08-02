"""Tests for app/services/ingestion/iea_policy_tracker.py.

Written 2026-07-31 alongside the suggestion inversion (triage plan Phase 1)
— the ingester previously had no test coverage at all.  File parsing is
exercised through a real temp CSV; the DB side uses the same in-memory
SQLite pattern as tests/test_gta.py.  The Haiku material classifier is a
no-op when ANTHROPIC_API_KEY is absent, which these tests rely on.
"""

from __future__ import annotations

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
from app.services.ingestion.iea_policy_tracker import (
    _category_is_fallback,
    _derive_category,
    _derive_direction,
    ingest_policy_tracker,
)


def _patch_sqlite_jsonb() -> None:
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler  # type: ignore[import]

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]


_TABLES = (
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
    Base.metadata.create_all(engine, tables=list(_TABLES))
    Session_ = sessionmaker(bind=engine)
    s = Session_()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def seeded(session: Session, monkeypatch) -> dict[str, int]:
    # The classifier must be a no-op — never call out in tests.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    session.add(Country(iso2="CA", iso3="CAN", name="Canada", common_names=["canada"]))
    session.add(Country(iso2="CN", iso3="CHN", name="China", common_names=["china"]))
    out: dict[str, int] = {}
    for name, hs in [("Lithium", "2836"), ("Natural Graphite", "2504")]:
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


_CSV_HEADER = "title,countries,description,status,year,technologies,policyType,jurisdiction\n"


def _csv_file(tmp_path, rows: str):
    p = tmp_path / "iea_pams.csv"
    p.write_text(_CSV_HEADER + rows)
    return str(p)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestDirectionAndCategoryHelpers:
    def test_restrictive_wins_when_a_restrictive_phrase_matches(self):
        # Real IEA policyType name — contains "export control".
        assert _derive_direction(["Export controls and restrictions"]) == "restrictive"

    def test_known_gap_compound_phrase_not_caught(self):
        # Documents ACTUAL behaviour: the taxonomy comment claims
        # "export financing restrictions" cannot land supportive, but the
        # restrictive keywords are exact substrings ("export restriction",
        # not "restriction") and this phrase interleaves "financing" — so
        # 'financ' wins.  Not a real IEA policyType name; noted in the IEA
        # categorization audit.  If this test starts failing because the
        # keyword list was tightened, delete it with a clear conscience.
        assert _derive_direction(["Export financing restrictions"]) == "supportive"

    def test_financing_is_supportive(self):
        assert _derive_direction(["Financing"]) == "supportive"

    def test_lists_are_neutral(self):
        assert _derive_direction(["Strategic mineral lists"]) == "neutral"

    def test_harmonised_category_map(self):
        # 2026-07-31 harmonisation decisions (Nicole):
        # subsidy family → financial_pressure (GTA convention)
        assert _derive_category(["Financing"]) == "financial_pressure"
        assert _derive_category(["Tax incentives"]) == "financial_pressure"
        # trade measures → geopolitical_trade, and they OUTRANK financing
        assert _derive_category(["Export controls and restrictions"]) == "geopolitical_trade"
        assert _derive_category(["Financing", "Export controls and restrictions"]) == "geopolitical_trade"
        # neutral machinery → regulatory_compliance (no more bare-"strategic" drag)
        assert _derive_category(["Strategic plans"]) == "regulatory_compliance"
        assert _derive_category(["Recycling support", "Strategic plans"]) == "regulatory_compliance"
        # international arrangements → geopolitical_trade (was fallback)
        assert _derive_category(["International arrangements"]) == "geopolitical_trade"
        assert _category_is_fallback(["International arrangements"]) is False
        # FDI keeps GTA's geopolitical convention despite containing "investment"
        assert _derive_category(["Foreign direct investment (FDI)"]) == "geopolitical_trade"

    def test_import_controls_and_tariffs_are_restrictive(self):
        # 2026-07-31 fix: previously "Import controls and restrictions" +
        # a recycling tag derived SUPPORTIVE, and tariffs derived neutral.
        assert _derive_direction(["Import controls and restrictions", "Minerals Recycling"]) == "restrictive"
        assert _derive_direction(["Tariffs and duties"]) == "restrictive"

    def test_fallback_detection_is_not_value_based(self):
        # "Recycling regulation" maps to regulatory_compliance via keyword —
        # same VALUE as the default, but it is NOT a fallback.
        assert _derive_category(["Recycling incentives"]) == "regulatory_compliance"
        assert _category_is_fallback(["Recycling incentives"]) is False
        # No keyword at all → fallback.
        assert _derive_category(["Mystery policy family"]) == "regulatory_compliance"
        assert _category_is_fallback(["Mystery policy family"]) is True


# ---------------------------------------------------------------------------
# Ingest — suggestion inversion (2026-07-31)
# ---------------------------------------------------------------------------

class TestIngestSuggestionInversion:
    def test_supportive_policy_lands_display_only_with_suggestions(
        self, session, seeded, tmp_path
    ):
        path = _csv_file(
            tmp_path,
            'CA lithium tax credit,CAN,"Tax credit supporting lithium refining capacity.",'
            'implemented,2024,,"[{""name"":""Financing""}]",Canada\n',
        )
        result = ingest_policy_tracker(session, path)
        assert result["failed"] == 0

        ev = session.scalars(select(RiskEvent)).one()
        # The machine proposes...
        assert ev.direction == "supportive"
        assert ev.suggested_category == "financial_pressure"  # subsidy family, GTA-harmonised (2026-07-31)
        assert ev.event_subtype == "POSITIVE_POLICY"
        # ...and does not dispose.
        assert ev.primary_category is None
        assert ev.triage_status == "display_only"
        assert ev.metadata_json["triage_route"] == "auto_display_only_supportive"
        assert ev.metadata_json["category_mapping"] == "explicit"
        assert ev.metadata_json["positive_policy"] is True

    def test_restrictive_policy_stays_pending(self, session, seeded, tmp_path):
        path = _csv_file(
            tmp_path,
            'CN graphite export controls,CHN,"Export controls on graphite products.",'
            'in force,2024,,"[{""name"":""Export controls and restrictions""}]",China\n',
        )
        ingest_policy_tracker(session, path)

        ev = session.scalars(select(RiskEvent)).one()
        assert ev.direction == "restrictive"
        assert ev.triage_status == "pending_triage"
        assert ev.primary_category is None
        # Restrictive IEA copies stay informational (no subtype) — GTA is
        # the authoritative risk-raising source for trade measures.
        assert ev.event_subtype is None
        assert ev.metadata_json["positive_policy"] is False
        assert ev.metadata_json["triage_route"] is None

    def test_fallback_category_marked(self, session, seeded, tmp_path):
        path = _csv_file(
            tmp_path,
            'CN lithium mystery policy,CHN,"Lithium-related measure of unknown family.",'
            'announced,2024,,"[{""name"":""Unrecognised policy family""}]",China\n',
        )
        ingest_policy_tracker(session, path)

        ev = session.scalars(select(RiskEvent)).one()
        assert ev.suggested_category == "regulatory_compliance"  # default
        assert ev.metadata_json["category_mapping"] == "fallback"

    def test_material_links_are_suggested(self, session, seeded, tmp_path):
        path = _csv_file(
            tmp_path,
            'CA lithium tax credit,CAN,"Tax credit supporting lithium refining.",'
            'implemented,2024,,"[{""name"":""Financing""}]",Canada\n',
        )
        ingest_policy_tracker(session, path)

        links = session.scalars(select(RiskEventMaterial)).all()
        assert links, "expected the lithium mention to attribute a material"
        for link in links:
            assert link.status == "suggested"
        ev = session.scalars(select(RiskEvent)).one()
        assert ev.metadata_json["n_materials"] == len({l.material_id for l in links})
