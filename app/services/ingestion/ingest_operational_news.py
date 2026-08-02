"""Operational news ingester — watchlist-driven event candidates (V1).

Purpose (approved 2026-07-27, Nicole): the operational pillar is manual-first
by design (spec §6) but the curation queue needs a regular inbound feed of
facility-level disruption signals. This ingester polls three feed families
against the CURATED facility watchlist (``data_source='partner_facility_seed'``
— never MRDS) and lands matching items as **display-only candidate events**
for partner triage.

Feed families
-------------
1. ``edgar``  — SEC EDGAR submissions API, 8-K + 6-K filings for watchlist
   operators with a CIK (12 of 19 public operators at build time). Official,
   free, documented. Reuses the submissions endpoint pattern from
   ``ingest_sec_edgar.py`` (which remains parked for the company path — this
   module only reads the recent-filings arrays, it does not touch
   ``sec_filing_signal`` events).
2. ``asx``    — the UNOFFICIAL Markit Digital announcements API that powers
   asx.com.au's own announcement pages, for operators whose
   ``public_ticker`` parses as an ASX listing (``ASX:XXX`` or ``XXX.AX``).
   There is no free official ASX feed; this endpoint is undocumented and
   can change without notice, so the adapter is strictly fail-soft: any
   error logs a warning and yields nothing. If ``operational_news.asx_error``
   fires every week, the endpoint is gone — drop the adapter or fund a
   paid feed. ("Include, fail-soft" — Nicole, 2026-07-27.)
3. ``google_news`` — Google News RSS targeted queries, one per watchlist
   facility: ``"<facility name>" (<disruption keywords>)``. The reliable free
   layer for everything the exchanges don't cover; noisier, which the
   display-only + triage flow absorbs.

Born display-only — the invariant
---------------------------------
Every event this module creates carries::

    event_type   = "operational_news_candidate"
    primary_category = NULL          (never autofilled — event_type is in
                                      _DISPLAY_ONLY_EVENT_TYPES AND
                                      metadata_json["scoring"]="display_only")
    severity_score   = NULL          (partner sets at triage)
    event_subtype    = NULL          (keyword SUGGESTION lives in
                                      metadata_json["suggested_subtype"] only —
                                      nothing scores off a guess)

Promotion is a triage act: the partner confirms the event is real and
operational, sets ``event_subtype`` + ``severity_score``, sets
``primary_category='operational'`` and removes ``metadata_json["scoring"]``.
Only then does the event reach ``_score_operational_market`` at the next
rescore. ("Keyword-suggest only" — Nicole, 2026-07-27.)

Anchoring
---------
* ``google_news`` items are facility-anchored by construction (the query was
  the facility's name): RiskEventFacility (match_reason
  ``watchlist_query``) + RiskEventGeography (facility country, primary) +
  RiskEventMaterial rows from ``facility_material_links`` (is_direct=True,
  match_reason ``facility_link``) + RiskEventCompany rows for the facility's
  operator(s)/JV partners from ``company_facilities`` (relevance 0.85,
  match_reason ``facility_operator`` — inferred via ownership, not named).
* ``edgar`` / ``asx`` items are company-anchored by construction:
  RiskEventCompany (relevance 1.0, ``named_company``). Facility/geo/material
  links are added ONLY when a watchlist facility name for that operator
  appears in the item text (match_reason ``facility_name_match``); otherwise
  the operator's candidate facilities are listed in
  ``metadata_json["candidate_facilities"]`` for the partner to attach at
  triage. No guessed links.

Keyword pre-filter: exchange-feed items must contain at least one
disruption keyword (title + summary) to land at all — an 8-K about a bond
issue never enters the queue. Google News items pass the same filter for
queue hygiene.

Idempotency
-----------
``content_hash`` (SHA-256 of title+summary+event_date, the GTA convention) is
checked before insert; SourceDocuments upsert on (source_id, external_id).
Weekly cadence with a default 14-day lookback deliberately overlaps — re-seen
items are no-ops.

Scheduling: ``ingest-operational-news-weekly`` (Sundays 20:00 UTC, before the
GTA/FR/WorldBank block) in ``app/tasks/ingestion_jobs.py``. CLI:
``bdi-ingest ingest-operational-news [--lookback-days N] [--feeds ...]
[--dry-run]``.
"""

from __future__ import annotations

import hashlib
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable, Optional
from urllib.parse import quote_plus

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    Company,
    CompanyFacility,
    Facility,
    FacilityMaterialLink,
    RiskEvent,
    RiskEventCompany,
    RiskEventFacility,
    RiskEventGeography,
    RiskEventMaterial,
    Source,
    SourceDocument,
)
from app.models.enums import DocumentType, ImplementationPhase, SourceType

log = structlog.get_logger(__name__)


EVENT_TYPE_CANDIDATE = "operational_news_candidate"
_SOURCE_NAME = "Operational news watchlist"
DEFAULT_LOOKBACK_DAYS = 14
ALL_FEEDS = ("edgar", "asx", "google_news")

# Curated-facility marker — the watchlist NEVER includes MRDS rows (spec §6:
# MRDS is a discovery/reference layer and never scores).
_CURATED_FACILITY_SOURCE = "partner_facility_seed"

# SEC forms worth queueing: 8-K (domestic current reports) + 6-K (foreign
# private issuers — BHP, Rio Tinto, Vale, SQM, South32 file these).
_EDGAR_FORMS = frozenset({"8-K", "6-K"})

# Unofficial ASX endpoint (no official free feed exists). This is the
# Markit Digital API that powers asx.com.au's own announcements pages
# (verified live 2026-07-27 — the older /asx/1/company/... endpoint now
# 404s). Response shape: {"data": {"items": [{"headline", "date",
# "documentKey", "isPriceSensitive", ...}]}}. Documents resolve at
# _ASX_DOCUMENT_URL. Can change without notice — adapter is fail-soft.
_ASX_ANNOUNCEMENTS_URL = (
    "https://asx.api.markitdigital.com/asx-research/1.0/companies/"
    "{ticker}/announcements?itemsPerPage=20"
)
_ASX_DOCUMENT_URL = (
    "https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/"
    "file/{document_key}"
)

_GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

# Disruption keywords for the Google News query string (human-readable OR
# list) — kept aligned with _SUBTYPE_SUGGESTIONS below.
_QUERY_KEYWORDS = (
    '"force majeure" OR strike OR shutdown OR suspend OR curtail OR halt '
    'OR "care and maintenance" OR "guidance cut" OR flood OR fire OR '
    'accident OR outage'
)

# Ordered (pattern, suggested_subtype): FIRST match wins, so more specific
# phrases sit above generic ones. Suggestions only — written to
# metadata_json["suggested_subtype"], never to the event_subtype column.
# Subtype vocabulary follows the manual-walkthrough conventions already in
# the DB (facility_shutdown / weather_supply_disruption / ...).
_SUBTYPE_SUGGESTIONS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"force\s+majeure", re.I), "force_majeure"),
    (re.compile(r"care\s+and\s+maintenance", re.I), "care_and_maintenance"),
    (re.compile(r"\bstrike\b|industrial\s+action|walk[- ]?out", re.I), "labor_strike"),
    (re.compile(r"guidance\s+(cut|lower|reduc|downgrade)|cuts?\s+.{0,20}guidance", re.I), "guidance_cut"),
    (re.compile(r"\bcurtail", re.I), "production_curtailment"),
    (re.compile(r"shut\s?down|\bshutter|\bidle[ds]?\b|\bmothball|suspend.{0,30}(production|operations|mining|processing)", re.I), "facility_shutdown"),
    (re.compile(r"flood|cyclone|hurricane|typhoon|drought|heavy\s+rain|monsoon|wildfire\s+smoke|weather", re.I), "weather_supply_disruption"),
    (re.compile(r"\bfire\b|explosion|collapse|fatalit|accident|derail|spill|tailings", re.I), "incident"),
    (re.compile(r"load[- ]?shedding|power\s+(outage|cut|shortage|crisis)|electricity\s+(shortage|rationing)|grid\b", re.I), "power_supply_disruption"),
    (re.compile(r"(permit|licen[cs]e)\s+(suspend|revok|withdraw)|injunction|court\s+(order|halt|suspend)|environmental\s+(stop|halt)", re.I), "permit_or_court_stoppage"),
    (re.compile(r"\bhalt(ed|s)?\b|suspen(d(ed|s)?|sion)\b", re.I), "operations_halt"),
)

# Pre-filter: an item must match at least one of these to enter the queue.
_OPERATIONAL_FILTER_RE = re.compile(
    "|".join(p.pattern for p, _ in _SUBTYPE_SUGGESTIONS), re.I,
)

_ASX_TICKER_RE = re.compile(r"^(?:ASX:(?P<a>[A-Z0-9]{2,5})|(?P<b>[A-Z0-9]{2,5})\.AX)$")


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

@dataclass
class WatchOperator:
    company_id: object          # UUID
    name: str
    cik: Optional[str]
    asx_ticker: Optional[str]
    facility_ids: list = field(default_factory=list)


@dataclass
class WatchFacility:
    facility_id: object         # UUID
    name: str
    country: str                # ISO2
    material_ids: list = field(default_factory=list)   # (material_id, is_primary)
    operator_name: Optional[str] = None
    # ALL companies linked via company_facilities (operator + JV partners) —
    # used to complete the company edge on facility-anchored events.
    operator_company_ids: list = field(default_factory=list)


@dataclass
class NewsItem:
    feed: str                   # edgar | asx | google_news
    external_id: str
    url: Optional[str]
    title: str
    summary: str
    published_at: Optional[datetime]
    # Anchors known at fetch time:
    company_id: Optional[object] = None     # edgar/asx
    facility_id: Optional[object] = None    # google_news
    raw_meta: dict = field(default_factory=dict)


def _parse_asx_ticker(public_ticker: Optional[str]) -> Optional[str]:
    """'ASX:LYC' / 'S32.AX' → 'LYC' / 'S32'; anything else → None.

    Deliberately narrow: we do NOT infer ASX tickers from the messy
    ``exchanges`` array ('ASX (primary)', ADR notes, ...) — a wrong ticker
    silently pulls another company's announcements.
    """
    if not public_ticker:
        return None
    m = _ASX_TICKER_RE.match(public_ticker.strip().upper())
    if not m:
        return None
    return m.group("a") or m.group("b")


def build_watchlist(session: Session) -> tuple[list[WatchFacility], list[WatchOperator]]:
    """Load curated facilities + their operators from the DB.

    Facilities: ``data_source='partner_facility_seed'`` only (all named, all
    with material links and an operator link as of 2026-07-27 — but none of
    that is assumed: missing pieces just narrow the anchor).
    """
    fac_rows = session.execute(
        select(Facility).where(Facility.data_source == _CURATED_FACILITY_SOURCE)
    ).scalars().all()
    fac_ids = [f.id for f in fac_rows]

    mat_rows = session.execute(
        select(
            FacilityMaterialLink.facility_id,
            FacilityMaterialLink.material_id,
            FacilityMaterialLink.is_primary_product,
        ).where(FacilityMaterialLink.facility_id.in_(fac_ids))
    ).all() if fac_ids else []
    mats_by_fac: dict = {}
    for fid, mid, is_primary in mat_rows:
        mats_by_fac.setdefault(fid, []).append((mid, bool(is_primary)))

    op_rows = session.execute(
        select(CompanyFacility.facility_id, Company)
        .join(Company, Company.id == CompanyFacility.company_id)
        .where(CompanyFacility.facility_id.in_(fac_ids))
    ).all() if fac_ids else []

    operators: dict = {}
    op_name_by_fac: dict = {}
    op_ids_by_fac: dict = {}
    for fid, company in op_rows:
        op = operators.get(company.id)
        if op is None:
            op = WatchOperator(
                company_id=company.id,
                name=company.canonical_name,
                cik=(str(company.cik).strip() if company.cik else None),
                asx_ticker=_parse_asx_ticker(company.public_ticker),
            )
            operators[company.id] = op
        op.facility_ids.append(fid)
        op_name_by_fac.setdefault(fid, company.canonical_name)
        op_ids_by_fac.setdefault(fid, []).append(company.id)

    facilities = [
        WatchFacility(
            facility_id=f.id,
            name=(f.name or "").strip(),
            country=f.country,
            material_ids=mats_by_fac.get(f.id, []),
            operator_name=op_name_by_fac.get(f.id),
            operator_company_ids=op_ids_by_fac.get(f.id, []),
        )
        for f in fac_rows
        if (f.name or "").strip()
    ]
    log.info(
        "operational_news.watchlist_built",
        facilities=len(facilities),
        operators=len(operators),
        with_cik=sum(1 for o in operators.values() if o.cik),
        with_asx=sum(1 for o in operators.values() if o.asx_ticker),
    )
    return facilities, list(operators.values())


# ---------------------------------------------------------------------------
# Feed adapters — each fail-soft: any error logs and yields nothing.
# ---------------------------------------------------------------------------

def _fetch_edgar_items(
    client: httpx.Client,
    operators: Iterable[WatchOperator],
    *,
    cutoff: datetime,
) -> list[NewsItem]:
    """8-K / 6-K filings since ``cutoff`` for operators with a CIK.

    Uses the EDGAR submissions JSON (recent-filings arrays). SEC requires a
    descriptive User-Agent — supplied by the caller-built client.
    """
    settings = get_settings()
    base = settings.sec_edgar_base_url.rstrip("/")
    items: list[NewsItem] = []
    for op in operators:
        if not op.cik:
            continue
        cik_padded = op.cik.zfill(10)
        try:
            resp = client.get(f"{base}/submissions/CIK{cik_padded}.json", timeout=60.0)
            resp.raise_for_status()
            data = resp.json()
            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            accessions = recent.get("accessionNumber", [])
            primary_docs = recent.get("primaryDocument", [])
            descriptions = recent.get("primaryDocDescription", [])
            item_codes = recent.get("items", [])
            for i, form in enumerate(forms):
                if form not in _EDGAR_FORMS:
                    continue
                try:
                    filed = datetime.strptime(dates[i], "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                except (ValueError, IndexError):
                    continue
                if filed < cutoff:
                    continue
                accession = accessions[i] if i < len(accessions) else ""
                acc_nodash = accession.replace("-", "")
                primary_doc = primary_docs[i] if i < len(primary_docs) else ""
                desc = descriptions[i] if i < len(descriptions) else ""
                codes = item_codes[i] if i < len(item_codes) else ""
                url = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{int(op.cik)}/{acc_nodash}/{primary_doc}"
                    if accession and primary_doc else None
                )
                items.append(NewsItem(
                    feed="edgar",
                    external_id=f"edgar:{accession}",
                    url=url,
                    title=f"{op.name} — {form} filing",
                    summary=" ".join(x for x in [desc, f"Items: {codes}" if codes else ""] if x),
                    published_at=filed,
                    company_id=op.company_id,
                    raw_meta={"form": form, "accession": accession,
                              "items": codes, "cik": op.cik},
                ))
        except Exception as exc:  # noqa: BLE001 — fail-soft per adapter contract
            log.warning("operational_news.edgar_error", company=op.name,
                        cik=op.cik, error=str(exc))
    return items


def _fetch_asx_items(
    client: httpx.Client,
    operators: Iterable[WatchOperator],
    *,
    cutoff: datetime,
) -> list[NewsItem]:
    """Market-sensitive ASX announcements via the UNOFFICIAL JSON endpoint.

    The endpoint is undocumented and may break or change shape without
    notice — every parse step tolerates missing fields, and any failure
    logs ``operational_news.asx_error`` and moves on. If this starts firing
    every week, the endpoint is gone: drop the adapter or fund a paid feed.
    """
    items: list[NewsItem] = []
    for op in operators:
        if not op.asx_ticker:
            continue
        try:
            resp = client.get(
                _ASX_ANNOUNCEMENTS_URL.format(ticker=op.asx_ticker), timeout=30.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            rows = (
                payload.get("data", {}).get("items")
                if isinstance(payload, dict) else None
            )
            if not isinstance(rows, list):
                log.warning("operational_news.asx_unexpected_shape",
                            ticker=op.asx_ticker)
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                header = (row.get("headline") or "").strip()
                if not header:
                    continue
                # Price-sensitive only — parity with the triage queue's
                # signal-vs-admin-noise line (routine "cessation of
                # securities" notices are flagged False).
                if row.get("isPriceSensitive") is False:
                    continue
                released = None
                raw_date = row.get("date")
                if isinstance(raw_date, str):
                    try:
                        released = datetime.fromisoformat(
                            raw_date.replace("Z", "+00:00")
                        )
                        if released.tzinfo is None:
                            released = released.replace(tzinfo=timezone.utc)
                    except ValueError:
                        released = None
                if released is not None and released < cutoff:
                    continue
                doc_key = (row.get("documentKey") or "").strip()
                ann_id = doc_key or header
                items.append(NewsItem(
                    feed="asx",
                    external_id=f"asx:{op.asx_ticker}:{ann_id}",
                    url=(
                        _ASX_DOCUMENT_URL.format(document_key=doc_key)
                        if doc_key else None
                    ),
                    title=f"{op.name} — {header}",
                    summary=header,
                    published_at=released,
                    company_id=op.company_id,
                    raw_meta={"ticker": op.asx_ticker, "announcement_id": ann_id,
                              "announcement_type": row.get("announcementType")},
                ))
        except Exception as exc:  # noqa: BLE001 — fail-soft per adapter contract
            log.warning("operational_news.asx_error", ticker=op.asx_ticker,
                        company=op.name, error=str(exc))
    return items


def _fetch_google_news_items(
    client: httpx.Client,
    facilities: Iterable[WatchFacility],
    *,
    cutoff: datetime,
    throttle_seconds: float = 0.3,
) -> list[NewsItem]:
    """One Google News RSS query per watchlist facility.

    Query: ``"<facility name>" (<disruption keywords>)``. Items are
    facility-anchored by construction. Throttled — ~320 facilities at
    0.3 s/query is ≈2 min, polite enough for a weekly job.
    """
    items: list[NewsItem] = []
    for fac in facilities:
        query = quote_plus(f'"{fac.name}" ({_QUERY_KEYWORDS})')
        try:
            resp = client.get(_GOOGLE_NEWS_RSS_URL.format(query=query), timeout=30.0)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for node in root.iter("item"):
                title = (node.findtext("title") or "").strip()
                link = (node.findtext("link") or "").strip() or None
                guid = (node.findtext("guid") or "").strip() or link or title
                desc = re.sub(r"<[^>]+>", " ", node.findtext("description") or "").strip()
                pub = None
                raw_pub = node.findtext("pubDate")
                if raw_pub:
                    try:
                        pub = parsedate_to_datetime(raw_pub)
                        if pub.tzinfo is None:
                            pub = pub.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        pub = None
                if not title:
                    continue
                if pub is not None and pub < cutoff:
                    continue
                items.append(NewsItem(
                    feed="google_news",
                    external_id=f"gnews:{hashlib.sha256(guid.encode()).hexdigest()[:24]}",
                    url=link,
                    title=title,
                    summary=desc[:2000],
                    published_at=pub,
                    facility_id=fac.facility_id,
                    raw_meta={"facility_name": fac.name, "query_country": fac.country},
                ))
        except Exception as exc:  # noqa: BLE001 — fail-soft per adapter contract
            log.warning("operational_news.google_news_error",
                        facility=fac.name, error=str(exc))
        if throttle_seconds:
            time.sleep(throttle_seconds)
    return items


# ---------------------------------------------------------------------------
# Classification + persistence
# ---------------------------------------------------------------------------

def _suggest_subtype(text: str) -> Optional[str]:
    """First-match keyword suggestion. None when nothing matches."""
    for pattern, subtype in _SUBTYPE_SUGGESTIONS:
        if pattern.search(text):
            return subtype
    return None


def _content_hash(title: str, summary: str, event_date: Optional[datetime]) -> str:
    date_part = event_date.isoformat() if event_date else ""
    return hashlib.sha256(f"{title}{summary}{date_part}".encode()).hexdigest()


def _get_or_create_source(session: Session) -> Source:
    existing = session.scalar(
        select(Source).where(Source.name == _SOURCE_NAME).limit(1)
    )
    if existing is not None:
        return existing
    src = Source(
        name=_SOURCE_NAME,
        source_type=SourceType.NEWS.value,
        phase=ImplementationPhase.PHASE_1.value,
        is_active=True,
        config_json={"feeds": list(ALL_FEEDS), "watchlist": _CURATED_FACILITY_SOURCE},
    )
    session.add(src)
    session.flush()
    return src


def _match_facilities_in_text(
    text: str, facilities: Iterable[WatchFacility],
) -> list[WatchFacility]:
    """Watchlist facilities whose name appears in ``text`` (case-insensitive,
    word-boundary). Short names (<4 chars) are skipped — too collision-prone.
    """
    matched = []
    lowered = text.lower()
    for fac in facilities:
        name = fac.name.lower()
        if len(name) < 4:
            continue
        if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", lowered):
            matched.append(fac)
    return matched


def ingest_operational_news(
    session: Session,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    feeds: Iterable[str] = ALL_FEEDS,
    dry_run: bool = False,
    http_client: Optional[httpx.Client] = None,
) -> dict:
    """Poll the configured feeds and land display-only candidate events.

    Caller commits (mirrors the other ingesters). Returns a summary dict.
    """
    feeds = tuple(feeds)
    unknown = set(feeds) - set(ALL_FEEDS)
    if unknown:
        raise ValueError(f"Unknown feeds {sorted(unknown)}; valid: {ALL_FEEDS}")

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    summary = {
        "feeds": list(feeds),
        "lookback_days": lookback_days,
        "items_fetched": 0,
        "items_filtered_out": 0,
        "events_created": 0,
        "events_skipped_existing": 0,
        "facility_links": 0,
        "company_links": 0,
        "material_links": 0,
        "dry_run": dry_run,
    }

    facilities, operators = build_watchlist(session)
    if not facilities and not operators:
        log.warning("operational_news.empty_watchlist")
        return summary
    fac_by_id = {f.facility_id: f for f in facilities}
    facs_by_company: dict = {}
    for op in operators:
        facs_by_company[op.company_id] = [
            fac_by_id[fid] for fid in op.facility_ids if fid in fac_by_id
        ]

    settings = get_settings()
    client = http_client or httpx.Client(
        follow_redirects=True,
        headers={"User-Agent": settings.sec_edgar_user_agent},
    )
    owns_client = http_client is None
    try:
        items: list[NewsItem] = []
        if "edgar" in feeds:
            items += _fetch_edgar_items(client, operators, cutoff=cutoff)
        if "asx" in feeds:
            items += _fetch_asx_items(client, operators, cutoff=cutoff)
        if "google_news" in feeds:
            items += _fetch_google_news_items(client, facilities, cutoff=cutoff)
    finally:
        if owns_client:
            client.close()

    summary["items_fetched"] = len(items)

    source = None if dry_run else _get_or_create_source(session)

    for item in items:
        text = f"{item.title} {item.summary}"
        if not _OPERATIONAL_FILTER_RE.search(text):
            summary["items_filtered_out"] += 1
            continue

        ch = _content_hash(item.title, item.summary, item.published_at)
        if session.scalar(
            select(RiskEvent.id).where(RiskEvent.content_hash == ch).limit(1)
        ) is not None:
            summary["events_skipped_existing"] += 1
            continue

        # Resolve anchors.
        matched_facs: list[WatchFacility] = []
        if item.facility_id is not None:                      # google_news
            fac = fac_by_id.get(item.facility_id)
            if fac is not None:
                matched_facs = [fac]
            match_reason = "watchlist_query"
        else:                                                 # edgar / asx
            matched_facs = _match_facilities_in_text(
                text, facs_by_company.get(item.company_id, []),
            )
            match_reason = "facility_name_match"

        suggested = _suggest_subtype(text)
        candidate_facilities = None
        if item.company_id is not None and not matched_facs:
            candidate_facilities = [
                {"facility_id": str(f.facility_id), "name": f.name, "country": f.country}
                for f in facs_by_company.get(item.company_id, [])
            ]

        if dry_run:
            summary["events_created"] += 1
            summary["facility_links"] += len(matched_facs)
            continue

        # SourceDocument upsert on (source_id, external_id).
        doc = session.scalar(
            select(SourceDocument).where(
                SourceDocument.source_id == source.id,
                SourceDocument.external_id == item.external_id,
            )
        )
        if doc is None:
            doc = SourceDocument(
                source_id=source.id,
                external_id=item.external_id,
                title=item.title[:1000],
                url=item.url,
                published_at=item.published_at,
                document_type=(
                    DocumentType.FILING.value if item.feed == "edgar"
                    else DocumentType.ARTICLE.value
                ),
                metadata_json={"feed": item.feed, **item.raw_meta},
            )
            session.add(doc)
            session.flush()

        event = RiskEvent(
            source_document_id=doc.id,
            event_type=EVENT_TYPE_CANDIDATE,
            event_subtype=None,               # suggestion lives in metadata only
            event_date=item.published_at,
            title=item.title[:1024],
            summary=item.summary or None,
            severity_score=None,              # partner sets at triage
            confidence_score=0.5,
            risk_categories_json=["operational"],
            geography_json=(
                {"primary": matched_facs[0].country} if matched_facs else None
            ),
            content_hash=ch,
            # 2026-07-31 harmonisation with the triage status model
            # (migration 066): this module's metadata-based state predates
            # the general model and now maps onto it — candidates awaiting
            # review are pending_triage (the archetypal case), promotion
            # sets scoring, rejection sets rejected.  The metadata flags
            # stay for one release as the documented back-compat guards.
            suggested_category="operational",
            triage_status="pending_triage",
            metadata_json={
                # BOTH display-only guards: the metadata flag is the
                # documented autofill escape hatch; the event_type is also in
                # _DISPLAY_ONLY_EVENT_TYPES (models/regulatory.py).
                "scoring": "display_only",
                "display_only_reason": "pending_triage",
                "source_feed": item.feed,
                **({"suggested_subtype": suggested} if suggested else {}),
                **({"candidate_facilities": candidate_facilities}
                   if candidate_facilities else {}),
            },
            verified=False,
        )
        # Explicit: candidates never score until a human promotes them.
        event.primary_category = None
        session.add(event)
        session.flush()

        seen_companies: set = set()
        if item.company_id is not None:
            session.add(RiskEventCompany(
                risk_event_id=event.id,
                company_id=item.company_id,
                relevance_score=1.0,
                match_reason="named_company",
            ))
            seen_companies.add(item.company_id)
            summary["company_links"] += 1

        seen_countries: set[str] = set()
        seen_materials: set[int] = set()
        for fac in matched_facs:
            session.add(RiskEventFacility(
                risk_event_id=event.id,
                facility_id=fac.facility_id,
                relevance_score=1.0,
                match_reason=match_reason,
            ))
            summary["facility_links"] += 1
            if fac.country and fac.country not in seen_countries:
                session.add(RiskEventGeography(
                    risk_event_id=event.id,
                    country_code=fac.country,
                    geography_context="primary",
                    relevance_score=1.0,
                ))
                seen_countries.add(fac.country)
            for mid, _is_primary in fac.material_ids:
                if mid in seen_materials:
                    continue
                session.add(RiskEventMaterial(
                    risk_event_id=event.id,
                    material_id=mid,
                    relevance_score=1.0,
                    is_direct=True,
                    match_reason="facility_link",
                ))
                seen_materials.add(mid)
                summary["material_links"] += 1
            # Complete the company edge: the matched facility's operator(s)
            # and JV partners from company_facilities. 0.85 = "strong match"
            # per the RiskEventCompany calibration — the company is inferred
            # via facility ownership, not named in the item text.
            for cid in fac.operator_company_ids:
                if cid in seen_companies:
                    continue
                session.add(RiskEventCompany(
                    risk_event_id=event.id,
                    company_id=cid,
                    relevance_score=0.85,
                    match_reason="facility_operator",
                ))
                seen_companies.add(cid)
                summary["company_links"] += 1

        summary["events_created"] += 1

    log.info("operational_news.done", **summary)
    return summary
