"""Tests for the operational news ingester (2026-07-27).

Covers the contracts that matter:

1. Watchlist builds from CURATED facilities only (spec §6: MRDS never
   enters the operational pillar, not even as a watchlist).
2. ASX ticker parsing is deliberately narrow (no exchange-array guessing).
3. Keyword pre-filter + subtype suggestion behave.
4. THE INVARIANT: every created event is display-only — primary_category
   stays NULL despite risk_categories_json=["operational"] (i.e. the Build 1
   autofill listener must NOT promote candidates).
5. Anchoring: google_news → facility + geo + material links;
   edgar → company link, facility links only on name match, candidate
   facilities recorded in metadata otherwise.
6. Idempotency: re-running the same feed content creates nothing new.
7. Fail-soft: a dead feed yields zero items but never raises.

Network is never touched — httpx.MockTransport supplies canned payloads.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models import (
    Company,
    CompanyFacility,
    Facility,
    FacilityMaterialLink,
    Material,
    RiskEvent,
    RiskEventCompany,
    RiskEventFacility,
    RiskEventGeography,
    RiskEventMaterial,
)
from app.services.ingestion.ingest_operational_news import (
    EVENT_TYPE_CANDIDATE,
    _parse_asx_ticker,
    _suggest_subtype,
    _OPERATIONAL_FILTER_RE,
    build_watchlist,
    ingest_operational_news,
)


@pytest.fixture()
def session() -> Session:
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
def seeded(session: Session) -> dict:
    """One curated facility (with operator + material) and one MRDS facility."""
    mat = Material(canonical_name="Lithium", category="metal")
    session.add(mat)
    session.flush()

    op = Company(
        canonical_name="Pilbara Minerals",
        is_public=True,
        public_ticker="ASX:PLS",
        cik="1111111",
    )
    session.add(op)
    session.flush()

    fac = Facility(
        facility_type="mine",
        name="Pilgangoora",
        country="AU",
        status="operating",
        data_source="partner_facility_seed",
    )
    mrds_fac = Facility(
        facility_type="mine",
        name="Some MRDS Deposit",
        country="US",
        status="operating",
        data_source="mrds",
        mrds_dep_id="10087113",
    )
    session.add_all([fac, mrds_fac])
    session.flush()

    session.add(CompanyFacility(company_id=op.id, facility_id=fac.id))
    session.add(FacilityMaterialLink(
        facility_id=fac.id, material_id=mat.id, is_primary_product=True,
    ))
    session.flush()
    return {"material": mat, "company": op, "facility": fac, "mrds": mrds_fac}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestAsxTickerParsing:
    def test_prefix_and_suffix_forms(self):
        assert _parse_asx_ticker("ASX:LYC") == "LYC"
        assert _parse_asx_ticker("S32.AX") == "S32"
        assert _parse_asx_ticker("syr.ax") == "SYR"

    def test_non_asx_listings_rejected(self):
        # LSE / SSE / SZSE / TYO formats must NOT be mistaken for ASX —
        # a wrong ticker silently pulls another company's announcements.
        for t in ("GLEN.L", "603993.SS", "SZSE:002460", "TYO:5713", "VALE", None, ""):
            assert _parse_asx_ticker(t) is None, t


class TestKeywordClassification:
    def test_specific_phrases_win_over_generic(self):
        assert _suggest_subtype("X declares force majeure and halts output") == "force_majeure"
        assert _suggest_subtype("Mine placed on care and maintenance") == "care_and_maintenance"

    def test_suggestions(self):
        assert _suggest_subtype("Union begins strike at smelter") == "labor_strike"
        assert _suggest_subtype("Producer cuts FY27 production guidance") == "guidance_cut"
        assert _suggest_subtype("Flooding disrupts concentrate haulage") == "weather_supply_disruption"

    def test_non_operational_text_no_suggestion_and_filtered(self):
        text = "Company completes bond issuance and announces dividend"
        assert _suggest_subtype(text) is None
        assert not _OPERATIONAL_FILTER_RE.search(text)


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

class TestWatchlist:
    def test_curated_only_never_mrds(self, session, seeded):
        facilities, operators = build_watchlist(session)
        names = {f.name for f in facilities}
        assert "Pilgangoora" in names
        assert "Some MRDS Deposit" not in names  # spec §6: MRDS never scores
        assert len(operators) == 1
        assert operators[0].asx_ticker == "PLS"
        assert operators[0].cik == "1111111"

    def test_materials_and_operator_attached(self, session, seeded):
        facilities, _ = build_watchlist(session)
        fac = next(f for f in facilities if f.name == "Pilgangoora")
        assert len(fac.material_ids) == 1
        assert fac.operator_name == "Pilbara Minerals"
        assert fac.country == "AU"


# ---------------------------------------------------------------------------
# End-to-end with mocked HTTP
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


# Frozen so content_hash is stable across re-runs within a test (a live
# timestamp made the idempotency test flaky across second boundaries).
_FIXED_PUB = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
    minute=0, second=0, microsecond=0,
)


def _gnews_rss(title: str, guid: str = "g1") -> str:
    pub = _FIXED_PUB.strftime("%a, %d %b %Y %H:%M:%S GMT")
    return f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>q</title>
<item><title>{title}</title><link>https://example.com/a1</link>
<guid>{guid}</guid><pubDate>{pub}</pubDate>
<description>&lt;p&gt;{title} details&lt;/p&gt;</description></item>
</channel></rss>"""


def _edgar_submissions(form_desc: str, accession: str = "0001-26-000001") -> dict:
    return {
        "filings": {"recent": {
            "form": ["8-K", "10-Q"],
            "filingDate": [_FIXED_PUB.strftime("%Y-%m-%d")] * 2,
            "accessionNumber": [accession, "0001-26-000002"],
            "primaryDocument": ["doc.htm", "q.htm"],
            "primaryDocDescription": [form_desc, "Quarterly report"],
            "items": ["7.01", ""],
        }}
    }


def _mock_client(edgar_desc: str, gnews_title: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "data.sec.gov" or "submissions" in request.url.path:
            return httpx.Response(200, json=_edgar_submissions(edgar_desc))
        if host == "news.google.com":
            return httpx.Response(
                200, content=_gnews_rss(gnews_title).encode(),
                headers={"content-type": "application/rss+xml"},
            )
        if host == "asx.api.markitdigital.com":
            return httpx.Response(500)  # dead endpoint — must be fail-soft
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _asx_payload(headline: str, price_sensitive: bool = True) -> dict:
    return {"data": {"displayName": "PLS GROUP LIMITED", "items": [{
        "announcementType": "PROGRESS REPORT",
        "date": _FIXED_PUB.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "documentKey": "2924-03110875-6A1333805",
        "headline": headline,
        "isPriceSensitive": price_sensitive,
    }]}}


class TestIngestEndToEnd:
    def test_display_only_invariant_and_anchoring(self, session, seeded):
        client = _mock_client(
            edgar_desc="Report on operations suspension",
            gnews_title="Pilgangoora operations halted after cyclone",
        )
        result = ingest_operational_news(session, http_client=client)
        session.commit()

        assert result["events_created"] == 2  # 1 edgar (8-K only) + 1 gnews
        events = session.scalars(select(RiskEvent)).all()
        assert len(events) == 2
        for ev in events:
            # THE invariant: born display-only, autofill must not promote.
            assert ev.primary_category is None
            assert ev.event_type == EVENT_TYPE_CANDIDATE
            assert ev.event_subtype is None
            assert ev.severity_score is None
            assert ev.metadata_json["scoring"] == "display_only"
            assert ev.metadata_json["display_only_reason"] == "pending_triage"
            assert ev.risk_categories_json == ["operational"]

        gnews_ev = next(
            e for e in events if e.metadata_json["source_feed"] == "google_news"
        )
        # Facility-anchored by construction → facility + geo + material links.
        fac_links = session.scalars(select(RiskEventFacility).where(
            RiskEventFacility.risk_event_id == gnews_ev.id)).all()
        assert [l.facility_id for l in fac_links] == [seeded["facility"].id]
        assert fac_links[0].match_reason == "watchlist_query"
        geo = session.scalars(select(RiskEventGeography).where(
            RiskEventGeography.risk_event_id == gnews_ev.id)).all()
        assert [(g.country_code, g.geography_context) for g in geo] == [("AU", "primary")]
        mats = session.scalars(select(RiskEventMaterial).where(
            RiskEventMaterial.risk_event_id == gnews_ev.id)).all()
        assert [m.material_id for m in mats] == [seeded["material"].id]
        assert mats[0].match_reason == "facility_link"
        # Company edge completed via facility ownership (operator at 0.85).
        gnews_comp = session.scalars(select(RiskEventCompany).where(
            RiskEventCompany.risk_event_id == gnews_ev.id)).all()
        assert [(c.company_id, c.relevance_score, c.match_reason) for c in gnews_comp] \
            == [(seeded["company"].id, 0.85, "facility_operator")]
        # Weather keyword in the title → suggestion recorded, subtype col empty.
        assert gnews_ev.metadata_json["suggested_subtype"] == "weather_supply_disruption"

        edgar_ev = next(
            e for e in events if e.metadata_json["source_feed"] == "edgar"
        )
        comp = session.scalars(select(RiskEventCompany).where(
            RiskEventCompany.risk_event_id == edgar_ev.id)).all()
        assert [c.company_id for c in comp] == [seeded["company"].id]
        # No facility name in the filing text → no guessed facility links,
        # candidates listed for triage instead.
        assert session.scalar(select(RiskEventFacility.id).where(
            RiskEventFacility.risk_event_id == edgar_ev.id)) is None
        assert edgar_ev.metadata_json["candidate_facilities"][0]["name"] == "Pilgangoora"

    def test_facility_name_match_links_facility_on_exchange_item(self, session, seeded):
        client = _mock_client(
            edgar_desc="Production suspended at Pilgangoora after incident",
            gnews_title="No operational keywords here at all",
        )
        result = ingest_operational_news(session, http_client=client)
        session.commit()
        assert result["events_created"] == 1  # gnews item fails the filter
        assert result["items_filtered_out"] >= 1
        ev = session.scalars(select(RiskEvent)).one()
        fac_links = session.scalars(select(RiskEventFacility).where(
            RiskEventFacility.risk_event_id == ev.id)).all()
        assert len(fac_links) == 1
        assert fac_links[0].match_reason == "facility_name_match"
        # The filing company is already linked at 1.0 — the facility-operator
        # pass must NOT duplicate it.
        comp = session.scalars(select(RiskEventCompany).where(
            RiskEventCompany.risk_event_id == ev.id)).all()
        assert [(c.relevance_score, c.match_reason) for c in comp] \
            == [(1.0, "named_company")]

    def test_keyword_filter_drops_non_operational_filings(self, session, seeded):
        client = _mock_client(
            edgar_desc="Completion of senior notes offering",
            gnews_title="Also nothing relevant",
        )
        result = ingest_operational_news(session, http_client=client)
        assert result["events_created"] == 0
        assert result["items_filtered_out"] == 2

    def test_idempotent_rerun(self, session, seeded):
        def run():
            return ingest_operational_news(
                session,
                http_client=_mock_client(
                    edgar_desc="Report on operations suspension",
                    gnews_title="Pilgangoora operations halted after cyclone",
                ),
            )
        first = run()
        session.commit()
        second = run()
        session.commit()
        assert first["events_created"] == 2
        assert second["events_created"] == 0
        assert second["events_skipped_existing"] == 2
        assert len(session.scalars(select(RiskEvent)).all()) == 2

    def test_dead_feed_is_fail_soft(self, session, seeded):
        # Every host 500s — the run completes with zero items, no raise.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = ingest_operational_news(session, http_client=client)
        assert result["events_created"] == 0
        assert result["items_fetched"] == 0

    def test_dry_run_writes_nothing(self, session, seeded):
        client = _mock_client(
            edgar_desc="Report on operations suspension",
            gnews_title="Pilgangoora operations halted after cyclone",
        )
        result = ingest_operational_news(session, http_client=client, dry_run=True)
        assert result["events_created"] == 2
        assert session.scalars(select(RiskEvent)).all() == []

    def test_asx_payload_parsed_and_admin_noise_dropped(self, session, seeded):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "asx.api.markitdigital.com":
                payload = _asx_payload("Production suspended after plant fire")
                payload["data"]["items"].append({
                    **_asx_payload("Cessation of securities", False)["data"]["items"][0],
                })
                return httpx.Response(200, json=payload)
            return httpx.Response(500)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = ingest_operational_news(session, http_client=client, feeds=["asx"])
        session.commit()
        # Price-sensitive operational item lands; non-sensitive admin row
        # never even reaches the keyword filter.
        assert result["events_created"] == 1
        ev = session.scalars(select(RiskEvent)).one()
        assert ev.primary_category is None
        assert ev.metadata_json["source_feed"] == "asx"
        assert "cdn-api.markitdigital.com" in ev.source_document.url
        comp = session.scalars(select(RiskEventCompany).where(
            RiskEventCompany.risk_event_id == ev.id)).all()
        assert [c.company_id for c in comp] == [seeded["company"].id]

    def test_unknown_feed_rejected(self, session, seeded):
        with pytest.raises(ValueError, match="Unknown feeds"):
            ingest_operational_news(session, feeds=["edgar", "bloomberg"])

    def test_lookback_excludes_old_items(self, session, seeded):
        old = (_now() - timedelta(days=40)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        rss = f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>q</title>
<item><title>Pilgangoora halted by strike</title>
<link>https://example.com/old</link><guid>old1</guid>
<pubDate>{old}</pubDate><description>old</description></item>
</channel></rss>"""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "news.google.com":
                return httpx.Response(200, content=rss.encode())
            return httpx.Response(500)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = ingest_operational_news(
            session, http_client=client, feeds=["google_news"],
        )
        assert result["items_fetched"] == 0
        assert result["events_created"] == 0
