"""EUR-Lex regulation ingestion — manifest-first upsert into ``regulations``.

This module seeds the ``regulations`` table (and its scope junction tables) with
EU instruments that directly affect battery supply chains. It is intentionally
manifest-driven rather than a general EUR-Lex crawler:

- Battery supply chain regulations are a small, well-defined set.
- EUR-Lex's HTML endpoints keyed on a CELEX number have been stable since 2014,
  so a lightweight HTTP fetch (no API key) reliably yields the document
  preamble we use as ``Regulation.summary``.
- Dynamic keyword search would require NLP classification to avoid noise.

When new regulations become relevant (e.g. a future EU Critical Minerals
Partnership regulation), add them to ``BATTERY_REGULATIONS`` here.

CELEX number format
    ``3`` (regulation) + ``YYYY`` (year) + ``R`` (regulation type) + ``NNNN``
    (number). Directives use ``L``. This format has been stable since the 1990s.

Scoring connection
    ``app/services/scoring/regulatory_risk.py`` has a ``COMPLIANCE_OBLIGATIONS``
    dict keyed by ``regulation_key``. When a new regulation is added to the
    manifest here, add a corresponding entry in ``COMPLIANCE_OBLIGATIONS`` to
    activate scoring uplift. ``EU_BATTERY_REG_2023``, ``CRMA_2024``, and
    ``EU_CBAM`` are candidates for uplift entries.

Risk event generation
    ``ingest_eurlex`` generates one ``RiskEvent`` per regulation and writes a
    ``RiskEventRegulation`` junction row to populate the ``risk_event_regulations``
    table. This is what gives the scoring engine's regulatory pillar its
    event-driven score component via ``get_events_for_regulations()``.

    Events use ``event_date = None`` (standing obligations — always in scope,
    never fall outside the 365-day evidence window). ``effective_date`` is stored
    in ``metadata_json`` so the decay engine can apply the ±90-day step-up
    multiplier when enforcement is imminent.

    Severity calibration (see ``_SEVERITY_BY_STATUS``):
      effective  → 0.55  (enforcement is live; meaningful compliance pressure)
      enacted    → 0.40  (passed but not yet in force; deadline visible)
      proposed   → 0.25  (early signal only)

Update cadence
    Run ``ingest-eurlex`` when a new EU regulation is enacted. No need to run
    more than quarterly — these instruments do not change frequently.

Geography code "EU"
    Used as a bloc identifier in ``regulation_geography_scope.country_code``.
    The scoring engine in ``regulatory_risk.py`` treats "EU" as applicable to
    all 27 member states when querying by geography.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import date
from typing import Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.documents import SourceDocument
from app.models.regulatory import (
    Regulation,
    RegulationGeographyScope,
    RegulationMaterialScope,
    RiskEvent,
    RiskEventRegulation,
)
from app.models.source import Source
from app.models.supply import Material

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Source registration
# ---------------------------------------------------------------------------

_SOURCE_NAME = "EUR-Lex"
_SOURCE_TYPE = "eurlex"
_SOURCE_PHASE = "1"
_SOURCE_BASE_URL = "https://eur-lex.europa.eu"

EURLEX_HTML_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:{celex}"

# Maximum characters of preamble text retained as ``Regulation.summary``.
_SUMMARY_MAX_CHARS = 800


# ---------------------------------------------------------------------------
# Manifest of target regulations
# ---------------------------------------------------------------------------
# Each entry maps to one row in ``regulations`` plus N rows in
# ``regulation_material_scope`` and ``regulation_geography_scope``. The manifest
# is the source of truth for material and geography scopes — we do not attempt
# to parse scopes from EUR-Lex HTML.
#
# When extending the manifest:
#   - ``regulation_key`` must be globally unique across all seeders.
#   - Material canonical_names must already exist in the materials table (run
#     ``seed-materials`` first).
#   - ``geography_scopes`` uses ISO2 country codes or the bloc identifier "EU".

BATTERY_REGULATIONS: list[dict] = [
    {
        "regulation_key": "EU_BATTERY_REG_2023",
        "celex": "32023R1542",
        "title": "EU Battery Regulation (EU) 2023/1542 — batteries and waste batteries",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "status": "effective",
        "publication_date": date(2023, 7, 28),
        "effective_date": date(2024, 8, 18),
        "policy_theme": "battery_lifecycle_compliance",
        "material_scopes": [
            ("Lithium", "disclosure_required"),
            ("Cobalt", "disclosure_required"),
            ("Nickel", "disclosure_required"),
            ("Manganese", "disclosure_required"),
            ("Natural Graphite", "disclosure_required"),
        ],
        "geography_scopes": [
            ("EU", "jurisdiction"),
        ],
    },
    {
        "regulation_key": "CRMA_2024",
        "celex": "32024R1252",
        "title": "Critical Raw Materials Act (EU) 2024/1252 — ensuring secure supply of critical raw materials",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "status": "effective",
        "publication_date": date(2024, 5, 23),
        "effective_date": date(2024, 5, 23),
        "policy_theme": "critical_materials_supply_security",
        "material_scopes": [
            # CRMA Annex II: Strategic Raw Materials — the full designation list.
            # "Strategic" is a higher classification than "critical" (Annex I).
            # EU member states face binding 2030 benchmarks:
            #   ≥10% domestic extraction, ≥40% processing, ≥15% recycling.
            # scope_type "strategic_raw_material" is used (rather than "covered")
            # so that scoring can apply a higher obligation weight than the default
            # for Annex I critical materials. See evidence_query._SCOPE_TYPE_WEIGHT.
            #
            # Canonical names must match materials.canonical_name exactly.
            # Any name not found in the DB is logged as a warning and skipped.
            #
            # Battery / EV drivetrain materials (cathode, anode, current collector)
            ("Lithium",                "strategic_raw_material"),
            ("Cobalt",                 "strategic_raw_material"),
            ("Nickel",                 "strategic_raw_material"),
            ("Manganese",              "strategic_raw_material"),
            ("Natural Graphite",       "strategic_raw_material"),
            ("Copper",                 "strategic_raw_material"),
            # Industrial + electronics materials
            ("Boron",                  "strategic_raw_material"),
            ("Gallium",                "strategic_raw_material"),
            ("Germanium",              "strategic_raw_material"),
            ("Magnesium",              "strategic_raw_material"),
            ("Silicon (Anode Grade)",  "strategic_raw_material"),  # covers silicon metal broadly
            ("Titanium",               "strategic_raw_material"),
            ("Tungsten",               "strategic_raw_material"),
            ("Niobium",                "strategic_raw_material"),
            ("Bismuth",                "strategic_raw_material"),
            # Platinum-group metals (fuel cell catalysts, sensors)
            ("Platinum-Group Metals",  "strategic_raw_material"),
            # Magnet rare earth elements (EV traction motors)
            ("Neodymium",              "strategic_raw_material"),
            ("Praseodymium",           "strategic_raw_material"),
            ("Dysprosium",             "strategic_raw_material"),
            ("Terbium",                "strategic_raw_material"),
        ],
        "geography_scopes": [
            ("EU", "jurisdiction"),
        ],
    },
    {
        "regulation_key": "EU_CBAM",
        "celex": "32023R0956",
        "title": "Carbon Border Adjustment Mechanism (EU) 2023/956 — CBAM",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "status": "effective",
        "publication_date": date(2023, 5, 16),
        "effective_date": date(2023, 10, 1),
        "policy_theme": "carbon_pricing_trade",
        "material_scopes": [
            ("Aluminum", "covered"),
            ("Nickel", "covered"),
        ],
        "geography_scopes": [
            ("EU", "jurisdiction"),
            ("CN", "targeted_country"),
            ("RU", "targeted_country"),
        ],
    },
    {
        "regulation_key": "EU_CSDDD",
        "celex": "32024L1760",
        "title": "Corporate Sustainability Due Diligence Directive (EU) 2024/1760 — CSDDD",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "status": "enacted",
        "publication_date": date(2024, 7, 5),
        "effective_date": date(2027, 7, 26),
        "policy_theme": "supply_chain_due_diligence",
        "material_scopes": [
            ("Cobalt", "disclosure_required"),
            ("Lithium", "disclosure_required"),
            ("Natural Graphite", "disclosure_required"),
        ],
        "geography_scopes": [
            ("EU", "jurisdiction"),
            ("CD", "targeted_country"),
            ("CN", "targeted_country"),
        ],
    },
    {
        "regulation_key": "EU_CONFLICT_MINERALS",
        "celex": "32017R0821",
        "title": "EU Conflict Minerals Regulation (EU) 2017/821 — responsible sourcing of minerals",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "status": "effective",
        "publication_date": date(2017, 5, 17),
        "effective_date": date(2021, 1, 1),
        "policy_theme": "responsible_sourcing",
        "material_scopes": [
            ("Cobalt", "disclosure_required"),
        ],
        "geography_scopes": [
            ("EU", "jurisdiction"),
            ("CD", "targeted_country"),
        ],
    },
    {
        "regulation_key": "EU_REACH_COBALT",
        "celex": "32006R1907",
        "title": "REACH Regulation (EC) 1907/2006 — Registration, Evaluation, Authorisation of Chemicals (cobalt compounds SVHC)",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "status": "effective",
        "publication_date": date(2006, 12, 18),
        "effective_date": date(2009, 6, 1),
        "policy_theme": "chemicals_regulatory",
        "material_scopes": [
            ("Cobalt", "restricted"),
        ],
        "geography_scopes": [
            ("EU", "jurisdiction"),
        ],
    },
]


# ---------------------------------------------------------------------------
# EUR-Lex summary fetcher
# ---------------------------------------------------------------------------

# ``<script>`` and ``<style>`` blocks are stripped wholesale before we collapse
# the remaining tags. Multiline + dotall so the regex spans the entire block.
_SCRIPT_OR_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>",
    flags=re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(html_text: str) -> str:
    """Return readable plain text from raw HTML using stdlib only.

    Removes ``<script>``/``<style>`` blocks first so their contents do not leak
    into the summary, then collapses every remaining tag to a space and
    normalises whitespace. ``html.unescape`` decodes HTML entities (``&amp;``,
    ``&nbsp;``, etc.) back to plain characters.
    """
    no_scripts = _SCRIPT_OR_STYLE_RE.sub(" ", html_text)
    no_tags = _TAG_RE.sub(" ", no_scripts)
    decoded = html.unescape(no_tags)
    return _WHITESPACE_RE.sub(" ", decoded).strip()


def fetch_eurlex_summary(celex: str, timeout: int = 30) -> Optional[str]:
    """Fetch the regulation's introductory text from EUR-Lex HTML.

    Uses CELEX number to construct the URL. Extracts the first
    ``_SUMMARY_MAX_CHARS`` (800) characters of readable text from the document
    preamble. Returns a truncated plain-text summary, or ``None`` if the fetch
    fails or the content cannot be parsed.

    Never raises — logs a warning and returns ``None`` on any HTTP, network, or
    parsing failure so the caller can still insert the regulation row.
    """
    url = EURLEX_HTML_URL.format(celex=celex)
    log.info("eurlex.fetch_summary.start", celex=celex, url=url)
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning(
            "eurlex.fetch_summary.http_error",
            celex=celex,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None
    except Exception as exc:  # pragma: no cover - defensive net for unexpected errors
        log.warning(
            "eurlex.fetch_summary.unexpected_error",
            celex=celex,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None

    try:
        plain = _strip_html(response.text)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("eurlex.fetch_summary.parse_error", celex=celex, error=str(exc))
        return None

    if not plain:
        log.warning("eurlex.fetch_summary.empty_body", celex=celex)
        return None

    summary = plain[:_SUMMARY_MAX_CHARS]
    log.info("eurlex.fetch_summary.done", celex=celex, chars=len(summary))
    return summary


# ---------------------------------------------------------------------------
# Source / SourceDocument helpers
# ---------------------------------------------------------------------------

def _get_or_create_eurlex_source(session: Session) -> int:
    """Get or create the ``Source`` row for EUR-Lex. Returns ``source.id``."""
    existing = session.scalar(select(Source).where(Source.name == _SOURCE_NAME))
    if existing is not None:
        return existing.id

    source = Source(
        name=_SOURCE_NAME,
        source_type=_SOURCE_TYPE,
        phase=_SOURCE_PHASE,
        is_active=True,
        config_json={"base_url": _SOURCE_BASE_URL, "method": "manifest"},
    )
    session.add(source)
    session.flush()
    log.info("eurlex.source_created", source_id=source.id)
    return source.id


def _get_or_create_source_document(
    session: Session,
    source_id: int,
    celex: str,
    title: str,
) -> int:
    """Idempotent SourceDocument upsert keyed on ``(source_id, external_id)``."""
    external_id = f"eurlex_{celex}"
    url = EURLEX_HTML_URL.format(celex=celex)

    existing = session.scalar(
        select(SourceDocument).where(
            SourceDocument.source_id == source_id,
            SourceDocument.external_id == external_id,
        )
    )
    if existing is not None:
        return existing.id

    doc = SourceDocument(
        source_id=source_id,
        external_id=external_id,
        title=title,
        url=url,
        document_type="regulation",
        metadata_json={"celex": celex, "url": url},
    )
    session.add(doc)
    session.flush()
    return doc.id


# ---------------------------------------------------------------------------
# Scope upserts
# ---------------------------------------------------------------------------

def _upsert_material_scopes(
    session: Session,
    *,
    regulation_id: int,
    regulation_key: str,
    scopes: list[tuple[str, str]],
    material_id_by_name: dict[str, int],
) -> int:
    """Insert any missing ``RegulationMaterialScope`` rows for one regulation.

    Uses check-then-insert against the ``(regulation_id, material_id)`` unique
    constraint. Existing rows are never modified — we treat curated scope_type
    values as immutable per regulation.

    Returns the number of new rows inserted (existing rows are silently kept).
    """
    inserted = 0
    for material_name, scope_type in scopes:
        material_id = material_id_by_name.get(material_name)
        if material_id is None:
            log.warning(
                "eurlex.material_not_found",
                regulation_key=regulation_key,
                material=material_name,
            )
            continue

        exists = session.scalar(
            select(RegulationMaterialScope).where(
                RegulationMaterialScope.regulation_id == regulation_id,
                RegulationMaterialScope.material_id == material_id,
            )
        )
        if exists is not None:
            continue

        session.add(
            RegulationMaterialScope(
                regulation_id=regulation_id,
                material_id=material_id,
                scope_type=scope_type,
            )
        )
        inserted += 1
    return inserted


def _upsert_geography_scopes(
    session: Session,
    *,
    regulation_id: int,
    regulation_key: str,
    scopes: list[tuple[str, str]],
) -> int:
    """Insert any missing ``RegulationGeographyScope`` rows for one regulation."""
    inserted = 0
    for country_code, scope_type in scopes:
        exists = session.scalar(
            select(RegulationGeographyScope).where(
                RegulationGeographyScope.regulation_id == regulation_id,
                RegulationGeographyScope.country_code == country_code,
            )
        )
        if exists is not None:
            continue

        session.add(
            RegulationGeographyScope(
                regulation_id=regulation_id,
                country_code=country_code,
                scope_type=scope_type,
            )
        )
        inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# Risk event generation helpers
# ---------------------------------------------------------------------------

# Severity by regulation status.
#
# Standing regulations use event_date=None so they never fall outside the
# 365-day evidence window and are always returned by get_events_for_regulations().
# effective_date is stored in metadata_json so the decay engine can apply the
# ±90-day step-up multiplier when an enforcement deadline is imminent.
#
# Calibration rationale:
#   effective → 0.55  live enforcement; meaningful compliance pressure on any
#                     company within scope
#   enacted   → 0.40  legislatively final but not yet in force (e.g. CSDDD
#                     effective 2027); compliance cost is already visible
#   proposed  → 0.25  early-stage signal only; weight kept low until enacted
_SEVERITY_BY_STATUS: dict[str, float] = {
    "effective": 0.55,
    "enacted":   0.40,
    "proposed":  0.25,
}
_DEFAULT_REGULATION_SEVERITY = 0.35


def _build_regulation_event(
    reg: dict,
    regulation_id: int,
    source_document_id: int,
) -> tuple[RiskEvent, RiskEventRegulation]:
    """Return an unsaved (RiskEvent, RiskEventRegulation) pair for one regulation.

    The caller must ``session.add()`` both objects and ``session.flush()``
    before the junction row can be committed (since the junction FK requires
    ``risk_event.id``).

    Design notes
    ------------
    ``event_date = None``
        Regulations are standing obligations, not one-time events. Using NULL
        means the row always satisfies the ``event_date IS NULL`` branch in the
        evidence-window query and gets ``recency_multiplier = 1.0`` via the
        ``_event_date_as_date(ew, fallback=as_of_date)`` fallback.

    ``confidence_score = 1.0``
        Manifest entries are hand-curated; there is no parser uncertainty.

    ``content_hash``
        Keyed on ``regulation_key + title`` — stable across re-ingest runs so
        the content_hash deduplication path in other ingesters would also catch
        accidental double-inserts.
    """
    key = reg["regulation_key"]
    title = reg["title"]
    severity = _SEVERITY_BY_STATUS.get(reg.get("status", ""), _DEFAULT_REGULATION_SEVERITY)
    effective_date: Optional[date] = reg.get("effective_date")

    event_title = f"Regulatory compliance obligation: {title}"
    content_hash = hashlib.sha256(
        f"{key}|{event_title}".encode()
    ).hexdigest()[:64]

    meta: dict = {
        "regulation_key": key,
        "celex": reg.get("celex", ""),
        "policy_theme": reg.get("policy_theme", ""),
    }
    if effective_date is not None:
        meta["effective_date"] = effective_date.isoformat()

    event = RiskEvent(
        source_document_id=source_document_id,
        event_type="REGULATORY_IMPLEMENTATION",
        event_date=None,            # standing obligation — always in evidence window
        title=event_title,
        severity_score=severity,
        confidence_score=1.0,
        risk_categories_json=["regulatory_compliance"],
        content_hash=content_hash,
        metadata_json=meta,
        verified=True,
    )
    # Junction row is returned with risk_event_id intentionally unset here;
    # caller must flush the event to obtain its id before committing.
    junction = RiskEventRegulation(
        regulation_id=regulation_id,
        relevance_score=1.0,
        match_reason="regulation_enacted",
    )
    return event, junction


# ---------------------------------------------------------------------------
# Main ingest function
# ---------------------------------------------------------------------------

def ingest_eurlex(
    session: Session,
    fetch_summaries: bool = True,
) -> dict[str, int]:
    """Upsert EUR-Lex battery regulations into the regulations table.

    Args:
        session: SQLAlchemy session. Committed once at the end of a successful run.
        fetch_summaries: If ``True`` (default), fetch regulation summary text
            from EUR-Lex HTML when a row is first inserted (or backfill the
            summary on an existing row whose ``summary`` is ``NULL``). Set
            ``False`` to run without network (tests, air-gapped environments).

    Returns:
        ``{
            "inserted": int,        # new Regulation rows created
            "updated": int,         # existing rows refreshed (summary backfilled)
            "skipped": int,         # rows where regulation_key already exists, no changes
            "material_scopes": int, # new RegulationMaterialScope rows
            "geography_scopes": int,# new RegulationGeographyScope rows
            "risk_events": int,     # new RiskEvent rows created for regulations
            "regulation_links": int,# new RiskEventRegulation junction rows created
        }``
    """
    source_id = _get_or_create_eurlex_source(session)

    # ── Pre-build material name → id lookup (single query) ──────────────────
    needed_material_names: set[str] = {
        material_name
        for reg in BATTERY_REGULATIONS
        for material_name, _ in reg["material_scopes"]
    }
    material_id_by_name: dict[str, int] = {
        m.canonical_name: m.id
        for m in session.scalars(
            select(Material).where(Material.canonical_name.in_(needed_material_names))
        ).all()
    }
    missing_materials = needed_material_names - set(material_id_by_name.keys())
    if missing_materials:
        log.warning(
            "eurlex.materials_not_in_db",
            missing=sorted(missing_materials),
            hint="run seed-materials first; affected scope rows will be skipped",
        )

    inserted = updated = skipped = 0
    material_scopes_inserted = geography_scopes_inserted = 0
    risk_events_inserted = regulation_links_inserted = 0

    for reg in BATTERY_REGULATIONS:
        key = reg["regulation_key"]
        celex = reg["celex"]
        title = reg["title"]

        existing = session.scalar(
            select(Regulation).where(Regulation.regulation_key == key)
        )

        if existing is None:
            summary: Optional[str] = None
            if fetch_summaries:
                summary = fetch_eurlex_summary(celex)

            source_document_id = _get_or_create_source_document(
                session, source_id, celex, title
            )

            regulation = Regulation(
                source_document_id=source_document_id,
                regulation_key=key,
                title=title,
                issuing_body=reg["issuing_body"],
                geography=reg["geography"],
                policy_theme=reg["policy_theme"],
                status=reg["status"],
                publication_date=reg["publication_date"],
                effective_date=reg["effective_date"],
                summary=summary,
                metadata_json={
                    "celex": celex,
                    "url": EURLEX_HTML_URL.format(celex=celex),
                    "ingest_source": _SOURCE_NAME,
                },
                verified=True,
            )
            session.add(regulation)
            session.flush()
            regulation_id = regulation.id
            inserted += 1
            log.info(
                "eurlex.regulation_inserted",
                regulation_key=key,
                celex=celex,
                has_summary=summary is not None,
            )
        else:
            regulation_id = existing.id

            # Backfill summary only — never overwrite an existing summary, and
            # never touch other manifest fields once the row has been seeded.
            if fetch_summaries and not existing.summary:
                summary = fetch_eurlex_summary(celex)
                if summary:
                    existing.summary = summary
                    updated += 1
                    log.info(
                        "eurlex.regulation_summary_backfilled",
                        regulation_key=key,
                        celex=celex,
                    )
                else:
                    skipped += 1
            else:
                skipped += 1

        material_scopes_inserted += _upsert_material_scopes(
            session,
            regulation_id=regulation_id,
            regulation_key=key,
            scopes=reg["material_scopes"],
            material_id_by_name=material_id_by_name,
        )
        geography_scopes_inserted += _upsert_geography_scopes(
            session,
            regulation_id=regulation_id,
            regulation_key=key,
            scopes=reg["geography_scopes"],
        )

        # ── Risk event generation ─────────────────────────────────────────────
        # One RiskEvent per regulation links via RiskEventRegulation to populate
        # risk_event_regulations — the table get_events_for_regulations() reads.
        # Idempotent: check for an existing junction row before creating anything.
        # A regulation may already have a linked event from a prior ingest run.
        has_event = session.scalar(
            select(RiskEventRegulation).where(
                RiskEventRegulation.regulation_id == regulation_id
            )
        )
        if has_event is None:
            source_doc_for_event = session.scalar(
                select(Regulation.source_document_id).where(
                    Regulation.id == regulation_id
                )
            )
            event, junction = _build_regulation_event(
                reg, regulation_id, source_doc_for_event
            )
            session.add(event)
            session.flush()            # populate event.id before setting FK
            junction.risk_event_id = event.id
            session.add(junction)
            risk_events_inserted += 1
            regulation_links_inserted += 1
            log.info(
                "eurlex.risk_event_created",
                regulation_key=key,
                event_id=event.id,
                severity=event.severity_score,
            )

    session.commit()

    result = {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "material_scopes": material_scopes_inserted,
        "geography_scopes": geography_scopes_inserted,
        "risk_events": risk_events_inserted,
        "regulation_links": regulation_links_inserted,
    }
    log.info("eurlex.ingest.done", **result)
    return result
