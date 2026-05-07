"""USGS Mineral Resources Data System (MRDS) ingester.

Ingests mine and processing facility data from the USGS MRDS dataset into the
``facilities`` and ``facility_material_links`` tables.

Data source
-----------
USGS Mineral Resources Data System — free, global, updated periodically.

Download the full CSV from:
  https://mrdata.usgs.gov/mrds/mrds-csv.zip

Unzip and pass the inner ``mrds.csv`` to:
  bdi-ingest ingest-mrds --local-file /path/to/mrds.csv

Alternatively, download a commodity-filtered subset via the MRDS web interface:
  https://mrdata.usgs.gov/mrds/

The full CSV is ~170 MB uncompressed and contains ~300,000 records worldwide.
The ingester filters to battery-critical commodities during processing so you
can safely pass the full file — unrecognised commodity rows are skipped.

MRDS data quality notes
-----------------------
- ``dev_stat`` (development status) reflects the last known status, which may
  be decades old for inactive prospects. Trust "Producer" and "Past Producer"
  more than "Prospect" for operational scoring.
- Coordinates are present for ~85% of records; the rest are skipped.
- Country field uses full English names ("United States", "Chile"), not ISO2
  codes. The ingester resolves these via a name→ISO2 lookup table.
- Commodity codes use MRDS shorthand (Li, Co, Ni, Cu, Mn, Gr). Multiple
  commodities per deposit are in separate ``commod1``/``commod2``/``commod3``
  columns (not a comma-separated list like GEM).
- ``dep_id`` is the stable MRDS deposit record ID — used as dedup key.

Deduplication
-------------
Primary dedup key: ``mrds_dep_id`` (MRDS ``dep_id`` field).
Fallback: coordinate proximity (0.05°) + facility_type + country, for any
rows where dep_id is absent (rare).

On re-run: existing rows matched by mrds_dep_id have mutable fields updated
(status, capacity, coordinates). New rows are inserted.

FacilityMaterialLink
--------------------
One row per (facility, material) pair. ``commod1`` → ``is_primary_product=True``;
``commod2`` and ``commod3`` → ``is_primary_product=False`` (by-products).

Capacity
--------
MRDS does not publish annual capacity figures — ``annual_capacity_tpy`` will
be NULL for all MRDS-sourced rows. The operational scoring pillar handles
NULL capacity by counting facilities (not weighting by tonnage) when capacity
data is absent. This is a known limitation; capacity data can be enriched
manually or via a future USGS MCS integration.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from pathlib import Path
from typing import Optional

import httpx
import structlog
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.facility import Facility, FacilityMaterialLink
from app.models.supply import Material
from app.services.ingestion.material_resolver import MaterialAliasResolver

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# MRDS commodity name → Material.canonical_name
#
# MRDS uses full English commodity names in commod1/2/3 columns (e.g.
# "Lithium", "Cobalt", "Tin") — not chemical-symbol abbreviations.
# Short abbreviations are retained as fallbacks for filtered MRDS exports
# that may use codes instead of full names.
# Values are lowercased before lookup. Unknown names are silently skipped.
# ---------------------------------------------------------------------------

MRDS_COMMODITY_MAP: dict[str, str] = {
    # ── Lithium ──────────────────────────────────────────────────────────────
    "lithium":                      "Lithium",
    "lithium carbonate":            "Lithium",
    "lithium brine":                "Lithium",
    # abbreviation fallbacks
    "li":                           "Lithium",
    "lith":                         "Lithium",

    # ── Cobalt ───────────────────────────────────────────────────────────────
    "cobalt":                       "Cobalt",
    "co":                           "Cobalt",
    "cob":                          "Cobalt",

    # ── Nickel ───────────────────────────────────────────────────────────────
    "nickel":                       "Nickel",
    "ni":                           "Nickel",
    "nick":                         "Nickel",

    # ── Manganese ────────────────────────────────────────────────────────────
    "manganese":                    "Manganese",
    "mn":                           "Manganese",
    "mang":                         "Manganese",

    # ── Natural Graphite ─────────────────────────────────────────────────────
    "graphite":                     "Natural Graphite",
    "natural graphite":             "Natural Graphite",
    "graphite, natural":            "Natural Graphite",
    "carbon, graphite":             "Natural Graphite",
    "gr":                           "Natural Graphite",
    "graph":                        "Natural Graphite",

    # ── Copper ───────────────────────────────────────────────────────────────
    "copper":                       "Copper",
    "cu":                           "Copper",
    "copp":                         "Copper",

    # ── Aluminum ─────────────────────────────────────────────────────────────
    "aluminum":                     "Aluminum",
    "aluminium":                    "Aluminum",
    "bauxite":                      "Aluminum",
    "alumina":                      "Aluminum",
    "al":                           "Aluminum",

    # ── Rare Earth Elements (aggregate) ──────────────────────────────────────
    "rare earths":                  "Rare Earth Elements",
    "rare-earth elements":          "Rare Earth Elements",
    "rare earth elements":          "Rare Earth Elements",
    "rare earth metals":            "Rare Earth Elements",
    "rare earth":                   "Rare Earth Elements",
    "ree":                          "Rare Earth Elements",
    "reo":                          "Rare Earth Elements",
    # Light REEs without individual rows in materials table
    "lanthanum":                    "Rare Earth Elements",
    "cerium":                       "Rare Earth Elements",
    "yttrium":                      "Rare Earth Elements",
    "europium":                     "Rare Earth Elements",
    "gadolinium":                   "Rare Earth Elements",
    "holmium":                      "Rare Earth Elements",
    "erbium":                       "Rare Earth Elements",
    "ytterbium":                    "Rare Earth Elements",
    "lutetium":                     "Rare Earth Elements",
    "scandium":                     "Rare Earth Elements",
    "samarium":                     "Rare Earth Elements",
    "la":                           "Rare Earth Elements",
    "ce":                           "Rare Earth Elements",
    "y":                            "Rare Earth Elements",
    "re":                           "Rare Earth Elements",

    # ── Individual magnet REEs (have their own materials table rows) ──────────
    "neodymium":                    "Neodymium",
    "nd":                           "Neodymium",
    "praseodymium":                 "Praseodymium",
    "pr":                           "Praseodymium",
    "dysprosium":                   "Dysprosium",
    "dy":                           "Dysprosium",
    "terbium":                      "Terbium",
    "tb":                           "Terbium",

    # ── Vanadium ─────────────────────────────────────────────────────────────
    "vanadium":                     "Vanadium",
    "v":                            "Vanadium",

    # ── Silicon (Anode Grade) ─────────────────────────────────────────────────
    "silicon":                      "Silicon (Anode Grade)",
    "silicon metal":                "Silicon (Anode Grade)",
    "si":                           "Silicon (Anode Grade)",

    # ── Phosphate (Battery Grade) ─────────────────────────────────────────────
    "phosphate":                    "Phosphate (Battery Grade)",
    "phosphorite":                  "Phosphate (Battery Grade)",
    "phosphate rock":               "Phosphate (Battery Grade)",
    "p":                            "Phosphate (Battery Grade)",

    # ── Chromium ─────────────────────────────────────────────────────────────
    "chromium":                     "Chromium",
    "chromite":                     "Chromium",
    "cr":                           "Chromium",

    # ── Molybdenum ───────────────────────────────────────────────────────────
    "molybdenum":                   "Molybdenum",
    "mo":                           "Molybdenum",

    # ── Niobium ──────────────────────────────────────────────────────────────
    "niobium":                      "Niobium",
    "columbium":                    "Niobium",       # historical US name
    "niobium (columbium)":          "Niobium",
    "nb":                           "Niobium",

    # ── Tantalum ─────────────────────────────────────────────────────────────
    "tantalum":                     "Tantalum",
    "ta":                           "Tantalum",

    # ── Titanium ─────────────────────────────────────────────────────────────
    "titanium":                     "Titanium",
    "ilmenite":                     "Titanium",
    "rutile":                       "Titanium",
    "ti":                           "Titanium",

    # ── Zirconium ────────────────────────────────────────────────────────────
    "zirconium":                    "Zirconium",
    "zircon":                       "Zirconium",
    "zr":                           "Zirconium",

    # ── Iron Ore (LFP Grade) ─────────────────────────────────────────────────
    "iron":                         "Iron Ore (LFP Grade)",
    "iron ore":                     "Iron Ore (LFP Grade)",
    "fe":                           "Iron Ore (LFP Grade)",

    # ── Magnesium ────────────────────────────────────────────────────────────
    "magnesium":                    "Magnesium",
    "magnesite":                    "Magnesium",
    "mg":                           "Magnesium",

    # ── Platinum-Group Metals ─────────────────────────────────────────────────
    "platinum":                     "Platinum-Group Metals",
    "palladium":                    "Platinum-Group Metals",
    "platinum-group metals":        "Platinum-Group Metals",
    "platinum group metals":        "Platinum-Group Metals",
    "pgm":                          "Platinum-Group Metals",
    "rhodium":                      "Platinum-Group Metals",
    "iridium":                      "Platinum-Group Metals",
    "osmium":                       "Platinum-Group Metals",
    "ruthenium":                    "Platinum-Group Metals",
    "pt":                           "Platinum-Group Metals",
    "pd":                           "Platinum-Group Metals",

    # ── Tungsten ─────────────────────────────────────────────────────────────
    "tungsten":                     "Tungsten",
    "wolframite":                   "Tungsten",
    "scheelite":                    "Tungsten",
    "w":                            "Tungsten",

    # ── Tin ──────────────────────────────────────────────────────────────────
    "tin":                          "Tin",
    "sn":                           "Tin",

    # ── Silver ───────────────────────────────────────────────────────────────
    "silver":                       "Silver",
    "ag":                           "Silver",

    # ── Fluorspar ────────────────────────────────────────────────────────────
    "fluorspar":                    "Fluorspar",
    "fluorite":                     "Fluorspar",
    "fluorine":                     "Fluorspar",
    "f":                            "Fluorspar",

    # ── Antimony ─────────────────────────────────────────────────────────────
    "antimony":                     "Antimony",
    "sb":                           "Antimony",

    # ── Zinc ─────────────────────────────────────────────────────────────────
    "zinc":                         "Zinc",
    "zn":                           "Zinc",

    # ── Rhenium ──────────────────────────────────────────────────────────────
    "rhenium":                      "Rhenium",

    # ── Gallium ──────────────────────────────────────────────────────────────
    "gallium":                      "Gallium",
    "ga":                           "Gallium",

    # ── Germanium ────────────────────────────────────────────────────────────
    "germanium":                    "Germanium",
    "ge":                           "Germanium",

    # ── Tellurium ────────────────────────────────────────────────────────────
    "tellurium":                    "Tellurium",
    "te":                           "Tellurium",

    # ── Indium ───────────────────────────────────────────────────────────────
    "indium":                       "Indium",
    "in":                           "Indium",

    # ── Boron ────────────────────────────────────────────────────────────────
    "boron":                        "Boron",
    "borax":                        "Boron",
    "borate":                       "Boron",
    "b":                            "Boron",

    # ── Selenium ─────────────────────────────────────────────────────────────
    "selenium":                     "Selenium",
    "se":                           "Selenium",

    # ── Bismuth ──────────────────────────────────────────────────────────────
    "bismuth":                      "Bismuth",
    "bi":                           "Bismuth",
}

# ---------------------------------------------------------------------------
# MRDS dev_stat → Facility.status
#
# MRDS development status values as they appear in the dataset.
# ---------------------------------------------------------------------------

MRDS_STATUS_MAP: dict[str, str] = {
    "producer":               "operating",
    "active":                 "operating",
    "operating":              "operating",
    "past producer":          "closed",
    "historical":             "closed",
    "closed":                 "closed",
    "abandoned":              "closed",
    "reclaimed":              "closed",
    "prospect":               "planned",
    "occurrence":             "planned",
    "exploration":            "planned",
    "development":            "under_construction",
    "construction":           "under_construction",
    "permitted":              "planned",
    "plant":                  "operating",    # processing plant, treat as operating
    "inactive":               "mothballed",
    "care and maintenance":   "mothballed",
    "suspended":              "mothballed",
    "on hold":                "mothballed",
}

# ---------------------------------------------------------------------------
# MRDS oper_type → Facility.facility_type
# ---------------------------------------------------------------------------

# Updated 2026-05-06 against actual MRDS data distribution.  The previous
# version listed values like "smelter" / "refinery" / "mill" / "concentrator"
# that NEVER appear in the real ``oper_type`` column — those were dead-code
# fallbacks.  Real distinct values found in the 304k-row file are:
#   surface, underground, surface-underground, placer, processing plant,
#   well, offshore, geothermal, brine operation, leach.
# ``smelter`` / ``refinery`` / ``concentrator`` now live in ``_NAME_KEYWORDS``
# below (the name-keyword fallback path) since they only ever appear in
# ``site_name`` / ``names`` text, not in oper_type.
MRDS_OPER_TYPE_MAP: dict[str, str] = {
    # ── Mining (→ "mine" facility_type) ────────────────────────────────
    "surface":              "mine",
    "underground":          "mine",
    "surface-underground":  "mine",
    "open pit":             "mine",
    "placer":               "mine",
    "brine":                "mine",
    "brine operation":      "mine",
    "solution":             "mine",     # solution mining (lithium brine)
    "in-situ":              "mine",
    "dredge":               "mine",
    "alluvial":             "mine",
    "quarry":               "mine",
    "well":                 "mine",     # extraction well (Li / B brine, geothermal)
    "offshore":             "mine",     # offshore extraction
    "geothermal":           "mine",     # geothermal brine for Li / B
    # ── Processing — granular facility_types (added 2026-05-06) ────────
    # These don't appear directly in oper_type but the name-keyword
    # fallback synthesises them, and the map below routes each to a
    # distinct facility_type so FACILITY_TYPE_TO_STAGE can hand out
    # different stages.
    "concentrator":         "concentrator",   # produces concentrate from ore
    "flotation":            "concentrator",
    "mill":                 "concentrator",   # mills typically = concentrators
    "smelter":              "smelter",        # produces matte / raw metal
    "smelting":             "smelter",
    "refinery":             "refinery",       # produces refined metal
    "refining":             "refinery",
    "leach":                "leach",          # hydromet — varies, conservative
    "processing plant":     "processing",     # ambiguous — keep conservative
    "processing":           "processing",
    "plant":                "processing",
}

# ---------------------------------------------------------------------------
# Facility.facility_type → FacilityMaterialLink.supply_chain_stage
# ---------------------------------------------------------------------------
# Granularity expansion (2026-05-06): the previous version collapsed every
# processing facility to ``intermediate`` regardless of type.  Now uses the
# distinct facility_types from MRDS_OPER_TYPE_MAP / _NAME_KEYWORDS to write
# a meaningful stage that the Material Concentration pillar's stage-rollup
# (STAGE_ROLLUP_WEIGHTS in market_aggregator.py) can use.
#
# hs_mapping_id is left NULL for all MRDS rows — manual confirmation required.

FACILITY_TYPE_TO_STAGE: dict[str, str] = {
    "mine":          "ore",
    "concentrator":  "concentrate",     # mills + concentrators — STAGE_ROLLUP_WEIGHTS 0.15
    "smelter":       "intermediate",    # produces matte / raw metal
    "refinery":      "refined",         # produces refined metal — STAGE_ROLLUP_WEIGHTS 0.25
    "leach":         "intermediate",    # ambiguous; conservative
    "processing":    "intermediate",    # ambiguous; conservative
}

# ---------------------------------------------------------------------------
# Name-keyword fallback for unknown ``oper_type``
# ---------------------------------------------------------------------------
# 48% of MRDS rows have oper_type empty or "unknown"; ~31k of the active
# subset (Producer / Past Producer / Plant) carry classifying keywords in
# ``site_name`` or ``names``.  This list runs in order — more specific terms
# first — so e.g. "Quarry and Plant" classifies as a quarry, not a plant.
#
# Keywords return values compatible with MRDS_OPER_TYPE_MAP keys above so the
# fallback feeds the same downstream pipeline.
#
# False-positive notes from real data:
#   * "Smelter Knolls South Borrow Pit" — "Smelter Knolls" is a place name,
#     not a smelter.  Accepted (single-digit FP volume).
#   * Bare "leach" hits person-name surnames ("Hanks-Miller-Leach Group") —
#     restricted here to the qualified forms ("heap leach", "leach plant").
#   * "Concentrator Mine" — both keywords appear; "concentrator" wins via
#     ordering.  Real volume is tiny (<20 rows); accepted.
_NAME_KEYWORDS_ORDERED: list[tuple[str, str]] = [
    # Refining (highest stage) — small absolute count but real signal
    ("refinery",            "refinery"),
    ("refining",            "refinery"),
    # Smelting
    ("smelter",             "smelter"),
    ("smelting",            "smelter"),
    # Concentrators (specific qualified forms first)
    ("concentrator",        "concentrator"),
    ("flotation",           "concentrator"),
    ("concentrating mill",  "concentrator"),
    # Leach plant — only qualified forms (bare "leach" hits surnames)
    ("heap leach",          "leach"),
    ("leach plant",         "leach"),
    ("leach pad",           "leach"),
    # Mining-side keywords — checked BEFORE generic mill/plant so
    # "Quarry and Plant" classifies as quarry, not plant.
    ("quarry",              "quarry"),
    ("open-pit",            "open pit"),
    ("open pit",            "open pit"),
    ("strip mine",          "surface"),
    ("placer",              "placer"),
    ("dredge",              "dredge"),
    ("alluvial",            "alluvial"),
    ("shaft",               "underground"),
    # Generic processing — checked AFTER mining keywords on purpose
    ("processing plant",    "processing plant"),
    ("processing facility", "processing plant"),
    ("mill",                "mill"),       # mills are usually concentrators
    ("plant",               "plant"),      # ambiguous; routed to "processing"
    # Generic mine + pit keywords last (most permissive)
    ("pit",                 "open pit"),   # 6k hits, mostly real mining pits
    ("mine",                "surface"),    # default mine type when unclear
]


def _classify_from_name(
    site_name: Optional[str],
    names: Optional[str],
) -> Optional[str]:
    """Infer an MRDS_OPER_TYPE_MAP-compatible value from facility name text.

    ``site_name`` is the authoritative facility name; ``names`` is the
    operator/owner — used only as fallback because operator names can
    contain misleading historical / corporate terms.  Stops at first
    keyword match (more specific terms come first in
    ``_NAME_KEYWORDS_ORDERED``).

    Returns None if neither field contains a recognised keyword.
    """
    import re as _re

    candidates: list[str] = []
    if site_name:
        candidates.append(site_name.lower())
    if names:
        candidates.append(names.lower())
    if not candidates:
        return None

    # Word-boundary regex per keyword to avoid matching inside other words
    # ("mine" inside "minerals", "pit" inside "Pittsburgh", etc.).
    for kw, oper_type in _NAME_KEYWORDS_ORDERED:
        pattern = _re.compile(r"\b" + _re.escape(kw) + r"\b", _re.IGNORECASE)
        for text in candidates:
            if pattern.search(text):
                return oper_type
    return None

# ---------------------------------------------------------------------------
# Country full name → ISO2 (MRDS uses full English names)
# ---------------------------------------------------------------------------

_MRDS_COUNTRY_OVERRIDES: dict[str, str] = {
    # MRDS / legacy spellings that may not exist in countries.common_names.
    "congo, dem. rep.": "CD",
    "congo, democratic republic of the": "CD",
    "democratic republic of the congo": "CD",
    "drc": "CD",
    "turkiye": "TR",
    "viet nam": "VN",
    "korea, republic of": "KR",
    "united states of america": "US",
    "usa": "US",
}

# Battery-critical commodity codes — used to pre-filter the full MRDS dataset
# before any DB lookups, keeping memory usage manageable.
_TARGET_COMMODITIES: frozenset[str] = frozenset(MRDS_COMMODITY_MAP.keys())

# MRDS full-dataset download URL (zip containing mrds.csv)
MRDS_CSV_ZIP_URL = "https://mrdata.usgs.gov/mrds/mrds-csv.zip"


# ---------------------------------------------------------------------------
# Country resolution
# ---------------------------------------------------------------------------

def _build_country_map(session: Session) -> dict[str, str]:
    """Build a lowercase name/alias → ISO2 lookup from the countries table.

    Mirrors the pattern used by other ingestors (e.g. the MCS PDF parser) so we
    don't maintain a giant hardcoded mapping. MRDS-specific edge spellings live
    in `_MRDS_COUNTRY_OVERRIDES`.
    """
    from app.models.country import Country  # noqa: PLC0415

    result: dict[str, str] = {}
    rows = session.execute(
        select(Country.iso2, Country.name, Country.common_names)
    ).all()

    for iso2, canonical, common_names in rows:
        if isinstance(canonical, str) and canonical:
            result[canonical.lower()] = iso2
        if isinstance(iso2, str) and iso2:
            result[iso2.lower()] = iso2
        if common_names and isinstance(common_names, list):
            for alias in common_names:
                if isinstance(alias, str) and alias:
                    result[alias.lower()] = iso2

    result.update(_MRDS_COUNTRY_OVERRIDES)
    return result


def _resolve_country(raw: str, country_map: dict[str, str]) -> Optional[str]:
    """Return ISO2 from a MRDS country string.

    MRDS uses full English names. Returns None for unresolvable strings
    so the caller can skip the row rather than assign a wrong country.
    """
    if not raw:
        return None
    stripped = raw.strip()
    # Some MRDS rows already use ISO2
    if len(stripped) == 2 and stripped.isalpha():
        return stripped.upper()
    return country_map.get(stripped.lower())


# ---------------------------------------------------------------------------
# Commodity resolution — handles commod1/2/3 columns
# ---------------------------------------------------------------------------

def _resolve_commodities(
    row: dict,
    *,
    resolver: MaterialAliasResolver,
) -> list[str]:
    """Extract canonical material names from MRDS commod1/2/3 columns.

    Primary path: resolve via ``material_source_aliases`` using
    ``MaterialAliasResolver`` under ``source_system="mrds"`` (consistent with
    other ingestors).

    Fallback path: legacy ``MRDS_COMMODITY_MAP`` dict for deployments that
    haven't seeded MRDS aliases yet.

    Returns list preserving order: commod1 first (primary), then co-products.
    Skips unrecognised commodity values without raising.
    """
    result: list[str] = []
    for col in ("commod1", "commod2", "commod3"):
        raw_original = str(row.get(col, "") or "").strip()
        raw = raw_original.lower()
        if not raw:
            continue

        # Prefer DB-backed aliases.
        alias_result = resolver.resolve("mrds", raw_original)
        if alias_result.status == "ok" and alias_result.material is not None:
            canonical = alias_result.material.canonical_name
            if canonical not in result:
                result.append(canonical)
            continue
        if alias_result.status == "skipped":
            continue

        # Legacy fallback (pre-alias seeding).
        canonical = MRDS_COMMODITY_MAP.get(raw)
        if canonical and canonical not in result:
            result.append(canonical)
    return result


# ---------------------------------------------------------------------------
# Row pre-filter — skip rows with no battery-critical commodity
# (avoids loading 300k records into memory)
# ---------------------------------------------------------------------------

def _has_target_commodity(row: dict) -> bool:
    for col in ("commod1", "commod2", "commod3"):
        val = str(row.get(col, "") or "").strip().lower()
        if val in _TARGET_COMMODITIES:
            return True
    return False


# ---------------------------------------------------------------------------
# Main ingestion function
# ---------------------------------------------------------------------------

def ingest_mrds(
    session: Session,
    local_file: Optional[str] = None,
    batch_size: int = 500,
) -> dict:
    """Ingest USGS MRDS mine data into facilities + facility_material_links.

    Parameters
    ----------
    session:
        Active SQLAlchemy session. The ingester commits every ``batch_size``
        matched rows so progress is preserved if the connection drops.
    local_file:
        Path to the MRDS CSV file or ZIP. If omitted the ingester downloads
        directly from USGS. The CSV is ~170 MB; rows are streamed one at a
        time rather than loaded into memory all at once.
    batch_size:
        Number of matched rows between commits. Lower values persist progress
        more frequently at the cost of slightly more DB round-trips.
        Default 500 (commit roughly every ~500 battery-relevant rows found).

    Returns
    -------
    dict with keys: facilities_inserted, facilities_updated, links_inserted,
    links_updated, skipped_no_country, skipped_no_commodity, skipped_no_coords,
    rows_scanned, rows_matched.
    """
    rows = _iter_rows(local_file)

    # Pre-load material name → id map once
    material_map: dict[str, int] = {
        name: mid
        for name, mid in session.execute(
            select(Material.canonical_name, Material.id)
        ).all()
    }

    # Pre-load country aliases → ISO2 once (MRDS uses full names, not ISO2)
    country_map = _build_country_map(session)

    # Pre-load material source aliases for MRDS name resolution
    resolver = MaterialAliasResolver(session)

    facilities_inserted  = 0
    facilities_updated   = 0
    links_inserted       = 0
    links_updated        = 0
    skipped_no_country         = 0
    skipped_no_commodity       = 0
    skipped_no_coords          = 0
    skipped_planned            = 0
    skipped_closed             = 0     # added 2026-05-06 — past producers etc.
    rows_inferred_from_name    = 0     # facility_type recovered from site_name/names
    rows_scanned               = 0
    rows_matched               = 0
    link_material_ids_by_facility: dict[uuid.UUID, set[int]] = {}
    pending_links: dict[tuple[uuid.UUID, int], FacilityMaterialLink] = {}

    for row in rows:
        rows_scanned += 1

        # Pre-filter: skip rows with no battery-critical commodity
        if not _has_target_commodity(row):
            continue

        rows_matched += 1

        # ── dep_id ────────────────────────────────────────────────────────
        dep_id = str(row.get("dep_id", "") or "").strip() or None

        # ── Country ───────────────────────────────────────────────────────
        country = _resolve_country(str(row.get("country", "") or ""), country_map)
        if not country:
            log.debug(
                "mrds.skipped_no_country",
                dep_id=dep_id,
                raw_country=row.get("country"),
            )
            skipped_no_country += 1
            continue

        # ── Commodities ───────────────────────────────────────────────────
        canonical_materials = _resolve_commodities(row, resolver=resolver)
        if not canonical_materials:
            skipped_no_commodity += 1
            continue

        # ── Coordinates ───────────────────────────────────────────────────
        try:
            lat = float(str(row.get("latitude", "") or "").strip())
            lon = float(str(row.get("longitude", "") or "").strip())
            if lat == 0.0 and lon == 0.0:
                raise ValueError("zero coordinates")
        except (ValueError, TypeError):
            # Skip rows without reliable coordinates — can't dedup or map them
            skipped_no_coords += 1
            continue

        # ── Status (filter early — skip rows scoring won't use) ──────────
        status_raw = str(row.get("dev_stat", "") or "").lower().strip()
        status = MRDS_STATUS_MAP.get(status_raw, "planned")
        # Skip:
        #   * "planned" — prospects, occurrences, exploration, permitted.
        #     These are mineral occurrences and speculative entries; not
        #     producing capacity.  ~150k rows of the 304k file.
        #   * "closed"  — past producer / historical / abandoned / reclaimed.
        #     ~128k rows.  Long-shuttered mines don't add operational
        #     scoring signal — the operational pillar measures CURRENT
        #     productive capacity.  Added 2026-05-06 per partner direction.
        # Keep:
        #   * "operating"          — Producer, active, plant
        #   * "mothballed"         — care/maintenance, suspended, on hold
        #     (could restart — real signal for operational risk)
        #   * "under_construction" — development, construction
        if status == "planned":
            skipped_planned += 1
            continue
        if status == "closed":
            skipped_closed += 1
            continue

        # ── Region and mine name (read before facility_type for the
        #     name-keyword fallback below) ────────────────────────────────
        region   = str(row.get("state", "") or "").strip() or None
        mine_name = str(row.get("site_name", "") or "").strip() or None
        reporter = str(row.get("names", "") or "").strip() or None

        # ── facility_type — with name-keyword fallback (2026-05-06) ─────
        # 48% of MRDS rows have empty or "unknown" oper_type but ~47% of
        # those carry classifying keywords in site_name / names.  Recover
        # that signal before falling back to the conservative "mine"
        # default.
        oper_raw = str(row.get("oper_type", "") or "").lower().strip()
        if not oper_raw or oper_raw == "unknown":
            inferred = _classify_from_name(mine_name, reporter)
            if inferred:
                oper_raw = inferred
                rows_inferred_from_name += 1
        facility_type = MRDS_OPER_TYPE_MAP.get(oper_raw, "mine")

        metadata: dict = {
            "source": "mrds",
            "dep_id": dep_id,
        }
        if reporter:
            metadata["mrds_names"] = reporter[:256]   # cap length
        raw_commod = "/".join(
            str(row.get(c, "") or "").strip()
            for c in ("commod1", "commod2", "commod3")
            if row.get(c)
        )
        if raw_commod:
            metadata["mrds_commodities"] = raw_commod

        # ── Find or create Facility ───────────────────────────────────────
        facility: Optional[Facility] = None

        if dep_id:
            facility = session.scalar(
                select(Facility).where(Facility.mrds_dep_id == dep_id)
            )

        # Coordinate proximity fallback (~5 km tolerance)
        if facility is None:
            lat_r = round(lat, 2)
            lon_r = round(lon, 2)
            facility = session.scalar(
                select(Facility).where(
                    and_(
                        Facility.facility_type == facility_type,
                        Facility.country == country,
                        Facility.latitude.between(lat_r - 0.05, lat_r + 0.05),
                        Facility.longitude.between(lon_r - 0.05, lon_r + 0.05),
                    )
                )
            )

        # facility_type is mutable now (added 2026-05-06) so re-running
        # ingest after the granularity expansion + name-keyword fallback
        # reclassifies existing rows.  Without this, old rows would keep
        # their pre-fix facility_type values and the new logic would only
        # affect first-time inserts.
        mutable = {
            "name":           mine_name,
            "status":         status,
            "facility_type":  facility_type,
            "latitude":       lat,
            "longitude":      lon,
            "region":         region,
            "data_source":    "mrds",
            "metadata_json":  metadata,
        }

        if facility is None:
            facility = Facility(
                id=uuid.uuid4(),
                country=country,
                mrds_dep_id=dep_id,
                **mutable,   # includes facility_type (added 2026-05-06)
            )
            session.add(facility)
            session.flush()
            facilities_inserted += 1
        else:
            changed = [f for f, v in mutable.items() if getattr(facility, f) != v]
            for f in changed:
                setattr(facility, f, mutable[f])
            if dep_id and facility.mrds_dep_id is None:
                facility.mrds_dep_id = dep_id
                changed.append("mrds_dep_id")
            if changed:
                facilities_updated += 1

        # ── FacilityMaterialLink upsert ───────────────────────────────────
        linked_material_ids = link_material_ids_by_facility.get(facility.id)
        if linked_material_ids is None:
            linked_material_ids = set(
                session.scalars(
                    select(FacilityMaterialLink.material_id).where(
                        FacilityMaterialLink.facility_id == facility.id
                    )
                ).all()
            )
            link_material_ids_by_facility[facility.id] = linked_material_ids

        for idx, mat_name in enumerate(canonical_materials):
            material_id = material_map.get(mat_name)
            if material_id is None:
                log.debug(
                    "mrds.material_not_in_db",
                    canonical_name=mat_name,
                    dep_id=dep_id,
                )
                continue

            is_primary = (idx == 0)

            if material_id not in linked_material_ids:
                new_link = FacilityMaterialLink(
                    facility_id=facility.id,
                    material_id=material_id,
                    annual_capacity_tpy=None,   # MRDS does not publish capacity
                    is_primary_product=is_primary,
                    supply_chain_stage=FACILITY_TYPE_TO_STAGE.get(facility_type),
                    hs_mapping_id=None,         # requires manual confirmation
                )
                session.add(new_link)
                pending_links[(facility.id, material_id)] = new_link
                linked_material_ids.add(material_id)
                links_inserted += 1
            else:
                existing_link = session.scalar(
                    select(FacilityMaterialLink).where(
                        and_(
                            FacilityMaterialLink.facility_id == facility.id,
                            FacilityMaterialLink.material_id == material_id,
                        )
                    )
                )
                if existing_link is None:
                    existing_link = pending_links.get((facility.id, material_id))
                    if existing_link is None:
                        continue
                changed = False
                if existing_link.is_primary_product != is_primary:
                    existing_link.is_primary_product = is_primary
                    changed = True
                # Re-stage existing links so the granularity expansion +
                # name-keyword fallback (added 2026-05-06) take effect on
                # rows ingested before this fix.  Previously stage was
                # write-once; rerunning ingest-mrds left old rows on the
                # original conservative classification.
                expected_stage = FACILITY_TYPE_TO_STAGE.get(facility_type)
                if existing_link.supply_chain_stage != expected_stage:
                    existing_link.supply_chain_stage = expected_stage
                    changed = True
                if changed:
                    links_updated += 1

        # Commit every batch_size matched rows so progress survives disconnects.
        # Clear pending_links after each commit — those ORM objects are expired
        # and won't be needed again (each dep_id is unique within a run).
        if rows_matched % batch_size == 0:
            session.commit()
            pending_links.clear()
            log.info(
                "mrds.batch_committed",
                rows_scanned=rows_scanned,
                rows_matched=rows_matched,
                facilities_inserted=facilities_inserted,
                facilities_updated=facilities_updated,
                skipped_planned=skipped_planned,
                skipped_closed=skipped_closed,
                rows_inferred_from_name=rows_inferred_from_name,
            )

    # Final commit for the last partial batch
    session.commit()

    result = {
        "rows_scanned":            rows_scanned,
        "rows_matched":            rows_matched,
        "facilities_inserted":     facilities_inserted,
        "facilities_updated":      facilities_updated,
        "links_inserted":          links_inserted,
        "links_updated":           links_updated,
        "skipped_no_country":      skipped_no_country,
        "skipped_no_commodity":    skipped_no_commodity,
        "skipped_no_coords":       skipped_no_coords,
        "skipped_planned":         skipped_planned,
        # Added 2026-05-06 (N4 audit fix):
        "skipped_closed":          skipped_closed,
        "rows_inferred_from_name": rows_inferred_from_name,
    }
    log.info("mrds.done", **result)
    return result


# ---------------------------------------------------------------------------
# File / stream loading — all return generators to avoid loading 300k rows
# into memory at once. ZIP files must be decompressed upfront (zipfile does
# not support true streaming decompression), but the DictReader still yields
# rows one at a time rather than materialising a full list.
# ---------------------------------------------------------------------------

from typing import Generator

def _iter_rows(local_file: Optional[str]) -> Generator[dict, None, None]:
    """Yield CSV rows one at a time. Downloads from USGS if no local file given."""
    if local_file:
        path = Path(local_file)
        if not path.exists():
            raise FileNotFoundError(f"MRDS file not found: {path}")
        suffix = path.suffix.lower()
        if suffix == ".zip":
            yield from _iter_zip(path)
        else:
            yield from _iter_csv_path(path)
    else:
        log.info(
            "mrds.downloading",
            url=MRDS_CSV_ZIP_URL,
            note="Downloading ~25 MB zip; will stream rows after decompression",
        )
        yield from _download_and_iter()


def _iter_csv_path(path: Path) -> Generator[dict, None, None]:
    """Stream rows from a plain CSV file without loading it into memory."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f)


def _iter_zip(path: Path) -> Generator[dict, None, None]:
    """Decompress a ZIP and stream rows from the inner CSV."""
    import zipfile
    with zipfile.ZipFile(path) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError(f"No CSV found inside {path}")
        with zf.open(csv_names[0]) as f:
            # zipfile entries are not seekable; decode into StringIO for DictReader
            content = f.read().decode("utf-8-sig")
            yield from csv.DictReader(io.StringIO(content))


def _download_and_iter() -> Generator[dict, None, None]:
    """Download MRDS zip from USGS and stream rows. Requires network access."""
    import zipfile

    resp = httpx.get(MRDS_CSV_ZIP_URL, follow_redirects=True, timeout=120)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError("No CSV found in MRDS zip download")
        with zf.open(csv_names[0]) as f:
            content = f.read().decode("utf-8-sig")
            yield from csv.DictReader(io.StringIO(content))
