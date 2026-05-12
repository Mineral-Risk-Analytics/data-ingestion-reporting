"""EUR-Lex regulation ingestion — alias-resolver based.

This module fetches regulation summaries and creates risk events for EU
regulations.  It does NOT define which regulations exist — that's
``seed_regulations.py``'s job — and it does NOT auto-create regulation
rows when it encounters an unknown CELEX number.

Architecture (post 2026-05-05 refactor)
---------------------------------------
The list of CELEX numbers this ingester pulls is derived at runtime from
``regulation_aliases``: every row with ``source_system='eurlex_celex'``
and ``is_skipped=false`` is in scope.  To add a new EU regulation, the
sequence is:

    1. Add the regulation to ``seed_regulations.py:_REGULATIONS``
       (with material_scopes + geography_scopes).
    2. Add a CELEX → regulation_key alias to
       ``seed_regulation_aliases.py:_EURLEX_CELEX``.
    3. ``bdi-ingest seed-regulations`` then ``seed-regulation-aliases``.
    4. ``bdi-ingest ingest-eurlex`` will pick it up on the next run.

Mutable fields this ingester writes
-----------------------------------
On the existing ``Regulation`` row (looked up via the alias resolver):
  * ``summary``                 — backfilled when NULL (HTML fetch from EUR-Lex)
  * ``metadata_json.url``       — stored on first attach
  * ``metadata_json.last_seen`` — bumped on every successful fetch
  * ``source_document_id``      — set to the EUR-Lex SourceDocument
                                  (created on first attach if missing)

Risk event generation
---------------------
One ``RiskEvent`` per regulation links via ``RiskEventRegulation`` to
populate ``risk_event_regulations`` — the table
``get_events_for_regulations()`` reads.  Events use ``event_date=None``
(standing obligations — always in scope) and severity per
``_SEVERITY_BY_STATUS``:

    effective → 0.55  enforcement live; meaningful compliance pressure
    enacted   → 0.40  passed but not yet in force; deadline visible
    proposed  → 0.25  early signal only

Update cadence
    Run ``ingest-eurlex`` quarterly — these instruments do not change
    frequently, but ``last_seen`` provides a freshness signal for
    partner-side data quality monitoring.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import date, datetime, timezone
from typing import Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.documents import SourceDocument
from app.models.regulatory import (
    Regulation,
    RegulationMaterialScope,
    RegulationSourceAlias,
    RiskEvent,
    RiskEventMaterial,
    RiskEventRegulation,
)
from app.models.source import Source
from app.services.ingestion.regulation_resolver import RegulationAliasResolver

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Source registration
# ---------------------------------------------------------------------------

_SOURCE_NAME = "EUR-Lex"
_SOURCE_TYPE = "eurlex"
_SOURCE_PHASE = "1"
_SOURCE_BASE_URL = "https://eur-lex.europa.eu"
_ALIAS_SOURCE_SYSTEM = "eurlex_celex"

EURLEX_HTML_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:{celex}"

# Maximum characters of preamble text retained as ``Regulation.summary``.
_SUMMARY_MAX_CHARS = 800


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
    """Return readable plain text from raw HTML using stdlib only."""
    no_scripts = _SCRIPT_OR_STYLE_RE.sub(" ", html_text)
    no_tags = _TAG_RE.sub(" ", no_scripts)
    decoded = html.unescape(no_tags)
    return _WHITESPACE_RE.sub(" ", decoded).strip()


def fetch_eurlex_summary(celex: str, timeout: int = 30) -> Optional[str]:
    """Fetch the regulation's introductory text from EUR-Lex HTML.

    Uses CELEX number to construct the URL.  Extracts the first
    ``_SUMMARY_MAX_CHARS`` (800) characters of readable text from the
    document preamble.  Returns a truncated plain-text summary, or
    ``None`` if the fetch fails or content cannot be parsed.

    Never raises — logs a warning and returns ``None`` on any HTTP /
    network / parsing failure so the caller can still proceed.
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
    except Exception as exc:  # pragma: no cover - defensive net
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
        config_json={"base_url": _SOURCE_BASE_URL, "method": "alias_resolver"},
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
# Risk event generation helpers
# ---------------------------------------------------------------------------

# Severity by regulation status.  See module docstring for calibration.
# Public aliases below — exported so the API layer (RegulationRead schema)
# can return the per-regulation severity weight without importing this
# ingestion module.  Underscore-prefixed names retained for backwards
# compatibility with existing internal callers.
SEVERITY_BY_STATUS: dict[str, float] = {
    "effective": 0.55,
    "enacted":   0.40,
    "proposed":  0.25,
}
DEFAULT_REGULATION_SEVERITY = 0.35

# Backwards-compatible internal aliases — keep until existing callers migrate.
_SEVERITY_BY_STATUS = SEVERITY_BY_STATUS
_DEFAULT_REGULATION_SEVERITY = DEFAULT_REGULATION_SEVERITY


def status_severity_weight(status: str | None) -> float:
    """Return the severity weight a RiskEvent should carry given the
    regulation's status.  Public helper so the API can serialize the
    same value the ingester writes.
    """
    if not status:
        return DEFAULT_REGULATION_SEVERITY
    return SEVERITY_BY_STATUS.get(status, DEFAULT_REGULATION_SEVERITY)


# Relevance score by RegulationMaterialScope.scope_type.  Used when
# propagating the regulation's scoped materials onto the emitted RiskEvent.
#
# Calibration:
#   banned (1.00)              — the regulation explicitly prohibits this
#                                material; events should surface at full weight.
#   restricted (0.90)          — regulation imposes constraints (caps, due-
#                                diligence requirements, geographic limits).
#   covered (0.80)             — material falls under the regulation's
#                                purview but no specific prohibition.
#   disclosure_required (0.70) — softest binding; only requires reporting.
#
# The 0.70–1.00 band intentionally sits above keyword-based attribution
# scores (typically 0.50–0.85) — RegulationMaterialScope is partner-curated,
# which is the highest-confidence attribution mechanism we have.
_SCOPE_RELEVANCE: dict[str, float] = {
    "banned": 1.00,
    "restricted": 0.90,
    "covered": 0.80,
    "disclosure_required": 0.70,
}
_DEFAULT_SCOPE_RELEVANCE = 0.80


def _attach_material_scope_to_event(
    session: Session,
    event_id: int,
    regulation_id: int,
) -> int:
    """Write one RiskEventMaterial row per RegulationMaterialScope entry.

    Returns the number of junction rows added.  Idempotent at the
    (event_id, material_id) pair level — the surrounding loop only calls
    this after an event was newly created, so duplicate writes are not
    expected, but we still guard against them.
    """
    scopes = session.scalars(
        select(RegulationMaterialScope).where(
            RegulationMaterialScope.regulation_id == regulation_id
        )
    ).all()
    if not scopes:
        return 0

    added = 0
    seen_material_ids: set[int] = set()
    for scope in scopes:
        if scope.material_id in seen_material_ids:
            continue
        seen_material_ids.add(scope.material_id)
        relevance = _SCOPE_RELEVANCE.get(scope.scope_type, _DEFAULT_SCOPE_RELEVANCE)
        session.add(
            RiskEventMaterial(
                risk_event_id=event_id,
                material_id=scope.material_id,
                relevance_score=relevance,
                match_reason=f"regulation_scope:{scope.scope_type}",
            )
        )
        added += 1
    return added


def _build_regulation_event(
    regulation: Regulation,
    celex: str,
    source_document_id: int,
) -> tuple[RiskEvent, RiskEventRegulation]:
    """Return an unsaved (RiskEvent, RiskEventRegulation) pair.

    ``event_date = None`` because regulations are standing obligations,
    not one-time events.  ``effective_date`` is stored in metadata_json
    so the decay engine can apply the ±90-day step-up multiplier when
    enforcement is imminent.

    ``content_hash`` is keyed on ``regulation_key + title`` — stable
    across re-ingest runs so the dedup path catches accidental
    double-inserts.
    """
    key = regulation.regulation_key
    title = regulation.title or key
    severity = _SEVERITY_BY_STATUS.get(
        regulation.status or "", _DEFAULT_REGULATION_SEVERITY
    )
    effective_date: Optional[date] = regulation.effective_date

    event_title = f"Regulatory compliance obligation: {title}"
    content_hash = hashlib.sha256(
        f"{key}|{event_title}".encode()
    ).hexdigest()[:64]

    meta: dict = {
        "regulation_key": key,
        "celex": celex,
        "policy_theme": regulation.policy_theme or "",
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
    junction = RiskEventRegulation(
        regulation_id=regulation.id,
        relevance_score=1.0,
        match_reason="regulation_enacted",
    )
    return event, junction


# ---------------------------------------------------------------------------
# Main ingest function
# ---------------------------------------------------------------------------

def _list_active_celex(session: Session) -> list[str]:
    """Return CELEX numbers for every active eurlex_celex alias.

    Skips rows where ``is_skipped=true`` (partner-curated audit trail of
    considered-and-rejected EUR-Lex documents).
    """
    rows = session.scalars(
        select(RegulationSourceAlias).where(
            RegulationSourceAlias.source_system == _ALIAS_SOURCE_SYSTEM,
            RegulationSourceAlias.is_skipped.is_(False),
        )
    ).all()
    return [row.source_key for row in rows]


def ingest_eurlex(
    session: Session,
    fetch_summaries: bool = True,
) -> dict[str, int]:
    """Refresh EUR-Lex regulation data and create risk events.

    Iterates every active CELEX number in ``regulation_aliases``,
    resolves it to a canonical ``Regulation`` row, and:
      * Backfills ``summary`` when NULL (HTML fetch from EUR-Lex)
      * Bumps ``metadata_json.last_seen``
      * Attaches ``source_document_id`` if not yet set
      * Creates one ``RiskEvent`` + ``RiskEventRegulation`` junction
        per regulation if not yet present

    Does NOT create regulations.  CELEX values without an alias row
    are surfaced as warnings — partner needs to add the regulation +
    alias in the seed files.

    Args:
        session:          SQLAlchemy session.  Committed at end of run.
        fetch_summaries:  If False, skips the HTTP fetch entirely.
                          Useful for tests / air-gapped environments.

    Returns:
        Counts of inserts / updates / skips, plus risk event creation.
    """
    source_id = _get_or_create_eurlex_source(session)
    resolver = RegulationAliasResolver(session)

    celex_list = _list_active_celex(session)
    if not celex_list:
        log.warning(
            "eurlex.no_aliases_seeded",
            hint=(
                "regulation_aliases is empty for source_system='eurlex_celex'. "
                "Run `bdi-ingest seed-regulation-aliases` first."
            ),
        )
        return {
            "celex_processed": 0,
            "summary_backfilled": 0,
            "summary_skipped": 0,
            "unknown_celex": 0,
            "skipped_celex": 0,
            "source_documents_attached": 0,
            "risk_events_created": 0,
        }

    summary_backfilled = summary_skipped = 0
    unknown_celex = skipped_celex = 0
    source_docs_attached = 0
    risk_events_created = 0
    material_links_created = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for celex in celex_list:
        result = resolver.resolve(_ALIAS_SOURCE_SYSTEM, celex)

        if result.status == "unknown":
            log.warning(
                "eurlex.unknown_celex",
                celex=celex,
                hint=(
                    "alias row missing — add to seed_regulation_aliases.py "
                    "and re-run seed-regulation-aliases."
                ),
            )
            unknown_celex += 1
            continue

        if result.status == "skipped":
            skipped_celex += 1
            continue

        regulation = result.regulation
        assert regulation is not None  # status == "ok"
        title = regulation.title or regulation.regulation_key

        # ── Attach SourceDocument if not yet linked ─────────────────────
        if regulation.source_document_id is None:
            source_document_id = _get_or_create_source_document(
                session, source_id, celex, title
            )
            regulation.source_document_id = source_document_id
            source_docs_attached += 1
        else:
            source_document_id = regulation.source_document_id

        # ── Backfill summary if missing ─────────────────────────────────
        if fetch_summaries and not regulation.summary:
            fetched = fetch_eurlex_summary(celex)
            if fetched:
                regulation.summary = fetched
                summary_backfilled += 1
            else:
                summary_skipped += 1
        else:
            summary_skipped += 1

        # ── Bump last_seen on metadata_json (idempotent) ────────────────
        meta = dict(regulation.metadata_json or {})
        meta["last_seen"] = now_iso
        meta.setdefault("celex", celex)
        meta.setdefault("url", EURLEX_HTML_URL.format(celex=celex))
        regulation.metadata_json = meta

        # ── Risk event: one per regulation, idempotent ──────────────────
        has_event = session.scalar(
            select(RiskEventRegulation).where(
                RiskEventRegulation.regulation_id == regulation.id
            )
        )
        if has_event is None:
            event, junction = _build_regulation_event(
                regulation, celex, source_document_id
            )
            session.add(event)
            session.flush()
            junction.risk_event_id = event.id
            session.add(junction)

            # Propagate the regulation's scoped materials onto the event so
            # the per-material event feed actually surfaces EU regulations.
            # Without this, RegulationMaterialScope lived in isolation —
            # the regulation knew which materials it covered, but the
            # emitted RiskEvent did not, leaving the material-level feed
            # under-attributed for any user filtering by mineral.
            material_links_created += _attach_material_scope_to_event(
                session,
                event_id=event.id,
                regulation_id=regulation.id,
            )
            risk_events_created += 1
            log.info(
                "eurlex.risk_event_created",
                regulation_key=regulation.regulation_key,
                celex=celex,
                event_id=event.id,
                severity=event.severity_score,
            )

    session.commit()

    result_summary = {
        "celex_processed": len(celex_list),
        "summary_backfilled": summary_backfilled,
        "summary_skipped": summary_skipped,
        "unknown_celex": unknown_celex,
        "skipped_celex": skipped_celex,
        "source_documents_attached": source_docs_attached,
        "risk_events_created": risk_events_created,
        "material_links_created": material_links_created,
    }
    log.info("eurlex.ingest.done", **result_summary)
    return result_summary


# ---------------------------------------------------------------------------
# Backfill: attach material junctions onto previously-ingested EUR-Lex events
# ---------------------------------------------------------------------------

def backfill_regulation_event_materials(session: Session) -> dict[str, int]:
    """Add RiskEventMaterial rows for past EUR-Lex events that lack them.

    Use when this attribution fix lands on a DB that already has EUR-Lex
    events ingested without material junctions.  Safe to re-run: for each
    candidate event we check the (event_id, material_id) pair and only
    insert junctions for scoped materials that aren't already linked.

    Returns counts:
        events_examined:   how many EUR-Lex-linked RiskEvents we looked at
        events_updated:    how many of those got at least one new junction
        material_links_added: total new RiskEventMaterial rows inserted
    """
    # Pull every RiskEvent linked to a Regulation via RiskEventRegulation.
    rows = session.execute(
        select(RiskEvent.id, RiskEventRegulation.regulation_id)
        .join(
            RiskEventRegulation,
            RiskEventRegulation.risk_event_id == RiskEvent.id,
        )
    ).all()

    events_examined = len(rows)
    events_updated = 0
    material_links_added = 0

    for event_id, regulation_id in rows:
        # Skip if this event already has any RiskEventMaterial row — we
        # don't want to mix scope-derived attribution with content-derived
        # attribution after the fact.
        already_linked = session.execute(
            select(RiskEventMaterial.id)
            .where(RiskEventMaterial.risk_event_id == event_id)
            .limit(1)
        ).first()
        if already_linked is not None:
            continue

        added = _attach_material_scope_to_event(
            session, event_id=event_id, regulation_id=regulation_id
        )
        if added > 0:
            events_updated += 1
            material_links_added += added

    session.commit()
    result = {
        "events_examined": events_examined,
        "events_updated": events_updated,
        "material_links_added": material_links_added,
    }
    log.info("eurlex.backfill_material_links.done", **result)
    return result
