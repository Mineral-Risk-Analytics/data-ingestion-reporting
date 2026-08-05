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
# 2026-06-11 — expanded to cover all 77 official intervention types using
# canonical names from /api/v1/gta/mappings/.  All trade-flow restrictions
# (export/import side) route to geopolitical_trade; all
# localisation/procurement family routes to regulatory_compliance;
# production-subsidy family routes to financial_pressure (captures the
# fiscal-distortion character better than geopolitical).
GTA_INTERVENTION_CATEGORY_MAP: dict[str, str] = {
    # ── geopolitical_trade ────────────────────────────────────────────────
    # Export-side restrictions
    "Export tax":                              "geopolitical_trade",
    "Export ban":                              "geopolitical_trade",
    "Export tariff quota":                     "geopolitical_trade",
    "Export quota":                            "geopolitical_trade",
    "Export licensing requirement":            "geopolitical_trade",
    "Export-related non-tariff measure, nes":  "geopolitical_trade",
    "Foreign customer limit":                  "geopolitical_trade",
    "Voluntary export-restraint arrangements": "geopolitical_trade",
    "Voluntary export-price restraints":       "geopolitical_trade",
    "Export price benchmark":                  "geopolitical_trade",
    # Import-side restrictions
    "Import ban":                              "geopolitical_trade",
    "Import licensing requirement":            "geopolitical_trade",
    "Import-related non-tariff measure, nes":  "geopolitical_trade",
    "Import tariff quota":                     "geopolitical_trade",
    "Import quota":                            "geopolitical_trade",
    "Import tariff":                           "geopolitical_trade",
    "Internal taxation of imports":            "geopolitical_trade",
    "Import monitoring":                       "geopolitical_trade",
    "Minimum import price":                    "geopolitical_trade",
    "Import price benchmark":                  "geopolitical_trade",
    "Other import charges":                    "geopolitical_trade",
    "Port restriction":                        "geopolitical_trade",
    "Selective import channel restriction":    "geopolitical_trade",
    "Post-sales service restriction (MAST Chapter K1)": "geopolitical_trade",
    # Trade defense (anti-dumping family)
    "Anti-dumping":                            "geopolitical_trade",
    "Safeguard":                               "geopolitical_trade",
    "Anti-subsidy":                            "geopolitical_trade",
    "Anti-circumvention":                      "geopolitical_trade",
    "Special safeguard":                       "geopolitical_trade",
    # FDI / investment-side
    "FDI: Entry and ownership rule":           "geopolitical_trade",
    "FDI: Treatment and operations, nes":      "geopolitical_trade",
    "FDI: Financial incentive":                "geopolitical_trade",
    "Controls on commercial transactions and investment instruments": "geopolitical_trade",
    "Controls on credit operations":           "geopolitical_trade",
    "Corporate control order":                 "geopolitical_trade",
    "Trade payment measure":                   "geopolitical_trade",
    "Trade balancing measure":                 "geopolitical_trade",
    "Competitive devaluation":                 "geopolitical_trade",
    "Repatriation & surrender requirements":   "geopolitical_trade",
    "Control on personal transactions":        "geopolitical_trade",
    "Distribution restriction":                "geopolitical_trade",
    "Price stabilisation":                     "geopolitical_trade",
    "Instrument unclear":                      "geopolitical_trade",
    # Legacy / plural fallbacks
    "Export taxes":                            "geopolitical_trade",
    "Export quotas":                           "geopolitical_trade",
    "Export licensing requirements":           "geopolitical_trade",
    "Export bans":                             "geopolitical_trade",
    "Export subsidies":                        "geopolitical_trade",
    "Investment measure":                      "geopolitical_trade",
    "Sanitary and phytosanitary measure":      "regulatory_compliance",
    "Technical barrier to trade":              "regulatory_compliance",

    # ── regulatory_compliance (localisation / procurement family) ─────────
    # See PROCUREMENT_POLICY subtype routing above for the rationale.
    "Local content requirement":               "regulatory_compliance",
    "Local operations requirement":            "regulatory_compliance",
    "Local labour requirement":                "regulatory_compliance",
    "Public procurement preference margin":    "regulatory_compliance",
    "Public procurement localisation":         "regulatory_compliance",
    "Public procurement access":               "regulatory_compliance",
    "Public procurement, nes":                 "regulatory_compliance",
    "Local content incentive":                 "regulatory_compliance",
    "Local supply requirement for exports":    "regulatory_compliance",
    "Local value added requirement":           "regulatory_compliance",
    "Local value added incentive":             "regulatory_compliance",
    "Local operations incentive":              "regulatory_compliance",
    "Local labour incentive":                  "regulatory_compliance",
    "Localisation, nes":                       "regulatory_compliance",
    "Intellectual property protection":        "regulatory_compliance",
    "Labour market access":                    "regulatory_compliance",
    "Post-migration treatment":                "regulatory_compliance",
    # Legacy
    "Local content requirements":              "regulatory_compliance",
    "Procurement":                             "regulatory_compliance",
    "Procurement policy":                      "regulatory_compliance",
    "Public-private partnership":              "regulatory_compliance",

    # ── financial_pressure (production-subsidy + state-aid family) ───────
    # Fiscal-distortion character — fits financial-pressure pillar better
    # than geopolitical when the implementing country IS subsidising
    # domestic output rather than restricting trade.
    "State loan":                              "financial_pressure",
    "Financial grant":                         "financial_pressure",
    "In-kind grant":                           "financial_pressure",
    "Production subsidy":                      "financial_pressure",
    "Interest payment subsidy":                "financial_pressure",
    "Loan guarantee":                          "financial_pressure",
    "Tax or social insurance relief":          "financial_pressure",
    "Consumption subsidy":                     "financial_pressure",
    "Tax-based export incentive":              "financial_pressure",
    "Export subsidy":                          "financial_pressure",
    "Other export incentive":                  "financial_pressure",
    "Import incentive":                        "financial_pressure",
    "Trade finance":                           "financial_pressure",
    "Financial assistance in foreign market":  "financial_pressure",
    "State aid, nes":                          "financial_pressure",
    "State aid, unspecified":                  "financial_pressure",
    "Debt purchase":                           "financial_pressure",
    "Equity stake":                            "financial_pressure",
    "Financial investment support":            "financial_pressure",
    "Lending support":                         "financial_pressure",
    # Legacy
    "Production subsidies":                    "financial_pressure",
    "Producer support":                        "financial_pressure",
    "State aid":                               "financial_pressure",
}
DEFAULT_GTA_CATEGORY = "geopolitical_trade"

# ── Supply-risk direction (2026-07-31, GTA audit F1) ──────────────────────
# GTA's "Red" evaluation means "harmful to foreign commercial interests" —
# which is not the same thing as "supply risk for a battery-materials
# buyer".  55% of the stored corpus is subsidies: state aid that mostly
# ADDS or diversifies supply (802 plain financial grants; a third of the
# subsidy events come from the US, Japan, Australia, Canada and Germany —
# IRA-era industrial policy).  Deriving a direction from the subtype family
# lets the triage layer default supportive events to display_only instead
# of narrating them as financial-pressure risk.
#
# Whether supportive events should eventually LOWER risk is a scoring
# design question, deliberately deferred (see the triage plan).
_SUBTYPE_DIRECTION: dict[str, str] = {
    "EXPORT_RESTRICTION": "restrictive",
    "IMPORT_DISRUPTION":  "restrictive",
    "TRADE_DEFENSE":      "restrictive",
    "FDI_RESTRICTION":    "restrictive",
    "PROCUREMENT_POLICY": "restrictive",   # localisation constrains sourcing
    "EXPORT_SUBSIDY":     "supportive",    # production-subsidy family
}
_DEFAULT_DIRECTION = "neutral"             # subtype NULL (rare: 4 of 2,629)


def _direction_for(event_subtype: Optional[str]) -> str:
    """Supply-risk direction for a subtype family — see _SUBTYPE_DIRECTION."""
    if event_subtype is None:
        return _DEFAULT_DIRECTION
    return _SUBTYPE_DIRECTION.get(event_subtype, _DEFAULT_DIRECTION)

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
#
# 2026-06-11 — API migration: this map now uses the canonical intervention_type
# strings from the official GTA taxonomy (77 types, pulled via
# /api/v1/gta/mappings/).  Singular/plural drift from old CSV exports is no
# longer a concern because the API returns canonical names directly.  Old
# string variants (plural forms, abbreviated names) are kept for backwards
# compatibility with manually-exported CSVs but should not need to be
# extended further.
_INTERVENTION_SUBTYPE_MAP: dict[str, str] = {
    # ── EXPORT_RESTRICTION (supply-side restrictions) ─────────────────────
    "Export tax":                              "EXPORT_RESTRICTION",
    "Export ban":                              "EXPORT_RESTRICTION",
    "Export tariff quota":                     "EXPORT_RESTRICTION",
    "Export quota":                            "EXPORT_RESTRICTION",
    "Export licensing requirement":            "EXPORT_RESTRICTION",
    "Export-related non-tariff measure, nes":  "EXPORT_RESTRICTION",
    "Foreign customer limit":                  "EXPORT_RESTRICTION",
    "Voluntary export-restraint arrangements": "EXPORT_RESTRICTION",
    "Voluntary export-price restraints":       "EXPORT_RESTRICTION",
    "Export price benchmark":                  "EXPORT_RESTRICTION",
    # Legacy plural forms (old CSV exports — API uses singulars)
    "Export taxes":                            "EXPORT_RESTRICTION",
    "Export quotas":                           "EXPORT_RESTRICTION",
    "Export licensing requirements":           "EXPORT_RESTRICTION",
    "Export bans":                             "EXPORT_RESTRICTION",

    # ── IMPORT_DISRUPTION (import-side restrictions) ──────────────────────
    "Import ban":                              "IMPORT_DISRUPTION",
    "Import licensing requirement":            "IMPORT_DISRUPTION",
    "Import-related non-tariff measure, nes":  "IMPORT_DISRUPTION",
    "Import tariff quota":                     "IMPORT_DISRUPTION",
    "Import quota":                            "IMPORT_DISRUPTION",
    "Import tariff":                           "IMPORT_DISRUPTION",
    "Internal taxation of imports":            "IMPORT_DISRUPTION",
    "Import monitoring":                       "IMPORT_DISRUPTION",
    "Minimum import price":                    "IMPORT_DISRUPTION",
    "Import price benchmark":                  "IMPORT_DISRUPTION",
    "Other import charges":                    "IMPORT_DISRUPTION",
    "Port restriction":                        "IMPORT_DISRUPTION",
    "Selective import channel restriction":    "IMPORT_DISRUPTION",
    "Post-sales service restriction (MAST Chapter K1)": "IMPORT_DISRUPTION",
    # Legacy plural forms
    "Import tariffs":                          "IMPORT_DISRUPTION",
    "Import quotas":                           "IMPORT_DISRUPTION",
    "Import bans":                             "IMPORT_DISRUPTION",

    # ── PROCUREMENT_POLICY (localisation/procurement preferences) ─────────
    # Routes to regulatory_compliance pillar via GTA_INTERVENTION_CATEGORY_MAP.
    # Does NOT feed tariff_exposure / export_restriction_exposure even when
    # title-text heuristics would otherwise match (informational only).
    "Local content requirement":               "PROCUREMENT_POLICY",
    "Local operations requirement":            "PROCUREMENT_POLICY",
    "Local labour requirement":                "PROCUREMENT_POLICY",
    "Public procurement preference margin":    "PROCUREMENT_POLICY",
    "Public procurement localisation":         "PROCUREMENT_POLICY",
    "Public procurement access":               "PROCUREMENT_POLICY",
    "Public procurement, nes":                 "PROCUREMENT_POLICY",
    "Local content incentive":                 "PROCUREMENT_POLICY",
    "Local supply requirement for exports":    "PROCUREMENT_POLICY",
    "Local value added requirement":           "PROCUREMENT_POLICY",
    "Local value added incentive":             "PROCUREMENT_POLICY",
    "Local operations incentive":              "PROCUREMENT_POLICY",
    "Local labour incentive":                  "PROCUREMENT_POLICY",
    "Localisation, nes":                       "PROCUREMENT_POLICY",
    # Legacy convenience strings (manual CSV exports may carry these)
    "Local content requirements":              "PROCUREMENT_POLICY",
    "Procurement":                             "PROCUREMENT_POLICY",
    "Procurement policy":                      "PROCUREMENT_POLICY",
    "Public-private partnership":              "PROCUREMENT_POLICY",

    # ── EXPORT_SUBSIDY (production-subsidy family — distort downstream) ──
    # See G-Cov-3 (2026-05-09) — these feed
    # production_subsidy_distortion on the Geopolitical pillar.
    "State loan":                              "EXPORT_SUBSIDY",
    "Financial grant":                         "EXPORT_SUBSIDY",
    "In-kind grant":                           "EXPORT_SUBSIDY",
    "Production subsidy":                      "EXPORT_SUBSIDY",
    "Interest payment subsidy":                "EXPORT_SUBSIDY",
    "Loan guarantee":                          "EXPORT_SUBSIDY",
    "Tax or social insurance relief":          "EXPORT_SUBSIDY",
    "Consumption subsidy":                     "EXPORT_SUBSIDY",
    "Tax-based export incentive":              "EXPORT_SUBSIDY",
    "Export subsidy":                          "EXPORT_SUBSIDY",
    "Other export incentive":                  "EXPORT_SUBSIDY",
    "Import incentive":                        "EXPORT_SUBSIDY",  # subsidising imports
    "Trade finance":                           "EXPORT_SUBSIDY",
    "Financial assistance in foreign market":  "EXPORT_SUBSIDY",
    "State aid, nes":                          "EXPORT_SUBSIDY",
    "State aid, unspecified":                  "EXPORT_SUBSIDY",
    "Debt purchase":                           "EXPORT_SUBSIDY",
    "Equity stake":                            "EXPORT_SUBSIDY",
    "Financial investment support":            "EXPORT_SUBSIDY",
    "Lending support":                         "EXPORT_SUBSIDY",
    # Legacy plural forms
    "Production subsidies":                    "EXPORT_SUBSIDY",
    "Producer support":                        "EXPORT_SUBSIDY",

    # ── TRADE_DEFENSE (anti-dumping / safeguard family) ──────────────────
    # New subtype (2026-06-11) — these are reactive investigations into
    # alleged unfair trade practices, qualitatively distinct from
    # outright restrictions.  Routes to geopolitical_trade pillar.
    "Anti-dumping":                            "TRADE_DEFENSE",
    "Safeguard":                               "TRADE_DEFENSE",
    "Anti-subsidy":                            "TRADE_DEFENSE",
    "Anti-circumvention":                      "TRADE_DEFENSE",
    "Special safeguard":                       "TRADE_DEFENSE",

    # ── FDI_RESTRICTION (capital / investment controls) ──────────────────
    # New subtype (2026-06-11) — investment-side restrictions don't fit
    # the trade-flow buckets cleanly.  Captured for partner-facing
    # diagnostic without feeding tariff/export scoring sub-inputs.
    "FDI: Entry and ownership rule":           "FDI_RESTRICTION",
    "FDI: Treatment and operations, nes":      "FDI_RESTRICTION",
    "FDI: Financial incentive":                "FDI_RESTRICTION",
    "Controls on commercial transactions and investment instruments": "FDI_RESTRICTION",
    "Controls on credit operations":           "FDI_RESTRICTION",
    "Corporate control order":                 "FDI_RESTRICTION",

    # Types intentionally NOT mapped (subtype stays NULL, informational):
    #   9 Competitive devaluation, 10 Repatriation & surrender requirements,
    #   13 Control on personal transactions, 24 Intellectual property
    #   protection, 31 Labour market access, 32 Post-migration treatment,
    #   33 Trade payment measure, 34 Trade balancing measure,
    #   60 Instrument unclear, 61 Price stabilisation,
    #   77 Distribution restriction
}

# 2026-06-11 — Integer-ID-keyed mirror of _INTERVENTION_SUBTYPE_MAP.
# Used by the API path which receives the canonical type name in the
# response.  Built lazily from the string map + a name→ID lookup loaded
# from the /mappings/ endpoint at module import time would be ideal, but
# we keep it hardcoded here so partner edits are visible in code review.
# IDs match the official GTA taxonomy (pulled 2026-06-11).  See
# https://api.globaltradealert.org/api/v1/gta/mappings/ for the source.
_INTERVENTION_ID_TO_SUBTYPE: dict[int, str] = {
    # EXPORT_RESTRICTION
    18: "EXPORT_RESTRICTION", 19: "EXPORT_RESTRICTION", 20: "EXPORT_RESTRICTION",
    21: "EXPORT_RESTRICTION", 35: "EXPORT_RESTRICTION", 37: "EXPORT_RESTRICTION",
    39: "EXPORT_RESTRICTION", 69: "EXPORT_RESTRICTION", 70: "EXPORT_RESTRICTION",
    74: "EXPORT_RESTRICTION",
    # IMPORT_DISRUPTION
    22: "IMPORT_DISRUPTION", 36: "IMPORT_DISRUPTION", 38: "IMPORT_DISRUPTION",
    44: "IMPORT_DISRUPTION", 45: "IMPORT_DISRUPTION", 47: "IMPORT_DISRUPTION",
    48: "IMPORT_DISRUPTION", 50: "IMPORT_DISRUPTION", 71: "IMPORT_DISRUPTION",
    72: "IMPORT_DISRUPTION", 73: "IMPORT_DISRUPTION", 75: "IMPORT_DISRUPTION",
    76: "IMPORT_DISRUPTION", 78: "IMPORT_DISRUPTION",
    # EXPORT_SUBSIDY (production-subsidy family)
    2:  "EXPORT_SUBSIDY",  3:  "EXPORT_SUBSIDY",  4:  "EXPORT_SUBSIDY",
    5:  "EXPORT_SUBSIDY",  6:  "EXPORT_SUBSIDY",  7:  "EXPORT_SUBSIDY",
    8:  "EXPORT_SUBSIDY", 14:  "EXPORT_SUBSIDY", 15:  "EXPORT_SUBSIDY",
    16: "EXPORT_SUBSIDY", 17:  "EXPORT_SUBSIDY", 23:  "EXPORT_SUBSIDY",
    54: "EXPORT_SUBSIDY", 55:  "EXPORT_SUBSIDY", 59:  "EXPORT_SUBSIDY",
    62: "EXPORT_SUBSIDY", 80:  "EXPORT_SUBSIDY", 81:  "EXPORT_SUBSIDY",
    82: "EXPORT_SUBSIDY", 83:  "EXPORT_SUBSIDY",
    # PROCUREMENT_POLICY
    28: "PROCUREMENT_POLICY", 29: "PROCUREMENT_POLICY", 30: "PROCUREMENT_POLICY",
    40: "PROCUREMENT_POLICY", 41: "PROCUREMENT_POLICY", 42: "PROCUREMENT_POLICY",
    43: "PROCUREMENT_POLICY", 57: "PROCUREMENT_POLICY", 63: "PROCUREMENT_POLICY",
    64: "PROCUREMENT_POLICY", 65: "PROCUREMENT_POLICY", 66: "PROCUREMENT_POLICY",
    67: "PROCUREMENT_POLICY", 68: "PROCUREMENT_POLICY",
    # TRADE_DEFENSE
    51: "TRADE_DEFENSE", 52: "TRADE_DEFENSE", 53: "TRADE_DEFENSE",
    56: "TRADE_DEFENSE", 58: "TRADE_DEFENSE",
    # FDI_RESTRICTION
    11: "FDI_RESTRICTION", 12: "FDI_RESTRICTION", 25: "FDI_RESTRICTION",
    26: "FDI_RESTRICTION", 27: "FDI_RESTRICTION", 79: "FDI_RESTRICTION",
    # Intentionally unmapped:
    # 9, 10, 13, 24, 31, 32, 33, 34, 60, 61, 77
}

# RiskEvent.event_type is String(128); truncate to fit.
_EVENT_TYPE_MAX = 128
# Summary bound.  Raised from 500 → 8000 on 2026-07-30.
#
# The old 500-char cap was a display convenience ("keep the row printable in
# admin tables") that silently destroyed source content: 2,497 of 2,575 stored
# GTA events were sliced mid-word at exactly 500 characters with no ellipsis,
# and the full text is not recoverable from the database because
# SourceDocument.raw_text is never populated on this path.  Truncation for
# display belongs in the presentation layer, not the ingester.
#
# 8000 is a sanity bound, not a design target — GTA intervention descriptions
# run to a few thousand characters at the high end.  ``risk_events.summary`` is
# TEXT, so there is no storage-level constraint being respected here.
_SUMMARY_MAX = 8000
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

# ---------------------------------------------------------------------------
# API client (2026-06-11) — replaces the bulk-CSV path for partners with an
# API key.  The CSV path is kept for offline testing via --local-file.
# ---------------------------------------------------------------------------
#
# Endpoint shape (verified against the OpenAPI schema at /api/schema/):
#   POST /api/v2/gta/data/
#     headers: Authorization: APIKey <key>
#              Content-Type: application/json
#     body:    {"limit": 1000, "offset": 0,
#               "request_data": {
#                 "affected_products": ["280530","290141",...],  # HS codes
#                 "gta_evaluation":    [4, 5],                    # "Red" only
#                 "announcement_period": ["YYYY-MM-DD","YYYY-MM-DD"],
#                 "intervention_types": [int, ...]                # optional
#               }}
#     response: [ { intervention_id, state_act_id, state_act_title,
#                   intervention_description (array of {status,order_nr,
#                   modified,text}), intervention_url, state_act_url,
#                   state_act_source (array), is_official_source,
#                   gta_evaluation ("Red"/"Amber"/"Green"),
#                   implementing_jurisdictions (array of {id,name,iso}),
#                   affected_jurisdictions (array of {id,name,iso}),
#                   intervention_type (string),
#                   mast_chapter, mast_subchapter,
#                   affected_sectors (array of {sector_id,name}),
#                   affected_products (array of {product_id,name,prior_level,
#                                                new_level,unit,date_implemented,
#                                                date_removed}),
#                   date_announced, date_published, date_implemented,
#                   date_removed, is_in_force, last_updated, last_edited,
#                   latest_action_date, latest_action_name }, ... ]
#
# Pagination is by `limit` (max 1000) and `offset`.  We loop until a page
# returns fewer than the requested limit (or 0 rows).

_GTA_API_PATH_V2_DATA   = "/api/v2/gta/data/"
_GTA_API_PATH_V1_TICKER = "/api/v1/gta/ticker/"
# gta_evaluation IDs (per OpenAPI doc): 4 = Red (harmful), 5 = also harmful
# nuance.  We pass both to capture the full "harmful" universe.
_GTA_API_HARMFUL_EVAL_IDS = [4, 5]


def fetch_gta_interventions_api(
    api_key: str,
    hs_prefixes: list[str] = BATTERY_HS_PREFIXES,
    since_date: Optional[date] = None,
    until_date: Optional[date] = None,
    intervention_type_ids: Optional[list[int]] = None,
    base_url: Optional[str] = None,
    page_size: Optional[int] = None,
    rate_limit_delay: Optional[float] = None,
    timeout: int = 180,
    max_pages: Optional[int] = None,
) -> list[dict]:
    """Fetch Red interventions from the GTA API, paginated.

    Args:
        api_key:                 GTA API key (use settings.gta_api_key).
        hs_prefixes:             **Full HS codes** (typically 6-digit) for
                                 server-side ``affected_products`` filter.
                                 NOTE despite the parameter name, the GTA API
                                 matches on full codes only — 4-digit values
                                 return zero results (F-GTA-API-1, 2026-06-11).
                                 Empty list = no product filter (returns all).
        since_date, until_date:  Bounds for announcement_period; either side
                                 may be None to disable that bound.  Default
                                 since=2018-01-01 (matches CSV parser
                                 behaviour) when both are None.
        intervention_type_ids:   Optional integer-ID filter (per the official
                                 GTA taxonomy, see _INTERVENTION_ID_TO_SUBTYPE
                                 for the launch-relevant subset).  None
                                 returns all types.
        base_url, page_size,
        rate_limit_delay:        Override settings if given (testing).
        timeout:                 Per-request read timeout in seconds.  Raised
                                 60 → 180 on 2026-07-31: production pages
                                 routinely take ~30s and deeper offsets are
                                 slower; 60s killed a full pull on page 3.
        max_pages:               Safety cap — None means iterate until
                                 fewer-than-page-size rows return.  Tests
                                 should pass max_pages=1.

    Returns:
        List of API response dicts (one per intervention) — raw, unmodified.
        Pass to :func:`parse_gta_api_response` for normalisation into the
        same dict shape that the CSV path produces.

    Raises:
        httpx.HTTPStatusError on non-2xx response.
        ValueError if api_key is empty.
    """
    if not api_key:
        raise ValueError(
            "GTA API key is required.  Set settings.gta_api_key or pass via the CLI."
        )

    # Lazy settings import to avoid circular import at module load.
    from app.core.config import get_settings
    s = get_settings()
    base = (base_url or s.gta_base_url).rstrip("/")
    page = page_size or s.gta_page_size
    delay = rate_limit_delay if rate_limit_delay is not None else s.gta_rate_limit_delay

    # Default since=2018-01-01 if neither bound is set — matches the CSV
    # parser's since_year=2018 default and avoids accidentally pulling the
    # entire 2009-onwards corpus on a fresh run.
    if since_date is None and until_date is None:
        since_date = date(2018, 1, 1)

    request_filters: dict[str, Any] = {
        "gta_evaluation": _GTA_API_HARMFUL_EVAL_IDS,
    }
    if since_date or until_date:
        request_filters["announcement_period"] = [
            (since_date.isoformat() if since_date else None),
            (until_date.isoformat() if until_date else None),
        ]
    if hs_prefixes:
        request_filters["affected_products"] = list(hs_prefixes)
    if intervention_type_ids:
        request_filters["intervention_types"] = list(intervention_type_ids)

    headers = {
        "Authorization": f"APIKey {api_key}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    url = f"{base}{_GTA_API_PATH_V2_DATA}"

    log.info(
        "gta.api.fetch.start",
        url=url,
        hs_prefix_count=len(hs_prefixes) if hs_prefixes else 0,
        since=str(since_date) if since_date else None,
        until=str(until_date) if until_date else None,
        page_size=page,
    )

    results: list[dict] = []
    offset = 0
    pages_fetched = 0

    with httpx.Client(timeout=timeout) as client:
        while True:
            if max_pages is not None and pages_fetched >= max_pages:
                log.info("gta.api.fetch.max_pages_reached", pages=pages_fetched)
                break

            body = {
                "limit":        page,
                "offset":       offset,
                "request_data": request_filters,
            }
            # ── 429 / transient-5xx backoff (2026-07-13) ──────────────
            # GTA rate-limits per key (observed account-level 429 after a
            # few same-day runs).  Honor Retry-After when present, else
            # exponential backoff 30s → 60s → 120s → 240s → 480s.  Other
            # 4xx (auth, bad request) still raise immediately — retrying
            # those wastes quota.
            #
            # 2026-07-31: read timeouts are retryable too.  Observed in
            # production: pages routinely take ~30s and deeper offsets are
            # slower; page 3 of a full pull exceeded the old 60s client
            # timeout, and because a timeout raises an exception rather
            # than returning a status, it escaped this loop entirely and
            # killed the run — after two pages of metered records had
            # already been pulled and were then thrown away.  Timeouts get
            # a shorter backoff than 429s (10s → 20s → 40s → 80s → 160s):
            # they signal a slow server, not a rate limit.
            _RETRYABLE = {429, 500, 502, 503, 504}
            attempt = 0
            timeout_attempt = 0
            while True:
                try:
                    response = client.post(url, headers=headers, json=body)
                except httpx.TimeoutException as exc:
                    timeout_attempt += 1
                    if timeout_attempt > 5:
                        raise
                    wait_s = min(10.0 * (2 ** (timeout_attempt - 1)), 300.0)
                    log.warning(
                        "gta.api.fetch.timeout_retry",
                        attempt=timeout_attempt,
                        wait_seconds=wait_s,
                        offset=offset,
                        error=str(exc) or type(exc).__name__,
                    )
                    import time as _t
                    _t.sleep(wait_s)
                    continue
                if response.status_code not in _RETRYABLE:
                    response.raise_for_status()
                    break
                attempt += 1
                if attempt > 5:
                    response.raise_for_status()  # exhausted — surface the error
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_s = max(1.0, float(retry_after)) if retry_after else 30.0 * (2 ** (attempt - 1))
                except ValueError:
                    wait_s = 30.0 * (2 ** (attempt - 1))
                wait_s = min(wait_s, 600.0)
                log.warning(
                    "gta.api.fetch.rate_limited",
                    status=response.status_code,
                    attempt=attempt,
                    wait_seconds=wait_s,
                    retry_after_header=retry_after,
                )
                import time as _t
                _t.sleep(wait_s)
            chunk = response.json()
            pages_fetched += 1

            if not isinstance(chunk, list):
                # Surface a clear error if the API contract drifts under us.
                log.warning(
                    "gta.api.fetch.unexpected_shape",
                    type=type(chunk).__name__,
                    keys=list(chunk.keys())[:5] if isinstance(chunk, dict) else None,
                )
                break

            results.extend(chunk)
            log.info(
                "gta.api.fetch.page",
                page=pages_fetched,
                offset=offset,
                returned=len(chunk),
                cumulative=len(results),
            )
            if len(chunk) < page:
                # Last page — fewer rows than requested limit.
                break
            offset += page
            if delay > 0:
                import time as _t
                _t.sleep(delay)

    log.info("gta.api.fetch.done", total=len(results), pages=pages_fetched)
    return results


def parse_gta_api_response(
    rows: list[dict],
    hs_prefixes: list[str] = BATTERY_HS_PREFIXES,
    since_year: Optional[int] = 2018,
    skip_hs_filter: bool = False,
    country_name_map: Optional[dict[str, str]] = None,
) -> list[dict]:
    """Convert API rows into the same normalised dict shape ``parse_gta_csv``
    produces, so downstream ingest code is path-agnostic.

    Filtering applied (mirrors parse_gta_csv to keep behaviour parity):
      * gta_evaluation must be "Red" (server-side filter usually handles this,
        but a defensive client-side check survives any API drift).
      * At least one affected_products HS code must match ``hs_prefixes``
        (skipped when ``skip_hs_filter=True``).
      * event_date (implemented preferred, else announced) >= ``since_year``.

    Args:
        rows:               Raw list from :func:`fetch_gta_interventions_api`.
        hs_prefixes:        4-digit HS prefixes for client-side double-check
                            (server already filtered when prefixes passed
                            to the API call).
        since_year:         Skip events whose effective year is older.
        skip_hs_filter:     Trust GTA's curation — keep all matched products.
        country_name_map:   Optional alias-table override for ISO2 resolution
                            (not strictly needed here — the API returns ISO3
                            codes directly — but accepted for signature parity
                            with parse_gta_csv).

    Returns:
        Normalised list of dicts with the same keys parse_gta_csv produces.
    """
    parsed: list[dict] = []
    kept = 0

    for row in rows:
        # ── 1. Red filter (defensive — server-side filter usually catches) ──
        evaluation = (row.get("gta_evaluation") or "").strip().lower()
        if evaluation and evaluation not in {"red", "harmful"}:
            continue

        # ── 2. HS filter — affected_products is structured ──────────────────
        affected_products = row.get("affected_products") or []
        all_hs: list[str] = []
        for p in affected_products:
            pid = p.get("product_id") if isinstance(p, dict) else None
            if pid is None:
                continue
            # product_id is an integer like 280530.  Format as 6-digit string.
            code = str(pid).zfill(6)
            all_hs.append(code)

        if skip_hs_filter:
            matched = all_hs
        else:
            matched = [c for c in all_hs if _matches_prefix(c, hs_prefixes)]
            if not matched:
                continue

        # ── 3. Date filter ──────────────────────────────────────────────────
        date_implemented = _parse_date(row.get("date_implemented") or "")
        date_announced   = _parse_date(row.get("date_announced") or "")
        # 2026-07-30: future implementation dates are re-anchored to the
        # announcement rather than stored forward — see _resolve_event_date.
        event_date, scheduled_implementation = _resolve_event_date(
            date_implemented, date_announced
        )
        if event_date is None:
            if scheduled_implementation is not None:
                # Announced *and* implemented in the future — nothing has
                # happened yet.  Skipped rather than stored so it cannot enter
                # an evidence pool before it is real.
                log.info(
                    "gta.api.parse.future_only",
                    intervention_id=row.get("intervention_id"),
                    scheduled=scheduled_implementation.isoformat(),
                )
            else:
                log.warning(
                    "gta.api.parse.bad_date",
                    intervention_id=row.get("intervention_id"),
                )
            continue
        if since_year is not None and event_date.year < since_year:
            continue

        # ── 4. Country resolution — API gives ISO3, downstream wants ISO2 ──
        implementing_iso2 = _resolve_iso3_jurisdiction(
            row.get("implementing_jurisdictions") or [],
            country_name_map,
        )
        affected_iso2_list = _resolve_iso3_list(
            row.get("affected_jurisdictions") or [],
            country_name_map,
        )

        # ── 5. Intervention type → subtype/category ─────────────────────────
        intervention_type = (row.get("intervention_type") or "").strip()
        risk_category = GTA_INTERVENTION_CATEGORY_MAP.get(
            intervention_type, DEFAULT_GTA_CATEGORY
        )

        # ── 6. Title + summary ──────────────────────────────────────────────
        title = (row.get("state_act_title") or "").strip()
        if not title:
            continue
        # intervention_description is an array of {status, order_nr, modified,
        # text}.  Concatenate text fields (stripping HTML for the summary).
        desc_text = ""
        for d in row.get("intervention_description") or []:
            if isinstance(d, dict) and d.get("text"):
                desc_text += d["text"] + "\n"
        summary = _strip_html(desc_text).strip()[:_SUMMARY_MAX]

        # ── 7. Structural fields ────────────────────────────────────────────
        intervention_id = row.get("intervention_id") or 0
        try:
            gta_id = int(intervention_id)
        except (TypeError, ValueError):
            gta_id = 0

        in_force = bool(row.get("is_in_force"))
        implementation_level = row.get("implementation_level")
        mast_chapter         = row.get("mast_chapter")
        mast_subchapter      = row.get("mast_subchapter")    # NEW (precision)
        eligible_firm        = row.get("eligible_firm")
        # affected_sectors is a structured list — flatten to comma-joined names
        # for backwards compat with the CSV-derived string format.
        sectors = row.get("affected_sectors") or []
        affected_sectors_raw = ", ".join(
            s.get("name", "") for s in sectors if isinstance(s, dict) and s.get("name")
        ) or None

        # ── 7b. Precision fields (2026-06-11) ───────────────────────────────
        # Confidence-related: is_official_source + source citation count +
        # whether jurisdictions were inferred vs explicit.
        is_official_source     = row.get("is_official_source")
        state_act_source_arr   = row.get("state_act_source") or []
        source_count           = len(state_act_source_arr) if isinstance(state_act_source_arr, list) else 0
        inferred_jurisdictions = row.get("inferred_jurisdictions")

        # Severity-related: tariff prior_level → new_level magnitude.  Use
        # the MAX magnitude across affected products as the event-level
        # multiplier — a multi-HS intervention with mixed magnitudes gets
        # the severity its largest jump implies, on the principle that the
        # most-impacted product drives the headline signal.
        product_records = affected_products  # already-extracted list
        max_tariff_multiplier = 1.0
        per_product_meta: list[dict] = []
        for p in product_records:
            if not isinstance(p, dict):
                continue
            prior = p.get("prior_level")
            new   = p.get("new_level")
            mult  = compute_tariff_magnitude_multiplier(prior, new)
            if mult > max_tariff_multiplier:
                max_tariff_multiplier = mult
            # Per-product metadata for downstream precision (per-HS recency
            # and per-HS removed-date tracking).
            per_product_meta.append({
                "hs_code":         str(p.get("product_id") or "").zfill(6),
                "prior_level":     prior,
                "new_level":       new,
                "unit":            p.get("unit"),
                "date_implemented": p.get("date_implemented"),
                "date_removed":    p.get("date_removed"),
            })

        # Latest-action tracking — capture so downstream can either refresh
        # event_date or expose an "updated_at" diagnostic.
        latest_action_date_str = row.get("latest_action_date")
        latest_action_name     = row.get("latest_action_name")
        latest_action_dt       = _parse_date(latest_action_date_str or "")

        # Computed confidence override (None if no precision signals — falls
        # back to the legacy 0.9 in downstream).
        confidence_override = compute_event_confidence(
            is_official_source=is_official_source,
            source_count=source_count if source_count > 0 else None,
            inferred_jurisdictions=inferred_jurisdictions,
        )

        parsed.append({
            "gta_id":                gta_id,
            "title":                 title,
            "event_date":            event_date,
            "implementing_iso2":     implementing_iso2,
            "intervention_type":     intervention_type,
            "risk_category":         risk_category,
            "matched_hs_codes":      matched,
            "summary":               summary,
            "in_force":              in_force,
            "affected_iso2_list":    affected_iso2_list,
            "implementation_level":  implementation_level,
            "is_horizontal":         False,    # API doesn't expose; default False
            "date_announced":        date_announced,
            "scheduled_implementation_date": scheduled_implementation,
            "mast_chapter":          mast_chapter,
            "eligible_firm":         eligible_firm,
            "affected_sectors_raw":  affected_sectors_raw,
            # ── 2026-06-11 API precision additions ────────────────────────
            "tariff_magnitude_multiplier": max_tariff_multiplier,
            "confidence_override":   confidence_override,
            "latest_action_date":    latest_action_dt,
            # API-only metadata — preserved into RiskEvent.metadata_json by
            # the existing ingest function via the raw_row passthrough.
            "raw_row": {
                "intervention_id":         intervention_id,
                "state_act_id":            row.get("state_act_id"),
                "intervention_url":        row.get("intervention_url"),
                "state_act_url":           row.get("state_act_url"),
                "is_official_source":      is_official_source,
                "state_act_source_count":  source_count,
                "inferred_jurisdictions":  inferred_jurisdictions,
                "intervention_type":       intervention_type,
                "gta_evaluation":          row.get("gta_evaluation"),
                "mast_chapter":            mast_chapter,
                "mast_subchapter":         mast_subchapter,
                "per_product":             per_product_meta,
                "max_tariff_multiplier":   max_tariff_multiplier,
                "latest_action_date":      latest_action_date_str,
                "latest_action_name":      latest_action_name,
                # Source mode flag — lets the downstream ingest function
                # distinguish CSV-origin vs API-origin events for debugging.
                "_source_mode":      "api",
            },
        })
        kept += 1

    log.info("gta.api.parse.done", rows_seen=len(rows), rows_kept=kept)
    return parsed


def _resolve_api_jurisdiction_entry(
    entry: dict,
    country_name_map: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Resolve one API jurisdiction dict ``{id, name, iso}`` to ISO2.

    Resolution order (fixed 2026-07-13 — the original implementation
    ignored the API's own ``iso`` field AND called the name resolver
    without the DB map, so it fell back to the hardcoded name list and
    dropped ~16% of interventions as unknown-country):

      1. ``iso`` (ISO3) via the ISO3→ISO2 entries that
         _build_country_name_map now includes — spelling-proof.
      2. ``name`` via the full DB-backed resolver (common_names +
         hardcoded fallback) — covers EU ('European Union', iso3 NULL)
         and any row missing iso3.
    """
    if not isinstance(entry, dict):
        return None
    iso3 = (entry.get("iso") or "").strip().upper()
    if iso3 and country_name_map:
        hit = country_name_map.get(iso3)
        if hit:
            return hit
    name = entry.get("name") or ""
    return _resolve_country(name, country_name_map) or None


# EU membership as of 2020 (post-Brexit, 27 states).  Used only to collapse
# a multi-member implementing list into the bloc — if enlargement ever adds a
# member, the worst failure mode is that a list containing the new state
# stops collapsing and falls back to first-entry, which is the old behaviour.
_EU_MEMBER_ISO2: frozenset[str] = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
    "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
    "PL", "PT", "RO", "SK", "SI", "ES", "SE",
})


def _resolve_iso3_jurisdiction(
    jurisdiction_list: list[dict],
    country_name_map: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Resolve an API implementing-jurisdiction list to a single ISO2.

    The original implementation took ``jurisdiction_list[0]`` unconditionally.
    For EU-wide measures GTA enumerates the member states individually —
    alphabetically — so every EU regulation in the corpus landed with
    ``implementing_iso2 = "AT"``: Austria was "implementing" the EU–US
    countermeasures package because Austria sorts first.  (The tell was
    already in the data: those same rows carried the *supranational*
    implementation-level multiplier.)

    Rules, in order:

      1. Resolve every entry, not just the first.
      2. If any entry resolves to ``EU`` itself, the bloc is the implementer.
      3. If more than one distinct member state resolves and ALL of them are
         EU members, collapse to ``EU`` — that is what an enumerated list of
         member states means.  ``countries.iso2`` explicitly admits ``EU``,
         so downstream joins and the geography pill both handle it.
      4. Otherwise (single country, or a mixed multilateral list like a G7
         action) keep the first resolved entry — for genuinely mixed lists
         there is no honest single answer, and first-entry at least matches
         prior behaviour while the full list is preserved separately in the
         affected/geography structures.
    """
    if not jurisdiction_list:
        return None
    resolved: list[str] = []
    for entry in jurisdiction_list:
        iso = _resolve_api_jurisdiction_entry(entry, country_name_map)
        if iso and iso not in resolved:
            resolved.append(iso)
    if not resolved:
        return None
    if "EU" in resolved:
        return "EU"
    if len(resolved) > 1 and all(c in _EU_MEMBER_ISO2 for c in resolved):
        return "EU"
    return resolved[0]


def _resolve_iso3_list(
    jurisdiction_list: list[dict],
    country_name_map: Optional[dict[str, str]] = None,
) -> list[str]:
    """Same as _resolve_iso3_jurisdiction but returns the full list."""
    out: list[str] = []
    for j in jurisdiction_list:
        iso = _resolve_api_jurisdiction_entry(j, country_name_map)
        if iso and iso not in out:
            out.append(iso)
    return out


def _strip_html(text: str) -> str:
    """Best-effort HTML→plain-text for intervention_description summaries."""
    if not text:
        return ""
    # Remove tags via simple regex; for summary use, we don't need a full parser.
    import re as _re
    no_tags = _re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    return _re.sub(r"\s+", " ", no_tags).strip()


# ---------------------------------------------------------------------------
# CSV download (legacy — kept for --local-file fallback only)
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


def _resolve_event_date(
    date_implemented: Optional[datetime],
    date_announced: Optional[datetime],
    *,
    now: Optional[datetime] = None,
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Return ``(event_date, scheduled_implementation_date)``.

    GTA publishes interventions with an implementation date that may be in the
    future — an announced measure taking effect next year.  Storing that
    forward date as ``RiskEvent.event_date`` is wrong in a specific and costly
    way: every evidence window in the scoring engine is a *trailing* window
    (``event_date >= cutoff``, no upper bound), so a future-dated event
    satisfies every window forever, and ``decay.py`` clamps negative ages to
    the 1.20 multiplier ceiling — so it is weighted *more* heavily than an
    event happening today, permanently.

    The rule here is deliberately narrow:

    - implementation date in the past → use it, nothing scheduled
    - implementation date in the future, announcement in the past → date the
      event to the announcement (the announcement *is* the thing that
      happened) and preserve the forward date as metadata
    - both in the future → return ``(None, scheduled)`` so the caller skips
      the row; nothing has happened yet
    - no implementation date → fall through to the announcement date

    Preserving the forward date rather than discarding it matters: an
    announced-but-not-yet-in-force export ban is real information, and the
    triage surface should be able to show "takes effect 2029-01-01".
    """
    now = now or datetime.now(timezone.utc)
    event_date = date_implemented or date_announced
    if event_date is None:
        return None, None
    if event_date <= now:
        return event_date, None
    if date_announced is not None and date_announced <= now:
        return date_announced, event_date
    return None, event_date


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
        # ISO3 → ISO2 (2026-07-13): the GTA API supplies ISO3 in every
        # jurisdiction record; 3-letter upper keys cannot collide with
        # country-name keys.  129/130 rows carry iso3 (EU is NULL — it
        # resolves via its name/common_names instead).
        if getattr(row, "iso3", None):
            name_map[row.iso3.strip().upper()] = row.iso2
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


def _derive_hs_codes_for_api_from_db(session: Session) -> list[str]:
    """Return zero-padded 6-digit HS codes for the GTA API ``affected_products`` filter.

    The /api/v2/gta/data/ endpoint matches ``affected_products`` against full
    6-digit HS codes (not prefixes).  Passing 4-digit strings returns 0 rows.
    This helper expands the DB-stored mappings to 6-digit codes:

      * 6-digit rows pass through unchanged.
      * 4-digit rows are expanded by padding with "00" — covers the
        umbrella heading when GTA tags an intervention at heading-level
        rather than subheading-level (common for "all of HS 2604" tariff
        actions).  Catches most coverage; sub-codes still need explicit
        6-digit rows in the DB to be filtered server-side.

    Falls back to empty list (no server-side filter — client-side prefix
    filter still applies) when the table is empty.
    """
    hs_map = _build_hs_material_map(session)
    if not hs_map:
        log.warning(
            "gta.derive_hs_codes_for_api.empty_table",
            hint="Run 'bdi-ingest seed-hs-mappings' before ingest-gta --use-api.",
        )
        return []
    codes: set[str] = set()
    for raw_prefix in hs_map.keys():
        clean = str(raw_prefix).replace(".", "")
        if len(clean) >= 6:
            codes.add(clean[:6])
        elif len(clean) == 4:
            # Pad to 6-digit umbrella code (e.g. "2604" → "260400").
            codes.add(clean + "00")
        # Lengths < 4 are unexpected; skip silently.
    return sorted(codes)


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

        # 2026-07-30: future implementation dates are re-anchored to the
        # announcement rather than stored forward — see _resolve_event_date.
        event_date, scheduled_implementation = _resolve_event_date(
            _parse_date(date_implemented_str),
            _parse_date(date_announced_str),
        )
        if event_date is None:
            if scheduled_implementation is not None:
                log.info(
                    "gta.parse.future_only",
                    gta_id=row.get(col["id"]),
                    scheduled=scheduled_implementation.isoformat(),
                )
            else:
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
                "scheduled_implementation_date": scheduled_implementation,
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
# Permalinks
# ---------------------------------------------------------------------------
#
# Verified 2026-07-30: both patterns return HTTP 200 on the canonical host.
# ``www.globaltradealert.org`` 301-redirects to the no-www host, so the no-www
# form is stored to avoid a redirect hop on every click-through.

_GTA_INTERVENTION_URL = "https://globaltradealert.org/intervention/{id}"
_GTA_STATE_ACT_URL = "https://globaltradealert.org/state-act/{id}"


def gta_permalink(
    gta_id: Optional[int],
    state_act_id: Optional[int] = None,
) -> Optional[str]:
    """Public GTA URL for an intervention, or ``None`` if neither id is usable.

    The intervention page is preferred: it is the narrower record and matches
    the granularity of a single ``RiskEvent``.  The state-act page is the
    fallback for rows that carry only the parent act id.
    """
    if gta_id:
        return _GTA_INTERVENTION_URL.format(id=int(gta_id))
    if state_act_id:
        return _GTA_STATE_ACT_URL.format(id=int(state_act_id))
    return None


def _get_or_create_intervention_document(
    session: Session,
    *,
    source_id: int,
    gta_id: int,
    state_act_id: Optional[int],
    title: str,
    event_date: Optional[datetime],
    run_document_id: int,
    dry_run: bool = False,
) -> Optional[int]:
    """One ``SourceDocument`` per GTA intervention, carrying its permalink.

    Before 2026-07-30 every GTA event pointed at a single per-run bulk-CSV
    document whose ``url`` was NULL.  ``GET /materials/{id}/risk-events`` reads
    ``SourceDocument.url`` and nothing else, so all 2,575 GTA events rendered
    with no source link — the provenance existed in the database but was
    invisible to anyone using the product.

    Storing the permalink in ``metadata_json`` alone would not have fixed that
    without an API change; a per-intervention document fixes it at the data
    layer *and* gives each event a stable provenance row of its own.

    Falls back to the per-run document when no permalink is derivable, so the
    provenance chain is never broken — a missing link is preferable to a
    dangling ``source_document_id``.

    With ``dry_run=True`` this never writes: an existing document's id is
    still returned (lookups are reads), but a document that *would* be created
    returns ``None`` so the caller can count it without creating it.
    """
    permalink = gta_permalink(gta_id, state_act_id)
    if permalink is None:
        return run_document_id

    external_id = (
        f"gta_intervention_{int(gta_id)}" if gta_id
        else f"gta_state_act_{int(state_act_id)}"
    )
    existing = session.scalar(
        select(SourceDocument).where(
            SourceDocument.source_id == source_id,
            SourceDocument.external_id == external_id,
        )
    )
    if existing is not None:
        # Repair rows written before the permalink existed.
        if not existing.url and not dry_run:
            existing.url = permalink
        return existing.id

    if dry_run:
        return None

    doc = SourceDocument(
        source_id=source_id,
        external_id=external_id,
        url=permalink,
        title=title[:1024] if title else external_id,
        document_type="trade_intervention",
        published_at=event_date,
        metadata_json={
            "gta_id": gta_id or None,
            "state_act_id": state_act_id,
            "permalink": permalink,
        },
    )
    session.add(doc)
    session.flush()
    return doc.id


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def _content_hash(title: str, summary: str, event_date: datetime) -> str:
    """LEGACY SHA-256 of ``f'{title}{summary}{event_date.isoformat()}'``.

    Retained only so ``scripts/backfill_gta_quality.py`` can recognise rows
    written before 2026-07-30 and re-key them.  Do not use for new inserts —
    see ``_gta_identity_hash`` for why.
    """
    payload = f"{title}{summary}{event_date.isoformat()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _gta_identity_hash(gta_id: int, title: str, event_date: datetime) -> str:
    """Stable dedupe key for a GTA intervention.

    The legacy key hashed the summary text, which made *summary length* part
    of row identity.  Two consequences, both observed in production:

    1.  Raising ``_SUMMARY_MAX`` changes the hash of every event, so a
        re-ingest would have inserted 2,575 duplicates.  Only the unindexed
        JSONB ``gta_id`` fallback in ``_existing_event_id`` prevented that,
        and that fallback degrades to an O(N) Python scan on SQLite.
    2.  GTA revises intervention descriptions in place.  Under the legacy key
        a revised description reads as a brand-new event, so the same
        intervention accumulates rows over time.

    Keying on the GTA intervention id instead makes identity mean what it
    should: one row per intervention, whose content can be updated in place.
    ``risk_events.content_hash`` is ``String(64)`` and indexed but *not*
    unique, so re-keying existing rows is safe.

    Interventions with no usable id fall back to the old title+date shape
    (minus the summary), which is at least stable under a cap change.
    """
    payload = (
        f"gta:{gta_id}" if gta_id
        else f"gta:noid:{title}{event_date.isoformat()}"
    )
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


def _refresh_existing_event(
    session: Session,
    *,
    event_id: int,
    summary: Optional[str],
    content_hash: str,
    source_document_id: Optional[int],
    metadata_additions: Optional[dict] = None,
    dry_run: bool = False,
) -> list[str]:
    """Repair source-derived fields on an already-stored GTA event.

    Added 2026-07-30.  Until now the ingest loop *skipped* any event it
    recognised, which meant a fix to the ingester could only ever improve
    future rows — the 2,497 events already truncated at 500 characters would
    have stayed truncated forever, because the full text is not recoverable
    from the database (``SourceDocument.raw_text`` is never populated on this
    path).  Re-ingesting is the only repair route, so the loop has to be able
    to update.

    **This function deliberately touches only fields derived from the source
    feed.**  It never writes:

      ``verified``, ``primary_category``, ``severity_score``,
      ``confidence_score``, ``risk_categories_json``

    nor any material / HS-code / geography link.  Those are either curated by
    hand or produced by scoring logic, and an ingest pass must not silently
    revert a human decision.  Summary is only replaced when the incoming text
    is *longer* than what is stored, so a feed that regresses to a shorter
    description cannot destroy content either.

    Returns the list of field names actually changed (empty when the row was
    already correct), so the caller can distinguish "refreshed" from
    "skipped" and so ``--dry-run`` can report a precise blast radius.
    """
    ev = session.get(RiskEvent, event_id)
    if ev is None:
        return []

    changed: list[str] = []

    if summary and (ev.summary is None or len(summary) > len(ev.summary)):
        if not dry_run:
            ev.summary = summary
        changed.append("summary")

    if content_hash and ev.content_hash != content_hash:
        if not dry_run:
            ev.content_hash = content_hash
        changed.append("content_hash")

    if source_document_id is not None and ev.source_document_id != source_document_id:
        if not dry_run:
            ev.source_document_id = source_document_id
        changed.append("source_document_id")

    if metadata_additions:
        current = dict(ev.metadata_json or {})
        merged = {**current}
        for key, value in metadata_additions.items():
            if value is None:
                continue
            if current.get(key) != value:
                merged[key] = value
        if merged != current:
            if not dry_run:
                # Reassign rather than mutate — SQLAlchemy does not track
                # in-place mutation of a plain JSONB dict.
                ev.metadata_json = merged
            changed.append("metadata_json")

    return changed


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

    2026-08-04: export bans now take the not-in-force discount like every
    other type.  The original early return handed 0.9 to any ban regardless
    of state, which surfaced on the EU–US countermeasures package: an export
    ban suspended before its effective date — never in force for a single
    day — carried the corpus-maximum severity while the sibling tariff
    intervention of the same state act correctly showed the 0.3 path.  A
    suspended or revoked ban is a latent threat, not a blocked supply line;
    0.3 is the honest reading.  Caveat that applies to ALL types, not just
    bans: severity is computed at insert only — ``_refresh_existing_event``
    deliberately never rewrites it — so an in-force flip in either
    direction goes stale on stored rows.  The recalibrate-gta-severity
    backfill recomputes untriaged rows from stored metadata when needed.

    See ``_apply_severity_modifiers`` for the implementation-level + horizontal
    multipliers added 2026-05-06 (Scope 2 audit refinements).
    """
    if "ban" in intervention_type.lower() and "export" in intervention_type.lower():
        return 0.9 if in_force else 0.3
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


def compute_tariff_magnitude_multiplier(
    prior_level: Optional[float],
    new_level: Optional[float],
) -> float:
    """Tariff-magnitude refinement (2026-06-11, API-path precision upgrade).

    Currently every tariff-class intervention gets the same base severity
    (0.7) regardless of whether the rate went 5% → 7% or 5% → 50%.  When
    the API gives us ``prior_level`` and ``new_level`` for affected
    products, we can scale within that bucket.

    Multiplier rules:
      * Both levels present and new > prior → ``1 + min(log10(new/prior), 0.5)``
        Yields:
          1.5x →   +0.18  (small bump, 5% → 7.5%)
          2.0x →   +0.30  (moderate, 5% → 10%)
          5.0x →   +0.50  (cap, 5% → 25%)
          10x  →   +0.50  (cap holds, prevents runaway)
      * new ≤ prior or either NULL → 1.0  (no refinement, defensive default)

    Cap at 0.5 because runaway multipliers risk pushing severity past
    the meaning of 1.0 — keeping the bump within a half-bucket preserves
    the tier semantics the pillar expects.
    """
    import math
    if prior_level is None or new_level is None:
        return 1.0
    try:
        p = float(prior_level)
        n = float(new_level)
    except (TypeError, ValueError):
        return 1.0
    if p <= 0 or n <= 0 or n <= p:
        return 1.0
    bump = min(math.log10(n / p), 0.5)
    return 1.0 + bump


# ---------------------------------------------------------------------------
# Confidence calibration (2026-06-11, API-path precision upgrade)
# ---------------------------------------------------------------------------
# Pre-API: every GTA event was inserted with a flat ``confidence_score=0.9``.
# The API gives us three signals we can read to refine this:
#
#   is_official_source (bool)
#       True  → GTA validated against an official government press release / gazette
#       False → secondary citation only (news article, secondary report)
#   source_count
#       Length of ``state_act_source`` array — number of distinct citations
#       backing this intervention.  More sources = higher confidence.
#   inferred_jurisdictions (string status)
#       "Inferred"  → affected country list was derived by GTA analysts (not
#                     explicit in source) — reduces confidence in attribution
#       any other value (or None) → no adjustment
#
# Combined into a single ``confidence_score`` ∈ [0.5, 0.95]:
#
#   base = 0.9 if is_official_source else 0.6
#   if source_count >= 2: base += 0.05  (multi-source confirmation)
#   if inferred:          base -= 0.10  (attribution uncertainty)
#   clamp to [0.50, 0.95]

_CONFIDENCE_OFFICIAL  = 0.90
_CONFIDENCE_UNOFFICIAL = 0.60
_CONFIDENCE_MULTI_SOURCE_BUMP = 0.05
_CONFIDENCE_INFERRED_PENALTY  = 0.10
_CONFIDENCE_FLOOR = 0.50
_CONFIDENCE_CEIL  = 0.95


def compute_event_confidence(
    is_official_source: Optional[bool],
    source_count: Optional[int],
    inferred_jurisdictions: Optional[str],
) -> float:
    """Per-event confidence calibration from GTA-API signal triplet.

    See module-level comments above for the rule set.  Each input may be
    None when reading from a CSV row (which carries none of these fields),
    in which case the function returns the legacy flat 0.9 default.
    """
    if is_official_source is None and source_count is None and inferred_jurisdictions is None:
        # No API-only signals available — CSV-path default unchanged.
        return _CONFIDENCE_OFFICIAL

    base = _CONFIDENCE_OFFICIAL if is_official_source else _CONFIDENCE_UNOFFICIAL
    if source_count is not None and source_count >= 2:
        base += _CONFIDENCE_MULTI_SOURCE_BUMP
    if inferred_jurisdictions and str(inferred_jurisdictions).lower() == "inferred":
        base -= _CONFIDENCE_INFERRED_PENALTY
    return max(_CONFIDENCE_FLOOR, min(_CONFIDENCE_CEIL, base))


def _apply_severity_modifiers(
    base_severity: float,
    *,
    implementation_level: Optional[str],
    is_horizontal: bool,
    tariff_magnitude_multiplier: float = 1.0,
) -> float:
    """Apply Scope-2 severity refinements on top of the base severity.

    Multiplicative.  Result clamped to [0.0, 1.0] since the downstream
    scorer expects a normalised severity.

    ``tariff_magnitude_multiplier`` (2026-06-11) is the API-derived
    refinement for tariff/quota interventions — defaults to 1.0 (no
    change) for CSV path and for non-tariff intervention types.
    """
    sev = base_severity
    if implementation_level:
        mult = _IMPLEMENTATION_LEVEL_MULTIPLIER.get(implementation_level.strip().lower())
        if mult is not None:
            sev *= mult
    if is_horizontal:
        sev *= _IS_HORIZONTAL_MULTIPLIER
    if tariff_magnitude_multiplier != 1.0:
        sev *= tariff_magnitude_multiplier
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
    use_api: bool = False,
    api_since_date: Optional[date] = None,
    api_until_date: Optional[date] = None,
    dry_run: bool = False,
    api_raw_cache: Optional[str] = None,
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
        dry_run: When ``True``, parse and classify normally but write nothing:
            the run reports what *would* be inserted and which existing rows
            *would* be refreshed (with per-field counts), then rolls back.
            Added 2026-07-30 so the blast radius of a repair re-ingest can be
            inspected before it touches the database.
        api_raw_cache: Optional path for caching the raw API payload (API mode
            only).  If the file exists, it is read INSTEAD of calling the API
            — no key needed, no metered records consumed.  If it does not
            exist, the API is fetched normally and the payload is written
            there on success.  Added 2026-07-31 because GTA meters by records
            pulled per month: without this, a dry-run + real-run sequence
            pulls everything twice, and a run that dies mid-parse throws away
            everything it pulled.  Delete the file to force a fresh pull.

    Returns:
        dict with these keys::

            downloaded_rows         rows kept after Red + HS filters
            inserted                new RiskEvent rows created (or would-be, in dry-run)
            refreshed_existing      existing rows repaired in place (2026-07-30)
            refresh_field_counts    dict of field name → rows changed (2026-07-30)
            skipped_existing        recognised rows needing no repair
            material_links          RiskEventMaterial rows created
            hs_mapping_links        RiskEventHsMapping rows created (stage attribution)
            geography_links         RiskEventGeography rows created
            skipped_unknown_country events inserted but with no resolvable country
            dry_run                 1 when the run was a dry run, else 0
    """
    # Derive HS prefix filter from the DB (or fall back to hardcoded list).
    effective_prefixes = list(hs_prefixes) if hs_prefixes else _derive_hs_prefixes_from_db(session)
    log.info("gta.ingest.hs_prefixes", count=len(effective_prefixes), source="db" if not hs_prefixes else "override")

    # Build country name → ISO2 map from the DB (or empty dict if not seeded).
    country_name_map = _build_country_name_map(session)
    log.info("gta.ingest.country_name_map", entries=len(country_name_map))

    # ── Fetch path: API (preferred) or CSV (legacy fallback) ─────────────
    # The API path (2026-06-11) uses the canonical GTA taxonomy, applies
    # server-side HS filtering, and returns ~95% less data over the wire.
    # CSV path is retained for the --local-file flow (offline testing) and
    # for environments without an API key.
    if use_api:
        import json as _json
        import pathlib as _pathlib

        raw_rows: Optional[list] = None
        cache_path = _pathlib.Path(api_raw_cache) if api_raw_cache else None
        if cache_path is not None and cache_path.exists():
            # Cache hit — no API call, no key needed, no metered records.
            raw_rows = _json.loads(cache_path.read_text())
            log.info(
                "gta.ingest.api_raw_cache_hit",
                path=str(cache_path),
                rows=len(raw_rows),
            )

        if raw_rows is None:
            from app.core.config import get_settings
            api_key = get_settings().gta_api_key
            if not api_key:
                raise ValueError(
                    "ingest_gta(use_api=True) but settings.gta_api_key is empty. "
                    "Set GTA_API_KEY in the env or .env file."
                )
            # Convert since_year to a date floor for the API call when no
            # explicit api_since_date is given.
            effective_since = api_since_date or (
                date(since_year, 1, 1) if since_year is not None else None
            )
            # F-GTA-API-1 (2026-06-11): the API matches affected_products against
            # full 6-digit HS codes, not 4-digit prefixes.  effective_prefixes
            # is 4-digit (correct for client-side prefix matching downstream),
            # so derive a separate 6-digit list for the server-side filter.
            api_hs_codes = (
                [] if skip_hs_filter else _derive_hs_codes_for_api_from_db(session)
            )
            log.info(
                "gta.ingest.mode_api",
                since=effective_since.isoformat() if effective_since else None,
                until=api_until_date.isoformat() if api_until_date else None,
                client_side_prefix_count=len(effective_prefixes),
                server_side_hs_code_count=len(api_hs_codes),
            )
            raw_rows = fetch_gta_interventions_api(
                api_key=api_key,
                hs_prefixes=api_hs_codes,
                since_date=effective_since,
                until_date=api_until_date,
            )
            if cache_path is not None:
                # Written only after a fully successful fetch, so a partial
                # pull can never masquerade as a complete cache.
                cache_path.write_text(_json.dumps(raw_rows))
                log.info(
                    "gta.ingest.api_raw_cache_written",
                    path=str(cache_path),
                    rows=len(raw_rows),
                )

        interventions = parse_gta_api_response(
            raw_rows,
            hs_prefixes=effective_prefixes,
            since_year=since_year,
            skip_hs_filter=skip_hs_filter,
            country_name_map=country_name_map,
        )
    elif local_file is not None:
        import pathlib
        path = pathlib.Path(local_file)
        if not path.exists():
            raise FileNotFoundError(f"GTA local file not found: {local_file}")
        log.info("gta.ingest.local_file", path=str(path), size_bytes=path.stat().st_size)
        raw_bytes = path.read_bytes()
        interventions = parse_gta_csv(
            raw_bytes,
            hs_prefixes=effective_prefixes,
            since_year=since_year,
            skip_hs_filter=skip_hs_filter,
            country_name_map=country_name_map,
        )
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
    # Renamed from ``source_document_id`` 2026-07-30: this is the *per-run*
    # bulk document.  Events now point at per-intervention documents carrying
    # their permalink (see _get_or_create_intervention_document); the run
    # document remains as the fallback for rows with no derivable permalink.
    run_document_id = _create_gta_source_document(
        session, source_id, len(interventions), today
    )

    hs_material_map = _build_hs_material_map(session)

    inserted = 0
    refreshed_existing = 0                    # added 2026-07-30
    refresh_field_counts: dict[str, int] = {}  # added 2026-07-30
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
        # 2026-06-11 API precision additions — None when reading from CSV
        # path or when API row didn't carry the field.  Used to refine
        # severity / confidence below the legacy flat values.
        tariff_magnitude_multiplier = float(
            intervention.get("tariff_magnitude_multiplier") or 1.0
        )
        confidence_override = intervention.get("confidence_override")
        latest_action_date = intervention.get("latest_action_date")
        scheduled_implementation = intervention.get("scheduled_implementation_date")
        # If the API recorded a more-recent action date, prefer it for
        # recency-decay purposes — captures status changes (revocation,
        # scope expansion) that occurred after the original implementation.
        #
        # 2026-07-30: bounded above by "now".  latest_action_date can itself be
        # forward-dated, and pushing event_date into the future re-introduces
        # exactly the defect _resolve_event_date exists to prevent.
        if latest_action_date and isinstance(event_date, datetime):
            if event_date < latest_action_date <= datetime.now(timezone.utc):
                event_date = latest_action_date

        # GTA's parent "state act" id, when the row carries one.  Used only as
        # a permalink fallback for rows with no intervention id.
        state_act_id: Optional[int] = None
        _raw_row = intervention.get("raw_row") or {}
        for _key in ("state_act_id", "state_act", "act_id"):
            _val = _raw_row.get(_key)
            if _val:
                try:
                    state_act_id = int(str(_val).strip())
                    break
                except (TypeError, ValueError):
                    continue

        # 2026-07-30: identity is the GTA intervention id, not a hash of the
        # summary text — see _gta_identity_hash.
        ch = _gta_identity_hash(gta_id, title, event_date)
        existing_id = _existing_event_id(session, content_hash=ch, gta_id=gta_id)
        if existing_id is not None:
            # Previously this was a blind ``continue``.  Now the row is
            # repaired in place: truncated summaries are replaced with the
            # full text, the legacy content_hash is re-keyed, and the event is
            # re-pointed at its own permalink document.  Curated and scored
            # fields are never touched — see _refresh_existing_event.
            permalink = gta_permalink(gta_id, state_act_id)
            permalink_doc_id = _get_or_create_intervention_document(
                session,
                source_id=source_id,
                gta_id=gta_id,
                state_act_id=state_act_id,
                title=title,
                event_date=event_date,
                run_document_id=run_document_id,
                dry_run=dry_run,
            )
            changed = _refresh_existing_event(
                session,
                event_id=existing_id,
                summary=summary,
                content_hash=ch,
                source_document_id=permalink_doc_id,
                metadata_additions={
                    "permalink": permalink,
                    "source_url": permalink,
                    "state_act_id": state_act_id,
                    "scheduled_implementation_date": (
                        scheduled_implementation.isoformat()
                        if isinstance(scheduled_implementation, datetime)
                        else None
                    ),
                },
                dry_run=dry_run,
            )
            if dry_run and permalink_doc_id is None and permalink:
                # The intervention document does not exist yet, so a real run
                # would create it and repoint the event at it.  Counted here
                # because _refresh_existing_event cannot see a doc id that was
                # deliberately not created.
                if "source_document_id" not in changed:
                    changed = [*changed, "source_document_id"]
            if changed:
                refreshed_existing += 1
                for _field in changed:
                    refresh_field_counts[_field] = (
                        refresh_field_counts.get(_field, 0) + 1
                    )
            else:
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
        # tariff_magnitude_multiplier (2026-06-11) refines severity within
        # tariff buckets based on the actual rate change (prior_level →
        # new_level) when the API exposed it.  Defaults to 1.0 (no change)
        # for CSV path / non-tariff types — backwards-compatible.
        base_severity = _severity_for(intervention_type, in_force)
        severity = _apply_severity_modifiers(
            base_severity,
            implementation_level=implementation_level,
            is_horizontal=is_horizontal,
            tariff_magnitude_multiplier=tariff_magnitude_multiplier,
        )

        # ── Build metadata_json (Scope-2 fields included) ──────────────────
        metadata: dict[str, Any] = {
            "gta_id": gta_id,
            "gta_hs_codes": matched_hs_codes,
            "raw_intervention_type": intervention["intervention_type"],
            "in_force": in_force,
            # 2026-07-30: source_url used to record the bulk download URL —
            # the same value on every event of a run, and one that has since
            # gone 404 in production metadata.  It now records the event's own
            # permalink when one is derivable, with the bulk URL preserved
            # separately.  Metadata should not claim a source it cannot show.
            "source_url": gta_permalink(gta_id, state_act_id) or url,
            "permalink": gta_permalink(gta_id, state_act_id),
            "bulk_source_url": url,
            "state_act_id": state_act_id,
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
        if isinstance(scheduled_implementation, datetime):
            # 2026-07-30: the forward-looking implementation date preserved by
            # _resolve_event_date.  The event itself is dated to its
            # announcement; this records when the measure actually takes
            # effect, so the triage/content surfaces can show it.
            metadata["scheduled_implementation_date"] = (
                scheduled_implementation.isoformat()
            )
            metadata["in_force_status"] = "announced_not_yet_in_force"
        if affected_iso2_list:
            metadata["affected_iso2_list"] = affected_iso2_list

        # ── 2026-06-11 Tier 1/2 API precision metadata passthrough ──────────
        # parse_gta_api_response() stages these into the intervention dict
        # under top-level keys (tariff_magnitude_multiplier, latest_action_date)
        # and a nested raw_row block (state_act_id, mast_subchapter, per_product,
        # is_official_source, state_act_source_count, inferred_jurisdictions).
        # CSV path leaves these absent; only API path populates them.  Before
        # this passthrough was wired the parser comment at line ~945 promised
        # the values would flow through to metadata_json but the dict
        # construction above never read them — silent feature gap until
        # caught by post-ingest verification on 2026-06-11.
        if tariff_magnitude_multiplier != 1.0:
            metadata["tariff_magnitude_multiplier"] = tariff_magnitude_multiplier
        if latest_action_date is not None:
            metadata["latest_action_date"] = (
                latest_action_date.isoformat()
                if isinstance(latest_action_date, datetime)
                else str(latest_action_date)
            )
        raw_row_block = intervention.get("raw_row")
        if isinstance(raw_row_block, dict) and raw_row_block.get("_source_mode") == "api":
            for fld in (
                "state_act_id", "mast_subchapter", "per_product",
                "is_official_source", "state_act_source_count",
                "inferred_jurisdictions", "latest_action_name",
            ):
                val = raw_row_block.get(fld)
                if val is not None and val != [] and val != "":
                    metadata[fld] = val

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

        # ── Suggestion fields (2026-07-31, triage plan Phase 1) ────────────
        # The category/direction the machine derived are SUGGESTIONS —
        # primary_category stays NULL until a human confirms in triage, so
        # this event cannot enter a scoring pool (pillar queries filter on
        # primary_category, migration 062).
        direction = _direction_for(event_subtype)
        # Audit F6: unknown intervention types fall back to
        # DEFAULT_GTA_CATEGORY silently — mark them so the triage UI can
        # sort uncertain rows first.
        metadata["category_mapping"] = (
            "explicit" if intervention_type in GTA_INTERVENTION_CATEGORY_MAP
            else "fallback"
        )
        metadata["n_materials"] = len({m for m, _, _ in resolved_codes})
        # Audit F1 (decided 2026-07-31, Nicole): supportive events —
        # subsidies that mostly ADD supply — land as display_only rather
        # than queueing for scoring triage.  Still visible, still reviewable,
        # promotable to scoring in the UI; just not narrated as risk by
        # default.  Recorded in metadata so the routing is auditable.
        if direction == "supportive":
            triage_status = "display_only"
            metadata["triage_route"] = "auto_display_only_supportive"
        else:
            triage_status = "pending_triage"

        if dry_run:
            # All gates passed — this row would insert.  Counted and skipped
            # before any write so the dry run leaves no trace.
            inserted += 1
            continue

        # 2026-07-30: events point at their own per-intervention document
        # (carrying the permalink), not the per-run bulk document.
        intervention_doc_id = _get_or_create_intervention_document(
            session,
            source_id=source_id,
            gta_id=gta_id,
            state_act_id=state_act_id,
            title=title,
            event_date=event_date,
            run_document_id=run_document_id,
        )

        event = RiskEvent(
            source_document_id=intervention_doc_id,
            event_type=intervention_type or "trade_intervention",
            event_subtype=event_subtype,  # typed col (migration 040); None when no subtype maps
            event_date=event_date,
            title=title,
            summary=summary,
            severity_score=severity,
            # confidence_override (2026-06-11): API path computes a
            # per-event confidence from is_official_source + source_count +
            # inferred_jurisdictions.  CSV path passes None → legacy 0.9
            # default preserved.
            confidence_score=(confidence_override if confidence_override is not None else 0.9),
            risk_categories_json=[risk_category],
            # Inverted 2026-07-31 (triage plan Phase 1): the machine
            # suggests, a human assigns.  primary_category deliberately
            # NOT set — see the suggestion block above.
            suggested_category=risk_category,
            direction=direction,
            triage_status=triage_status,
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

        # ── Breadth discount + direct/broad flag (migration 056) ────────
        # An intervention tagged to many materials via a long HS list is,
        # per material, weak evidence: 43% of GTA events are single-
        # material, but omnibus measures carry 13-34 tags and previously
        # counted at full relevance on every one of them.  n <= 3 keeps
        # full weight and is_direct=True (material pages show these);
        # broader events get relevance * 3/n and route to the cross-
        # material industry-events surface only.  Same values as the
        # migration-056 backfill, so re-ingests are idempotent.
        _n_materials = len(material_best_rel)
        _breadth = min(1.0, 3.0 / _n_materials) if _n_materials else 1.0
        _is_direct = _n_materials <= 3

        for material_id, relevance in material_best_rel.items():
            session.add(
                RiskEventMaterial(
                    risk_event_id=event.id,
                    material_id=material_id,
                    relevance_score=relevance * _breadth,
                    is_direct=_is_direct,
                    match_reason="hs_code",
                    # 2026-07-31 (triage plan Phase 1): machine-written
                    # links are suggestions until confirmed in triage.
                    status="suggested",
                )
            )
            material_links_created += 1
        for hs_mapping_id, relevance in hs_mapping_best_rel.items():
            session.add(
                RiskEventHsMapping(
                    risk_event_id=event.id,
                    hs_mapping_id=hs_mapping_id,
                    relevance_score=relevance * _breadth,
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
                refreshed_so_far=refreshed_existing,
                skipped_so_far=skipped_existing,
            )
            pending_in_batch = 0

    if dry_run:
        # The parse/classify pass may still have created the Source and the
        # per-run document via flush — roll everything back so a dry run
        # leaves the database exactly as it found it.
        session.rollback()
        log.info("gta.ingest.dry_run_rolled_back")
    else:
        session.commit()

    result = {
        "downloaded_rows": len(interventions),
        "inserted": inserted,
        # Added 2026-07-30 (repair re-ingest):
        "refreshed_existing": refreshed_existing,
        "refresh_field_counts": refresh_field_counts,
        "dry_run": 1 if dry_run else 0,
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
