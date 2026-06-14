"""
Federal Register ingestion: fetch policy notices and create GEOPOLITICAL_TRADE
and REGULATORY_COMPLIANCE RiskEvent rows with precise event_subtype classification.

Why standalone (not through the generic IngestionPipeline)
-----------------------------------------------------------
The generic pipeline's build_regulatory_risk_event() does not set event_subtype
in metadata_json, so events never populate the geo pillar's export_restriction_exposure
or tariff_exposure sub-inputs. This module classifies each document before creating
the event, ensuring the right evidence_aggregator path fires.

Signal types produced
---------------------
  EXPORT_RESTRICTION  → feeds geo pillar export_restriction_exposure sub-input
                        (evidence_aggregator checks event_subtype == "EXPORT_RESTRICTION")
  TARIFF              → feeds geo pillar tariff_exposure sub-input
  TRADE_POLICY        → feeds geo pillar tariff_exposure sub-input (same aggregator path)
  REGULATORY_COMPLIANCE → feeds regulatory pillar event-driven portion

Targeted search queries
-----------------------
Four query groups are run in sequence, each covering a distinct risk signal type.
Documents matching multiple queries are deduplicated by content_hash — the event
is classified by whichever query first claims it, in priority order:
  1. export_control  (EXPORT_RESTRICTION, highest scoring impact)
  2. tariff          (TARIFF)
  3. uflpa           (REGULATORY_COMPLIANCE — specific to battery supply chain policy)
  4. critical_minerals (TRADE_POLICY — broader market signals)

Severity calibration
--------------------
Base severity by document type:
  Rule (final)       → 0.70   confirmed policy, full enforcement
  Proposed Rule      → 0.50   signal only, may change
  Notice             → 0.40   informational / procedural
  Presidential Doc   → 0.80   executive authority
  Other              → 0.40

+0.12 per high-signal keyword hit (battery, lithium, graphite, cobalt, nickel,
UFLPA, FEOC, Xinjiang, Section 301, Section 232), capped at 0.92.

API notes
---------
  Base URL:  https://www.federalregister.gov/api/v1
  Endpoint:  GET /documents.json
  Auth:      None — completely public API
  Rate limit: none documented; 0.5s sleep between pages is conservative

Idempotency
-----------
content_hash (SHA-256 of document_number + publication_date) prevents duplicate
RiskEvent rows on re-run. SourceDocument rows are deduplicated by (source_id,
external_id) — re-runs update the document but skip event + company link creation.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urlencode

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import RiskCategory
from app.models.documents import SourceDocument
from app.models.enums import DocumentType, ImplementationPhase, SourceType
from app.models.regulatory import RiskEvent, RiskEventCompany, RiskEventGeography, RiskEventHsMapping, RiskEventMaterial, RiskEventRegulation
from app.models.source import Source
from app.models.supply import Material
from app.services.ingestion import feature_flags
from app.services.ingestion.entity_resolution import (
    build_company_cache,
    persist_company_links,
    resolve_companies_for_event,
)
from app.services.ingestion.normalizers.geography_resolver import GeographyCache
from app.services.ingestion.material_classifier import MaterialClassifier
from app.services.ingestion.material_resolver import MaterialAliasResolver
from app.services.ingestion.normalizers.material_baskets import (
    ANODE_BASKET,
    BATTERY_BASKET,
    CATHODE_BASKET,
    PERMANENT_MAGNET_BASKET,
    resolve_basket_to_attributions,
)
from app.services.ingestion.normalizers.material_resolver import MaterialCache, MaterialResolver
from app.services.ingestion.parsers.regulation_parser import parse_federal_register_document
from app.services.ingestion.regulation_resolver import (
    RegulationAliasResolver,
    ResolveResult,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://www.federalregister.gov/api/v1"
_SOURCE_NAME = "Federal Register API"
_PER_PAGE = 100          # max items per API page (FR allows up to 1000, 100 is safe)
_RATE_LIMIT_DELAY = 0.5  # seconds between API calls
_API_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=10.0, pool=10.0)

# Severity boost per keyword hit; base is set by doc type (see _severity())
_KEYWORD_BOOST = 0.12

# ---------------------------------------------------------------------------
# Material detection
# ---------------------------------------------------------------------------

# NOTE: MaterialCache was defined here through Phase 1 (PR 16b) and moved to
# normalizers/material_resolver.py in Phase 2 so that other ingesters can
# import it without a circular dependency.  The import at the top of this file
# re-exports MaterialCache; any existing callers of
#   from app.services.ingestion.ingest_federal_register import MaterialCache
# should be updated to use the normalizers path.

# Keywords that signal high relevance to battery supply chain
_HIGH_SIGNAL_KEYWORDS = frozenset({
    "battery", "lithium", "graphite", "cobalt", "nickel", "manganese",
    "uflpa", "feoc", "xinjiang", "forced labor", "forced labour",
    "section 301", "section 232", "critical mineral", "electric vehicle",
    "inflation reduction act", "domestic content", "clean vehicle",
    "anode", "cathode", "cathode active material", "precursor",
})

# Doc types that warrant auto-staging a suggested regulation when
# the FR document number isn't already a known alias.  Limited to
# high-authority "this document IS a new regulation" types — lower-
# authority docs (Notice, Proposed Rule) just attempt resolve and
# skip if unknown, because they're typically enforcement actions
# under existing regulations, not new ones.  Comparison is
# case-insensitive against parsed.doc_type.
_STAGE_REGULATION_DOC_TYPES = frozenset({
    "rule", "final rule", "interim final rule", "executive order",
})


# ---------------------------------------------------------------------------
# Battery / CRM relevance gate (mirrors IEA's _has_battery_relevance)
# ---------------------------------------------------------------------------
# Used to decide whether an event with no resolved material attribution
# should be PRESERVED (with metadata_json["needs_material_review"] = True
# for analyst triage) or DROPPED as off-scope.  Matches the IEA Phase 1
# pattern so the review-queue is populated consistently across ingesters.
#
# Deliberately broader than `_HIGH_SIGNAL_KEYWORDS` (which feeds the
# severity boost): the review-queue gate should err on the side of
# preserving borderline events; the analyst is the last line of defence.
# Bare-mineral keywords mirror IEA's list verbatim — divergence here
# silently shifts what crosses the FR review-queue threshold vs IEA's.
_FR_BATTERY_RELEVANCE_KEYWORDS: tuple[str, ...] = (
    "battery", "batteries",
    "ev ", " ev,", " ev.",
    "electric vehicle", "electric vehicles",
    "critical mineral", "critical minerals",
    "critical raw material", "critical raw materials",
    "strategic mineral", "strategic raw material",
    "lithium-ion", "li-ion",
    "anode", "cathode", "electrolyte",
    "energy storage",
    "cobalt", "lithium", "nickel", "manganese", "graphite",
    "phosphate", "copper", "aluminum", "rare earth",
    # FR-specific signals not in IEA's list — these wordings appear
    # commonly in FR titles even when no bare mineral is named.
    "uflpa", "feoc", "xinjiang", "forced labor", "forced labour",
    "section 301", "section 232",
    "trade representative", "import restriction", "export control",
)


def _has_fr_battery_relevance(text: str, query_name: str | None = None) -> bool:
    """Deterministic battery / CRM relevance gate for FR events.

    Used when title-attribution returns no materials and we need to
    decide preserve-for-review vs drop-as-off-scope.  Passes if EITHER
    the source text contains a battery / CRM keyword, OR the query is
    one of the inherently in-scope query families where empty
    attribution is still meaningful (BIS/OFAC sanctions, UFLPA, etc.).
    """
    if not text:
        return False
    blob = text.lower()
    if any(kw in blob for kw in _FR_BATTERY_RELEVANCE_KEYWORDS):
        return True
    # Query-level in-scope-by-construction: these query streams are
    # narrow enough that the analyst still wants to see un-attributed
    # events.  Trade-remedy and mining_minerals are NOT in this set —
    # their volume is too high to preserve unattributed.
    if query_name in {
        "ofac_sanctions",
        "bis_export_controls",
        "uflpa",
        "presidential_critical_mineral",
        "presidential_sanction",
        "presidential_section_232",
        "presidential_section_301",
    }:
        return True
    return False


# ---------------------------------------------------------------------------
# Title-attribution patterns (2026-05-12 FR restructure)
# ---------------------------------------------------------------------------
# Refactored 2026-05-12 to split into two narrow lists.  Direct mineral
# names (Group 1 in the original 47-entry list) are gone — MaterialCache's
# canonical-name path already covers them via the DB-driven keywords +
# canonical name regex.  What remains here is the FR-specific behaviour
# MaterialCache can't express:
#
#   * _BASKET_FANOUT_PATTERNS — one pattern → multiple material attributions
#     (e.g., "lithium-ion batteries" → 5 minerals).  Basket VALUES live in
#     normalizers/material_baskets.py so IEA and FR don't drift apart.
#
#   * _AD_CVD_PATTERNS — FR-specific commodity wordings that disambiguate
#     a single material more precisely than the bare word would (e.g.,
#     "aluminum foil" is more confident than just "aluminum" appearing).
#     These could eventually move into hs_code_material_mappings.keywords
#     so MaterialCache picks them up; a separate task tracks that
#     migration.
#
# Steel is deliberately omitted per partner direction (2026-05-12) — it's
# a downstream processed product, not a launch-list material.
import re as _re

_BASKET_FANOUT_PATTERNS: list[tuple[_re.Pattern[str], list[str]]] = [
    # Lithium-ion / LIB cathode-anode chemistry → 5 minerals
    (_re.compile(r"\b(?:lithium.?ion|li.?ion) batter(?:ies|y)\b", _re.I), BATTERY_BASKET),
    (_re.compile(r"\b(?:electric vehicle|ev) batter(?:ies|y)\b",  _re.I), BATTERY_BASKET),
    (_re.compile(r"\bbattery (?:material|cell|pack|module)s?\b",  _re.I), BATTERY_BASKET),

    # Cathode-only — drops graphite (anode)
    (_re.compile(
        r"\bcathode (?:active material|material)?\b|\bcam\b", _re.I,
    ), CATHODE_BASKET),
    (_re.compile(
        r"\b(?:cathode|anode) precursor(?:s)?\b|\b(?:p?cam|pcam)\b", _re.I,
    ), CATHODE_BASKET),

    # Anode-only — graphite + silicon
    (_re.compile(r"\banode (?:material)?\b",                      _re.I), ANODE_BASKET),

    # Electrolyte salts → lithium-only
    (_re.compile(
        r"\b(?:electrolyte salt(?:s)?|lipf6|lifsi|libf4)\b", _re.I,
    ), ["Lithium"]),
    (_re.compile(r"\bsolid.?state batter(?:ies|y)\b",             _re.I), ["Lithium"]),

    # Graphite-product wordings — anode-grade
    (_re.compile(
        r"\b(?:graphite electrode|spherical graphite|spheroidized graphite)\b", _re.I,
    ), ["Natural Graphite"]),

    # Permanent magnet alloys → REE + Cobalt basket
    (_re.compile(
        r"\b(?:neodymium|ndfeb|samarium[- ]cobalt) magnet(?:s)?\b", _re.I,
    ), PERMANENT_MAGNET_BASKET),
]


_AD_CVD_PATTERNS: list[tuple[_re.Pattern[str], list[str]]] = [
    # Aluminum products
    (_re.compile(
        r"\baluminum (?:foil|extrusion(?:s)?|sheet|wire|plate|coil)\b", _re.I,
    ), ["Aluminum"]),
    (_re.compile(r"\bunwrought aluminum\b",                       _re.I), ["Aluminum"]),

    # Copper products
    (_re.compile(
        r"\bcopper (?:rod|foil|tube|wire|pipe|cathode)\b", _re.I,
    ), ["Copper"]),
    (_re.compile(r"\bunwrought copper\b",                         _re.I), ["Copper"]),

    # Silicon products (industrial / PV — anode-grade material proxy)
    (_re.compile(r"\b(?:ferrosilicon|silicon metal)\b",           _re.I), ["Silicon (Anode Grade)"]),
    (_re.compile(
        r"\b(?:quartz surface|silicon photovoltaic|pv cell|solar (?:cell|wafer|module))\b", _re.I,
    ), ["Silicon (Anode Grade)"]),
    (_re.compile(r"\bcalcium silicon\b",                          _re.I), ["Silicon (Anode Grade)"]),

    # Magnesium products
    (_re.compile(r"\bmagnesium (?:metal|ingot)\b",                _re.I), ["Magnesium"]),
    (_re.compile(r"\bunwrought magnesium\b",                      _re.I), ["Magnesium"]),

    # Manganese ferro-alloys
    (_re.compile(r"\b(?:ferromanganese|silicomanganese)\b",       _re.I), ["Manganese"]),

    # Nickel intermediates
    (_re.compile(
        r"\b(?:ferronickel|nickel matte|nickel sulfate)\b", _re.I,
    ), ["Nickel"]),
    (_re.compile(r"\bunwrought nickel\b",                         _re.I), ["Nickel"]),

    # Tin / Zinc unwrought
    (_re.compile(r"\bunwrought tin\b",                            _re.I), ["Tin"]),
    (_re.compile(r"\bunwrought zinc\b",                           _re.I), ["Zinc"]),
]


# Relevance values applied per attribution source.  Aligned with the
# 2026-05-12 IEA recalibration so basket-inference attribution has the same
# downstream weight across ingesters.
_TITLE_BASKET_RELEVANCE = 0.40   # broad inference — category → mineral list
_TITLE_ADCVD_RELEVANCE  = 0.90   # specific commodity wording — high confidence


def _attribute_from_title(
    title: str,
    material_cache: "MaterialCache",
    mat_resolver: "MaterialResolver",
) -> list[tuple[int, float, str, int | None]]:
    """Apply layered title-attribution: MaterialCache + basket fanout + AD/CVD.

    Layers (highest specificity first, but dedup by material_id):

      1. **MaterialCache.detect()** — DB-driven canonical-name + keyword
         matches.  Covers direct mineral mentions ("lithium", "cobalt") and
         any commodity wording registered via
         ``hs_code_material_mappings.keywords``.  Relevance per match comes
         from the cache (frequency-weighted, ~0.10-0.85).

      2. **AD/CVD patterns** — FR-specific commodity wordings that imply
         one material more confidently than the bare word would (e.g.
         "aluminum foil").  Fixed relevance 0.90.

      3. **Basket fanout patterns** — one pattern → multiple materials
         (e.g. "lithium-ion batteries" → 5 minerals).  Resolves through
         the shared baskets in ``normalizers/material_baskets.py``.
         Fixed relevance 0.40 (category inference, low-confidence).

    Returns tuples in the shape MaterialCache.detect produces so callers
    route the result through ``_persist_material_links`` unchanged:
    ``(material_id, relevance, matched_pattern, hs_mapping_id)``.  HS
    mapping ID flows through from MaterialCache where available; the
    basket and AD/CVD layers return None.

    Deduplicated by material_id — earlier-layer wins for the
    matched_pattern label.
    """
    if not title:
        return []

    seen_material_ids: set[int] = set()
    matches: list[tuple[int, float, str, int | None]] = []

    # Layer 1: MaterialCache — DB-driven canonical / keyword matches.
    # Most direct mineral names land here without needing a hardcoded
    # regex in this file.  HS-mapping-id flows through when known.
    for cache_match in material_cache.detect(title):
        mat_id = cache_match[0]
        if mat_id in seen_material_ids:
            continue
        seen_material_ids.add(mat_id)
        matches.append(cache_match)

    # Layer 2: AD/CVD product wordings — high-confidence, FR-specific.
    for pattern, material_names in _AD_CVD_PATTERNS:
        m = pattern.search(title)
        if m is None:
            continue
        matched_text = m.group(0)[:32]
        for canonical_name in material_names:
            material = mat_resolver.resolve_by_canonical_name(canonical_name)
            if material is None or material.id in seen_material_ids:
                continue
            seen_material_ids.add(material.id)
            matches.append((
                material.id,
                _TITLE_ADCVD_RELEVANCE,
                f"title_adcvd:{matched_text}",
                None,
            ))

    # Layer 3: Basket fanout — low-confidence category inference.
    for pattern, basket_canonical_names in _BASKET_FANOUT_PATTERNS:
        m = pattern.search(title)
        if m is None:
            continue
        matched_text = m.group(0)[:32]
        basket_attributions = resolve_basket_to_attributions(
            basket_canonical_names,
            mat_resolver,
            relevance=_TITLE_BASKET_RELEVANCE,
            match_reason_prefix="title_basket",
            matched_text=matched_text,
        )
        for attribution in basket_attributions:
            mat_id = attribution[0]
            if mat_id in seen_material_ids:
                continue
            seen_material_ids.add(mat_id)
            matches.append(attribution)

    return matches


# ---------------------------------------------------------------------------
# Q5 (Mining & minerals regulatory) title denylist
# ---------------------------------------------------------------------------
# Q5 filters by mining-agency (USGS, BLM, MSHA, OSMRE) but those agencies
# publish a flood of unrelated content — turtle databases, military land
# withdrawals, oil & gas leases, grazing administration, recreation rules.
# The 2026-05-12 sample showed ~80% of Q5 results are off-topic.  This
# denylist catches the obvious off-topic patterns cheaply before we incur
# any Haiku cost.  Anything that passes the denylist still goes through
# the Haiku scope-filter (in case a turtle document somehow slips through
# this list).
#
# Entries here should be HIGH-PRECISION drops — false-negative direction
# (let through a borderline doc that Haiku will then classify) is
# preferred over false-positive direction (drop a real mining doc by
# accident).
_Q5_TITLE_DENYLIST: tuple[_re.Pattern[str], ...] = (
    _re.compile(r"\bturtle(?:s)?\b",                  _re.I),
    _re.compile(r"\bgrazing\b",                       _re.I),
    _re.compile(r"\baquatic species\b",               _re.I),
    _re.compile(r"\boil and gas (?:lease|drilling)\b", _re.I),
    _re.compile(r"\bnonindigenous\b",                 _re.I),
    _re.compile(r"\binvasive species\b",              _re.I),
    _re.compile(r"\bmilitary lands? withdrawal\b",    _re.I),
    _re.compile(r"\brecreation (?:fee|area|permit)\b", _re.I),
    _re.compile(r"\bnational park\b",                 _re.I),
    _re.compile(r"\bwild horse(?:s)?\b",              _re.I),
    _re.compile(r"\bwildlife refuge\b",               _re.I),
    _re.compile(r"\bmigratory bird\b",                _re.I),
    _re.compile(r"\bfish(?:ery|eries)\b",             _re.I),
)


def _q5_passes_denylist(title: str) -> bool:
    """Returns True if the title does NOT match any denylist pattern."""
    if not title:
        return True  # Empty title — let the Haiku scope-filter decide
    for pattern in _Q5_TITLE_DENYLIST:
        if pattern.search(title):
            return False
    return True

# ---------------------------------------------------------------------------
# Query configuration
# ---------------------------------------------------------------------------

@dataclass
class QueryConfig:
    """Configuration for one FR search query.

    Rewritten 2026-05-12 to use STRUCTURED API filters (agencies, doc_types,
    CFR title/part) instead of relying on `conditions[term]` AND-text search
    alone.  The FR API's full-text search does not support OR expressions —
    multiple space-separated words are ANDed — so previous queries like
    ``"export restriction battery"`` required all three words in a document
    and missed Section 232 aluminum tariffs that don't say "battery".

    The new model lets a query specify any combination of:
      * ``term``           — free-text search (use sparingly; AND semantics)
      * ``agencies``       — agency slugs (e.g. "industry-and-security-bureau")
      * ``doc_types``      — type codes (RULE / PRORULE / NOTICE / PRESDOCU)
      * ``cfr_title`` /
        ``cfr_part``       — CFR title + part for regulation filtering

    Each query also chooses its own:
      * ``lookback_days``  — date window (rare-policy streams like UFLPA
                              need 365d; high-volume streams 90d)
      * ``haiku_policy``   — when to invoke the LLM extraction layer
                              ("always" / "fallback" / "after_denylist" /
                              "never")

    Slug verification: agency slugs in this file have been verified against
    the live `/api/v1/agencies.json` endpoint.  Common gotcha: BIS is
    ``industry-and-security-bureau`` (not ``bureau-of-industry-and-security``)
    and OFAC is ``foreign-assets-control-office`` (not
    ``office-of-foreign-assets-control``).
    """
    name: str
    event_subtype: str
    risk_categories: list[str]

    # Search-filter parameters — all optional; at least one of (term, agencies,
    # doc_types, cfr_title) must be set or the query will return everything.
    term: Optional[str] = None
    agencies: Optional[list[str]] = None
    doc_types: Optional[list[str]] = None
    cfr_title: Optional[int] = None
    cfr_part: Optional[str] = None

    # Ingestion-strategy parameters.
    lookback_days: int = 90
    # haiku_policy values:
    #   "always"          — call extract_fr_attribution on every doc.  Use
    #                       for OFAC (boilerplate abstracts) and PRESDOCU
    #                       (empty abstracts) where title+abstract carry
    #                       no usable signal.
    #   "fallback"        — call Haiku ONLY when title regex returns nothing.
    #                       Use for BIS, Trade Remedy where title pattern
    #                       catches most cases.
    #   "after_denylist"  — Q5 mining-agency stream: apply denylist filter
    #                       first to drop biology/grazing/oil-gas noise,
    #                       then run Haiku on whatever passes.
    #   "never"           — title regex only.  Use for UFLPA where the term
    #                       filter has already narrowed to high-precision.
    haiku_policy: str = "fallback"
    apply_q5_denylist: bool = False

    base_severity_boost: float = 0.0


# 2026-05-12 query-strategy rewrite — see audit notes in this directory.
# The new query set uses STRUCTURED filters (agency, type, CFR) rather than
# the old 5 AND-text queries.  Volume-per-query expected (based on
# 2026-05-12 90-day probe):
#   ofac_sanctions:           ~29/90d
#   bis_export_controls:      ~12/180d (sparse)
#   trade_remedy:             ~100/90d after secondary filter
#   presidential_*:           ~40-50/365d combined (5 term sub-queries)
#   mining_minerals:          ~90/90d, mostly noise → ~15/90d after denylist
#   uflpa:                    ~0-2/365d (sparse — kept as bonus stream)
#
# Queries run in this order; a doc claimed by an earlier query is excluded
# from later ones (priority routing preserved from prior design).
TARGETED_QUERIES: list[QueryConfig] = [
    # ── Q1: OFAC sanctions designations ──────────────────────────────────
    # Cleanest sanctions-designation stream.  Abstracts are boilerplate, so
    # Haiku reads the full text to resolve SDN names against the material
    # taxonomy.  doc_type filter to NOTICE excludes the occasional OFAC
    # rulemaking which already fires under separate paths.
    QueryConfig(
        name="ofac_sanctions",
        event_subtype="EXPORT_RESTRICTION",
        risk_categories=[
            RiskCategory.GEOPOLITICAL_TRADE.value,
            RiskCategory.REGULATORY_COMPLIANCE.value,
        ],
        agencies=["foreign-assets-control-office"],
        doc_types=["NOTICE"],
        haiku_policy="always",
        base_severity_boost=0.20,
        lookback_days=90,
    ),
    # ── Q2: BIS export controls ──────────────────────────────────────────
    # Entity List adds, EAR amendments, advanced-tech/metallurgy controls.
    # Volume low enough (~12/90d) that no further narrowing is needed.
    # Longer lookback because the stream is sparse.
    QueryConfig(
        name="bis_export_controls",
        event_subtype="EXPORT_RESTRICTION",
        risk_categories=[
            RiskCategory.GEOPOLITICAL_TRADE.value,
            RiskCategory.REGULATORY_COMPLIANCE.value,
        ],
        agencies=["industry-and-security-bureau"],
        haiku_policy="fallback",
        base_severity_boost=0.15,
        lookback_days=180,
    ),
    # ── Q3: Trade remedy actions ─────────────────────────────────────────
    # ITA / USITC / USTR / CBP — AD/CVD reviews, Section 301, tariff
    # classifications, UFLPA enforcement.  High volume (528/90d) — title-
    # regex commodity dictionary catches the majority cheaply; Haiku as
    # fallback only.
    QueryConfig(
        name="trade_remedy",
        event_subtype="TARIFF",
        risk_categories=[RiskCategory.GEOPOLITICAL_TRADE.value],
        agencies=[
            "international-trade-administration",
            "international-trade-commission",
            "trade-representative-office-of-united-states",
            "u-s-customs-and-border-protection",
        ],
        haiku_policy="fallback",
        base_severity_boost=0.10,
        lookback_days=90,
    ),
    # ── Q4a-e: Presidential trade actions ────────────────────────────────
    # PRESDOCU docs have EMPTY abstracts (verified 2026-05-12 probe) and
    # frequently opaque titles (EOs reference prior proclamations by
    # number, not commodity).  Five separate term filters to compensate
    # for the FR API's AND semantics; dedup is by content_hash later.
    # Haiku always — full-text extraction is the only realistic path.
    QueryConfig(
        name="presidential_tariff",
        event_subtype="TARIFF",
        risk_categories=[RiskCategory.GEOPOLITICAL_TRADE.value],
        doc_types=["PRESDOCU"],
        term="tariff",
        haiku_policy="always",
        base_severity_boost=0.10,
        lookback_days=365,
    ),
    QueryConfig(
        name="presidential_section_232",
        event_subtype="TARIFF",
        risk_categories=[RiskCategory.GEOPOLITICAL_TRADE.value],
        doc_types=["PRESDOCU"],
        term="Section 232",
        haiku_policy="always",
        base_severity_boost=0.15,
        lookback_days=365,
    ),
    QueryConfig(
        name="presidential_section_301",
        event_subtype="TARIFF",
        risk_categories=[RiskCategory.GEOPOLITICAL_TRADE.value],
        doc_types=["PRESDOCU"],
        term="Section 301",
        haiku_policy="always",
        base_severity_boost=0.15,
        lookback_days=365,
    ),
    QueryConfig(
        name="presidential_critical_mineral",
        event_subtype="TRADE_POLICY",
        risk_categories=[
            RiskCategory.GEOPOLITICAL_TRADE.value,
            RiskCategory.REGULATORY_COMPLIANCE.value,
        ],
        doc_types=["PRESDOCU"],
        term="critical mineral",
        haiku_policy="always",
        lookback_days=365,
    ),
    QueryConfig(
        name="presidential_sanction",
        event_subtype="EXPORT_RESTRICTION",
        risk_categories=[
            RiskCategory.GEOPOLITICAL_TRADE.value,
            RiskCategory.REGULATORY_COMPLIANCE.value,
        ],
        doc_types=["PRESDOCU"],
        term="sanction",
        haiku_policy="always",
        base_severity_boost=0.20,
        lookback_days=365,
    ),
    # ── Q5: Mining & minerals regulatory ─────────────────────────────────
    # USGS / BLM / MSHA / OSMRE.  Heavy noise (turtles, grazing, military
    # land, oil & gas leases — ~80% of agency volume).  Denylist drops the
    # obvious noise cheaply; Haiku scope-filters the remainder.
    QueryConfig(
        name="mining_minerals",
        event_subtype="REGULATORY_COMPLIANCE",
        risk_categories=[
            RiskCategory.REGULATORY_COMPLIANCE.value,
            RiskCategory.OPERATIONAL.value,
        ],
        agencies=[
            "geological-survey",
            "land-management-bureau",
            "mine-safety-and-health-administration",
            "surface-mining-reclamation-and-enforcement-office",
        ],
        haiku_policy="after_denylist",
        apply_q5_denylist=True,
        lookback_days=90,
    ),
    # ── Bonus: UFLPA Entity List ─────────────────────────────────────────
    # Kept as a separate query rather than dropped — UFLPA Entity List
    # actions are rare in FR (DHS publishes 2-5/year and they've slowed
    # under the current administration), but when they fire they're
    # high-signal.  365d lookback because the cadence is annual not
    # quarterly.  Title regex covers most because the literal "UFLPA"
    # appears in titles when relevant.
    QueryConfig(
        name="uflpa",
        event_subtype="REGULATORY_COMPLIANCE",
        risk_categories=[
            RiskCategory.REGULATORY_COMPLIANCE.value,
            RiskCategory.OPERATIONAL.value,
        ],
        term="UFLPA",
        haiku_policy="fallback",
        base_severity_boost=0.20,
        lookback_days=365,
    ),
]


# ---------------------------------------------------------------------------
# Federal Register topic → material name mapping
# ---------------------------------------------------------------------------
# The Federal Register API returns a curated ``topics`` list per document
# (controlled vocabulary maintained by the Office of the Federal Register).
# When ``topics`` resolves to one or more canonical materials we treat that
# as authoritative — at least as confident as a stage-specific keyword
# match because the FR's controlled vocabulary is editor-curated rather
# than auto-generated.
#
# 2026-05-14 migration: the mapping previously lived as a hardcoded
# ``_TOPIC_MATERIAL_MAP`` dict in this file.  It now lives as data in
# ``material_source_aliases`` (source_system='federal_register_topic'),
# seeded by ``seed_material_source_aliases.py::_FEDERAL_REGISTER_TOPICS``.
# Lookup is via ``MaterialAliasResolver`` — same pattern USGS / MCS /
# Pink Sheet ingesters use.  Benefits:
#   * adding a new topic mapping is a seed-script edit + re-seed; no
#     code change;
#   * the editorial skip-list (topics deliberately not attributed, e.g.
#     "critical materials", "imports") is stored as ``is_skipped=true``
#     rows with explicit ``skip_reason`` — replaces a code comment with
#     a queryable audit trail;
#   * unknown topics surface in the seed-script's warn path, so drift
#     between FR's vocabulary and our mapping is visible.
#
# Relevance is hardcoded here at 0.90 because the alias table itself
# carries no per-source confidence field.  See material_source_aliases
# schema notes — adding per-source confidence would require a column
# migration.

_TOPIC_SOURCE_SYSTEM = "federal_register_topic"

# Relevance assigned to topic-derived material attributions.  Same magnitude
# as the unique-keyword path in MaterialCache (0.90 base).
_TOPIC_MATCH_RELEVANCE = 0.90


def _resolve_topics_to_materials(
    topics: list[str],
    alias_resolver: "MaterialAliasResolver",
) -> list[tuple[int, float, str, int | None]]:
    """Translate Federal Register topic strings to material matches.

    Returns tuples in the same shape MaterialCache.detect uses so callers
    can pass the result through ``_persist_material_links`` unchanged:
    ``(material_id, relevance, matched_keyword, hs_mapping_id)``.

    ``hs_mapping_id`` is None — topic attribution is stage-ambiguous in
    the same way canonical_name matches are.

    Topics that resolve to ``status='skipped'`` (editorial skip-list:
    "critical materials", "imports", "tariff", etc.) are silently
    dropped.  Topics that resolve to ``status='unknown'`` are also
    dropped silently here (the seed script's warn path is the right
    place to surface vocabulary drift, not the hot ingest loop).
    Duplicate materials (multiple topics resolving to the same material)
    are collapsed; the first topic wins.
    """
    seen_material_ids: set[int] = set()
    matches: list[tuple[int, float, str, int | None]] = []
    for raw_topic in topics:
        if not raw_topic:
            continue
        result = alias_resolver.resolve(_TOPIC_SOURCE_SYSTEM, raw_topic)
        if result.status != "ok" or result.material is None:
            continue
        if result.material.id in seen_material_ids:
            continue
        seen_material_ids.add(result.material.id)
        matches.append((
            result.material.id,
            _TOPIC_MATCH_RELEVANCE,
            f"fr_topic:{raw_topic[:48]}",
            None,
        ))
    return matches


# ---------------------------------------------------------------------------
# Persist helpers for new junction rows
# ---------------------------------------------------------------------------

def _persist_material_links(
    session: Session,
    event: RiskEvent,
    matches: list[tuple[int, float, str, int | None]],
) -> int:
    """
    Write RiskEventMaterial rows for each detected material AND
    RiskEventHsMapping rows where hs_mapping_id is known (stage attribution).

    Idempotent: skips pairs that already exist (unique constraints on
    risk_event_id + material_id and risk_event_id + hs_mapping_id).

    Returns the number of RiskEventMaterial rows written (not counting
    RiskEventHsMapping rows, which are a side-effect of stage attribution).
    """
    written = 0
    hs_written = 0

    for material_id, relevance, matched_keyword, hs_mapping_id in matches:
        # ── material-level junction (always) ──────────────────────────────
        existing_mat = session.scalar(
            select(RiskEventMaterial).where(
                RiskEventMaterial.risk_event_id == event.id,
                RiskEventMaterial.material_id == material_id,
            ).limit(1)
        )
        if existing_mat is None:
            session.add(RiskEventMaterial(
                risk_event_id=event.id,
                material_id=material_id,
                relevance_score=relevance,
                match_reason=f"keyword_match:{matched_keyword[:48]}",
            ))
            written += 1

        # ── stage-level junction (only when stage is known) ───────────────
        if hs_mapping_id is not None:
            existing_hs = session.scalar(
                select(RiskEventHsMapping).where(
                    RiskEventHsMapping.risk_event_id == event.id,
                    RiskEventHsMapping.hs_mapping_id == hs_mapping_id,
                ).limit(1)
            )
            if existing_hs is None:
                session.add(RiskEventHsMapping(
                    risk_event_id=event.id,
                    hs_mapping_id=hs_mapping_id,
                    relevance_score=relevance,
                    match_reason="keyword_match",
                ))
                hs_written += 1

    if written or hs_written:
        session.flush()
    return written


def _persist_geography_links(
    session: Session,
    event: RiskEvent,
    geos: list[tuple[str, str, float]],
) -> int:
    """
    Write RiskEventGeography rows for each detected country.
    Idempotent: skips pairs already present.
    Returns the number of rows written.
    """
    written = 0
    for iso2, context, relevance in geos:
        existing = session.scalar(
            select(RiskEventGeography).where(
                RiskEventGeography.risk_event_id == event.id,
                RiskEventGeography.country_code == iso2,
            ).limit(1)
        )
        if existing is not None:
            continue
        session.add(RiskEventGeography(
            risk_event_id=event.id,
            country_code=iso2,
            geography_context=context,
            relevance_score=relevance,
        ))
        written += 1
    if written:
        session.flush()
    return written


# ---------------------------------------------------------------------------
# Targeted patch for existing events
# ---------------------------------------------------------------------------

# Doc types where publication date and effective date meaningfully diverge.
# Notices and proposed rules rarely have a separate effective date — only
# final rules and presidential documents typically do.
_EFFECTIVE_DATE_DOC_TYPES = frozenset({
    "rule", "final rule", "interim final rule",
    "presidential document", "executive order", "proclamation",
})


def patch_fr_events(
    session: Session,
    *,
    fix_uflpa_routing: bool = True,
    fix_effective_dates: bool = True,
    dry_run: bool = False,
) -> dict[str, int]:
    """
    Retroactively apply two improvements to existing Federal Register events:

    1. UFLPA routing (fix_uflpa_routing=True):
       Adds RiskCategory.OPERATIONAL to the risk_categories_json of events
       ingested under the 'uflpa' query.  These events affect operational
       risk (supplier/mine continuity) as well as regulatory compliance, but
       the original ingest only tagged them REGULATORY_COMPLIANCE.
       Pure DB update — no API calls required.

    2. effective_on date (fix_effective_dates=True):
       Updates event_date on existing FR events from publication_date to
       effective_on where they differ.  Re-fetches effective_on from the
       Federal Register API in batches of 100 documents using the
       conditions[document_number][] parameter.  Only targets event types
       where the gap is meaningful (final rules, executive orders,
       presidential documents). Notices and proposed rules are skipped.

    Args:
        session:              SQLAlchemy session. Commits after each fix.
        fix_uflpa_routing:    Add OPERATIONAL category to UFLPA events.
        fix_effective_dates:  Re-fetch and apply effective_on dates.
        dry_run:              Compute changes but do NOT write or commit.

    Returns:
        {
            "uflpa_events_patched": int,
            "effective_dates_fetched": int,
            "effective_dates_updated": int,
            "api_errors": int,
        }
    """
    uflpa_patched = 0
    effective_fetched = 0
    effective_updated = 0
    api_errors = 0

    # ── Fix 1: UFLPA → OPERATIONAL routing ──────────────────────────────────
    if fix_uflpa_routing:
        uflpa_events = list(session.scalars(
            select(RiskEvent).where(
                RiskEvent.event_type == "federal_register_notice",
                # JSONB path operator: metadata_json->>'query_name' = 'uflpa'
                RiskEvent.metadata_json["query_name"].astext == "uflpa",
            )
        ).all())

        operational_val = RiskCategory.OPERATIONAL.value
        for ev in uflpa_events:
            cats: list = list(ev.risk_categories_json or [])
            if operational_val not in cats:
                if not dry_run:
                    cats.append(operational_val)
                    ev.risk_categories_json = cats
                uflpa_patched += 1

        if not dry_run and uflpa_patched:
            session.commit()

        log.info(
            "patch_fr_events.uflpa_routing",
            dry_run=dry_run,
            patched=uflpa_patched,
        )

    # ── Fix 2: effective_on dates ────────────────────────────────────────────
    if fix_effective_dates:
        from app.models.documents import SourceDocument

        # Only target doc types where publication ≠ effective matters.
        # metadata_json->>'doc_type' is stored by _upsert_source_document.
        candidate_rows = list(session.execute(
            select(RiskEvent, SourceDocument)
            .join(
                SourceDocument,
                SourceDocument.id == RiskEvent.source_document_id,
            )
            .where(
                RiskEvent.event_type == "federal_register_notice",
                # Lower-case comparison for safety; FR returns mixed-case types.
                # Use func.lower on the JSONB text extraction.
                SourceDocument.metadata_json["doc_type"].astext.ilike(
                    "%rule%"
                )
                | SourceDocument.metadata_json["doc_type"].astext.ilike(
                    "%presidential%"
                )
                | SourceDocument.metadata_json["doc_type"].astext.ilike(
                    "%executive order%"
                )
                | SourceDocument.metadata_json["doc_type"].astext.ilike(
                    "%proclamation%"
                ),
            )
        ).all())

        if not candidate_rows:
            log.info("patch_fr_events.effective_dates.no_candidates")
        else:
            log.info(
                "patch_fr_events.effective_dates.candidates",
                count=len(candidate_rows),
            )

            # Build (doc_number → (event, source_doc)) mapping
            doc_map: dict[str, tuple] = {}
            for ev, src in candidate_rows:
                doc_number = (
                    (src.metadata_json or {}).get("document_number")
                    or src.external_id
                )
                if doc_number:
                    doc_map[doc_number] = (ev, src)

            # Batch-fetch effective_on from the FR API, 100 documents per call.
            # The FR API supports conditions[document_number][] for multi-doc lookup.
            doc_numbers = list(doc_map.keys())
            _BATCH = 100

            with httpx.Client(follow_redirects=True) as client:
                for batch_start in range(0, len(doc_numbers), _BATCH):
                    batch = doc_numbers[batch_start: batch_start + _BATCH]
                    time.sleep(_RATE_LIMIT_DELAY)

                    try:
                        effective_map = _fetch_effective_on_batch(client, batch)
                        effective_fetched += len(effective_map)
                    except Exception as exc:
                        log.warning(
                            "patch_fr_events.effective_dates.api_error",
                            batch_start=batch_start,
                            error=str(exc),
                        )
                        api_errors += 1
                        continue

                    for doc_num, effective_on in effective_map.items():
                        if doc_num not in doc_map:
                            continue
                        ev, src = doc_map[doc_num]
                        if not effective_on:
                            continue

                        new_dt = datetime.combine(
                            effective_on, datetime.min.time(), tzinfo=timezone.utc
                        )

                        # Skip if the stored event_date already matches
                        if ev.event_date and abs(
                            (ev.event_date.replace(tzinfo=timezone.utc)
                             if ev.event_date.tzinfo is None
                             else ev.event_date)
                            - new_dt
                        ).days == 0:
                            continue

                        if not dry_run:
                            ev.event_date = new_dt
                            # Store pub_date in metadata_json if not already there
                            meta = dict(ev.metadata_json or {})
                            if "publication_date" not in meta and src.published_at:
                                meta["publication_date"] = src.published_at.date().isoformat()
                            meta["effective_on"] = effective_on.isoformat()
                            ev.metadata_json = meta

                        effective_updated += 1
                        log.debug(
                            "patch_fr_events.effective_dates.updated",
                            doc_number=doc_num,
                            old_date=ev.event_date.isoformat() if ev.event_date else None,
                            new_date=new_dt.isoformat(),
                        )

            if not dry_run and effective_updated:
                session.commit()

        log.info(
            "patch_fr_events.effective_dates",
            dry_run=dry_run,
            fetched=effective_fetched,
            updated=effective_updated,
            api_errors=api_errors,
        )

    log.info(
        "patch_fr_events.done",
        dry_run=dry_run,
        uflpa_patched=uflpa_patched,
        effective_updated=effective_updated,
    )

    return {
        "uflpa_events_patched": uflpa_patched,
        "effective_dates_fetched": effective_fetched,
        "effective_dates_updated": effective_updated,
        "api_errors": api_errors,
    }


def _fetch_effective_on_batch(
    client: httpx.Client,
    document_numbers: list[str],
) -> dict[str, date]:
    """
    Fetch effective_on dates for a list of document numbers using the FR API
    single-document endpoint (/documents/{doc_number}.json).

    The search endpoint does not support conditions[document_number][] bulk
    lookup, so we call the per-document endpoint once per number. Callers
    should pass batches of reasonable size (≤100) and respect rate limits
    between calls. Returns {document_number: effective_on_date} for documents
    that have a non-null effective_on. Documents without one are omitted.
    """
    result: dict[str, date] = {}
    for doc_num in document_numbers:
        url = f"{_BASE_URL}/documents/{doc_num}.json?fields[]=effective_on&fields[]=publication_date"
        try:
            resp = client.get(url, timeout=_API_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            raw_eff = data.get("effective_on")
            if raw_eff:
                result[doc_num] = datetime.strptime(raw_eff[:10], "%Y-%m-%d").date()
        except Exception as exc:
            log.debug(
                "patch_fr_events.effective_dates.doc_fetch_error",
                doc_number=doc_num,
                error=str(exc),
            )
        time.sleep(_RATE_LIMIT_DELAY)
    return result


# ---------------------------------------------------------------------------
# Historical backfill
# ---------------------------------------------------------------------------

def backfill_fr_links(
    session: Session,
    *,
    force: bool = False,
    batch_size: int = 100,
    dry_run: bool = False,
) -> dict[str, int]:
    """
    Retroactively tag existing Federal Register risk events with
    RiskEventMaterial and RiskEventGeography rows.

    Events ingested before material/geography detection was added have no
    junction rows and are invisible to get_events_for_material() and any
    geography-filtered queries. This function scans those events and fills
    in the missing rows using the same detection logic as the live ingester.

    Idempotent: the persist helpers skip pairs that already exist, so re-runs
    are safe. Use --force to re-scan events that already have some links (useful
    after adding new aliases or geo patterns).

    Args:
        session:    SQLAlchemy session. Commits after each batch of events.
        force:      If True, run detection on ALL FR events regardless of
                    whether they already have links. The per-row uniqueness
                    constraint means no duplicates will be created.
                    Default False: skips events that already have at least one
                    material link OR at least one geography link.
        batch_size: Number of events to process before committing. Default 100.
        dry_run:    Detect and count but do NOT write or commit any rows.

    Returns:
        {
            "events_scanned": int,
            "events_skipped": int,    # already fully tagged (no-force mode)
            "events_tagged": int,     # had at least one new link written
            "material_links_written": int,
            "geography_links_written": int,
        }
    """
    material_cache = MaterialCache.build(session)
    material_resolver = MaterialResolver(session)
    alias_resolver = MaterialAliasResolver(session)
    geo_cache = GeographyCache.build(session)

    # Subqueries to check existing links — used only in non-force mode to
    # skip events already tagged. exists() is much cheaper than COUNT for
    # large event tables.
    def _has_material_link(event_id: int) -> bool:
        return session.scalar(
            select(RiskEventMaterial.material_id)
            .where(RiskEventMaterial.risk_event_id == event_id)
            .limit(1)
        ) is not None

    def _has_geography_link(event_id: int) -> bool:
        return session.scalar(
            select(RiskEventGeography.country_code)
            .where(RiskEventGeography.risk_event_id == event_id)
            .limit(1)
        ) is not None

    # Only process FR-sourced events.
    # event_type is set to "federal_register_notice" by _insert_risk_event.
    events: list[RiskEvent] = list(
        session.scalars(
            select(RiskEvent).where(
                RiskEvent.event_type == "federal_register_notice"
            )
        ).all()
    )

    events_scanned = 0
    events_skipped = 0
    events_tagged = 0
    material_links_written = 0
    geography_links_written = 0
    pending_since_commit = 0

    for ev in events:
        events_scanned += 1

        # In default mode skip events that already have links on both sides.
        if not force:
            if _has_material_link(ev.id) and _has_geography_link(ev.id):
                events_skipped += 1
                continue

        # Reconstruct search text from stored title + summary.
        search_text = " ".join(filter(None, [ev.title, ev.summary]))
        if not search_text.strip():
            events_skipped += 1
            continue

        # Material attribution: topics first, keyword fallback (Tier 1.5
        # audit, 2026-05-09).  Topics live in
        # ``SourceDocument.metadata_json['topics']`` — pulled from the FR
        # API at ingest time.  When topics resolve to materials we skip
        # the keyword scan, matching the live ingester's behaviour.
        topics: list[str] = []
        if ev.source_document_id is not None:
            sd = session.get(SourceDocument, ev.source_document_id)
            if sd is not None and isinstance(sd.metadata_json, dict):
                raw_topics = sd.metadata_json.get("topics") or []
                if isinstance(raw_topics, list):
                    topics = [t for t in raw_topics if isinstance(t, str)]

        detected_materials = _resolve_topics_to_materials(topics, alias_resolver)
        if not detected_materials:
            detected_materials = material_cache.detect(search_text)
        detected_geos = geo_cache.detect(search_text)

        if dry_run:
            if detected_materials or detected_geos:
                events_tagged += 1
                material_links_written += len(detected_materials)
                geography_links_written += len(detected_geos)
            continue

        mat_written = _persist_material_links(session, ev, detected_materials)
        geo_written = _persist_geography_links(session, ev, detected_geos)

        if mat_written or geo_written:
            events_tagged += 1
            material_links_written += mat_written
            geography_links_written += geo_written

        pending_since_commit += 1
        if pending_since_commit >= batch_size:
            session.commit()
            pending_since_commit = 0

        log.debug(
            "backfill_fr_links.event_tagged",
            event_id=ev.id,
            materials=mat_written,
            geographies=geo_written,
        )

    if not dry_run and pending_since_commit:
        session.commit()

    log.info(
        "backfill_fr_links.done",
        dry_run=dry_run,
        force=force,
        events_scanned=events_scanned,
        events_skipped=events_skipped,
        events_tagged=events_tagged,
        material_links_written=material_links_written,
        geography_links_written=geography_links_written,
    )

    return {
        "events_scanned": events_scanned,
        "events_skipped": events_skipped,
        "events_tagged": events_tagged,
        "material_links_written": material_links_written,
        "geography_links_written": geography_links_written,
    }


# ---------------------------------------------------------------------------
# Severity scoring
# ---------------------------------------------------------------------------

_DOC_TYPE_SEVERITY: dict[str, float] = {
    "rule":                  0.70,
    "final rule":            0.70,
    "interim final rule":    0.65,
    "proposed rule":         0.50,
    "advance notice":        0.40,
    "notice":                0.40,
    "presidential document": 0.80,
    "executive order":       0.85,
    "proclamation":          0.75,
}


def _severity(doc_type: str | None, abstract: str | None, title: str | None,
              query: QueryConfig) -> float:
    doc_type_norm = (doc_type or "notice").lower()
    base = _DOC_TYPE_SEVERITY.get(doc_type_norm, 0.40)
    base += query.base_severity_boost

    # Keyword boost
    text = " ".join(filter(None, [abstract, title])).lower()
    hits = sum(1 for kw in _HIGH_SIGNAL_KEYWORDS if kw in text)
    boosted = base + min(0.22, hits * _KEYWORD_BOOST)

    return round(min(0.92, max(0.30, boosted)), 2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _content_hash(document_number: str, publication_date: str) -> str:
    payload = f"{document_number}|{publication_date}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _get_or_create_source(session: Session) -> Source:
    existing = session.scalar(
        select(Source).where(Source.name == _SOURCE_NAME)
    )
    if existing:
        return existing
    source = Source(
        name=_SOURCE_NAME,
        source_type=SourceType.FEDERAL_REGISTER.value,
        phase=ImplementationPhase.PHASE_1.value,
        is_active=True,
        config_json={"base_url": _BASE_URL},
    )
    session.add(source)
    session.flush()
    log.info("ingest_federal_register.source_created", source_id=source.id)
    return source


def _upsert_source_document(
    session: Session,
    source: Source,
    external_id: str,
    title: str | None,
    url: str | None,
    published_at: datetime | None,
    abstract: str | None,
    metadata_json: dict,
) -> tuple[SourceDocument, bool]:
    """Returns (document, is_new). is_new=False means event already created — skip."""
    existing = session.scalar(
        select(SourceDocument).where(
            SourceDocument.source_id == source.id,
            SourceDocument.external_id == external_id,
        )
    )
    if existing:
        return existing, False

    doc = SourceDocument(
        source_id=source.id,
        external_id=external_id,
        title=title,
        url=url,
        published_at=published_at,
        document_type=DocumentType.API_JSON.value,
        raw_text=abstract,
        metadata_json=metadata_json,
    )
    session.add(doc)
    session.flush()
    return doc, True


def _event_exists_by_hash(session: Session, content_hash: str) -> bool:
    return session.scalar(
        select(RiskEvent.id).where(RiskEvent.content_hash == content_hash).limit(1)
    ) is not None


def _insert_risk_event(
    session: Session,
    doc: SourceDocument,
    title: str,
    summary: str | None,
    event_date: datetime | None,
    severity: float,
    risk_categories: list[str],
    query: QueryConfig,
    geo_primary: str | None,
    agencies: list[str],
    content_hash: str,
) -> RiskEvent:
    ev = RiskEvent(
        source_document_id=doc.id,
        event_type="federal_register_notice",
        event_subtype=query.event_subtype,  # typed col (migration 040)
        event_date=event_date,
        title=title[:1024],
        summary=summary,
        severity_score=severity,
        confidence_score=0.70,
        risk_categories_json=risk_categories,
        geography_json={"primary": geo_primary, "scope": "federal"},
        content_hash=content_hash,
        metadata_json={
            # event_subtype kept in metadata_json for one release cycle to
            # support backwards-compat readers; drop later.
            "event_subtype": query.event_subtype,
            "query_name": query.name,
            "agencies": agencies[:8],
            "source": "federal_register_api",
            # publication_date preserved here so callers can distinguish
            # from event_date (which may be effective_on for final rules).
            "publication_date": doc.published_at.date().isoformat()
            if doc.published_at else None,
        },
    )
    session.add(ev)
    session.flush()
    return ev


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------

def _fetch_page(
    client: httpx.Client,
    query: QueryConfig,
    since_date: date,
    page: int,
) -> tuple[list[dict], int]:
    """
    Fetch one page of Federal Register documents for the given query.
    Returns (items, total_pages).

    2026-05-12 rewrite: now honours QueryConfig's structured filter fields
    (``agencies``, ``doc_types``, ``cfr_title``, ``cfr_part``) in addition
    to the legacy ``term``.  Builds the FR API query string manually with
    literal square brackets — the Rails router doesn't match percent-
    encoded brackets so ``conditions%5Bterm%5D`` returns nothing.
    """
    # Build the query string manually with literal square brackets.
    # Both urllib.parse.urlencode (default) and httpx params= percent-encode
    # brackets → conditions%5Bterm%5D — which the FR API Rails router does not
    # match. Passing quote_via with safe='[]' keeps the brackets literal, matching
    # the form the FR API documentation shows and expects.
    def _q(s: str, safe: str, encoding: str | None = None, errors: str | None = None) -> str:
        return quote(str(s), safe="[]", encoding=encoding, errors=errors)

    param_pairs: list[tuple[str, str]] = [
        ("conditions[publication_date][gte]", since_date.isoformat()),
        ("per_page", str(_PER_PAGE)),
        ("page", str(page)),
        ("order", "newest"),
    ]

    # Optional free-text term (legacy + UFLPA + PRESDOCU sub-queries).
    if query.term:
        param_pairs.append(("conditions[term]", query.term))

    # Agency slug filter — multi-valued OR within the parameter.
    if query.agencies:
        for slug in query.agencies:
            param_pairs.append(("conditions[agencies][]", slug))

    # Document-type filter.  When omitted, the FR API returns ALL types.
    # When the QueryConfig doesn't specify, default to the historical set
    # (RULE / PRORULE / NOTICE / PRESDOCU) to preserve compatibility with
    # legacy queries.  doc_type codes must be the FR API's short abbrev's,
    # not human-readable names — passing "Rule" silently matches zero docs.
    doc_types = query.doc_types or ["RULE", "PRORULE", "NOTICE", "PRESDOCU"]
    for doc_type_code in doc_types:
        param_pairs.append(("conditions[type][]", doc_type_code))

    # CFR title / part filter (regulation queries).  Title is an integer;
    # part can be a single part ("502") or a range ("500-599") — the latter
    # syntax was UNRELIABLE in the 2026-05-12 probe, so prefer single-part
    # or agency-only filters where possible.
    if query.cfr_title is not None:
        param_pairs.append(("conditions[cfr][title]", str(query.cfr_title)))
        if query.cfr_part is not None:
            param_pairs.append(("conditions[cfr][part]", str(query.cfr_part)))

    for field in (
        "document_number", "title", "abstract", "publication_date",
        "effective_on", "agencies", "html_url", "pdf_url",
        "raw_text_url", "topics", "type",
    ):
        param_pairs.append(("fields[]", field))

    url = f"{_BASE_URL}/documents.json?{urlencode(param_pairs, quote_via=_q)}"
    resp = client.get(url, timeout=_API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    items = data.get("results") or []
    total_pages = data.get("total_pages") or 1

    return [r for r in items if isinstance(r, dict)], int(total_pages)


def _fetch_full_text(
    client: httpx.Client,
    raw_text_url: Optional[str],
    max_chars: int = 12_000,
) -> Optional[str]:
    """Fetch a document's full text body from the FR API.

    Used by the Haiku-extraction path for queries whose abstract is
    boilerplate (OFAC) or empty (PRESDOCU).  Returns ``None`` on any HTTP
    error or when ``raw_text_url`` is missing — the caller falls back to
    title + abstract.

    Truncated to ``max_chars`` to bound per-call Anthropic input tokens.
    The classifier itself also truncates at ``_MAX_TEXT_CHARS`` (8K
    inside material_classifier.py) so this is a defence-in-depth cap on
    bytes-transferred from the FR API.
    """
    if not raw_text_url:
        return None
    try:
        resp = client.get(raw_text_url, timeout=_API_TIMEOUT)
        resp.raise_for_status()
        text = resp.text
        if not text:
            return None
        return text[:max_chars]
    except (httpx.HTTPStatusError, httpx.TimeoutException) as exc:
        log.debug(
            "ingest_federal_register.full_text_fetch_failed",
            url=raw_text_url,
            error=str(exc),
        )
        return None
    except Exception as exc:  # noqa: BLE001 — never crash ingest on a fetch
        log.warning(
            "ingest_federal_register.full_text_fetch_unexpected",
            url=raw_text_url,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None


# ---------------------------------------------------------------------------
# Main ingest function
# ---------------------------------------------------------------------------

def ingest_federal_register(
    session: Session,
    since_date: Optional[date] = None,
    max_pages: int = 5,
    queries: Optional[list[QueryConfig]] = None,
) -> dict[str, int]:
    """
    Fetch Federal Register notices and create GEOPOLITICAL_TRADE /
    REGULATORY_COMPLIANCE RiskEvent rows with precise event_subtype classification.

    Per-query attribution flow (2026-05-12 Phase 2 rewrite)
    -------------------------------------------------------
    For each document, attribution proceeds through up to FOUR layers
    governed by the query's ``haiku_policy``:

      1.  **Q5 denylist** (when ``apply_q5_denylist=True``) — drop the
          item entirely if the title matches the mining-agency-noise
          denylist (turtles, grazing, oil/gas, etc.).  No further work.

      2.  **Topic + title attribution** (always runs, no LLM cost):
            a. ``_resolve_topics_to_materials`` over the FR controlled-
               vocabulary topics list (high-precision).
            b. ``_attribute_from_title`` — MaterialCache + AD/CVD +
               basket-fanout patterns on title + abstract.

      3.  **Haiku ``extract_fr_attribution``** (when policy allows):
            - "always"          — always run on raw text or abstract.
            - "fallback"        — run only when (2) returned 0 materials.
            - "after_denylist"  — run on whatever passed (1).
            - "never"           — skip entirely.
          The Haiku verdict can:
            * drop the event as off-scope (``is_in_scope=False`` and
              ``scope_confidence >= 0.7``);
            * contribute additional materials (Haiku-confirmed names);
            * contribute additional countries (with role context);
            * provide material × country links stored in metadata_json.

      4.  **Preserve-for-review path** — when steps 2 and 3 produce no
          material attribution but the event still passes a deterministic
          battery/CRM relevance gate (``_has_fr_battery_relevance``),
          the event is written WITHOUT RiskEventMaterial rows but with
          ``metadata_json["needs_material_review"] = true`` so an
          analyst can triage it later.  Mirrors the IEA Phase 1 review
          queue pattern.

    Regulation linkage (2026-05-19)
    -------------------------------
    After each event is persisted (and material / geography links are
    written), the FR doc number is checked against the
    ``regulation_aliases`` table under ``source_system="federal_register"``
    via ``RegulationAliasResolver``:

      * If the alias resolves to a tracked Regulation, a
        ``RiskEventRegulation`` junction row is written (relevance_score
        = 1.0, match_reason = "federal_register:ok").
      * If the alias is unknown AND the doc type is one of the
        high-authority types in ``_STAGE_REGULATION_DOC_TYPES``
        (Rule / Final Rule / Interim Final Rule / Executive Order),
        ``resolve_or_stage`` auto-creates a verified=False suggested
        regulation + alias row and links the event to it.  This
        populates the partner-review queue automatically as new
        regulations are published.
      * For lower-authority doc types (Notice, Proposed Rule, generic
        Presidential Documents) the resolver only attempts resolve —
        if unknown, the event flows through without a regulation link.
        These are typically enforcement actions / announcements UNDER
        existing regulations, not new regulations themselves; auto-
        staging them would flood the queue with low-signal entries.

    Per-query date windows
    ----------------------
    Each query honours its own ``lookback_days`` field.  The effective
    cutoff date is ``max(since_date, today - lookback_days)`` — i.e.,
    the caller's floor can extend the window backward but a query's
    lookback_days can NOT extend further than the caller asks.  Rare
    high-signal queries (UFLPA, PRESDOCU) use 365-day lookbacks; the
    volume streams (trade_remedy, mining_minerals) stay at 90.

    Args:
        session:    SQLAlchemy session. Commits internally after each page.
        since_date: Floor for publication_date filter.  Default: 365 days
                    ago (changed 2026-05-12 from 90 → 365 to accommodate
                    PRESDOCU + UFLPA windows).  Each query then narrows
                    further by its own lookback_days.
        max_pages:  Maximum pages to fetch per query.  Each page = up to
                    100 documents.  Default 5 → up to 500 docs/query.
        queries:    Override the default TARGETED_QUERIES list.

    Returns:
        Counter dict with attribution + skip provenance:
          documents_created, events_created,
          company_links,                          # 0 when
                                                  # feature_flags.LINK_EVENTS_TO_COMPANIES
                                                  # is False (the default —
                                                  # Foundation phase 3 disables
                                                  # ingest-time event→company
                                                  # linking; see Phase 5
                                                  # company overlay)
          material_links, geography_links,
          skipped_existing,                       # content-hash duplicate
          skipped_off_scope_keyword,              # failed cheap CRM gate
          skipped_off_scope_haiku,                # Haiku said off-scope
          skipped_q5_denylist,                    # mining-agency noise
          inserted_no_candidates_for_review,      # preserved, 0 attribution
          inserted_haiku_rejected_for_review,     # preserved, Haiku said
                                                  # in-scope but extracted
                                                  # no usable materials
          inserted_haiku_low_confidence_for_review,  # preserved, Haiku
                                                  # verdict had low
                                                  # confidence (True < 0.5
                                                  # OR False < 0.7)
          haiku_extract_calls,                    # billable invocations
          api_errors
    """
    if since_date is None:
        # 2026-05-12 default change: was 90 days; some queries need a
        # 365-day window (UFLPA, PRESDOCU) and the per-query
        # lookback_days will narrow back to 90 where appropriate.
        since_date = date.today() - timedelta(days=365)

    active_queries = queries if queries is not None else TARGETED_QUERIES

    log.info(
        "ingest_federal_register.start",
        since_date=since_date.isoformat(),
        max_pages=max_pages,
        query_count=len(active_queries),
    )

    source = _get_or_create_source(session)
    # Company resolution is gated by feature_flags.LINK_EVENTS_TO_COMPANIES
    # (default False — Foundation phase 3 disables ingest-time event→company
    # linking; Phase 5 company-overlay path supersedes it).  Skip the cache
    # build entirely when the flag is off to avoid the wasted query +
    # selectinload of every Company + aliases + exposures.  See
    # entity_resolution.persist_company_links docstring for the
    # architectural rationale.
    company_cache = (
        build_company_cache(session)
        if feature_flags.LINK_EVENTS_TO_COMPANIES
        else None
    )
    material_cache = MaterialCache.build(session)
    material_resolver = MaterialResolver(session)
    # 2026-05-14: alias resolver replaces the prior _TOPIC_MATERIAL_MAP
    # hardcoded dict.  Built once per run; in-process cached per source
    # system so per-doc lookups are O(1) after the first hit.
    alias_resolver = MaterialAliasResolver(session)
    # 2026-05-19: regulation alias resolver — links FR events to canonical
    # Regulation rows when document_number is a known alias.  Instantiated
    # once per run so the per-source_system alias cache stays warm across
    # all events.  Not feature-flag gated — this is a partner-curation
    # tool, not a scoring feature.
    reg_alias_resolver = RegulationAliasResolver(session)
    geo_cache = GeographyCache.build(session)

    # Single MaterialClassifier instance reused across all queries / docs.
    # No-op when ANTHROPIC_API_KEY isn't set (graceful degrade to keyword-
    # only flow). Includes its own in-process cache so re-asking on the
    # same text within a run is free.
    materials_by_id: dict[int, str] = {
        m.id: m.canonical_name
        for m in session.scalars(select(Material)).all()
    }
    classifier = MaterialClassifier(materials_by_id=materials_by_id)

    # Track document_numbers claimed by earlier queries to avoid duplicate events
    # when the same FR document matches multiple search terms.
    claimed_doc_numbers: set[str] = set()

    documents_created = 0
    events_created = 0
    company_links = 0
    material_links = 0
    geography_links = 0
    skipped_existing = 0
    skipped_off_scope_keyword = 0
    skipped_off_scope_haiku = 0
    skipped_q5_denylist = 0
    inserted_no_candidates_for_review = 0
    inserted_haiku_rejected_for_review = 0
    inserted_haiku_low_confidence_for_review = 0
    haiku_extract_calls = 0
    api_errors = 0
    regulation_links_created = 0
    regulation_suggestions_staged = 0

    with httpx.Client(follow_redirects=True) as client:
        for query in active_queries:
            # Per-query date floor: don't fetch older than `lookback_days`
            # even if the global since_date goes further back.  Inverse-
            # cap: never go past the caller's floor either.
            query_since = max(
                since_date,
                date.today() - timedelta(days=query.lookback_days),
            )

            log.info(
                "ingest_federal_register.query_start",
                query=query.name,
                event_subtype=query.event_subtype,
                lookback_days=query.lookback_days,
                query_since=query_since.isoformat(),
                haiku_policy=query.haiku_policy,
            )

            for page in range(1, max_pages + 1):
                time.sleep(_RATE_LIMIT_DELAY)

                try:
                    items, total_pages = _fetch_page(client, query, query_since, page)
                except (httpx.HTTPStatusError, httpx.TimeoutException, ValueError) as exc:
                    log.warning(
                        "ingest_federal_register.api_error",
                        query=query.name,
                        page=page,
                        error=str(exc),
                    )
                    api_errors += 1
                    break
                except Exception as exc:
                    # Catch-all so unexpected errors (ConnectError, SSLError, etc.)
                    # surface in api_errors rather than silently aborting the run.
                    log.warning(
                        "ingest_federal_register.api_error_unexpected",
                        query=query.name,
                        page=page,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    api_errors += 1
                    break

                log.info(
                    "ingest_federal_register.page_fetched",
                    query=query.name,
                    page=page,
                    items_returned=len(items),
                    total_pages=total_pages,
                )

                for item in items:
                    parsed = parse_federal_register_document(item)
                    if not parsed.external_id:
                        continue

                    doc_number = parsed.document_number or parsed.external_id

                    # ── Layer 1: Q5 denylist (cheap title gate) ───────────
                    if query.apply_q5_denylist and not _q5_passes_denylist(parsed.title or ""):
                        skipped_q5_denylist += 1
                        log.debug(
                            "ingest_federal_register.skipped_q5_denylist",
                            doc_number=doc_number,
                            title=(parsed.title or "")[:80],
                        )
                        continue

                    # Skip if already claimed by a higher-priority query
                    if doc_number in claimed_doc_numbers:
                        skipped_existing += 1
                        continue
                    claimed_doc_numbers.add(doc_number)

                    ch = _content_hash(
                        doc_number,
                        parsed.publication_date.isoformat() if parsed.publication_date else "",
                    )

                    # Check for existing risk event (idempotent re-runs)
                    if _event_exists_by_hash(session, ch):
                        skipped_existing += 1
                        continue

                    pub_dt = (
                        datetime.combine(
                            parsed.publication_date, datetime.min.time(), tzinfo=timezone.utc
                        )
                        if parsed.publication_date else None
                    )

                    # Prefer effective_on over publication_date for event_date.
                    # Using pub_dt alone biases regulations as "older" than they are
                    # in effect — a rule published in January but effective in July
                    # would be time-weighted as a 6-month-old signal on day one.
                    effective_dt = (
                        datetime.combine(
                            parsed.effective_date, datetime.min.time(), tzinfo=timezone.utc
                        )
                        if parsed.effective_date else None
                    )
                    event_dt = effective_dt or pub_dt

                    # ── Layer 2: Topic + title attribution (no LLM) ───────
                    # Combined text passed to MaterialCache via
                    # _attribute_from_title; the AD/CVD + basket fanout
                    # patterns inside that helper match the same way on
                    # title and abstract.
                    search_text = " ".join(
                        filter(None, [parsed.title, parsed.abstract_text])
                    )
                    detected_geos = geo_cache.detect(search_text)
                    topic_matches = _resolve_topics_to_materials(
                        parsed.topics, alias_resolver,
                    )
                    title_matches = _attribute_from_title(
                        search_text, material_cache, material_resolver,
                    )
                    # Dedup by material_id: topic > title (topic relevance
                    # is 0.90 vs title-basket 0.40, so topic should win
                    # the row).  When the same material lands in both
                    # lists, the topic version is kept.
                    seen_mids: set[int] = set()
                    detected_materials: list[tuple[int, float, str, int | None]] = []
                    for src_matches in (topic_matches, title_matches):
                        for cand in src_matches:
                            if cand[0] in seen_mids:
                                continue
                            seen_mids.add(cand[0])
                            detected_materials.append(cand)

                    # ── Layer 3: Conditional Haiku extraction ─────────────
                    haiku_payload: Optional[dict] = None
                    needs_haiku = (
                        query.haiku_policy == "always"
                        or (
                            query.haiku_policy == "fallback"
                            and not detected_materials
                        )
                        or query.haiku_policy == "after_denylist"
                    )
                    if needs_haiku and classifier.enabled:
                        # Fetch full text for Haiku — fall back to
                        # title+abstract when raw_text_url missing/fails.
                        full_text = _fetch_full_text(client, parsed.raw_text_url)
                        haiku_input_text = full_text or search_text
                        if haiku_input_text and len(haiku_input_text) >= 200:
                            haiku_payload = classifier.extract_fr_attribution(
                                haiku_input_text
                            )
                            haiku_extract_calls += 1

                    # ── Haiku scope gate (three-way verdict) ──────────────
                    # 2026-05-17 calibration: the previous gate dropped only
                    # on high-confidence rejection (is_in_scope=False AND
                    # conf >= 0.7) and accepted everything else.  That let
                    # through low-confidence acceptances like "National
                    # Ocean Month, 2025" (is_in_scope=True at conf=0.65)
                    # which are ceremonial proclamations Haiku should have
                    # been more decisive about.
                    #
                    # New three-way verdict:
                    #   - High-confidence reject (False + conf >= 0.7) → drop
                    #   - High-confidence accept (True + conf >= 0.5) → proceed
                    #     normally with Haiku-extracted materials/countries
                    #   - Low confidence either direction (True + conf < 0.5,
                    #     OR False + conf < 0.7) → preserve for analyst
                    #     review with review_reason="haiku_low_confidence",
                    #     regardless of whether other attribution paths
                    #     resolved materials.  When Haiku is uncertain, the
                    #     safest move is to surface the event to a human
                    #     rather than admit or drop silently.
                    haiku_low_confidence = False
                    if haiku_payload is not None:
                        is_in_scope = haiku_payload.get("is_in_scope")
                        scope_conf = float(
                            haiku_payload.get("scope_confidence") or 0.0,
                        )

                        if is_in_scope is False and scope_conf >= 0.7:
                            skipped_off_scope_haiku += 1
                            log.debug(
                                "ingest_federal_register.skipped_off_scope_haiku",
                                doc_number=doc_number,
                                scope_confidence=scope_conf,
                                query=query.name,
                            )
                            continue

                        if (
                            (is_in_scope is True and scope_conf < 0.5)
                            or (is_in_scope is False and scope_conf < 0.7)
                        ):
                            haiku_low_confidence = True

                    # Merge Haiku-extracted materials into detected_materials.
                    # Haiku confidence becomes the relevance for new mids;
                    # existing mids (already from topic / title) keep their
                    # earlier relevance — the cheap signal is at least as
                    # trustworthy as Haiku for the materials it found.
                    if haiku_payload is not None:
                        for m in haiku_payload.get("materials", []) or []:
                            canonical = (m.get("name") or "").strip()
                            conf = float(m.get("confidence") or 0.0)
                            if not canonical or conf < 0.5:
                                continue
                            material = material_resolver.resolve_by_canonical_name(canonical)
                            if material is None or material.id in seen_mids:
                                continue
                            evidence = (m.get("evidence") or "")[:24]
                            reason = (
                                f"haiku_fr:{canonical[:20]}|{evidence}"
                                if evidence else f"haiku_fr:{canonical[:48]}"
                            )
                            detected_materials.append(
                                (material.id, conf, reason[:64], None)
                            )
                            seen_mids.add(material.id)

                    # ── Bundle 2 (2026-06-07): Refine basket-fanout attributions ───
                    # Basket fanout (Layer 3 in _extract_from_title) attributes
                    # 5 materials at uniform 0.40 relevance for titles matching
                    # "lithium-ion batteries" and similar patterns.  Now that
                    # Haiku has read the document via extract_fr_attribution, we
                    # can confirm or attenuate each basket member based on
                    # whether the document substantively discusses it.
                    #
                    # Three-way refinement when Haiku gave a high-confidence
                    # scope accept (scope_conf >= 0.5):
                    #   Haiku confirmed (in materials list, conf >= 0.5) →
                    #     relevance bumped to max(0.40, 0.60) — confirmed
                    #   Haiku not mentioning this basket member →
                    #     relevance downweighted 0.40 → 0.20 (uncertain, NOT
                    #     dropped — Haiku may have missed a real one).
                    #   Haiku low-confidence or absent → no refinement (keep
                    #     0.40 baseline; review queue catches it elsewhere).
                    if (
                        haiku_payload is not None
                        and is_in_scope is True
                        and scope_conf >= 0.5
                    ):
                        haiku_mat_ids: set[int] = set()
                        for m in haiku_payload.get("materials", []) or []:
                            mname = (m.get("name") or "").strip()
                            mconf = float(m.get("confidence") or 0.0)
                            if not mname or mconf < 0.5:
                                continue
                            mat = material_resolver.resolve_by_canonical_name(mname)
                            if mat is not None:
                                haiku_mat_ids.add(mat.id)
                        refined: list = []
                        for mid, rel, reason, hs_id in detected_materials:
                            if reason.startswith("title_basket"):
                                if mid in haiku_mat_ids:
                                    # Confirmed by Haiku — bump relevance.
                                    new_rel = max(rel, 0.60)
                                    new_reason = f"basket_haiku_confirmed:{reason[len('title_basket:'):][:24]}"
                                    refined.append((mid, new_rel, new_reason[:64], hs_id))
                                else:
                                    # Not confirmed — downweight rather than drop
                                    # (Haiku may have missed a real basket member).
                                    new_rel = rel * 0.5
                                    new_reason = f"basket_haiku_unconfirmed:{reason[len('title_basket:'):][:22]}"
                                    refined.append((mid, new_rel, new_reason[:64], hs_id))
                            else:
                                refined.append((mid, rel, reason, hs_id))
                        detected_materials = refined

                    # Merge Haiku-extracted countries into detected_geos.
                    # Existing geo_cache matches keep their context;
                    # Haiku-only countries map to a context derived from
                    # the role field.  Dedupe by ISO2.
                    if haiku_payload is not None:
                        existing_iso2 = {iso for iso, _, _ in detected_geos}
                        for c in haiku_payload.get("countries", []) or []:
                            iso2 = (c.get("iso2") or "").strip().upper()[:2]
                            if not iso2 or iso2 in existing_iso2:
                                continue
                            role = (c.get("role") or "").lower()
                            conf = float(c.get("confidence") or 0.0)
                            if conf < 0.5:
                                continue
                            geo_context = {
                                "implementer": "implementer",
                                "affected":    "affected",
                                "subject":     "primary",
                            }.get(role, "affected")
                            detected_geos.append((iso2, geo_context, conf))
                            existing_iso2.add(iso2)

                    # ── Layer 4: Preserve-for-review gate ─────────────────
                    # Three review-queue triggers, in precedence order:
                    #   (a) Haiku gave a low-confidence verdict (either
                    #       direction) — preserve regardless of whether
                    #       materials were detected.  The scope decision
                    #       itself is uncertain; an analyst should look.
                    #   (b) No material attribution resolved AND the event
                    #       passes the deterministic CRM keyword gate —
                    #       preserve so the analyst can decide.
                    #   (c) No material attribution AND CRM gate fails —
                    #       drop as off-scope.
                    # Mirrors IEA Phase 1 review queue pattern, extended
                    # 2026-05-17 with the low-confidence trigger.
                    needs_material_review = False
                    review_reason: Optional[str] = None

                    if haiku_low_confidence:
                        needs_material_review = True
                        review_reason = "haiku_low_confidence"
                        inserted_haiku_low_confidence_for_review += 1
                    elif not detected_materials:
                        if not _has_fr_battery_relevance(search_text, query.name):
                            skipped_off_scope_keyword += 1
                            log.debug(
                                "ingest_federal_register.skipped_off_scope_keyword",
                                doc_number=doc_number,
                                title=(parsed.title or "")[:80],
                                query=query.name,
                            )
                            continue
                        needs_material_review = True
                        if haiku_payload is not None:
                            review_reason = "haiku_returned_no_materials"
                            inserted_haiku_rejected_for_review += 1
                        else:
                            review_reason = "no_candidates"
                            inserted_no_candidates_for_review += 1

                    # ── Persist the document + event ──────────────────────
                    doc, is_new = _upsert_source_document(
                        session=session,
                        source=source,
                        external_id=parsed.external_id,
                        title=parsed.title,
                        url=parsed.html_url or parsed.pdf_url,
                        published_at=pub_dt,
                        abstract=parsed.abstract_text,
                        metadata_json={
                            "document_number": doc_number,
                            "agencies": parsed.agencies[:8],
                            "topics": parsed.topics[:10],
                            "doc_type": parsed.doc_type,
                            "query_name": query.name,
                            "effective_on": parsed.effective_date.isoformat()
                            if parsed.effective_date else None,
                        },
                    )

                    if is_new:
                        documents_created += 1

                    severity = _severity(
                        parsed.doc_type, parsed.abstract_text, parsed.title, query
                    )

                    # Set geo_primary to the highest-relevance primary geography
                    # (used for display in RiskEvent.geography_json)
                    geo_primary = None
                    primary_geos = [
                        (iso2, score) for iso2, ctx, score in detected_geos
                        if ctx == "primary"
                    ]
                    if primary_geos:
                        geo_primary = max(primary_geos, key=lambda x: x[1])[0]
                    elif detected_geos:
                        # No "primary"-context match; fall back to the
                        # highest-relevance geography regardless of context.
                        geo_primary = max(detected_geos, key=lambda x: x[2])[0]

                    ev = _insert_risk_event(
                        session=session,
                        doc=doc,
                        title=parsed.title or doc_number,
                        summary=parsed.abstract_text,
                        event_date=event_dt,
                        severity=severity,
                        risk_categories=query.risk_categories,
                        query=query,
                        geo_primary=geo_primary,
                        agencies=parsed.agencies,
                        content_hash=ch,
                    )
                    events_created += 1

                    # Merge review-queue + Haiku payload into metadata_json.
                    # Done as a follow-up update because _insert_risk_event
                    # builds its own metadata block and we want to layer
                    # additions without re-plumbing every call-site arg.
                    extra_meta: dict = dict(ev.metadata_json or {})
                    extra_meta["needs_material_review"] = needs_material_review
                    if review_reason is not None:
                        extra_meta["review_reason"] = review_reason
                    if haiku_payload is not None:
                        extra_meta["haiku_scope"] = {
                            "is_in_scope": haiku_payload.get("is_in_scope"),
                            "confidence": haiku_payload.get("scope_confidence"),
                        }
                        # material_country_links is the 3-way relationship
                        # data — stored verbatim until/unless we add a
                        # junction table.  Capped to 16 links to bound the
                        # JSONB row size; richer event-coverage docs are
                        # rare in this stream.
                        mc_links = haiku_payload.get("material_country_links") or []
                        if mc_links:
                            extra_meta["material_country_links"] = mc_links[:16]
                    ev.metadata_json = extra_meta

                    # Tag materials (skipped when review-path; the empty
                    # detected_materials list short-circuits the helper).
                    mat_written = _persist_material_links(session, ev, detected_materials)
                    material_links += mat_written

                    # Tag geographies detected in title/abstract + Haiku.
                    geo_written = _persist_geography_links(session, ev, detected_geos)
                    geography_links += geo_written

                    # Resolve and link companies — gated by feature flag.
                    # When LINK_EVENTS_TO_COMPANIES is False (Foundation
                    # phase 3 default), skip the resolver loop entirely
                    # rather than running it and discarding the result
                    # inside persist_company_links.  Saves CPU on the
                    # 75-company × N-event inner loop and avoids the
                    # per-event WARN log spam from the suppression path.
                    linked = 0
                    if feature_flags.LINK_EVENTS_TO_COMPANIES:
                        co_matches = resolve_companies_for_event(
                            session, ev, company_cache,
                        )
                        persist_company_links(session, ev, co_matches)
                        linked = len(co_matches)
                        company_links += linked

                    # Regulation linkage — link this event to a canonical Regulation
                    # row when the FR doc number is a known alias.  For high-authority
                    # doc types (Rule / IFR / EO), auto-stage a suggested regulation
                    # when the doc number is unknown.  Lower-authority docs (Notice,
                    # PRORULE, PRESDOCU other than EO) only attempt resolve — they're
                    # typically enforcement under existing regulations, not new ones.
                    #
                    # Per-run resolver instance was created at startup so the alias
                    # cache stays warm across all events in this ingest run.
                    doc_type_lc = (parsed.doc_type or "").strip().lower()
                    if doc_number:
                        if doc_type_lc in _STAGE_REGULATION_DOC_TYPES:
                            reg_result = reg_alias_resolver.resolve_or_stage(
                                source_system="federal_register",
                                source_key=doc_number,
                                title_hint=parsed.title,
                                context_hint=(
                                    f"{query.name} | {parsed.doc_type} | "
                                    f"{(parsed.title or '')[:200]}"
                                ),
                            )
                            if reg_result.status == "staged":
                                regulation_suggestions_staged += 1
                        else:
                            reg_result = reg_alias_resolver.resolve(
                                "federal_register", doc_number,
                            )

                        if (
                            reg_result.status in ("ok", "staged")
                            and reg_result.regulation is not None
                        ):
                            session.add(RiskEventRegulation(
                                risk_event_id=ev.id,
                                regulation_id=reg_result.regulation.id,
                                relevance_score=1.0,
                                match_reason=f"federal_register:{reg_result.status}",
                            ))
                            regulation_links_created += 1

                    log.debug(
                        "ingest_federal_register.event_created",
                        document_number=doc_number,
                        subtype=query.event_subtype,
                        severity=severity,
                        companies_linked=linked,
                        materials_linked=mat_written,
                        geographies_linked=geo_written,
                        needs_review=needs_material_review,
                        haiku_called=haiku_payload is not None,
                    )

                # Commit after each page to return DB connection to pool
                session.commit()

                log.info(
                    "ingest_federal_register.page_done",
                    query=query.name,
                    page=page,
                    total_pages=total_pages,
                )

                if page >= total_pages:
                    break

    log.info(
        "ingest_federal_register.done",
        documents_created=documents_created,
        events_created=events_created,
        company_links=company_links,
        material_links=material_links,
        geography_links=geography_links,
        regulation_links_created=regulation_links_created,
        regulation_suggestions_staged=regulation_suggestions_staged,
        skipped_existing=skipped_existing,
        skipped_off_scope_keyword=skipped_off_scope_keyword,
        skipped_off_scope_haiku=skipped_off_scope_haiku,
        skipped_q5_denylist=skipped_q5_denylist,
        inserted_no_candidates_for_review=inserted_no_candidates_for_review,
        inserted_haiku_rejected_for_review=inserted_haiku_rejected_for_review,
        inserted_haiku_low_confidence_for_review=inserted_haiku_low_confidence_for_review,
        haiku_extract_calls=haiku_extract_calls,
        api_errors=api_errors,
    )
    return {
        "documents_created": documents_created,
        "events_created": events_created,
        "company_links": company_links,
        "material_links": material_links,
        "geography_links": geography_links,
        "regulation_links_created": regulation_links_created,
        "regulation_suggestions_staged": regulation_suggestions_staged,
        "skipped_existing": skipped_existing,
        "skipped_off_scope_keyword": skipped_off_scope_keyword,
        "skipped_off_scope_haiku": skipped_off_scope_haiku,
        "skipped_q5_denylist": skipped_q5_denylist,
        "inserted_no_candidates_for_review": inserted_no_candidates_for_review,
        "inserted_haiku_rejected_for_review": inserted_haiku_rejected_for_review,
        "inserted_haiku_low_confidence_for_review": inserted_haiku_low_confidence_for_review,
        "haiku_extract_calls": haiku_extract_calls,
        "api_errors": api_errors,
    }
