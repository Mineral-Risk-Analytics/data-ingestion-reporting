"""Global Trade Alert (GTA) harmful-intervention ingestion.

GTA, maintained by the University of St. Gallen, is the authoritative free
database of government trade-policy interventions worldwide. Without these
records the Geopolitical/Trade pillar materially understates risk for
graphite (China), nickel (Indonesia), and cobalt (DRC) — every major export
restriction since 2018 is sourced from GTA.

We ingest **Red** (harmful) interventions only — Amber (announced) and Green
(liberalising) are out of scope for risk scoring. Each intervention is
filtered down to the rows whose affected HS codes match
``BATTERY_HS_PREFIXES`` and persisted as one ``RiskEvent`` plus
``RiskEventGeography`` / ``RiskEventMaterial`` junction rows.

GTA download URL stability
    GTA occasionally restructures their download endpoints. If the primary
    URL stops returning a CSV, check
    ``https://www.globaltradealert.org/data_extraction`` for the current
    bulk download link and update :data:`GTA_DOWNLOAD_URL` here. The URL
    actually used is recorded in each event's ``metadata_json["source_url"]``.

Column-name resilience
    GTA CSV column names have changed between releases (e.g.
    ``implementing_jurisdiction`` vs ``implementing_country``). The parser
    builds a lowercase column map from the actual header row and never
    assumes fixed column positions. If a *required* column is missing the
    parser raises ``ValueError`` with the available column names so the
    operator can update the alias map.

HS code format
    GTA stores HS codes as 6-digit strings, sometimes with leading zeros
    stripped during CSV roundtrips. We zero-pad to six digits before
    comparing against :data:`BATTERY_HS_PREFIXES`.

Severity calibration
    - Export bans                       → 0.9  (explicitly blocks supply)
    - All other active Red interventions → 0.7
    - Inactive / removed interventions  → 0.3
    The scoring engine applies recency decay on top of these base values.

Review workflow
    GTA events are inserted with ``verified=False``. The existing admin
    event-review UI promotes them; this ingester never auto-verifies.

Update cadence
    GTA refreshes weekly. Re-running this command is safe — duplicates are
    suppressed by ``RiskEvent.content_hash`` (and a secondary
    ``metadata_json->>'gta_id'`` check) so only new interventions hit the
    insert path.
"""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import date, datetime, timezone
from typing import Any, Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.country import Country
from app.models.documents import SourceDocument
from app.models.regulatory import (
    RiskEvent,
    RiskEventGeography,
    RiskEventHsMapping,
    RiskEventMaterial,
)
from app.models.source import Source
from app.services.ingestion.comtrade import (
    _build_hs_material_map,
    _resolve_material_id,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants & taxonomies
# ---------------------------------------------------------------------------

# NOTE: The old bulk-CSV endpoint (/data_extraction/download) no longer exists.
# GTA now requires registration and an API key to access data programmatically.
# See https://globaltradealert.org/api-access to register.
#
# Until the API is wired up, use the --local-file option in the CLI to pass
# a CSV you have manually exported from https://globaltradealert.org/data-center.
#
# When the API is ready, set this to:
#   "https://api.globaltradealert.org/api/v1/data/"
# and update download_gta_csv() to use POST + Authorization: APIKey <key>.
GTA_DOWNLOAD_URL = (
    "https://www.globaltradealert.org/data_extraction/download"
    "?dataset=state-acts&format=csv"
)

_SOURCE_NAME = "Global Trade Alert"
_SOURCE_TYPE = "gta"
_SOURCE_PHASE = "1"
_SOURCE_BASE_URL = "https://www.globaltradealert.org"

# Filter GTA rows where any affected HS code starts with one of these prefixes.
# Keep aligned with app/services/ingestion/seed_hs_mappings.py — any material
# tracked there should have its ore and metal-form HS codes represented here.
BATTERY_HS_PREFIXES: list[str] = [
    # ── Lithium ──────────────────────────────────────────────────────────────
    "2501",   # Salt; sulphur; earths/stone — lithium minerals (spodumene, petalite)
    "2825",   # Inorganic bases — lithium hydroxide (2825.20); primary battery electrolyte form
    "2836",   # Carbonates — lithium carbonate (2836.91); primary cathode precursor
    "2805",   # Alkali + rare earth metals — lithium metal (2805.12); REE metals (2805.30)
    # ── Natural Graphite ─────────────────────────────────────────────────────
    "2504",   # Natural graphite — all forms (flake, vein, amorphous)
    # ── Manganese ────────────────────────────────────────────────────────────
    "2602",   # Manganese ores and concentrates
    "8111",   # Manganese and articles — unwrought metal
    # ── Nickel ───────────────────────────────────────────────────────────────
    "2604",   # Nickel ores and concentrates
    "7501",   # Nickel mattes, oxide sinters and intermediate products
    "7502",   # Unwrought nickel
    # ── Cobalt ───────────────────────────────────────────────────────────────
    "2605",   # Cobalt ores and concentrates  ← DRC / Zambia export restrictions
    "8105",   # Cobalt mattes; unwrought cobalt; cobalt powders
    # ── Copper ───────────────────────────────────────────────────────────────
    "2603",   # Copper ores and concentrates
    "7402",   # Copper (unrefined); copper anodes for electrolytic refining
    "7403",   # Refined copper and copper alloys — unwrought
    # ── Rare Earth Elements (incl. magnet REEs: Nd, Pr, Dy, Tb) ─────────────
    "2846",   # Compounds of rare earth metals — primary processed trade form
    # 2805.30 already covered above (rare earth metals unwrought)
    # ── Platinum-Group Metals (fuel cell catalysts, sensors) ─────────────────
    "7110",   # Platinum; palladium; rhodium; iridium; osmium; ruthenium
    "2616",   # Precious metal ores — PGM-bearing ores (2616.90)
    # ── Tungsten ─────────────────────────────────────────────────────────────
    "2611",   # Tungsten ores and concentrates
    "8101",   # Tungsten and articles — unwrought metal, powders
    # ── Minor critical metals (Ga, Ge, In, Nb, V, Cr) ────────────────────────
    "8112",   # Chapter 81 minor metals: Ga (8112.21/29), Ge (8112.31/39),
              # In (8112.61/69), Nb (8112.92), V, Cr — all CRMA Annex II strategic
    # ── Antimony (battery separator flame retardants) ─────────────────────────
    "2617",   # Other ores — antimony ores (2617.10); some REE-bearing ores
    "8110",   # Antimony and articles — unwrought metal
    # ── Magnesium ────────────────────────────────────────────────────────────
    "8104",   # Magnesium and articles
    # ── Titanium ─────────────────────────────────────────────────────────────
    "8108",   # Titanium and articles — metal sponge, EV structural materials
    # ── Batteries and cells ──────────────────────────────────────────────────
    "8507",   # Electric accumulators — batteries and cells
    "8548",   # Waste and scrap of primary cells, batteries (recycling policy signals)
]

# Map GTA implementing country names (or ISO2) → ISO2.
# GTA uses ISO2 codes in some columns and full country names in others.
# _resolve_country() handles bare ISO2 codes directly; this map only needs to
# cover full-name variants that appear in GTA CSV exports.
GTA_COUNTRY_MAP: dict[str, str] = {
    # ── Major battery material producers ─────────────────────────────────────
    "China": "CN",
    "Indonesia": "ID",
    "Democratic Republic of the Congo": "CD",
    "DRC": "CD",
    "Congo, Democratic Republic": "CD",
    "Congo, Dem. Rep.": "CD",
    "Russia": "RU",
    "Russian Federation": "RU",
    "Chile": "CL",
    "Australia": "AU",
    "South Africa": "ZA",
    "Philippines": "PH",
    "Mozambique": "MZ",
    "Zimbabwe": "ZW",
    "Bolivia": "BO",
    "Bolivia, Plurinational State of": "BO",
    "Argentina": "AR",
    "Zambia": "ZM",
    "Peru": "PE",
    "Brazil": "BR",
    "Morocco": "MA",
    "Madagascar": "MG",
    "Kazakhstan": "KZ",
    "Myanmar": "MM",
    "Burma": "MM",
    "Lao People's Democratic Republic": "LA",
    "Laos": "LA",
    "Malaysia": "MY",
    "Thailand": "TH",
    "Vietnam": "VN",
    "Viet Nam": "VN",
    "Papua New Guinea": "PG",
    "Guinea": "GN",
    "Côte d'Ivoire": "CI",
    "Ivory Coast": "CI",
    "Tanzania": "TZ",
    "Tanzania, United Republic of": "TZ",
    "Namibia": "NA",
    "Gabon": "GA",
    "Ghana": "GH",
    "Nigeria": "NG",
    "Ethiopia": "ET",
    # ── Major consumer / manufacturing economies ──────────────────────────────
    "United States of America": "US",
    "United States": "US",
    "USA": "US",
    "European Union": "EU",
    "Germany": "DE",
    "France": "FR",
    "United Kingdom": "GB",
    "Japan": "JP",
    "South Korea": "KR",
    "Korea, Republic of": "KR",
    "Korea": "KR",
    "Canada": "CA",
    "India": "IN",
    "Mexico": "MX",
    "Norway": "NO",
    "Finland": "FI",
    "Sweden": "SE",
    "Netherlands": "NL",
    "Belgium": "BE",
    "Poland": "PL",
    "Czech Republic": "CZ",
    "Czechia": "CZ",
    "Spain": "ES",
    "Italy": "IT",
    "Portugal": "PT",
    "Turkey": "TR",
    "Türkiye": "TR",
    "Taiwan": "TW",
    "Taiwan, Province of China": "TW",
    "Singapore": "SG",
    "New Zealand": "NZ",
}

# Map GTA intervention_type → app.constants.RiskCategory string value.
GTA_INTERVENTION_CATEGORY_MAP: dict[str, str] = {
    "Export taxes": "geopolitical_trade",
    "Export quotas": "geopolitical_trade",
    "Export licensing requirements": "geopolitical_trade",
    "Export bans": "geopolitical_trade",
    "Export subsidies": "geopolitical_trade",
    "Import tariff": "geopolitical_trade",
    "Import quota": "geopolitical_trade",
    "Import ban": "geopolitical_trade",
    "Sanitary and phytosanitary measure": "regulatory_compliance",
    "Technical barrier to trade": "regulatory_compliance",
    # 2026-06-06: Local content requirements and Procurement re-routed
    # to regulatory_compliance.  These are domestic-content compliance
    # mandates (Buy-American, EU domestic-procurement preferences) rather
    # than trade restrictions — the regulatory_compliance category better
    # captures the compliance-obligation character.  They also carry the
    # PROCUREMENT_POLICY event_subtype so they don't feed tariff_exposure
    # or export_restriction_exposure even when they would otherwise have
    # matched a title-text fallback.
    "Local content requirement": "regulatory_compliance",
    "Local content requirements": "regulatory_compliance",
    "Trade finance": "financial_pressure",
    "Investment measure": "geopolitical_trade",
    "State aid": "financial_pressure",
    "Procurement": "regulatory_compliance",
    "Procurement policy": "regulatory_compliance",
    "Public-private partnership": "regulatory_compliance",
}
DEFAULT_GTA_CATEGORY = "geopolitical_trade"

# Map GTA intervention_type → event_subtype stored in metadata_json["event_subtype"].
#
# The scoring engine in market_aggregator.py and evidence_aggregator.py reads
# this field to route events into the correct signal bucket (export_restriction_exposure,
# trade_volatility, etc.). Without an explicit subtype the engine falls back to
# title-text matching ("export" + "ban/restrict/control"), which misses events whose
# titles describe the measure without those keywords.
#
# Supply-side interventions (implementing country restricts outbound trade):
#   EXPORT_RESTRICTION  → export_restriction_exposure pillar
# Demand-side interventions (importing country restricts inbound trade):
#   IMPORT_DISRUPTION   → trade_volatility pillar (lower weight)
# Production subsidies (implementing country subsidises domestic production
# — distorts downstream competition without restricting trade):
#   EXPORT_SUBSIDY      → production_subsidy_distortion sub-input on
#                         the Geopolitical pillar (G-Cov-3, 2026-05-09)
# No subtype is set for instruments that don't map cleanly to any bucket
# (Procurement, Public-private partnership) — text matching handles those.
_INTERVENTION_SUBTYPE_MAP: dict[str, str] = {
    # Procurement-side interventions (Buy-American mandates, domestic-content
    # procurement preferences, local-content requirements applied through
    # government purchasing).  Added 2026-06-06.  These actions create
    # market distortion in the implementing country but are not "trade
    # restrictions" in the traditional sense — they affect international
    # trade flows indirectly through public-sector demand-side preferences.
    # The PROCUREMENT_POLICY subtype routes nowhere by default (no entry
    # in the tariff/export sub-input matchers in market_aggregator), making
    # these events informational rather than score-moving.  Partner can
    # promote them to a scored sub-input via the existing GeoCovragePillar
    # tuning if customer use cases warrant.
    "Procurement":                       "PROCUREMENT_POLICY",
    "Procurement policy":                "PROCUREMENT_POLICY",
    "Local content requirement":         "PROCUREMENT_POLICY",
    "Local content requirements":        "PROCUREMENT_POLICY",
    "Public-private partnership":        "PROCUREMENT_POLICY",
    # Export-side interventions (implementing country IS the producer
    # whose supply just got constrained).
    # 2026-05-06: GTA's actual data uses singular forms ("Export licensing
    # requirement", not "requirements") — the prior plural keys here
    # silently failed to match real GTA rows, leaving event_subtype NULL
    # and tariff/export sub-scores at zero.  Both forms accepted now.
    "Export taxes":                  "EXPORT_RESTRICTION",
    "Export tax":                    "EXPORT_RESTRICTION",
    "Export quotas":                 "EXPORT_RESTRICTION",
    "Export quota":                  "EXPORT_RESTRICTION",
    "Export licensing requirements": "EXPORT_RESTRICTION",
    "Export licensing requirement":  "EXPORT_RESTRICTION",
    "Export bans":                   "EXPORT_RESTRICTION",
    "Export ban":                    "EXPORT_RESTRICTION",
    # Import-side interventions (affected country IS the producer being
    # tariffed; see the G11 audit fix in hs_node_scorer.py).
    "Import tariff":                 "IMPORT_DISRUPTION",
    "Import tariffs":                "IMPORT_DISRUPTION",
    "Import quota":                  "IMPORT_DISRUPTION",
    "Import quotas":                 "IMPORT_DISRUPTION",
    "Import ban":                    "IMPORT_DISRUPTION",
    "Import bans":                   "IMPORT_DISRUPTION",
    # Production-subsidy interventions (G-Cov-3, 2026-05-09).
    # GTA classifies these under several distinct intervention_type
    # strings; ~306 of 1,728 battery interventions (~18%) fall in this
    # bucket and previously got event_subtype=NULL.  See
    # docs/coverage-gap-plan-2026-05.md G-Cov-3 for the inventory and
    # the scoring-side wiring (production_subsidy_distortion).
    "Trade finance":                          "EXPORT_SUBSIDY",
    "State loan":                             "EXPORT_SUBSIDY",
    "Financial assistance in foreign market": "EXPORT_SUBSIDY",
    "Loan guarantee":                         "EXPORT_SUBSIDY",
    "Financial grant":                        "EXPORT_SUBSIDY",
    "State aid, unspecified":                 "EXPORT_SUBSIDY",
    "Equity stake":                           "EXPORT_SUBSIDY",
    "Financial investment support":           "EXPORT_SUBSIDY",
    # Production subsidies that target output rather than trade — same
    # bucket because the downstream distortion mechanism is identical.
    "Production subsidy":                     "EXPORT_SUBSIDY",
    "Production subsidies":                   "EXPORT_SUBSIDY",
    "Producer support":                       "EXPORT_SUBSIDY",
}

# RiskEvent.event_type is String(128); truncate to fit.
_EVENT_TYPE_MAX = 128
# Summary kept short so the row stays printable in admin tables.
_SUMMARY_MAX = 500
# Flush every N inserts to keep the SQLAlchemy unit-of-work compact.
_FLUSH_EVERY = 100

# Note (2026-05-12): an earlier draft of this module included a
# ``_should_attribute_via_hs`` classifier that gated RiskEventMaterial /
# RiskEventHsMapping writes for ~63% of GTA events based on mast_chapter
# / eligible_firm / intervention_subtype.  That classifier was reverted
# after an empirical distribution audit showed it was over-aggressive —
# the "fake breadth" premise was a misread of a small outlier cluster,
# not the actual data shape.  See the Step-3 ticket in
# Automotive Data Solutions/github_issues_event_attribution.md for the
# full reasoning trail.  We still capture mast_chapter / eligible_firm /
# affected_sectors into metadata_json (useful for future analysis) but
# no longer act on them at ingest time.  The existing
# _INTERVENTION_SUBTYPE_MAP correctly routes subsidies to the
# EXPORT_SUBSIDY event_subtype, which feeds a different scoring
# sub-input than restrictions do.


# ---------------------------------------------------------------------------
# CSV download
# ---------------------------------------------------------------------------

def download_gta_csv(url: str = GTA_DOWNLOAD_URL, timeout: int = 120) -> bytes:
    """Download the GTA State Acts CSV and return raw bytes.

    Streams the response so the full file is not held as a single allocation
    until every chunk has been received (the bulk export is ~50–100 MB
    uncompressed).

    Raises:
        httpx.HTTPStatusError: on non-2xx HTTP response.
        httpx.RequestError: on network / DNS / timeout failures.
    """
    log.info("gta.download.start", url=url)
    chunks: list[bytes] = []
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            chunks.append(chunk)
    raw = b"".join(chunks)
    log.info("gta.download.done", bytes=len(raw))
    return raw


# ---------------------------------------------------------------------------
# CSV parsing & filtering
# ---------------------------------------------------------------------------

# Aliases per logical column. The first match (case-insensitive) in the CSV
# header row wins. Extending these keeps the ingester resilient to GTA
# header renames without touching the rest of the code.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "intervention_id", "state_act_id"),
    "title": ("title", "intervention_title", "state_act_title"),
    "date_announced": ("date_announced", "announced_date", "announcement_date"),
    "date_implemented": (
        "date_implemented",
        "implementation_date",
        "implemented_date",
    ),
    "date_removed": ("date_removed", "removal_date", "removed_date"),
    "implementing_jurisdiction": (
        "implementing_jurisdiction",
        # GTA curated exports use plural form
        "implementing_jurisdictions",
        "implementing_country",
        "jurisdiction",
        "implementer",
    ),
    "gta_evaluation": ("gta_evaluation", "evaluation", "traffic_light"),
    "intervention_type": ("intervention_type", "mast_chapter", "instrument"),
    "affected_hs_codes": (
        "affected_hs_codes",
        "hs_codes_affected",
        "hs_codes",
        "affected_products_hs",
        # GTA curated dataset exports name this column "Affected Products";
        # it may contain GTA internal product codes or HS codes depending on
        # the export type. Use --skip-hs-filter when ingesting curated exports
        # that are already pre-filtered (e.g. "Harmful Trade Policy
        # Interventions: Batteries") so the HS prefix check is bypassed.
        "affected_products",
    ),
    "description": ("description", "summary", "intervention_description"),
    "in_force": ("in_force", "currently_in_force", "is_in_force"),
    # Added 2026-05-06 (G11/Scope 2):
    # ``affected_jurisdictions`` — countries whose imports/exports are
    # constrained by the intervention.  Critical for IMPORT_DISRUPTION
    # events: the implementing country is the importing country, but the
    # producer country whose risk should rise is in this column.  GTA
    # bulk + curated exports both name the column "Affected Jurisdictions".
    "affected_jurisdictions": (
        "affected_jurisdictions",
        "affected_jurisdiction",
        "target_jurisdiction",
        "target_jurisdictions",
    ),
    # ``implementation_level`` — National | Sub-national | Multilateral.
    # Severity multiplier applied at ingest.
    "implementation_level": (
        "implementation_level",
        "implementing_level",
    ),
    # ``is_horizontal`` — boolean.  When True the intervention applies
    # broadly across products rather than being targeted; per-HS-code
    # signal is reduced.
    "is_horizontal": (
        "is_horizontal",
        "horizontal",
    ),
    # ── Added 2026-05-12 (attribution density audit, Step 3) ─────────────
    # Three more structural fields from the curated CSV, captured into
    # metadata_json for future analysis.  An earlier draft also used them
    # to gate HS-based material attribution; that gating was reverted
    # after the distribution audit (see the revert note near the top of
    # this module).  These columns remain useful as queryable metadata
    # — e.g., to slice events by mast_chapter for ad-hoc analysis — even
    # though we no longer act on them at ingest time.
    #
    # ``mast_chapter`` — MAST high-level classification (e.g., "Tariff
    # measures", "L: Subsidies", "P: Export-related measures").  Distri-
    # bution in interventions_batteries.csv: 59% L: Subsidies, 16% P,
    # 16% Tariff, remainder spread across 11 other chapters.  Curated CSV
    # column "Mast Chapter".
    "mast_chapter": (
        "mast_chapter",
        "mast chapter",
    ),
    # ``eligible_firm`` — who the intervention applies to.  Values like
    # SMEs / firm-specific / sector-specific indicate a targeted financial
    # program rather than a broad product policy.  Curated CSV column
    # "Eligible Firm".
    "eligible_firm": (
        "eligible_firm",
        "eligible firm",
    ),
    # ``affected_sectors`` — CPC sector codes (comma-separated).  Not used
    # by the classifier today but captured into metadata_json for future
    # debugging and possible per-sector filters.  Curated CSV column
    # "Affected Sectors".
    "affected_sectors": (
        "affected_sectors",
        "affected sectors",
    ),
}

# Required columns — the parser raises ValueError if any of these can't be
# resolved against the actual header row.
_REQUIRED_LOGICAL_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "implementing_jurisdiction",
    "gta_evaluation",
    "intervention_type",
    "affected_hs_codes",
)

_RED_VALUES = {"red", "harmful"}
_TRUE_STRS = {"true", "1", "yes", "y", "t"}

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y/%m")


def _build_column_map(headers: list[str]) -> dict[str, str]:
    """Resolve logical column names → actual header names from the CSV.

    Normalises headers and alias candidates to lowercase with spaces converted
    to underscores, so GTA curated exports ("Intervention ID", "GTA Evaluation",
    "Implementing Jurisdictions") match the underscore-style aliases without
    needing explicit duplicate entries.
    """

    def _norm(s: str) -> str:
        return s.strip().lower().replace(" ", "_")

    normalised = {_norm(h): h for h in headers if h is not None}
    resolved: dict[str, str] = {}
    for logical, candidates in _COLUMN_ALIASES.items():
        for cand in candidates:
            actual = normalised.get(_norm(cand))
            if actual is not None:
                resolved[logical] = actual
                break
    missing = [c for c in _REQUIRED_LOGICAL_COLUMNS if c not in resolved]
    if missing:
        raise ValueError(
            "GTA CSV is missing required column(s) "
            f"{missing}. Headers seen: {sorted(normalised.values())}. "
            "Update _COLUMN_ALIASES in app/services/ingestion/gta.py with the "
            "current GTA column names."
        )
    return resolved


def _parse_date(raw: str) -> Optional[datetime]:
    """Parse a date string tolerantly.

    GTA mixes ``YYYY-MM-DD`` and ``YYYY-MM`` (and occasionally with ``/``
    separators). Returns a tz-aware UTC ``datetime`` so ``RiskEvent.event_date``
    (a ``DateTime(timezone=True)`` column) gets a consistent value. If only a
    year-month is present, day defaults to 1.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            naive = datetime.strptime(raw, fmt)
            return naive.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _split_hs_codes(raw: str) -> list[str]:
    """Split a GTA HS codes cell into normalised 6-digit strings.

    GTA separates codes with semicolons or commas. Codes may arrive as
    integers (``"260400"`` → ``260400``) or with chapter dots
    (``"26.04.00"``).  We strip non-digits and truncate national extensions
    (HTS-10) to the HS-6 international form.

    **CPC vs HS discrimination.** GTA's "Affected Products" column carries
    either HS codes or CPC (Central Product Classification) codes depending
    on the dataset.  CPC tops out at 5 digits; HS is 6+ internationally.
    Codes shorter than 6 digits are CPC and are dropped — the previous
    implementation zero-padded them, which masked their identity and
    relied on accidental non-overlap with battery HS prefixes (which all
    start with 2/3/7/8) to avoid false positives.  Reject explicitly.
    """
    if not raw:
        return []
    out: list[str] = []
    # Allow either separator. csv parses already, so we work on the cell.
    parts = raw.replace(",", ";").split(";")
    for part in parts:
        digits = "".join(ch for ch in part if ch.isdigit())
        if len(digits) < 6:
            # CPC product code (or malformed) — not an HS code.
            continue
        out.append(digits[:6])
    return out


def _matches_prefix(hs_code: str, prefixes: list[str]) -> bool:
    return any(hs_code.startswith(p) for p in prefixes)


def _resolve_country(raw: str, country_name_map: Optional[dict[str, str]] = None) -> Optional[str]:
    """Map a GTA implementing-jurisdiction cell to ISO2, or ``None``.

    Resolution order:
      1. Already a 2-letter alpha string → return as-is (upper-cased).
      2. Lookup in ``country_name_map`` (built from ``countries.common_names``
         at ingest time via ``_build_country_name_map``).
      3. Fallback to the hardcoded ``GTA_COUNTRY_MAP`` (covers cases where the
         countries table has not been seeded yet).

    Returns ``None`` for anything unrecognised so the caller can log + continue
    rather than abort.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    # 1. Already ISO2.
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    # 2. DB-derived name map.
    if country_name_map:
        iso2 = country_name_map.get(raw)
        if iso2:
            return iso2
    # 3. Hardcoded fallback.
    return GTA_COUNTRY_MAP.get(raw)


def _resolve_country_list(
    raw: str,
    country_name_map: Optional[dict[str, str]] = None,
) -> list[str]:
    """Resolve a comma-separated GTA jurisdictions cell to a list of ISO2 codes.

    GTA's "Affected Jurisdictions" column contains comma-separated country
    names (e.g. ``"Argentina, Seychelles"`` or, for broad multilateral
    interventions, 100+ entries).  Each name is resolved through the same
    pipeline as ``_resolve_country``.  Unresolvable names are dropped
    silently — caller can compare ``len(out)`` to the expected count if it
    needs to flag unknowns.

    Returned list is deduplicated, preserving first-occurrence order so
    the primary affected jurisdiction (when present at the head of the
    list) stays at index 0.
    """
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for piece in raw.split(","):
        iso2 = _resolve_country(piece.strip(), country_name_map)
        if iso2 and iso2 not in seen:
            out.append(iso2)
            seen.add(iso2)
    return out


def _build_country_name_map(session: Session) -> dict[str, str]:
    """Build a full-name → ISO2 lookup from the ``countries`` table.

    Each row's ``common_names`` JSONB array is exploded so every alias maps
    to the row's ``iso2``.  Falls back gracefully to an empty dict if the
    table is empty (countries not yet seeded).
    """
    rows = session.scalars(select(Country)).all()
    name_map: dict[str, str] = {}
    for row in rows:
        # Always map the canonical name itself.
        name_map[row.name] = row.iso2
        if row.common_names:
            for alias in row.common_names:
                if alias:
                    name_map[str(alias)] = row.iso2
    return name_map


def _derive_hs_prefixes_from_db(session: Session) -> list[str]:
    """Return all HS code prefixes currently stored in ``hs_code_material_mappings``.

    Used by ``ingest_gta`` to derive its filter list dynamically so that adding
    a new material + HS mapping to the DB automatically expands GTA coverage
    without requiring a code change.

    Falls back to the hardcoded ``BATTERY_HS_PREFIXES`` list if the table is
    empty (e.g. HS mappings not yet seeded).
    """
    hs_map = _build_hs_material_map(session)
    if not hs_map:
        log.warning(
            "gta.derive_hs_prefixes.empty_table",
            hint="Run 'bdi-ingest seed-hs-mappings' before ingest-gta.",
        )
        return BATTERY_HS_PREFIXES
    # Normalise: strip dots, take unique 4-digit prefixes to keep the filter
    # broad (GTA codes are 6-digit but matching on 4 digits avoids sub-code gaps).
    prefixes: set[str] = set()
    for raw_prefix in hs_map.keys():
        clean = raw_prefix.replace(".", "")
        # Use 4-digit prefix for filtering (same logic as the HS resolver pass-2).
        if len(clean) >= 4:
            prefixes.add(clean[:4])
    return sorted(prefixes)


def _to_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in _TRUE_STRS


def parse_gta_csv(
    raw_bytes: bytes,
    hs_prefixes: list[str] = BATTERY_HS_PREFIXES,
    since_year: Optional[int] = 2018,
    skip_hs_filter: bool = False,
    country_name_map: Optional[dict[str, str]] = None,
) -> list[dict]:
    """Parse GTA CSV bytes into a list of normalised intervention dicts.

    Filters to:
        - ``gta_evaluation`` is "Red" / "harmful" (case-insensitive)
        - At least one affected HS code matches one of ``hs_prefixes``
          (skipped when ``skip_hs_filter=True`` — use for pre-filtered curated
          exports such as "Harmful Trade Policy Interventions: Batteries" where
          GTA has already applied product-level filtering using its own internal
          classification codes)
        - ``date_announced`` or ``date_implemented`` is on or after
          ``since_year`` (when set)

    Returned dict keys::

        gta_id              int    (from "id" column; 0 if unparseable)
        title               str
        event_date          datetime  (tz-aware UTC; date_implemented preferred)
        implementing_iso2   str | None
        intervention_type   str
        risk_category       str   (mapped via GTA_INTERVENTION_CATEGORY_MAP)
        matched_hs_codes    list[str]   (only codes matching the prefix list)
        summary             str         (truncated to 500 chars)
        in_force            bool
        raw_row             dict[str, str]  (full original row for metadata_json)
    """
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        log.warning("gta.parse.empty_csv")
        return []

    col = _build_column_map(list(reader.fieldnames))
    log.info(
        "gta.parse.start",
        rows_estimate="streaming",
        hs_prefixes=hs_prefixes,
        since_year=since_year,
    )

    results: list[dict] = []
    seen = kept = 0

    for row in reader:
        seen += 1

        # 1. Red interventions only.
        evaluation = (row.get(col["gta_evaluation"]) or "").strip().lower()
        if evaluation not in _RED_VALUES:
            continue

        # 2. HS code filter — skipped for pre-filtered curated exports.
        all_hs = _split_hs_codes(row.get(col["affected_hs_codes"]) or "")
        if skip_hs_filter:
            # Trust GTA's curation; record whatever codes are present.
            matched = all_hs
        else:
            matched = [c for c in all_hs if _matches_prefix(c, hs_prefixes)]
            if not matched:
                continue

        # 3. Date filter (announced OR implemented within the year window).
        date_implemented_str = (
            row.get(col["date_implemented"]) if "date_implemented" in col else ""
        ) or ""
        date_announced_str = (
            row.get(col["date_announced"]) if "date_announced" in col else ""
        ) or ""

        event_date = _parse_date(date_implemented_str) or _parse_date(date_announced_str)
        if event_date is None:
            log.warning(
                "gta.parse.bad_date",
                gta_id=row.get(col["id"]),
                date_implemented=date_implemented_str,
                date_announced=date_announced_str,
            )
            continue

        if since_year is not None and event_date.year < since_year:
            continue

        # 4. Country & category resolution.
        implementing_iso2 = _resolve_country(
            row.get(col["implementing_jurisdiction"]) or "",
            country_name_map=country_name_map,
        )

        intervention_type = (row.get(col["intervention_type"]) or "").strip()
        risk_category = GTA_INTERVENTION_CATEGORY_MAP.get(
            intervention_type, DEFAULT_GTA_CATEGORY
        )

        # 5. id, title, summary, in_force.
        gta_id_raw = (row.get(col["id"]) or "").strip()
        try:
            gta_id = int(gta_id_raw)
        except ValueError:
            gta_id = 0
            log.warning("gta.parse.bad_id", value=gta_id_raw)

        title = (row.get(col["title"]) or "").strip()
        if not title:
            continue
        description = (
            row.get(col["description"], "") if "description" in col else ""
        ) or ""
        summary = description.strip()[:_SUMMARY_MAX]

        in_force = _to_bool(
            row.get(col["in_force"]) if "in_force" in col else False
        )

        # ── Scope 2 fields (added 2026-05-06) ──────────────────────────────
        # Affected jurisdictions — countries whose imports/exports are
        # constrained.  Resolved here in the parser (rather than at ingest)
        # so the country_name_map only has to be built once per run and the
        # parsing/resolution failure modes are visible in parse stats.
        affected_iso2_list: list[str] = []
        if "affected_jurisdictions" in col:
            affected_raw = row.get(col["affected_jurisdictions"]) or ""
            affected_iso2_list = _resolve_country_list(
                affected_raw, country_name_map=country_name_map
            )

        # Implementation level — defaults to "National" when the column is
        # absent (older GTA exports) since the bulk of GTA data is national.
        implementation_level: Optional[str] = None
        if "implementation_level" in col:
            implementation_level = (
                row.get(col["implementation_level"]) or ""
            ).strip() or None

        # Is horizontal — boolean flag.  False when absent / unparseable.
        is_horizontal = False
        if "is_horizontal" in col:
            is_horizontal = _to_bool(row.get(col["is_horizontal"]))

        # ── Structural fields for the 2026-05-12 attribution classifier ──
        mast_chapter: Optional[str] = None
        if "mast_chapter" in col:
            mast_chapter = (row.get(col["mast_chapter"]) or "").strip() or None
        eligible_firm: Optional[str] = None
        if "eligible_firm" in col:
            eligible_firm = (row.get(col["eligible_firm"]) or "").strip() or None
        affected_sectors_raw: Optional[str] = None
        if "affected_sectors" in col:
            affected_sectors_raw = (row.get(col["affected_sectors"]) or "").strip() or None

        # Date announced — already parsed above for the event_date fallback.
        # Re-parse here so the policy_proximity_adjustment can use it as a
        # forward-looking effective date when distinct from date_implemented.
        date_announced = _parse_date(date_announced_str)

        results.append(
            {
                "gta_id": gta_id,
                "title": title,
                "event_date": event_date,
                "implementing_iso2": implementing_iso2,
                "intervention_type": intervention_type,
                "risk_category": risk_category,
                "matched_hs_codes": matched,
                "summary": summary,
                "in_force": in_force,
                # Scope 2 additions:
                "affected_iso2_list":    affected_iso2_list,
                "implementation_level":  implementation_level,
                "is_horizontal":         is_horizontal,
                "date_announced":        date_announced,
                # 2026-05-12 attribution-classifier additions:
                "mast_chapter":          mast_chapter,
                "eligible_firm":         eligible_firm,
                "affected_sectors_raw":  affected_sectors_raw,
                "raw_row": dict(row),
            }
        )
        kept += 1

    log.info("gta.parse.done", rows_seen=seen, rows_kept=kept)
    return results


# ---------------------------------------------------------------------------
# Source / SourceDocument helpers
# ---------------------------------------------------------------------------

def _get_or_create_gta_source(session: Session) -> int:
    """Get or create the ``Source`` row for Global Trade Alert. Returns ``source.id``."""
    existing = session.scalar(select(Source).where(Source.name == _SOURCE_NAME))
    if existing is not None:
        return existing.id

    source = Source(
        name=_SOURCE_NAME,
        source_type=_SOURCE_TYPE,
        phase=_SOURCE_PHASE,
        is_active=True,
        config_json={"base_url": _SOURCE_BASE_URL, "method": "bulk_csv"},
    )
    session.add(source)
    session.flush()
    log.info("gta.source_created", source_id=source.id)
    return source.id


def _create_gta_source_document(
    session: Session,
    source_id: int,
    row_count: int,
    run_date: date,
) -> int:
    """Idempotent ``SourceDocument`` upsert keyed on ``(source_id, external_id)``.

    One document per ingestion run-date. Re-running on the same date reuses
    the existing document; row-level deduplication is handled by
    ``RiskEvent.content_hash``.
    """
    external_id = f"gta_bulk_csv_{run_date.isoformat()}"
    existing = session.scalar(
        select(SourceDocument).where(
            SourceDocument.source_id == source_id,
            SourceDocument.external_id == external_id,
        )
    )
    if existing is not None:
        # Refresh row_count metadata so it reflects the latest run.
        if existing.metadata_json is not None:
            existing.metadata_json = {
                **(existing.metadata_json or {}),
                "row_count": row_count,
            }
        return existing.id

    doc = SourceDocument(
        source_id=source_id,
        external_id=external_id,
        title=f"Global Trade Alert — bulk CSV download {run_date.isoformat()}",
        document_type="trade_policy_database",
        metadata_json={"run_date": run_date.isoformat(), "row_count": row_count},
    )
    session.add(doc)
    session.flush()
    return doc.id


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def _content_hash(title: str, summary: str, event_date: datetime) -> str:
    """SHA-256 of ``f'{title}{summary}{event_date.isoformat()}'``.

    Per the prompt spec — no separator characters. The hash is the primary
    deduplication key for ``risk_events``; GTA occasionally re-publishes the
    same intervention with a new ``id`` so the row id alone is not enough.
    """
    payload = f"{title}{summary}{event_date.isoformat()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _existing_event_id(
    session: Session,
    *,
    content_hash: str,
    gta_id: int,
) -> Optional[int]:
    """Return the id of an existing ``RiskEvent`` for this intervention, if any.

    Primary check: ``content_hash`` equality.
    Secondary check: ``metadata_json->>'gta_id' = :gta_id`` — falls back to a
    Python-side check on SQLite (no JSONB ``->>`` operator there).
    """
    by_hash = session.scalar(
        select(RiskEvent.id).where(RiskEvent.content_hash == content_hash)
    )
    if by_hash is not None:
        return by_hash

    if gta_id:
        # Postgres path — JSONB key access. Cheap and correct.
        try:
            by_id = session.scalar(
                select(RiskEvent.id).where(
                    RiskEvent.metadata_json["gta_id"].as_string() == str(gta_id)
                )
            )
            if by_id is not None:
                return by_id
        except Exception:
            # SQLite (and any backend without JSONB ``->>``) — fall back to a
            # Python-side scan. This is O(N) but only invoked when the primary
            # hash check missed, which is rare in practice.
            for ev in session.scalars(
                select(RiskEvent).where(RiskEvent.metadata_json.is_not(None))
            ):
                meta = ev.metadata_json or {}
                if meta.get("gta_id") == gta_id:
                    return ev.id
    return None


# ---------------------------------------------------------------------------
# Severity calibration
# ---------------------------------------------------------------------------

def _severity_for(intervention_type: str, in_force: bool) -> float:
    """Conservative initial severity calibration.

    - ``0.9`` for explicit export bans (supply is blocked outright).
    - ``0.7`` for any other active Red intervention.
    - ``0.3`` for inactive / removed Red interventions (historical signal).

    The scoring engine applies recency decay on top of this base value, so
    recent interventions still dominate even when their severity score is
    moderate.

    See ``_apply_severity_modifiers`` for the implementation-level + horizontal
    multipliers added 2026-05-06 (Scope 2 audit refinements).
    """
    if "ban" in intervention_type.lower() and "export" in intervention_type.lower():
        return 0.9
    return 0.7 if in_force else 0.3


# ---------------------------------------------------------------------------
# Implementation-level severity multipliers — PARTNER-TUNABLE
# ---------------------------------------------------------------------------
# Source: GTA's "Implementation Level" column.  Distribution observed in the
# real interventions_batteries.csv (n=1728):
#   National 1292, Subnational 146, Supranational 69, NFI 258, IFI 48
#
# National      — legislative/regulatory action by a sovereign government.  Baseline 1.0×.
# Subnational   — US state, Indian state, etc.  Less binding; 0.5×.
# Supranational — EU directives, RCEP, ASEAN-level commitments.  Harder to reverse; 1.1×.
#
# NFI / IFI — financial-support instruments (trade finance, state loans, loan
# guarantees, financial grants, state aid, equity stakes) issued by:
#   * NFI = National Financial Institution (EXIM banks, state development banks,
#           sovereign wealth funds)
#   * IFI = International Financial Institution (European Investment Bank,
#           World Bank, EBRD, ADB, IADB)
#
# Once the EXPORT_SUBSIDY event_subtype routing was wired (G-Cov-3, 2026-05-09),
# these started feeding the Geopolitical pillar's production_subsidy_distortion
# sub-input.  Both currently at 1.0× — a placeholder pending partner
# calibration on the relative bite of IFI grants (typically smaller and more
# conditional than national programs) versus national NFI programs.
#
# RECOMMENDED PARTNER-TUNED VALUES (pending sign-off):
#   NFI 1.0× (no change — national-financial-institution programs typically
#             carry national-level political backing)
#   IFI 0.6× (international grants are smaller, more conditional, more
#             carefully-targeted than national programs)
#
# To apply the recommended IFI discount, partner edits the `ifi` entry below.
# Tests in tests/test_gta_severity_modifiers.py should be updated to match.
_IMPLEMENTATION_LEVEL_MULTIPLIER: dict[str, float] = {
    "national":      1.00,
    "subnational":   0.50,   # GTA real-data spelling (no hyphen)
    "sub-national":  0.50,   # alternate spelling tolerated
    "supranational": 1.10,   # GTA's actual category for multilateral commitments
    "multilateral":  1.10,   # legacy alias
    "nfi":           1.00,   # National Financial Institution — PARTNER-TUNABLE
    "ifi":           1.00,   # International Financial Institution — PARTNER-TUNABLE
                             # (recommended 0.6 once partner confirms)
}

# Horizontal interventions apply broadly across many products.  The
# per-HS-code signal is diluted because the policy isn't materially
# targeted at any one of them.
_IS_HORIZONTAL_MULTIPLIER: float = 0.65


def _apply_severity_modifiers(
    base_severity: float,
    *,
    implementation_level: Optional[str],
    is_horizontal: bool,
) -> float:
    """Apply Scope-2 severity refinements on top of the base severity.

    Multiplicative.  Result clamped to [0.0, 1.0] since the downstream
    scorer expects a normalised severity.
    """
    sev = base_severity
    if implementation_level:
        mult = _IMPLEMENTATION_LEVEL_MULTIPLIER.get(implementation_level.strip().lower())
        if mult is not None:
            sev *= mult
    if is_horizontal:
        sev *= _IS_HORIZONTAL_MULTIPLIER
    return max(0.0, min(1.0, sev))


# ---------------------------------------------------------------------------
# Main ingest function
# ---------------------------------------------------------------------------

def ingest_gta(
    session: Session,
    url: str = GTA_DOWNLOAD_URL,
    since_year: Optional[int] = 2018,
    hs_prefixes: Optional[list[str]] = None,
    local_file: Optional[str] = None,
    skip_hs_filter: bool = False,
) -> dict[str, int]:
    """Download (or read locally), parse, and ingest GTA harmful interventions.

    Args:
        session: SQLAlchemy session. Committed once at the end of a successful run.
        url: Override download URL — used only when ``local_file`` is not set.
        since_year: Only ingest interventions on or after this calendar year.
            Default ``2018`` covers China graphite (2023), Indonesia nickel
            (2019–2023), and DRC cobalt restrictions.
        hs_prefixes: Override HS prefix filter. Defaults to
            :data:`BATTERY_HS_PREFIXES`.
        local_file: Path to a locally-downloaded GTA CSV file. When set, skips
            the HTTP download entirely. Use this when the GTA bulk download
            requires authentication — download manually from
            https://globaltradealert.org/data-center and pass the path here.
        skip_hs_filter: When ``True``, bypass the HS prefix filter and ingest
            all Red interventions regardless of product codes. Use this for
            pre-filtered curated exports from the GTA data center (e.g.
            "Harmful Trade Policy Interventions: Batteries") where GTA has
            already applied product-level filtering. The full bulk State Acts
            export should always use the default ``False``.

    Returns:
        dict with these keys::

            downloaded_rows         rows kept after Red + HS filters
            inserted                new RiskEvent rows created
            skipped_existing        rows skipped due to content_hash / gta_id match
            material_links          RiskEventMaterial rows created
            hs_mapping_links        RiskEventHsMapping rows created (stage attribution)
            geography_links         RiskEventGeography rows created
            skipped_unknown_country events inserted but with no resolvable country
    """
    # Derive HS prefix filter from the DB (or fall back to hardcoded list).
    effective_prefixes = list(hs_prefixes) if hs_prefixes else _derive_hs_prefixes_from_db(session)
    log.info("gta.ingest.hs_prefixes", count=len(effective_prefixes), source="db" if not hs_prefixes else "override")

    # Build country name → ISO2 map from the DB (or empty dict if not seeded).
    country_name_map = _build_country_name_map(session)
    log.info("gta.ingest.country_name_map", entries=len(country_name_map))

    if local_file is not None:
        import pathlib
        path = pathlib.Path(local_file)
        if not path.exists():
            raise FileNotFoundError(f"GTA local file not found: {local_file}")
        log.info("gta.ingest.local_file", path=str(path), size_bytes=path.stat().st_size)
        raw_bytes = path.read_bytes()
    else:
        raw_bytes = download_gta_csv(url)
    interventions = parse_gta_csv(
        raw_bytes,
        hs_prefixes=effective_prefixes,
        since_year=since_year,
        skip_hs_filter=skip_hs_filter,
        country_name_map=country_name_map,
    )

    source_id = _get_or_create_gta_source(session)
    today = date.today()
    source_document_id = _create_gta_source_document(
        session, source_id, len(interventions), today
    )

    hs_material_map = _build_hs_material_map(session)

    inserted = 0
    skipped_existing = 0
    skipped_unknown_country = 0
    skipped_no_resolved_material = 0   # added 2026-05-06 (Scope 2)
    skipped_too_broad = 0              # added 2026-05-06 (Scope 2)
    material_links_created = 0
    hs_mapping_links_created = 0
    geography_links_created = 0
    pending_in_batch = 0

    # Cap on the number of "affected" countries written per event.  Above
    # this threshold the intervention is effectively non-targeted (e.g.
    # generic WTO commitments listing 160+ members), so we treat it as
    # ambient and skip writing affected rows — its tariff_exposure
    # contribution would be unrealistically high otherwise.
    #
    # Calibration (2026-05-06): set to 75 after surveying real GTA data.
    # The interventions_batteries.csv shows median=26, max=50 affected
    # countries per event.  An earlier cap of 25 would have skipped affected
    # rows for ~50% of real interventions, defeating the G11 fix.  75 is
    # comfortably above the observed max while still pruning truly generic
    # multilateral commitments (which list 100+ jurisdictions when present).
    # Revisit if a future GTA bulk export shifts the distribution.
    _AFFECTED_COUNTRY_CAP = 75

    for intervention in interventions:
        title = intervention["title"]
        summary = intervention["summary"]
        event_date: datetime = intervention["event_date"]
        gta_id = intervention["gta_id"]
        intervention_type = intervention["intervention_type"][:_EVENT_TYPE_MAX]
        implementing_iso2 = intervention["implementing_iso2"]
        matched_hs_codes = intervention["matched_hs_codes"]
        risk_category = intervention["risk_category"]
        in_force = intervention["in_force"]
        affected_iso2_list = intervention.get("affected_iso2_list") or []
        implementation_level = intervention.get("implementation_level")
        is_horizontal = bool(intervention.get("is_horizontal"))
        date_announced = intervention.get("date_announced")
        # 2026-05-12 attribution-classifier inputs (None when the curated
        # CSV doesn't carry the column — keeps the classifier permissive
        # for older exports rather than breaking ingest).
        mast_chapter = intervention.get("mast_chapter")
        eligible_firm = intervention.get("eligible_firm")
        affected_sectors_raw = intervention.get("affected_sectors_raw")

        ch = _content_hash(title, summary, event_date)
        if _existing_event_id(session, content_hash=ch, gta_id=gta_id) is not None:
            skipped_existing += 1
            continue

        # ── Strict attribution gates (Scope 2 audit, 2026-05-06) ───────────
        # Per partner direction (G11), an event with no resolvable producer
        # country or no resolvable tracked material contributes nothing
        # actionable to scoring — skip it entirely rather than insert a dead
        # row.

        # Gate 1: implementing country must resolve to ISO2.
        if implementing_iso2 is None:
            skipped_unknown_country += 1
            log.debug(
                "gta.unresolved_country",
                gta_id=gta_id,
                raw=intervention["raw_row"].get("implementing_jurisdiction"),
            )
            continue

        # Gate 2: at least one matched HS code must resolve to a seeded
        # hs_code_material_mapping.  Pre-resolve the codes here so the loop
        # below doesn't re-walk them and so the gate has a definitive answer
        # before any DB writes.
        #
        # 2026-05-09 (Tier 1.4 scope extension): also carry the mapping's
        # confidence forward so RiskEventMaterial / RiskEventHsMapping
        # relevance_score can be downscaled for low-confidence prefixes
        # (e.g. 4-digit fallbacks). Without this every GTA event was
        # written at hardcoded 0.9 regardless of how shaky the HS→material
        # link was, which made the per-material event feed noisy for
        # broadly-stamped tariffs.
        event_subtype = _INTERVENTION_SUBTYPE_MAP.get(intervention_type)
        resolved_codes: list[tuple[int, Optional[int], Optional[float]]] = []
        for hs_code in matched_hs_codes:
            mat_id, hs_map_id, hs_conf = _resolve_material_id(
                hs_code, hs_material_map
            )
            if mat_id is not None:
                resolved_codes.append((mat_id, hs_map_id, hs_conf))
        if not resolved_codes:
            skipped_no_resolved_material += 1
            log.debug(
                "gta.no_resolved_material",
                gta_id=gta_id,
                matched_hs_codes=matched_hs_codes,
            )
            continue

        # ── Severity calibration with Scope-2 modifiers ────────────────────
        base_severity = _severity_for(intervention_type, in_force)
        severity = _apply_severity_modifiers(
            base_severity,
            implementation_level=implementation_level,
            is_horizontal=is_horizontal,
        )

        # ── Build metadata_json (Scope-2 fields included) ──────────────────
        metadata: dict[str, Any] = {
            "gta_id": gta_id,
            "gta_hs_codes": matched_hs_codes,
            "raw_intervention_type": intervention["intervention_type"],
            "in_force": in_force,
            "source_url": url,
            "implementation_level": implementation_level,
            "is_horizontal": is_horizontal,
            # 2026-05-12 structural fields — captured even when None so
            # admin queries can inspect what the CSV exposed.  An earlier
            # draft of this code also used these to gate HS-based material
            # attribution; that gating was reverted after the distribution
            # audit (see the Step-3 ticket).  Capturing them is still
            # useful for future analysis even though we don't act on them
            # at ingest time.
            "mast_chapter": mast_chapter,
            "eligible_firm": eligible_firm,
            "affected_sectors_raw": affected_sectors_raw,
        }
        # event_subtype is on the typed column ``RiskEvent.event_subtype``
        # (migration 040).  Backwards-compat duplicate in metadata_json was
        # dropped 2026-06-06 after the one-release-cycle deprecation window
        # — all readers now use the typed column.
        if date_announced is not None:
            # Used by market_aggregator's policy_proximity_adjustment when
            # date_announced is within 90 days of as_of_date.
            metadata["effective_date"] = date_announced.date().isoformat()
        if affected_iso2_list:
            metadata["affected_iso2_list"] = affected_iso2_list

        # ── Decide whether to write "affected" geography rows ──────────────
        # Only IMPORT_DISRUPTION events carry meaningful "affected =
        # producer being tariffed" semantics.  For EXPORT_RESTRICTION events
        # the implementing country IS the producer; the GTA "Affected
        # Jurisdictions" list there enumerates *destinations* whose imports
        # are constrained, which is a different semantic and would mislead
        # the scorer if tagged "affected".  Skip writing affected rows for
        # non-import-disruption events.
        #
        # Additionally, if the affected list is too broad (e.g. all WTO
        # members), treat the intervention as non-targeted ambient signal
        # and skip the affected rows.  The event itself still inserts
        # because it has value as evidence; it just won't feed
        # tariff_exposure for any specific country.
        is_import_disruption = (event_subtype == "IMPORT_DISRUPTION")
        write_affected_rows = (
            is_import_disruption
            and 0 < len(affected_iso2_list) <= _AFFECTED_COUNTRY_CAP
        )
        if (
            is_import_disruption
            and len(affected_iso2_list) > _AFFECTED_COUNTRY_CAP
        ):
            skipped_too_broad += 1
            log.debug(
                "gta.affected_list_too_broad",
                gta_id=gta_id,
                affected_count=len(affected_iso2_list),
                cap=_AFFECTED_COUNTRY_CAP,
            )

        # ── Build geography_json for admin views ───────────────────────────
        geography_json: dict[str, Any] = {"primary": implementing_iso2}
        if write_affected_rows:
            geography_json["affected"] = affected_iso2_list

        event = RiskEvent(
            source_document_id=source_document_id,
            event_type=intervention_type or "trade_intervention",
            event_subtype=event_subtype,  # typed col (migration 040); None when no subtype maps
            event_date=event_date,
            title=title,
            summary=summary,
            severity_score=severity,
            confidence_score=0.9,
            risk_categories_json=[risk_category],
            geography_json=geography_json,
            content_hash=ch,
            metadata_json=metadata,
            verified=False,
        )
        session.add(event)
        session.flush()

        # ── Geography rows ─────────────────────────────────────────────────
        # Primary = implementing country.  Used by ``export_restriction``
        # sub-score in hs_node_scorer (the implementing country IS the
        # producer for export-side interventions).
        session.add(
            RiskEventGeography(
                risk_event_id=event.id,
                country_code=implementing_iso2,
                geography_context="primary",
                relevance_score=1.0,
            )
        )
        geography_links_created += 1

        # Affected = countries whose imports/exports are constrained.  Used
        # by ``tariff_exposure`` sub-score in hs_node_scorer (the affected
        # country IS the producer for import-side interventions — the one
        # whose exports just got tariffed).
        # Drop the implementing country if it appears in its own affected
        # list (a known GTA data quirk on multilateral/regional acts).
        if write_affected_rows:
            for affected_iso2 in affected_iso2_list:
                if affected_iso2 == implementing_iso2:
                    continue
                session.add(
                    RiskEventGeography(
                        risk_event_id=event.id,
                        country_code=affected_iso2,
                        geography_context="affected",
                        relevance_score=1.0,
                    )
                )
                geography_links_created += 1

        # ── Material + HS junction rows ────────────────────────────────────
        # ``resolved_codes`` was pre-built above as part of Gate 2.
        #
        # Relevance is the GTA event's intrinsic confidence (0.9, the same
        # value stamped on the RiskEvent itself) scaled by the HS→material
        # mapping confidence.  This means a precise 6-digit lithium tariff
        # gets relevance ≈ 0.9 × 1.0 = 0.9, while a 4-digit "ores and
        # concentrates" tariff that only weakly maps to a material gets
        # something like 0.9 × 0.4 = 0.36.  When a material has multiple
        # matching HS codes in the same event, we keep the highest
        # resulting relevance rather than blindly first-wins.
        _BASE_REL = 0.9

        material_best_rel: dict[int, float] = {}
        hs_mapping_best_rel: dict[int, float] = {}
        for material_id, hs_mapping_id, hs_conf in resolved_codes:
            scaled = _BASE_REL * (hs_conf if hs_conf is not None else 1.0)
            # Clamp to [0, 1] in case a future mapping gets confidence > 1.
            if scaled > 1.0:
                scaled = 1.0
            elif scaled < 0.0:
                scaled = 0.0
            prev = material_best_rel.get(material_id)
            if prev is None or scaled > prev:
                material_best_rel[material_id] = scaled
            if hs_mapping_id is not None:
                prev_hs = hs_mapping_best_rel.get(hs_mapping_id)
                if prev_hs is None or scaled > prev_hs:
                    hs_mapping_best_rel[hs_mapping_id] = scaled

        for material_id, relevance in material_best_rel.items():
            session.add(
                RiskEventMaterial(
                    risk_event_id=event.id,
                    material_id=material_id,
                    relevance_score=relevance,
                    match_reason="hs_code",
                )
            )
            material_links_created += 1
        for hs_mapping_id, relevance in hs_mapping_best_rel.items():
            session.add(
                RiskEventHsMapping(
                    risk_event_id=event.id,
                    hs_mapping_id=hs_mapping_id,
                    relevance_score=relevance,
                    match_reason="hs_code",
                )
            )
            hs_mapping_links_created += 1

        inserted += 1
        pending_in_batch += 1

        if pending_in_batch >= _FLUSH_EVERY:
            session.flush()
            log.info(
                "gta.ingest.batch_progress",
                inserted_so_far=inserted,
                skipped_so_far=skipped_existing,
            )
            pending_in_batch = 0

    session.commit()

    result = {
        "downloaded_rows": len(interventions),
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "material_links": material_links_created,
        "hs_mapping_links": hs_mapping_links_created,
        "geography_links": geography_links_created,
        "skipped_unknown_country": skipped_unknown_country,
        # Added 2026-05-06 (Scope 2 audit refinements):
        "skipped_no_resolved_material": skipped_no_resolved_material,
        "skipped_too_broad_affected":   skipped_too_broad,
    }
    log.info("gta.ingest.done", **result)
    return result
