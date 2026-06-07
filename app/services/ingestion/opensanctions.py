"""OpenSanctions consolidated sanctions list ingestion.

Downloads the free daily bulk CSV export from OpenSanctions
(https://www.opensanctions.org) and produces two types of ``RiskEvent`` rows:

1. **Company match events** (``event_type="sanctions_listing"``) — one event per
   company in the ``companies`` table whose canonical name, alias, or LEI matches
   a sanctioned entity. LEI matching is the primary path (exact, no false
   positives); name matching is the fallback.

2. **Geography signal events** (``event_type="geography_sanctions_exposure"``) —
   one event per high-concentration geography (default: CN, CD, RU, IR, KP)
   recording how many sanctioned company/organisation records are linked to that
   country in the OpenSanctions dataset.

Material attribution (May 2026 — N2 fix)
----------------------------------------
Sanctions list entries describe ENTITIES, not materials, so keyword-matching
the entity name would produce noise (a company called "Lithium Mining Corp"
doesn't necessarily make this event a Lithium-relevant signal — it makes the
COMPANY relevant; whether the material is depends on what they actually
trade).  We use structured data instead:

* For ``sanctions_listing`` events: pull rows from ``company_material_exposures``
  for the matched company.  Each non-zero exposure becomes a ``RiskEventMaterial``
  with ``relevance_score = exposure_score`` and ``match_reason = 'company_material_exposure'``.
  Norilsk Nickel sanctioned → Nickel + Cobalt + PGM events all attributed to
  the relevant materials at the partner-curated exposure weight.

* For ``geography_sanctions_exposure`` events: pull the latest
  ``material_production_shares`` rows for the country.  Each material where
  the country has ``production_share ≥ min_share`` (default 5%) becomes a
  ``RiskEventMaterial`` with ``relevance_score = production_share`` and
  ``match_reason = 'country_production_concentration'``.  Materials the
  country dominates (CN graphite at 70%) get high relevance; materials it
  produces marginally get low.  This is what propagates the country-aggregate
  signal into the material × geography rollup that scoring reads.

Matching strategy
-----------------
**Primary — LEI**: The ``identifiers`` column of the simple CSV contains
pipe-delimited identifiers such as ``lei-529900HNOAA1KXQJUQ27``. We extract
these and match against ``Company.lei``. This is exact and produces no false
positives. Run ``bdi-ingest ingest-gleif`` before this command to backfill LEIs.

**Fallback — normalised name**: Lowercases both sides, strips common legal
entity suffixes (Inc., Ltd., PJSC, JSC, OAO, PAO, plc, AG, GmbH …) and
collapses whitespace. Checked against ``Company.canonical_name`` and all
``CompanyAlias.alias`` values where ``alias_type`` is not ``"lei"`` or
``"ticker"`` (those are not human-readable names).

Important caveats
-----------------
- **Zero company events is the expected outcome** when none of the seeded
  companies are directly sanctioned. Most publicly-traded mining/OEM companies
  are not on OFAC/EU/UN sanctions lists — only their executives or subsidiaries
  may be. This is not a bug; re-run after populating more companies or when
  sanctions lists change.

- **Name matching can still produce false positives** for short or generic
  names. A human review step is recommended before acting on any match.

- **Geography events are volume signals, not quality signals.** A country with
  1,000 sanctioned shell companies is not necessarily riskier than one with 10
  major state-owned enterprises.

No raw sanctions entity records are stored in the database — only structured
``RiskEvent`` rows for confirmed matches.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.company import Company, CompanyAlias, CompanyMaterialExposure
from app.models.country import Country
from app.models.regulatory import (
    RiskEvent,
    RiskEventCompany,
    RiskEventGeography,
    RiskEventMaterial,
)
from app.models.supply import MaterialProductionShare
from app.services.ingestion import feature_flags

log = structlog.get_logger(__name__)

OPENSANCTIONS_CSV_URL = (
    "https://data.opensanctions.org/datasets/latest/sanctions/targets.simple.csv"
)

_SOURCE = "opensanctions"
_BATCH_SIZE = 100

# Only these schema types are relevant for company matching.
_COMPANY_SCHEMAS = {"Company", "Organization", "LegalEntity", "PublicBody"}

# ── Topic relevance filter (added 2026-05-09, Tier 1.2 audit) ───────────
# OpenSanctions tags each entity with one or more ``topics`` describing
# why it's listed.  For a mineral supply-chain risk model, only a subset
# of topics are actually informative — a sanctioned individual for tax
# fraud doesn't tell us anything about minerals, but a sanctioned
# state-owned enterprise tagged ``gov.soe`` + ``export.control`` does.
#
# Pre-2026-05-09 the parser kept every Company/Organization/LegalEntity/
# PublicBody row regardless of topic, which flooded the
# ``geography_sanctions_exposure`` event counts with PEP-fraud / war-crime
# / wanted-individual records.  Filtering by topic at parse time cuts
# that noise.
#
# Topics retained (any-of match → keep the entity):
_MATERIAL_RELEVANT_TOPICS: frozenset[str] = frozenset({
    # Direct sanctions impact on the entity or anyone connected to it
    "sanction",            # primary sanctions listing
    "sanction.linked",     # sanctioned via association
    # Export-control and trade-restriction lists
    "export.control",      # US Entity List, EU dual-use export controls
    "export.risk",         # export risk flag
    # Critical-minerals-relevant entity types
    "gov.soe",             # state-owned enterprise (CN minerals SOEs etc.)
    "mil",                 # military-linked entities
    "forced.labor",        # Xinjiang / DRC concerns directly relevant to minerals
    # Procurement-exclusion lists — affects supplier qualification
    "corp.disqual",        # disqualified company
    "debarment",           # government debarment
    # Financial-crime topics that could cascade to material supply
    "crime.fin",           # financial crime — counterparty risk
    "crime.boss",          # organised crime senior figures (limited mineral relevance but kept)
})

# Topics explicitly EXCLUDED (any-of match → drop the entity).
# Documented for clarity even though the include-list above is the
# operative filter — entities only matching these are noise for our use case.
_TOPIC_EXCLUDED_NOISE: frozenset[str] = frozenset({
    "crime.fraud",         # tax fraud / shell-company fraud — not material-relevant
    "crime.terror",        # individual terror designations
    "crime.war",           # war-crime PEPs (already covered by sanctions if relevant)
    "crime.theft",         # individual theft
    "pol.indiv",           # individual politicians
    "role.judge",          # judicial roles
    "wanted",              # wanted individuals (FBI etc.)
    "poi",                 # generic "person of interest"
})

# NOTE: _DEFAULT_HIGH_CONCENTRATION_GEOS was removed in Phase 2.
# The list is now loaded from countries.is_sanctions_risk (DB-backed).
# To change the list, update is_sanctions_risk flags via seed_countries.py
# and re-run `bdi-ingest seed-countries` — no code change required.
# CN, CD, RU, IR, KP are seeded as True in seed_countries.py.
_DEFAULT_HIGH_CONCENTRATION_GEOS: list[str] = []  # unused; kept for test compat

# Legal entity suffixes stripped during name normalisation.  Covers Western,
# Russian (PJSC/JSC/OAO/PAO/ZAO), CIS, and East-Asian conventions.
_LEGAL_SUFFIXES_RE = re.compile(
    r"\b("
    r"inc\.?|ltd\.?|co\.?|ag|plc|n\.?v\.?|s\.?a\.?|gmbh|llc|corp\.?|"
    r"limited|group|holding|holdings|international|corporation|company|"
    r"pjsc|ojsc|jsc|oao|pao|zao|ooo|ao|"  # Russian / CIS legal forms
    r"pty|nl|spa|sas|srl|bv|nv|kk|kft|as|ab|oy|oyj"  # AU/EU/Asian forms
    r")\b",
    re.IGNORECASE,
)

# Alias types that are NOT human-readable names and should be skipped during
# name-based matching (LEIs and tickers are handled separately or not at all).
_SKIP_ALIAS_TYPES = {"lei", "ticker"}


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_sanctions_csv(
    url: str = OPENSANCTIONS_CSV_URL,
    timeout: int = 120,
) -> io.BytesIO:
    """Stream-download the sanctions CSV and return an in-memory BytesIO buffer.

    The file is ~15–30 MB. Streamed to avoid a single large allocation.

    Raises:
        httpx.HTTPStatusError: on non-2xx HTTP response.
        httpx.TimeoutException: if the download exceeds ``timeout`` seconds.
    """
    log.info("opensanctions.download.start", url=url)
    chunks: list[bytes] = []

    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_bytes():
            chunks.append(chunk)

    buf = io.BytesIO(b"".join(chunks))
    buf.seek(0)
    log.info("opensanctions.download.done", bytes=buf.getbuffer().nbytes)
    return buf


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def _split_pipe(value: str) -> list[str]:
    """Split a pipe-separated cell value into a non-empty list of stripped strings."""
    if not value:
        return []
    return [part.strip() for part in value.split("|") if part.strip()]


def _split_topics(value: str) -> list[str]:
    """Split the OpenSanctions ``topics`` cell into a non-empty list.

    Unlike other fields in targets.simple.csv (aliases, countries,
    identifiers) which use ``|`` as the delimiter, ``topics`` uses ``;``.
    Tolerant of either separator so the test-suite CSV fixtures and real
    OpenSanctions exports both work.
    """
    if not value:
        return []
    # Normalise both delimiters to one separator, then split.
    normalised = value.replace("|", ";")
    return [part.strip() for part in normalised.split(";") if part.strip()]


def _parse_date(value: str) -> Optional[datetime]:
    """Parse an ISO date string to a datetime, returning None on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.split("T")[0])
    except ValueError:
        return None


def _extract_leis(identifiers_cell: str) -> list[str]:
    """Extract LEI values from a pipe-delimited identifiers cell.

    OpenSanctions uses the prefix ``lei-`` before each LEI value, e.g.:
        ``lei-529900HNOAA1KXQJUQ27|isin-DE0007664039``

    Returns a (possibly empty) list of bare LEI strings (uppercase).
    """
    leis: list[str] = []
    for part in _split_pipe(identifiers_cell):
        if part.lower().startswith("lei-"):
            lei = part[4:].strip().upper()
            if lei:
                leis.append(lei)
    return leis


def parse_sanctions_csv(buf: io.BytesIO) -> list[dict]:
    """Parse the CSV buffer into a list of sanctioned entity dicts.

    Only keeps rows where:
      - ``schema`` is one of: Company, Organization, LegalEntity, PublicBody
      - ``topics`` overlaps with ``_MATERIAL_RELEVANT_TOPICS`` (added
        2026-05-09 Tier 1.2 audit — drops PEPs/fraud/wanted-individual
        rows that aren't informative for mineral supply chain risk).

    Parses in a single streaming pass — does not load all rows into memory.

    Returns dicts with keys:
        opensanctions_id  (str)
        name              (str — primary name)
        aliases           (list[str] — pipe-split, stripped, lowercased, deduped)
        leis              (list[str] — extracted from ``identifiers`` column)
        countries         (list[str] — uppercase ISO2 codes)
        datasets          (list[str])
        topics            (list[str] — material-relevant topics only)
        first_seen        (datetime | None)
        last_seen         (datetime | None)
    """
    # Read bytes then wrap a fresh BytesIO so the TextIOWrapper does not close
    # the caller's buffer when it goes out of scope (Python 3.12+ GC behaviour).
    buf.seek(0)
    reader = csv.DictReader(io.TextIOWrapper(io.BytesIO(buf.read()), encoding="utf-8"))

    results: list[dict] = []
    schema_drops = 0
    topic_drops = 0
    for row in reader:
        schema = (row.get("schema") or "").strip()
        if schema not in _COMPANY_SCHEMAS:
            schema_drops += 1
            continue

        # Topic relevance filter — drop entities whose ALL topics are
        # outside the material-relevant set.  Entities with no topics
        # listed are kept (rare; defensive — name-matching against our
        # companies table still applies, and an entity with no topic
        # listed but a strong company match is worth keeping).
        topics = _split_topics(row.get("topics") or "")
        if topics:
            relevant_topics = [t for t in topics if t in _MATERIAL_RELEVANT_TOPICS]
            if not relevant_topics:
                topic_drops += 1
                continue
        else:
            relevant_topics = []

        # Aliases: split, lowercase, dedupe, exclude primary name duplicate
        primary_name = (row.get("name") or "").strip()
        raw_aliases = _split_pipe(row.get("aliases") or "")
        aliases_norm = list(
            dict.fromkeys(
                a.lower() for a in raw_aliases if a.lower() != primary_name.lower()
            )
        )

        countries = [c.strip().upper() for c in _split_pipe(row.get("countries") or "") if c.strip()]
        datasets = _split_pipe(row.get("datasets") or "")
        leis = _extract_leis(row.get("identifiers") or "")

        results.append(
            {
                "opensanctions_id": (row.get("id") or "").strip(),
                "name": primary_name,
                "aliases": aliases_norm,
                "leis": leis,
                "countries": countries,
                "datasets": datasets,
                "topics": relevant_topics,
                "first_seen": _parse_date(row.get("first_seen") or ""),
                "last_seen": _parse_date(row.get("last_seen") or ""),
            }
        )

    log.info(
        "opensanctions.parse.done",
        entity_count=len(results),
        dropped_by_schema=schema_drops,
        dropped_by_topic=topic_drops,
    )
    return results


# ---------------------------------------------------------------------------
# Company matching
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(name: str) -> str:
    """Lowercase, strip legal entity suffixes, strip punctuation, collapse whitespace.

    Strips common Western and Russian/CIS legal forms (PJSC, JSC, OAO, plc,
    Ltd., Inc., AG …) so that ``"Nornickel PJSC"`` and ``"Nornickel"`` both
    reduce to ``"nornickel"`` and compare equal.
    """
    name = name.lower()
    name = _LEGAL_SUFFIXES_RE.sub("", name)
    name = re.sub(r"[,.\-/\\]", " ", name)  # strip punctuation
    return _WHITESPACE_RE.sub(" ", name).strip()


def _build_name_index(entities: list[dict]) -> dict[str, list[dict]]:
    """Build a lookup dict: normalised_name → list of matching sanctioned entities.

    Indexes both ``name`` and each entry in ``aliases``.
    """
    index: dict[str, list[dict]] = {}
    for entity in entities:
        keys = [_normalise(entity["name"])] + [_normalise(a) for a in entity["aliases"]]
        for key in keys:
            if key:
                index.setdefault(key, []).append(entity)
    return index


def _build_lei_index(entities: list[dict]) -> dict[str, list[dict]]:
    """Build a lookup dict: LEI (uppercase) → list of matching sanctioned entities."""
    index: dict[str, list[dict]] = {}
    for entity in entities:
        for lei in entity.get("leis", []):
            if lei:
                index.setdefault(lei.upper(), []).append(entity)
    return index


def match_companies(
    session: Session,
    entities: list[dict],
) -> list[tuple[Any, list[dict]]]:
    """Return ``(Company ORM row, list of matching sanctioned entities)`` pairs.

    Matching is two-phase:
    1. **LEI match** (primary): exact match on ``Company.lei`` against the
       ``lei-*`` values extracted from the ``identifiers`` column. Zero false
       positives.
    2. **Name match** (fallback): normalised match on ``Company.canonical_name``
       and readable ``CompanyAlias`` values (alias_type not in ``lei``/``ticker``).

    Queries all companies and aliases in two bulk queries (no N+1 lookups).
    Returns only companies with at least one match.
    """
    name_index = _build_name_index(entities)
    lei_index = _build_lei_index(entities)

    if not name_index and not lei_index:
        return []

    all_companies: list[Company] = list(session.scalars(select(Company)).all())
    all_aliases: list[CompanyAlias] = list(session.scalars(select(CompanyAlias)).all())

    # Map company_id → Company for alias → company resolution
    company_by_id: dict[Any, Company] = {c.id: c for c in all_companies}

    matched: dict[Any, list[dict]] = {}  # company_id → matched entities

    # --- Phase 1: LEI match --------------------------------------------------
    lei_matches = 0
    for company in all_companies:
        if company.lei and company.lei.upper() in lei_index:
            matched.setdefault(company.id, []).extend(lei_index[company.lei.upper()])
            lei_matches += 1

    # --- Phase 2: name match (canonical + aliases) ---------------------------
    for company in all_companies:
        key = _normalise(company.canonical_name)
        if key and key in name_index:
            matched.setdefault(company.id, []).extend(name_index[key])

    for alias_row in all_aliases:
        if alias_row.alias_type in _SKIP_ALIAS_TYPES:
            continue
        key = _normalise(alias_row.alias)
        if key and key in name_index:
            company = company_by_id.get(alias_row.company_id)
            if company:
                matched.setdefault(company.id, []).extend(name_index[key])

    # Deduplicate matched entities per company by opensanctions_id
    result: list[tuple[Any, list[dict]]] = []
    for company_id, entity_list in matched.items():
        seen_ids: set[str] = set()
        unique: list[dict] = []
        for e in entity_list:
            if e["opensanctions_id"] not in seen_ids:
                seen_ids.add(e["opensanctions_id"])
                unique.append(e)
        company = company_by_id[company_id]
        result.append((company, unique))

    if not result:
        log.warning(
            "opensanctions.match.none_found",
            total_companies_checked=len(all_companies),
            note=(
                "Zero company matches is expected when none of the seeded companies "
                "are directly sanctioned. Run `bdi-ingest ingest-gleif` first to "
                "backfill LEIs for higher-precision matching."
            ),
        )
    else:
        log.info(
            "opensanctions.match.done",
            companies_matched=len(result),
            via_lei=lei_matches,
            via_name=len(result) - lei_matches,
        )
    return result


# ---------------------------------------------------------------------------
# Material attribution helpers
# ---------------------------------------------------------------------------
# Sanctions events attach to materials via STRUCTURED data, not keyword
# matching.  See module docstring "Material attribution" section for the
# rationale.

# Default floor for country-aggregate event attribution: a material has to be
# at least this share of world production for the country before we propagate
# the geography signal to it.  5% is permissive (admits Iron Ore CN, Aluminum
# RU, etc.) without flooding the junction table with hundredths-of-a-percent
# noise.
_DEFAULT_GEO_MIN_SHARE = 0.05


def _attribute_company_event_to_materials(
    session: Session,
    *,
    risk_event_id: int,
    company: Company,
) -> int:
    """Add ``RiskEventMaterial`` rows for a ``sanctions_listing`` event.

    For each material the company has non-zero ``exposure_score`` against
    in ``company_material_exposures`` (across any supply-chain stage —
    aggregated by ``MAX``), insert a junction row tagged with
    ``match_reason='company_material_exposure'``.

    A company with multi-stage exposure to the same material (e.g. mining
    AND refining cobalt) gets ONE junction row at the highest stage's
    exposure score — duplicate stages would violate the
    ``(risk_event_id, material_id)`` unique constraint and the highest
    stage represents the ceiling of supply-chain exposure anyway.

    Returns the count of rows inserted.
    """
    rows = session.execute(
        select(
            CompanyMaterialExposure.material_id,
            func.max(CompanyMaterialExposure.exposure_score).label("score"),
        )
        .where(CompanyMaterialExposure.company_id == company.id)
        .group_by(CompanyMaterialExposure.material_id)
    ).all()

    inserted = 0
    for material_id, score in rows:
        if score is None or float(score) <= 0:
            continue
        session.add(
            RiskEventMaterial(
                risk_event_id=risk_event_id,
                material_id=material_id,
                relevance_score=float(score),
                match_reason="company_material_exposure",
            )
        )
        inserted += 1
    return inserted


def _attribute_geo_event_to_materials(
    session: Session,
    *,
    risk_event_id: int,
    country_code: str,
    min_share: float = _DEFAULT_GEO_MIN_SHARE,
) -> int:
    """Add ``RiskEventMaterial`` rows for a ``geography_sanctions_exposure``
    event.

    Pulls the LATEST ``material_production_shares`` row per material for
    the given country (the table is annual; only the most recent year is
    relevant for current-state risk).  Materials where the country has
    ``production_share < min_share`` are skipped.

    The relevance_score on the junction equals the country's share of
    world production for that material — so a CN sanctions exposure event
    propagates to graphite at ~0.70 and to cobalt at ~0.04 (cobalt
    production is DRC-dominated, not CN), which is what we want.

    Returns the count of rows inserted.
    """
    # Latest reference_year per material for this country.  Pulled in a
    # single round-trip via a self-join on the max(year) subquery.
    latest_subq = (
        select(
            MaterialProductionShare.material_id,
            func.max(MaterialProductionShare.reference_year).label("max_year"),
        )
        .where(MaterialProductionShare.country_code == country_code)
        .group_by(MaterialProductionShare.material_id)
        .subquery()
    )

    rows = session.execute(
        select(
            MaterialProductionShare.material_id,
            MaterialProductionShare.production_share,
        )
        .join(
            latest_subq,
            (MaterialProductionShare.material_id == latest_subq.c.material_id)
            & (MaterialProductionShare.reference_year == latest_subq.c.max_year),
        )
        .where(
            MaterialProductionShare.country_code == country_code,
            MaterialProductionShare.production_share.is_not(None),
            MaterialProductionShare.production_share >= min_share,
        )
    ).all()

    inserted = 0
    for material_id, prod_share in rows:
        if prod_share is None:
            continue
        session.add(
            RiskEventMaterial(
                risk_event_id=risk_event_id,
                material_id=material_id,
                relevance_score=float(prod_share),
                match_reason="country_production_concentration",
            )
        )
        inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# Content hash — parameter-stable identifiers (2026-05-11)
# ---------------------------------------------------------------------------
#
# OpenSanctions company and geography events are conceptually *states*, not
# point-in-time occurrences.  The previous hash inputs (title + summary +
# event_date=now) embedded mutable values (the sanctioned-entity count, the
# dataset list, the run timestamp) so re-running this ingester produced a
# *new* row alongside the prior one rather than updating the existing row
# in place.  After the topic filter landed (Tier 1.2, 2026-05-09), counts
# also dropped sharply, which would have created visible duplicates against
# any pre-fix rows.
#
# Stable-key hashing keys on the conceptual identity of the signal:
#   - Company event:  one row per matched company
#   - Geo event:      one row per high-concentration country
# When the same identity recurs across runs, the existing row is UPDATEd
# in place (title / severity / summary / metadata refreshed, material +
# geography junctions deleted and re-inserted; company junctions left
# alone per the partial-rewrite audit decision).

def _company_event_stable_key(company_id: str) -> str:
    """Stable identifier for an OpenSanctions company-match event."""
    return f"opensanctions_company|company_id={company_id}"


def _geo_event_stable_key(country_iso2: str) -> str:
    """Stable identifier for an OpenSanctions country-geo event."""
    return f"opensanctions_geo|country={country_iso2}"


def _content_hash(stable_key: str) -> str:
    """SHA-256 of a parameter-stable event identifier."""
    return hashlib.sha256(stable_key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Main ingest
# ---------------------------------------------------------------------------

_OPENSANCTIONS_EVENT_TYPES = {"sanctions_listing", "geography_sanctions_exposure"}


def _days_since_last_run(session: Session) -> Optional[int]:
    """Return days since the most recent OpenSanctions ingest completed.

    Uses ``MAX(created_at)`` on ``risk_events`` for the two event types this
    ingester produces as a proxy for the last run. Returns ``None`` if no rows
    exist yet (i.e. first run).
    """
    max_created = session.scalar(
        select(func.max(RiskEvent.created_at)).where(
            RiskEvent.event_type.in_(_OPENSANCTIONS_EVENT_TYPES)
        )
    )
    if max_created is None:
        return None
    now = datetime.now(timezone.utc)
    # created_at is tz-aware; ensure max_created is too.
    if max_created.tzinfo is None:
        max_created = max_created.replace(tzinfo=timezone.utc)
    return (now - max_created).days


def ingest_opensanctions(
    session: Session,
    url: str = OPENSANCTIONS_CSV_URL,
    high_concentration_geos: Optional[list[str]] = None,
    min_interval_days: int = 6,
) -> dict[str, int]:
    """Download, parse, match, and insert OpenSanctions data as RiskEvents.

    Args:
        session:                  SQLAlchemy session. Commits internally.
        url:                      Override download URL (useful for tests).
        high_concentration_geos:  ISO2 codes to treat as high-risk geographies.
                                  When None (default), loads from
                                  ``countries.is_sanctions_risk = True`` — the
                                  DB-backed replacement for the former hardcoded
                                  ``_DEFAULT_HIGH_CONCENTRATION_GEOS`` list
                                  (was: CN, CD, RU, IR, KP).
                                  Pass an explicit list to override the DB query.
        min_interval_days:        Skip the download if the most recent
                                  OpenSanctions event was created within this
                                  many days. Default 6 — prevents re-downloading
                                  the full ~300 MB snapshot more than once a
                                  week. Pass 0 to force a run.

    Returns (post-2026-05-11 UPSERT semantics):
        {
            "company_events_inserted": int,    # new sanctions_listing rows
            "company_events_updated":  int,    # existing rows refreshed
            "company_events_skipped_existing": 0,  # legacy key, always 0
            "geography_events_inserted": int,
            "geography_events_updated":  int,
            "companies_matched": int,
            "total_entities_parsed": int,
            "skipped_too_recent": bool,
        }
    """
    if min_interval_days > 0:
        days_ago = _days_since_last_run(session)
        if days_ago is not None and days_ago < min_interval_days:
            log.info(
                "opensanctions.ingest.skipped_too_recent",
                days_since_last_run=days_ago,
                min_interval_days=min_interval_days,
            )
            return {
                "company_events_inserted": 0,
                "company_events_updated": 0,
                "company_events_skipped_existing": 0,
                "geography_events_inserted": 0,
                "geography_events_updated": 0,
                "company_material_links": 0,
                "geo_material_links": 0,
                "companies_matched": 0,
                "total_entities_parsed": 0,
                "skipped_too_recent": True,
            }

    if high_concentration_geos is None:
        # Load from DB — countries flagged is_sanctions_risk=True.
        # Falls back to empty list if countries table is not yet seeded.
        high_concentration_geos = list(
            session.scalars(
                select(Country.iso2).where(Country.is_sanctions_risk.is_(True))
            ).all()
        )
        log.info(
            "opensanctions.high_concentration_geos_from_db",
            geos=high_concentration_geos,
        )

    # --- Step 1: download and parse ------------------------------------------
    buf = download_sanctions_csv(url)
    entities = parse_sanctions_csv(buf)

    company_events_inserted = 0
    company_events_updated = 0
    geography_events_inserted = 0
    geography_events_updated = 0
    company_material_links = 0
    geo_material_links = 0
    batch_count = 0

    # --- Step 2: company match events ----------------------------------------
    matches = match_companies(session, entities)

    for company, matched_entities in matches:
        datasets = sorted({ds for e in matched_entities for ds in e["datasets"]})
        event_date = datetime.now(timezone.utc)
        title = f"{company.canonical_name} — Active Sanctions Match"
        summary = (
            f"Matched against {len(matched_entities)} sanctioned entity record(s) "
            f"in: {', '.join(datasets)}"
        )
        ch = _content_hash(_company_event_stable_key(str(company.id)))

        primary_country = (
            matched_entities[0]["countries"][0]
            if matched_entities[0]["countries"]
            else None
        )

        metadata = {
            "opensanctions_ids": [e["opensanctions_id"] for e in matched_entities],
            "datasets": datasets,
        }

        existing = session.scalar(
            select(RiskEvent).where(RiskEvent.content_hash == ch)
        )
        if existing is not None:
            # In-place UPDATE.  Rewrite fields that depend on the latest
            # snapshot of OpenSanctions data; refresh geography +
            # material junctions; leave the named-company link alone.
            existing.event_date = event_date
            existing.title = title
            existing.summary = summary
            existing.severity_score = 1.0
            existing.geography_json = {"primary": primary_country}
            existing.metadata_json = metadata
            session.flush()

            # Refresh geographies + material attribution
            from sqlalchemy import delete as _sql_delete
            session.execute(
                _sql_delete(RiskEventGeography).where(
                    RiskEventGeography.risk_event_id == existing.id,
                )
            )
            session.execute(
                _sql_delete(RiskEventMaterial).where(
                    RiskEventMaterial.risk_event_id == existing.id,
                )
            )

            seen_countries: set[str] = set()
            for e in matched_entities:
                for country in e["countries"]:
                    if country and country not in seen_countries:
                        seen_countries.add(country)
                        session.add(
                            RiskEventGeography(
                                risk_event_id=existing.id,
                                country_code=country,
                                geography_context="primary",
                                relevance_score=1.0,
                            )
                        )
            material_link_count = _attribute_company_event_to_materials(
                session,
                risk_event_id=existing.id,
                company=company,
            )
            company_material_links += material_link_count
            company_events_updated += 1
            log.info(
                "opensanctions.company_event.updated",
                company=company.canonical_name,
                matched_count=len(matched_entities),
                event_id=str(existing.id),
            )
            batch_count += 1
            if batch_count % _BATCH_SIZE == 0:
                session.flush()
            continue

        event = RiskEvent(
            event_type="sanctions_listing",
            # 2026-05-19: event_subtype="EXPORT_RESTRICTION" added so
            # these events feed the Geopolitical pillar's specific
            # export_restriction_exposure sub-input (not just the broad
            # event_score path).  Sanctions are functionally export
            # restrictions on the named entities; the sub-input gating
            # in evidence_aggregator.derive_geopolitical_inputs matches
            # on event_subtype.
            event_subtype="EXPORT_RESTRICTION",
            event_date=event_date,
            title=title,
            summary=summary,
            severity_score=1.0,
            confidence_score=1.0,
            # G-Cov-1 (2026-05-06): sanctions ARE regulatory action, so the
            # same event feeds both the Geopolitical pillar (supply-side
            # restriction signal) AND the Regulatory pillar (compliance
            # burden imposed on importers + named entities).  Tagging both
            # categories lets the regulatory aggregator pick these up
            # without duplicating the underlying RiskEvent row.  See
            # docs/coverage-gap-plan-2026-05.md.
            risk_categories_json=["geopolitical_trade", "regulatory_compliance"],
            geography_json={"primary": primary_country},
            content_hash=ch,
            metadata_json=metadata,
        )
        session.add(event)
        session.flush()

        if feature_flags.LINK_EVENTS_TO_COMPANIES:
            session.add(
                RiskEventCompany(
                    risk_event_id=event.id,
                    company_id=company.id,
                    relevance_score=1.0,
                    match_reason="named_company",
                )
            )
        else:
            # Foundation phase 3: ingestion no longer materialises company
            # links. The RiskEvent is still inserted so the market layer can
            # see the geography signal; company-event relevance now derives
            # from the Phase 5 exposure-overlay engine.
            log.warning(
                "opensanctions.company_link.suppressed",
                event_id=str(event.id),
                company=company.canonical_name,
                reason="LINK_EVENTS_TO_COMPANIES feature flag is disabled",
            )

        # ── Material attribution via company_material_exposures ─────────
        # Independent of the company-link feature flag: this writes the
        # event-to-material edge that scoring's material × geography
        # rollup reads, regardless of whether the company-link layer is
        # materialised at ingest time.
        material_link_count = _attribute_company_event_to_materials(
            session,
            risk_event_id=event.id,
            company=company,
        )
        company_material_links += material_link_count
        if material_link_count == 0:
            log.warning(
                "opensanctions.company_event.no_material_attribution",
                company=company.canonical_name,
                event_id=str(event.id),
                hint=(
                    "company_material_exposures has no rows for this "
                    "company; sanctions event won't propagate to material "
                    "scoring.  Add exposure rows via "
                    "seed_material_exposures.py for the materials this "
                    "company actually trades."
                ),
            )

        # One RiskEventGeography per unique country across all matched entities
        seen_countries: set[str] = set()
        for e in matched_entities:
            for country in e["countries"]:
                if country and country not in seen_countries:
                    seen_countries.add(country)
                    session.add(
                        RiskEventGeography(
                            risk_event_id=event.id,
                            country_code=country,
                            geography_context="primary",
                            relevance_score=1.0,
                        )
                    )

        log.info(
            "opensanctions.company_event.inserted",
            company=company.canonical_name,
            matched_count=len(matched_entities),
        )
        company_events_inserted += 1
        batch_count += 1
        if batch_count % _BATCH_SIZE == 0:
            session.flush()

    # --- Step 3: geography signal events -------------------------------------
    for country in high_concentration_geos:
        country_entities = [e for e in entities if country in e["countries"]]
        count = len(country_entities)
        if count == 0:
            log.debug("opensanctions.geo_event.skip_empty", country=country)
            continue

        dataset_counter: Counter[str] = Counter(
            ds for e in country_entities for ds in e["datasets"]
        )
        dataset_sample = sorted(ds for ds, _ in dataset_counter.most_common(5))

        event_date = datetime.now(timezone.utc)
        title = f"{country} — {count} Sanctioned Entities (OpenSanctions)"
        summary = (
            f"OpenSanctions bulk data contains {count} company/organisation records "
            f"linked to {country} across datasets: {', '.join(dataset_sample)}"
        )
        ch = _content_hash(_geo_event_stable_key(country))
        severity = min(1.0, count / 500)
        metadata = {
            "sanctioned_entity_count": count,
            "datasets": dataset_sample,
        }

        existing = session.scalar(
            select(RiskEvent).where(RiskEvent.content_hash == ch)
        )
        if existing is not None:
            # In-place UPDATE.  Title and severity both shift with `count`,
            # so refresh every field.  Then rewrite geography + material
            # junctions (the sanctioned-entity set may have changed
            # composition, not just size).
            existing.event_date = event_date
            existing.title = title
            existing.summary = summary
            existing.severity_score = severity
            existing.geography_json = {"primary": country}
            existing.metadata_json = metadata
            session.flush()

            from sqlalchemy import delete as _sql_delete
            session.execute(
                _sql_delete(RiskEventGeography).where(
                    RiskEventGeography.risk_event_id == existing.id,
                )
            )
            session.execute(
                _sql_delete(RiskEventMaterial).where(
                    RiskEventMaterial.risk_event_id == existing.id,
                )
            )
            session.add(
                RiskEventGeography(
                    risk_event_id=existing.id,
                    country_code=country,
                    geography_context="primary",
                    relevance_score=1.0,
                )
            )
            geo_link_count = _attribute_geo_event_to_materials(
                session,
                risk_event_id=existing.id,
                country_code=country,
            )
            geo_material_links += geo_link_count
            geography_events_updated += 1
            log.info(
                "opensanctions.geo_event.updated",
                country=country,
                sanctioned_count=count,
                severity=severity,
                material_links=geo_link_count,
            )
            batch_count += 1
            if batch_count % _BATCH_SIZE == 0:
                session.flush()
            continue

        geo_event = RiskEvent(
            event_type="geography_sanctions_exposure",
            # 2026-05-19: event_subtype="EXPORT_RESTRICTION" added —
            # geography sanctions exposure is the aggregate "this
            # country has accumulated sanctioned-entity volume" signal,
            # which functionally restricts trade with that country
            # broadly.  Routes into Geopolitical pillar's
            # export_restriction_exposure sub-input alongside the
            # company-listing events.
            event_subtype="EXPORT_RESTRICTION",
            event_date=event_date,
            title=title,
            summary=summary,
            severity_score=severity,
            confidence_score=0.85,
            # G-Cov-1 (2026-05-06): sanctions ARE regulatory action, so the
            # same event feeds both the Geopolitical pillar (supply-side
            # restriction signal) AND the Regulatory pillar (compliance
            # burden imposed on importers + named entities).  Tagging both
            # categories lets the regulatory aggregator pick these up
            # without duplicating the underlying RiskEvent row.  See
            # docs/coverage-gap-plan-2026-05.md.
            risk_categories_json=["geopolitical_trade", "regulatory_compliance"],
            geography_json={"primary": country},
            content_hash=ch,
            metadata_json=metadata,
        )
        session.add(geo_event)
        session.flush()

        session.add(
            RiskEventGeography(
                risk_event_id=geo_event.id,
                country_code=country,
                geography_context="primary",
                relevance_score=1.0,
            )
        )

        # ── Material attribution via material_production_shares ─────────
        # Each material the country produces above the threshold gets a
        # junction row weighted by the country's share.  This is what
        # turns a "CN — N sanctioned entities" event into a material-
        # level signal that propagates through the geography rollup.
        geo_link_count = _attribute_geo_event_to_materials(
            session,
            risk_event_id=geo_event.id,
            country_code=country,
        )
        geo_material_links += geo_link_count
        if geo_link_count == 0:
            log.warning(
                "opensanctions.geo_event.no_material_attribution",
                country=country,
                event_id=str(geo_event.id),
                hint=(
                    "material_production_shares has no rows above the "
                    "min_share threshold for this country.  Either the "
                    "country doesn't produce any tracked material above "
                    "5%, or USGS data hasn't been ingested — run "
                    "`bdi-ingest ingest-usgs <CSV>`."
                ),
            )

        log.info(
            "opensanctions.geo_event.inserted",
            country=country,
            sanctioned_count=count,
            severity=severity,
            material_links=geo_link_count,
        )
        geography_events_inserted += 1
        batch_count += 1
        if batch_count % _BATCH_SIZE == 0:
            session.flush()

    # --- Step 4: commit -------------------------------------------------------
    session.commit()

    log.info(
        "opensanctions.ingest.done",
        company_events_inserted=company_events_inserted,
        company_events_updated=company_events_updated,
        geography_events_inserted=geography_events_inserted,
        geography_events_updated=geography_events_updated,
        company_material_links=company_material_links,
        geo_material_links=geo_material_links,
        companies_matched=len(matches),
        total_entities_parsed=len(entities),
    )
    return {
        "company_events_inserted": company_events_inserted,
        "company_events_updated": company_events_updated,
        "geography_events_inserted": geography_events_inserted,
        "geography_events_updated": geography_events_updated,
        "company_material_links": company_material_links,
        "geo_material_links": geo_material_links,
        "companies_matched": len(matches),
        "total_entities_parsed": len(entities),
        "skipped_too_recent": False,
        # Backwards-compat key — under parameter-stable UPSERT semantics
        # there's no longer a "skipped" path; same-data re-runs UPDATE in
        # place.  Kept as 0 so dashboards and CLI output don't break.
        "company_events_skipped_existing": 0,
    }
