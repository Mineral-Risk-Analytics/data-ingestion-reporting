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
    RegulationGeographyScope,
    RegulationMaterialScope,
    RegulationSourceAlias,
    RiskEvent,
    RiskEventGeography,
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

# Bibliographic notice — different EUR-Lex view that exposes structured
# legal-status fields ("Date of effect", "Date of end of validity",
# "Repealed by", etc.).  Used by the status-change detector to flag
# regulations whose lifecycle has changed since seed time.  Added
# 2026-05-17 (Q1 fix).
EURLEX_BIBLIOGRAPHIC_URL = "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:{celex}"

# Maximum characters of preamble text retained as ``Regulation.summary``.
# 2026-05-17: bumped 800 → 2500 (partner direction).  800 chars captured
# only the preamble citations ("Having regard to..., Whereas..."), which
# was enough for list-view identification but typically cut off before
# the first operative article.  2500 chars (~400 words) captures the
# preamble plus the first 1-3 operative articles for most regulations,
# enough to identify what the regulation actually requires without
# storing the full legal text.  Full-text reads should still link out
# to EUR-Lex directly via metadata_json["url"].
_SUMMARY_MAX_CHARS = 2500


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
# Status-change detector (Q1, 2026-05-17)
# ---------------------------------------------------------------------------
# These patterns parse the EUR-Lex "bibliographic notice" page (different
# URL from the body HTML).  That page exposes structured legal-status
# fields as label: value pairs that survive the _strip_html flattening
# as recognisable substrings.
#
# Design note: we deliberately do NOT auto-mutate Regulation.status.  A
# parsed signal could be wrong (HTML structure changes, label ambiguity,
# "Date of end of validity: future date X (subject to amendment)"
# scenarios).  Instead, we write a review-pending entry to metadata_json
# and surface it via the list_pending_status_changes() helper so partner
# can confirm before the canonical status field changes.
#
# EUR-Lex bibliographic notice uses DD/MM/YYYY date format (EU standard).

_EURLEX_DATE_OF_EFFECT_RE = re.compile(
    r"Date of effect[:\s]+(\d{2}/\d{2}/\d{4})",
    flags=re.IGNORECASE,
)
_EURLEX_DATE_END_VALIDITY_RE = re.compile(
    r"Date of end of validity[:\s]+(\d{2}/\d{2}/\d{4})",
    flags=re.IGNORECASE,
)
# Matches a CELEX-formatted identifier after "Repealed by".  CELEX format
# is roughly: 4-digit year + sector letter + 4-5 digit number, sometimes
# with a sector prefix like 1 (treaties), 3 (legislation), 4 (case law).
# Pattern stays loose to tolerate format variations.
_EURLEX_REPEALED_BY_RE = re.compile(
    r"Repealed by[:\s]+(\d{5}[A-Z]\d{3,5})",
    flags=re.IGNORECASE,
)


def _parse_eu_date(date_str: str) -> Optional[date]:
    """Parse DD/MM/YYYY (EU standard) into a ``date`` object.  Returns
    ``None`` on any parse failure — defensive against off-format strings."""
    try:
        return datetime.strptime(date_str.strip(), "%d/%m/%Y").date()
    except (ValueError, AttributeError):
        return None


def fetch_eurlex_metadata(celex: str, timeout: int = 30) -> Optional[dict]:
    """Fetch the structured legal-status fields from EUR-Lex's
    bibliographic notice page.

    Returns a dict with any subset of the following keys (only present
    when found in the source):
        date_of_effect           — date  (Regulation's effective date)
        date_of_end_of_validity  — date  (set when a regulation has been
                                          formally end-dated)
        repealed_by_celex        — str   (CELEX of the repealing instrument)

    Returns ``None`` on any HTTP / network / parsing failure or when no
    recognisable fields could be extracted — caller treats ``None`` as
    "no detectable status signal", which is the conservative default.
    """
    url = EURLEX_BIBLIOGRAPHIC_URL.format(celex=celex)
    log.debug("eurlex.fetch_metadata.start", celex=celex, url=url)
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning(
            "eurlex.fetch_metadata.http_error",
            celex=celex,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(
            "eurlex.fetch_metadata.unexpected_error",
            celex=celex,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None

    try:
        text = _strip_html(response.text)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("eurlex.fetch_metadata.parse_error", celex=celex, error=str(exc))
        return None

    metadata: dict = {}

    m = _EURLEX_DATE_OF_EFFECT_RE.search(text)
    if m:
        parsed = _parse_eu_date(m.group(1))
        if parsed is not None:
            metadata["date_of_effect"] = parsed

    m = _EURLEX_DATE_END_VALIDITY_RE.search(text)
    if m:
        parsed = _parse_eu_date(m.group(1))
        if parsed is not None:
            metadata["date_of_end_of_validity"] = parsed

    m = _EURLEX_REPEALED_BY_RE.search(text)
    if m:
        metadata["repealed_by_celex"] = m.group(1)

    if not metadata:
        log.debug("eurlex.fetch_metadata.no_fields_found", celex=celex)
        return None

    log.info("eurlex.fetch_metadata.done", celex=celex, fields=list(metadata.keys()))
    return metadata


def detect_status_from_metadata(
    metadata: dict, as_of: date,
) -> Optional[str]:
    """Map EUR-Lex bibliographic fields to our internal status vocabulary.

    Returns one of ``"effective"`` / ``"enacted"`` / ``"superseded"``,
    or ``None`` when the metadata doesn't contain enough signal to
    classify.

    Priority order (strongest signal first):
      1. ``repealed_by_celex`` is set                         → "superseded"
      2. ``date_of_end_of_validity`` is in the past            → "superseded"
      3. ``date_of_effect`` is in the past (or today)          → "effective"
      4. ``date_of_effect`` is in the future                   → "enacted"
      5. Otherwise                                             → None

    Note: we deliberately don't return "proposed" — EUR-Lex hosts only
    adopted texts.  Proposed regulations have COM-document identifiers
    not CELEX-style numbers, and we'd need a separate ingester for
    those (out of scope for this version).
    """
    if not metadata:
        return None

    if metadata.get("repealed_by_celex"):
        return "superseded"

    end_validity = metadata.get("date_of_end_of_validity")
    if end_validity is not None and end_validity < as_of:
        return "superseded"

    effective_date = metadata.get("date_of_effect")
    if effective_date is None:
        return None

    if effective_date <= as_of:
        return "effective"
    return "enacted"


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
    # 2026-05-17 partner-direction recalibration:
    #   effective  : 0.55 → 0.70  (parity with FR final-rule baseline;
    #                              an actively-enforced EU regulation
    #                              should carry the same event weight as
    #                              a US final rule)
    #   enacted    : 0.40 (unchanged — "passed but not yet in force"
    #                      reflects compliance prep pressure, not active
    #                      enforcement)
    #   proposed   : 0.25 → 0.15  (clearer "early signal only" tier —
    #                              proposed regulations frequently die
    #                              in legislative process and shouldn't
    #                              accrete weight prematurely)
    #   superseded : 0.05 → 0.0   (a repealed regulation does NOT
    #                              contribute current risk — it's not in
    #                              effect.  Audit-trail value remains
    #                              via the metadata_json fields and the
    #                              fact that the event row still exists,
    #                              but score contribution is zero)
    "effective":  0.70,
    "enacted":    0.40,
    "proposed":   0.15,
    "superseded": 0.00,
}
DEFAULT_REGULATION_SEVERITY = 0.35

# Backwards-compatible internal aliases — keep until existing callers migrate.
_SEVERITY_BY_STATUS = SEVERITY_BY_STATUS
_DEFAULT_REGULATION_SEVERITY = DEFAULT_REGULATION_SEVERITY


def status_severity_weight(status: str | None) -> float:
    """Return the severity weight a RiskEvent should carry given the
    regulation's status.  Public helper so the API can serialize the
    same value the ingester writes.

    Note: this returns the global default per status.  For per-regulation
    overrides (where partner has curated a specific severity for a
    specific regulation × status pair), use ``regulation_severity_weight``.
    """
    if not status:
        return DEFAULT_REGULATION_SEVERITY
    return SEVERITY_BY_STATUS.get(status, DEFAULT_REGULATION_SEVERITY)


def regulation_severity_weight(regulation: "Regulation") -> float:
    """Return the effective severity for a regulation, considering any
    partner-curated per-regulation override.

    Lookup order (first hit wins):

      1. ``Regulation.metadata_json["severity_overrides"][status]``
         — partner-curated override expressing that a specific
         regulation deserves a different severity than the global
         per-status default.  Useful when the qualitative bite of two
         same-status regulations differs significantly (e.g., UFLPA
         carries customs-seizure risk; CBAM is a carbon adjustment).
      2. ``SEVERITY_BY_STATUS[status]`` — global default by status.
      3. ``DEFAULT_REGULATION_SEVERITY`` — fallback when status missing
         or unrecognised.

    Example seed entry surfacing the override pattern::

        {
            "regulation_key": "UFLPA",
            "status": "effective",
            "metadata_json": {
                "severity_overrides": {
                    "effective": 0.85,
                },
                "severity_override_rationale": (
                    "Customs-seizure consequence is qualitatively "
                    "stronger than the 0.70 effective-default suggests."
                ),
            },
        }

    The optional ``severity_override_rationale`` field is documentation
    only — the resolver doesn't read it — but lets partner explain why
    a specific regulation deserved a non-default value.
    """
    status = regulation.status or ""
    metadata = regulation.metadata_json
    if isinstance(metadata, dict):
        overrides = metadata.get("severity_overrides")
        if isinstance(overrides, dict) and status in overrides:
            try:
                value = float(overrides[status])
                # Clamp to a sane range so a typo in the seed file can't
                # produce a severity > 1.0 or < 0.0.  Same clamp the
                # downstream impact calculator already applies.
                return max(0.0, min(1.0, value))
            except (TypeError, ValueError):
                log.warning(
                    "eurlex.severity_override_invalid",
                    regulation_key=regulation.regulation_key,
                    status=status,
                    raw_value=overrides[status],
                )
    return status_severity_weight(status)


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
    "banned":                 1.00,
    "restricted":             0.90,
    # 2026-05-17: added per partner direction.  Used by EU Critical Raw
    # Materials Act and similar supply-chain-target regulations (CRMA's
    # 10%-extraction / 40%-processing / 25%-recycling / 15%-imports
    # diversification targets).  Sits between restricted and covered —
    # stronger than "paperwork only" (covered) because it mandates
    # supply-chain restructuring, but weaker than "binding cap"
    # (restricted) because the targets are aspirational/medium-term.
    "strategic_raw_material": 0.85,
    "covered":                0.80,
    "disclosure_required":    0.70,
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
        # 2026-05-17 (partner direction): warn on unknown scope_type
        # values so seed-data typos surface in logs rather than silently
        # defaulting to "covered" intensity (0.80).  Recognized values
        # are the keys of _SCOPE_RELEVANCE; anything else triggers the
        # fallback AND a warning that an operator can grep for.
        if scope.scope_type not in _SCOPE_RELEVANCE:
            log.warning(
                "eurlex.unknown_scope_type",
                regulation_id=regulation_id,
                material_id=scope.material_id,
                scope_type=scope.scope_type,
                fallback_relevance=_DEFAULT_SCOPE_RELEVANCE,
                hint=(
                    "Check seed_regulations.py for typos. Valid values: "
                    + ", ".join(sorted(_SCOPE_RELEVANCE.keys()))
                ),
            )
        relevance = _SCOPE_RELEVANCE.get(scope.scope_type, _DEFAULT_SCOPE_RELEVANCE)
        session.add(
            RiskEventMaterial(
                risk_event_id=event_id,
                material_id=scope.material_id,
                relevance_score=relevance,
                match_reason=f"regulation_scope:{scope.scope_type}",
                # Carry the regulation designation onto the junction row so
                # ``evidence_aggregator._impact`` can apply the scope-severity
                # multiplier (``banned`` 1.50× ... ``disclosure_required``
                # 0.50×) when scoring this material against this event.
                scope_type=scope.scope_type,
            )
        )
        added += 1
    return added


# Mapping from RegulationGeographyScope.scope_type to RiskEventGeography
# (geography_context, relevance_score).  Added 2026-05-17 (Q2 fix).
#
# Background:
#   RegulationGeographyScope.scope_type can be:
#     - "jurisdiction"     — country where the regulation has legal force
#                            (e.g., EU for the EU Battery Reg, US for UFLPA).
#                            This is the regulating authority, not the target.
#     - "origin_country"   — countries the regulation regulates imports FROM
#                            (e.g., due-diligence rules applying to imports
#                            from named producer countries).
#     - "targeted_country" — countries the regulation specifically targets
#                            (e.g., UFLPA → CN for Xinjiang origin; OFAC →
#                            CN/RU/KP/IR).
#
# RiskEventGeography.geography_context (the column on the junction table)
# uses a different vocabulary inherited from the policy-event sources:
#     - "implementer" — country issuing the action (US for FR documents)
#     - "affected"    — producing/exporting country whose trade is restricted
#     - "primary"     — explicit primary target of the action
#
# Relevance is asymmetric — explicit targets carry more weight than
# jurisdictional-only scope because the regulation's bite is concentrated
# on the named country.  Values mirror the asymmetry in _SCOPE_RELEVANCE
# for material scope_type:
#     targeted_country (1.00) ≈ banned material (1.00)
#     origin_country   (0.85) ≈ restricted (0.90)
#     jurisdiction     (0.70) ≈ disclosure_required (0.70)
#
# Open follow-up (Q3 — geography integration into Regulatory pillar
# scoring): the Regulatory pillar today gates events by regulation_keys,
# not by geography intersection.  So even after this junction is written,
# a US automaker sourcing nickel from Indonesia gets the same EU Battery
# Regulation event impact as a German automaker sourcing nickel from
# Indonesia — the geography junction is recorded but not yet consumed
# to upweight events whose target country overlaps with the company's
# source geographies.  Closing that loop requires changes inside the
# Regulatory pillar's evidence_aggregator path, not in this ingester.
_GEOGRAPHY_SCOPE_CONTEXT: dict[str, str] = {
    "jurisdiction":     "implementer",
    "origin_country":   "affected",
    "targeted_country": "primary",
}
_GEOGRAPHY_SCOPE_RELEVANCE: dict[str, float] = {
    "jurisdiction":     0.70,
    "origin_country":   0.85,
    "targeted_country": 1.00,
}
_DEFAULT_GEOGRAPHY_SCOPE_CONTEXT = "affected"
_DEFAULT_GEOGRAPHY_SCOPE_RELEVANCE = 0.85


def _attach_geography_scope_to_event(
    session: Session,
    event_id: int,
    regulation_id: int,
) -> int:
    """Write one RiskEventGeography row per RegulationGeographyScope entry.

    Mirrors ``_attach_material_scope_to_event`` for the geography junction
    table.  Maps the regulation's scope_type to a geography_context the
    rest of the engine understands, plus a relevance score weighted by
    how directly the regulation targets that country.

    Returns the number of junction rows added.  Dedupes by country_code
    within a single call so a regulation that scopes the same country
    under two scope_types only writes one row (the first scope_type
    encountered wins — a defensive guard; not expected for the current
    seed data).
    """
    scopes = session.scalars(
        select(RegulationGeographyScope).where(
            RegulationGeographyScope.regulation_id == regulation_id
        )
    ).all()
    if not scopes:
        return 0

    added = 0
    seen_country_codes: set[str] = set()
    for scope in scopes:
        if scope.country_code in seen_country_codes:
            continue
        seen_country_codes.add(scope.country_code)

        context = _GEOGRAPHY_SCOPE_CONTEXT.get(
            scope.scope_type, _DEFAULT_GEOGRAPHY_SCOPE_CONTEXT,
        )
        relevance = _GEOGRAPHY_SCOPE_RELEVANCE.get(
            scope.scope_type, _DEFAULT_GEOGRAPHY_SCOPE_RELEVANCE,
        )
        session.add(
            RiskEventGeography(
                risk_event_id=event_id,
                country_code=scope.country_code,
                geography_context=context,
                relevance_score=relevance,
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

    Severity comes from ``regulation_severity_weight`` which honours
    per-regulation overrides via ``metadata_json["severity_overrides"]``;
    callers wanting just the global per-status default can use
    ``status_severity_weight(status)`` directly.

    Confidence is 0.9 (not 1.0) as of 2026-05-17 partner-direction
    recalibration: the existence of an EU regulation is a documented
    fact, but our INTERPRETATION of its scope (material list, geography
    targeting) is partner-curated and could be wrong — so the soft side
    of the confidence value reflects that.
    """
    key = regulation.regulation_key
    title = regulation.title or key
    severity = regulation_severity_weight(regulation)
    effective_date: Optional[date] = regulation.effective_date

    event_title = f"Regulatory compliance obligation: {title}"
    # 2026-05-17 (Section 6 walkthrough): hash on regulation_key alone
    # rather than (key + title).  Previous title-based hash created a
    # second event row whenever the title changed in seed_regulations.py
    # (typo fixes, partner edits, translation updates).  regulation_key
    # is the partner-chosen stable identifier — the right dedup anchor.
    content_hash = hashlib.sha256(key.encode()).hexdigest()[:64]

    meta: dict = {
        "regulation_key": key,
        "celex": celex,
        "policy_theme": regulation.policy_theme or "",
    }
    if effective_date is not None:
        meta["effective_date"] = effective_date.isoformat()

    # 2026-05-17 (Section 6 walkthrough): event_type renamed from
    # "REGULATORY_IMPLEMENTATION" (uppercase, semantic) to
    # "eurlex_regulation" (lowercase, mechanism-based) for consistency
    # with other parsers (federal_register_notice, trade_alert,
    # iea_policy, sanctions_listing).  Downstream filters use
    # risk_categories_json, not event_type, so this rename is safe.
    event = RiskEvent(
        source_document_id=source_document_id,
        event_type="eurlex_regulation",
        event_date=None,            # standing obligation — always in evidence window
        title=event_title,
        severity_score=severity,
        confidence_score=0.9,
        risk_categories_json=["regulatory_compliance"],
        content_hash=content_hash,
        metadata_json=meta,
        verified=True,
    )
    # 2026-05-17 (Section 6 walkthrough): match_reason now varies by
    # status — `regulation_effective` / `regulation_enacted` /
    # `regulation_proposed` / `regulation_superseded`.  Previous fixed
    # string "regulation_enacted" was technically wrong for any
    # non-enacted status.  Audit-readable; doesn't affect scoring math.
    status = (regulation.status or "").strip().lower()
    if status in ("effective", "enacted", "proposed", "superseded"):
        match_reason = f"regulation_{status}"
    else:
        match_reason = "regulation_unspecified"
    junction = RiskEventRegulation(
        regulation_id=regulation.id,
        relevance_score=1.0,
        match_reason=match_reason,
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
    geography_links_created = 0
    status_changes_detected = 0
    status_changes_already_pending = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    today = date.today()

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

        # ── Detect status changes (Q1, 2026-05-17) ──────────────────────
        # Hit the EUR-Lex bibliographic notice page and check whether our
        # stored Regulation.status matches what EUR-Lex currently
        # publishes.  If different, flag for partner review — but do NOT
        # mutate Regulation.status automatically.  The seed file remains
        # authoritative until partner confirms the change.
        #
        # Defensive: skipped entirely when ``fetch_summaries=False`` so
        # tests / air-gapped runs don't try to hit the network.  Also
        # idempotent: if a status_review_pending entry already exists,
        # we update its detected_at timestamp but don't double-count.
        if fetch_summaries:
            detected_metadata = fetch_eurlex_metadata(celex)
            if detected_metadata:
                detected_status = detect_status_from_metadata(
                    detected_metadata, as_of=today,
                )
                if (
                    detected_status is not None
                    and detected_status != regulation.status
                ):
                    if "status_review_pending" in meta:
                        # Already flagged — refresh detected_at but
                        # don't increment the new-detection counter.
                        meta["status_review_pending"]["detected_at"] = now_iso
                        status_changes_already_pending += 1
                    else:
                        meta["status_review_pending"] = {
                            "detected_status": detected_status,
                            "current_status": regulation.status,
                            "detected_at": now_iso,
                            "observed": {
                                "date_of_effect": (
                                    detected_metadata["date_of_effect"].isoformat()
                                    if detected_metadata.get("date_of_effect")
                                    else None
                                ),
                                "date_of_end_of_validity": (
                                    detected_metadata["date_of_end_of_validity"].isoformat()
                                    if detected_metadata.get("date_of_end_of_validity")
                                    else None
                                ),
                                "repealed_by_celex": detected_metadata.get(
                                    "repealed_by_celex"
                                ),
                            },
                        }
                        status_changes_detected += 1
                        log.info(
                            "eurlex.status_change_detected",
                            celex=celex,
                            regulation_key=regulation.regulation_key,
                            current_status=regulation.status,
                            detected_status=detected_status,
                        )

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
            # Propagate the regulation's scoped geographies onto the event
            # (Q2 fix, 2026-05-17).  Mirrors the material-scope path above.
            # The data was already in RegulationGeographyScope (populated
            # by seed_regulations.py) but had no RiskEventGeography path
            # until this addition.  See _GEOGRAPHY_SCOPE_CONTEXT comment
            # for the scope_type → geography_context mapping rationale
            # and the noted follow-up about Regulatory pillar integration.
            geography_links_created += _attach_geography_scope_to_event(
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
        "geography_links_created": geography_links_created,
        "status_changes_detected": status_changes_detected,
        "status_changes_already_pending": status_changes_already_pending,
    }
    log.info("eurlex.ingest.done", **result_summary)
    return result_summary


# ---------------------------------------------------------------------------
# Backfill: attach material junctions onto previously-ingested EUR-Lex events
# ---------------------------------------------------------------------------

def backfill_regulation_event_materials(session: Session) -> dict[str, int]:
    """Add RiskEventMaterial AND RiskEventGeography rows for past EUR-Lex
    events that lack them.

    Use when this attribution fix lands on a DB that already has EUR-Lex
    events ingested without scope junctions.  Safe to re-run: for each
    candidate event we check whether junctions already exist and only
    insert new ones — never duplicate.

    Material and geography paths are independent — an event missing
    material junctions but having geography junctions (or vice versa)
    is handled correctly.  We do NOT mix scope-derived attribution with
    content-derived attribution: if any junction of a given kind already
    exists for an event, we skip the backfill of that kind for that
    event.  This avoids accidental upgrades of weaker keyword-derived
    attributions to scope-derived ones.

    Returns counts:
        events_examined:        how many EUR-Lex-linked RiskEvents we looked at
        events_updated:         how many got at least one new junction (either kind)
        material_links_added:   total new RiskEventMaterial rows inserted
        geography_links_added:  total new RiskEventGeography rows inserted
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
    geography_links_added = 0

    for event_id, regulation_id in rows:
        event_touched = False

        # ── Material backfill — independent of geography ─────────────────
        # Skip if this event already has any RiskEventMaterial row — we
        # don't want to mix scope-derived attribution with content-derived
        # attribution after the fact.
        material_already_linked = session.execute(
            select(RiskEventMaterial.id)
            .where(RiskEventMaterial.risk_event_id == event_id)
            .limit(1)
        ).first()
        if material_already_linked is None:
            added = _attach_material_scope_to_event(
                session, event_id=event_id, regulation_id=regulation_id
            )
            if added > 0:
                material_links_added += added
                event_touched = True

        # ── Geography backfill — independent of material ─────────────────
        geography_already_linked = session.execute(
            select(RiskEventGeography.id)
            .where(RiskEventGeography.risk_event_id == event_id)
            .limit(1)
        ).first()
        if geography_already_linked is None:
            added = _attach_geography_scope_to_event(
                session, event_id=event_id, regulation_id=regulation_id
            )
            if added > 0:
                geography_links_added += added
                event_touched = True

        if event_touched:
            events_updated += 1

    session.commit()
    result = {
        "events_examined": events_examined,
        "events_updated": events_updated,
        "material_links_added": material_links_added,
        "geography_links_added": geography_links_added,
    }
    log.info("eurlex.backfill_event_scopes.done", **result)
    return result


# ---------------------------------------------------------------------------
# Status-review queue (Q1, 2026-05-17)
# ---------------------------------------------------------------------------

def list_pending_status_changes(session: Session) -> list[dict]:
    """Return every regulation with a ``status_review_pending`` entry.

    Output is a list of dicts ready for serialisation (CLI dump,
    operator email, dashboard widget).  Each dict carries enough context
    for partner to confirm or reject the detected change without going
    back to EUR-Lex manually:

        regulation_key   — stable identifier ("EU_BATTERY_REG_2023")
        celex            — EU CELEX number ("32023R1542")
        current_status   — status currently stored on the Regulation row
        detected_status  — what the EUR-Lex bibliographic notice now shows
        detected_at      — when we first noticed (or last refreshed)
        observed         — the raw fields we extracted (date_of_effect,
                            date_of_end_of_validity, repealed_by_celex)
        next_step        — short hint for the partner on how to clear it

    Caller is responsible for the actual review / decision workflow.
    Once partner confirms, the resolution is to update the seed entry
    in ``seed_regulations.py``, re-seed, and (optionally) remove the
    ``status_review_pending`` key from the metadata via direct DB edit
    or the future ``resolve-status-change`` CLI command.
    """
    rows = session.scalars(
        select(Regulation).where(
            Regulation.metadata_json["status_review_pending"].isnot(None)
        )
    ).all()

    pending: list[dict] = []
    for regulation in rows:
        meta = regulation.metadata_json or {}
        review = meta.get("status_review_pending")
        if not review:
            # Defensive — JSONB ``->`` operator can match weakly; double-
            # check the key exists in the in-memory dict.
            continue
        pending.append({
            "regulation_key": regulation.regulation_key,
            "celex": meta.get("celex"),
            "current_status": review.get("current_status"),
            "detected_status": review.get("detected_status"),
            "detected_at": review.get("detected_at"),
            "observed": review.get("observed", {}),
            "next_step": (
                f"Confirm whether status='{review.get('detected_status')}' "
                f"is correct; if yes, update the entry in "
                f"seed_regulations.py to match and re-run "
                f"`bdi-ingest seed-regulations`."
            ),
        })
    return pending
