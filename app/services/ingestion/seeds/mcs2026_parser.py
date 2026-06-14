"""Parser for the USGS Mineral Commodity Summaries 2026 long-format CSV.

Replaces the 2025 wide-format parser (`usgs_mcs_parser.py`) for the new
USGS publication format.  Reads ``MCS2026_Commodities_Data.csv`` and
produces Material field dicts compatible with the existing CLI write
logic in ``cli.py::ingest_usgs_cmd``.

Why a new parser
----------------
The 2026 publication switched from wide-format (one row per country with
year columns side-by-side) to long-format (one row per
``commodity × country × statistic × year``).  The schema is completely
different and the 2025 parser cannot read it.  Notable improvements
unlocked by the new format:

* **Sub-type splits are structured.**  Silicon's ferrosilicon vs
  silicon metal are separate ``Statistics_detail`` values rather than
  needing a hand-coded `_HS_NODE_SHARES_CONFIG` lookup.  Same for
  Copper's mine vs refinery production.
* **YoY trend signal.**  Production rows in the 2026 file carry two
  Year strings (``"2024"`` and ``"2025"``); the parser computes a
  single-year YoY delta from them.  An earlier comment claimed a
  5-year series; that was aspirational — USGS condensed the older
  years out of the 2026 publication.  ``by_year`` time-series is at
  most 2 data points per country today.
* **US import sources are structured** in the CSV (``Import Sources``
  section).  Previously only the PDF parser had this data.
* **Critical-mineral flag** (``Is critical mineral 2025`` column)
  available as structured input rather than hardcoded.
* **Capacity data extracted as a distinct signal.** 220 Capacity rows
  across 8 tracked chapters (ALUMINUM, BISMUTH, GALLIUM, INDIUM,
  MAGNESIUM METAL, SELENIUM, TELLURIUM, TITANIUM) land in
  ``capacity_shares`` for downstream landing in
  ``material_capacity_shares`` (migration 045).  Enables a
  spare-capacity / utilization-overhang signal that pure production
  HHI can't express.

Encoding
--------
The MCS 2026 CSV file as published is Windows-1252 (cp1252) — em-dash,
en-dash, and other characters fail under strict UTF-8.  The loader
tries UTF-8 first so future USGS editions that publish UTF-8 work
without code changes, then falls back to cp1252 on UnicodeDecodeError.

Section-name matching is also unicode-dash-tolerant.  The Salient
Statistics section header USGS publishes today is
"Salient Statistics—United States" with U+2014 EM DASH; we match via
startswith("Salient Statistics") so an ASCII hyphen, en-dash variant,
or trailing-region change doesn't silently drop every chapter's salient
signals.

Output contract
---------------
Returns a list of material dicts where each dict has the same internal
keys the existing CLI write logic expects:

    {
      "canonical_name": str,
      "category":       str,
      "symbol_or_code": str,
      "hs_codes":       list[str],
      "criticality_score": float | None,
      "primary_producing_countries": list[str],
      "price_unit": str,
      "is_ira_critical_mineral": bool,
      "is_eu_crma_critical": bool,
      "patent_occurrence_trend": str | None,
      "data_availability": str,
      "_hhi_score":           float | None,
      "_reserve_hhi_score":   float | None,
      "_reserve_life_index":  float | None,
      "_production_yoy_pct":  float | None,
      "_capacity_utilization": float | None,
      "_production_shares":    list[dict],   # material-level (aggregated across sub-types)
      "_hs_production_shares": list[dict],   # per-HS-node (sub-type split)
      "_us_import_sources":    list[dict],   # per-HS-node, market_scope='us'
      "notes": str,
    }

Coexistence with the 2025 parser
--------------------------------
The 2025 parser (``usgs_mcs_parser.py``) is intentionally NOT removed.
Both parsers can be invoked from ``ingest-usgs``; format selection is
either explicit (``--csv-format {2025,2026}``) or auto-detected by
sniffing column headers.  Plan: keep both for one release cycle, then
drop the 2025 path once the 2026 path is verified end-to-end.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# Country name → ISO-2 mapping lives in its own module so it can outlive
# the deprecated 2025 wide-format parser.  Same data, no behaviour change.
from app.services.ingestion.seeds.usgs_country_mapping import (
    _COUNTRY_ISO2,
    _EXCLUDE_COUNTRIES,
)


# Chapter → canonical material mapping is now stored in
# ``material_source_aliases`` (source_system='mcs_2026_csv').  Per-material
# static facts (category, symbol, hs_codes, IRA/CRMA flags, etc.) live in
# the ``materials`` register seeded from ``seed_materials.py``.  This
# parser is now session-free: it emits raw records keyed by ``source_name``
# and the CLI resolves them against the alias table at upsert time.

# ---------------------------------------------------------------------------
# Sub-type → HS prefix mapping
# ---------------------------------------------------------------------------
# When a chapter's World Production section has multiple Statistics_detail
# values (sub-types), this map tells the parser which HS prefix each
# sub-type maps to.  Replaces the 2025 parser's `_HS_NODE_SHARES_CONFIG`.
#
# Format: {(MCS chapter, statistics_detail_lowercase): hs_prefix}
# Match is case-insensitive on the detail string and uses substring
# matching (so "Mine production: rounded" still matches "mine production").
#
# Currently only includes splits we want to surface as separate HS nodes.
# Sub-types not listed fall back to the canonical material's primary HS
# prefix (the first entry in `hs_codes`).

# ---------------------------------------------------------------------------
# Stage auto-classification by Detail substring (added 2026-05-09)
# ---------------------------------------------------------------------------
# Replaces most of the manual ``_DETAIL_TO_HS_PREFIX`` dict.  For each row
# in a "World *" section whose ``Statistics_detail`` matches one of these
# patterns (case-insensitive, FIRST match wins — order matters), the
# parser tags the row with the corresponding supply_chain_stage.  The CLI
# then resolves ``(material_id, stage)`` to an HsCodeMaterialMapping at
# write time.
#
# Why patterns instead of section names: MCS publishes mixed-stage data
# inside one section (BAUXITE AND ALUMINA's "World Alumina Refinery and
# Bauxite Mine Production" carries BOTH refinery and mine rows; Copper's
# "World Mine and Refinery Production" carries mine + refinery rows).
# The Statistics_detail column reliably tags each row.
#
# Industry naming caveat: "refinery production" maps to ``refined`` stage
# for most metals (Cu cathode, Pb refined, Zn refined) but to
# ``intermediate`` for BAUXITE AND ALUMINA (alumina is Al2O3 oxide, not
# Al metal).  The "alumina, refinery" pattern wins because it appears
# above the generic "refinery production" pattern.
_DETAIL_STAGE_PATTERNS: list[tuple[str, str]] = [
    # PATTERN CONVENTION: each pattern is a multi-word substring (lower-
    # cased at match time).  Avoid single-word patterns ("ore", "metal")
    # — they false-match too easily across unrelated chapters.
    #
    # ── Specific patterns first (must match before the generic catches) ──
    ("alumina, refinery",   "intermediate"),  # BAUXITE → Al2O3 oxide intermediate
    ("bauxite, mine",       "ore"),           # BAUXITE → bauxite ore

    # ── BORON (no "mine production" / "refinery production" strings) ──
    # USGS publishes per-mineral-form rows.  BORON-specific substrings
    # confirmed via 2026-05-24 cross-chapter audit — none false-match
    # other chapters in MCS 2026.  "Production—All forms" is an aggregate
    # that gets skipped via the "all forms" filter in _classify_detail_stage.
    ("crude borates",       "ore"),           # BORON ore form
    ("crude ore",           "ore"),           # BORON ore form
    ("datolite ore",        "ore"),           # BORON ore form
    ("ulexite",             "ore"),           # BORON ore mineral
    ("refined borates",     "refined"),       # BORON refined
    ("boric oxide",         "refined"),       # BORON refined (B2O3)
    ("compounds",           "refined"),       # BORON refined; only chapter using "compounds" in production details

    # ── GALLIUM, TITANIUM: byproduct/sponge stages ────────────────────
    # GALLIUM is recovered as a byproduct of bauxite/zinc refining — no
    # mine.  "Primary production" = first metallic Ga output = refined stage.
    ("primary production",  "refined"),       # GALLIUM
    # TITANIUM sponge metal = first solid Ti form from Kroll process = refined.
    ("sponge metal",        "refined"),       # TITANIUM

    # ── Generic patterns (catch-alls for the standard metals) ────────
    ("smelter production",  "refined"),       # ALUMINUM smelter → Al metal refined
    ("refinery production", "refined"),       # COPPER refinery → cathode refined
    ("mine production",     "ore"),           # everything else: mine = ore
]


_DETAIL_TO_HS_PREFIX: dict[tuple[str, str], str] = {
    # Sub-type → HS prefix overrides for cases where multiple products
    # exist at the same supply_chain_stage and stage-based lookup alone
    # can't disambiguate.  Trimmed 2026-05-09 — Copper entries removed
    # because the new ``_DETAIL_STAGE_PATTERNS`` auto-classification
    # produces the same result via stage-based lookup (mine → ore HS 2603,
    # refinery → refined HS 7403).
    #
    # SILICON keeps its overrides because both ferrosilicon and silicon
    # metal are at the same nominal stage (refined) but trade as different
    # HS prefixes.  The current seed_hs_mappings has them at:
    #   720221 — ferrosilicon (≥4% Si) → battery_grade per partner
    #   280461 — silicon metal (≥99.99% Si) → refined
    # Stage-based lookup wouldn't distinguish them; explicit override here.
    ("SILICON", "ferrosilicon"):    "720221",
    ("SILICON", "silicon metal"):   "280461",
}


# ---------------------------------------------------------------------------
# Section name patterns
# ---------------------------------------------------------------------------
# 2026 CSV uses ~22 distinct world-production section name variants.  Treat
# any section starting with "World " as a world-production section and
# rely on the ``Statistics`` column to confirm it's "Production" (rather
# than "Reserves" or "Capacity") for the actual production tonnage rows.

# Section-name matching is unicode-dash-tolerant.  The exact string USGS
# publishes today is "Salient Statistics—United States" with U+2014 EM
# DASH; we match by prefix so any unicode-dash variant (en-dash U+2013,
# ASCII hyphen "-", trailing region change) still works.
_SALIENT_SECTION_PREFIX = "Salient Statistics"
_IMPORT_SECTION = "Import Sources"
_WORLD_SECTION_PREFIX = "World "


def _is_salient_section(section: str | None) -> bool:
    """Return True for any Salient Statistics section header variant."""
    if not section:
        return False
    return section.lstrip().startswith(_SALIENT_SECTION_PREFIX)

# Note: an earlier ``_SALIENT_PATTERNS`` constant was defined here as a
# substring-match lookup for salient signals, but the actual
# ``_extract_us_salient_signals`` function used inline substring checks
# instead.  Removed 2026-05-31 — if a future refactor wants to centralise
# the patterns, re-add and route the function through them.


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

# USGS sentinel strings for "no quantitative value available" — withheld
# under disclosure rules, not applicable, suppressed for small magnitudes,
# or qualitative-only.  Verified against MCS 2026 actual cell content
# (8,886 rows): NA (353 occurrences), em-dash (313), W (224), E (92),
# s (48), XX (8).  Lowercase 's' and uppercase 'XX' were previously
# handled by accident via the float() ValueError net; listing them
# explicitly makes the no-data semantics intentional, not incidental.
_NO_DATA_SENTINELS: frozenset[str] = frozenset({
    "W",      # withheld (proprietary data protection)
    "NA",     # not applicable
    "N/A",
    "E",      # estimated indicator with no value attached
    "s",      # less than half the unit shown (effectively zero)
    "XX",     # value withheld for disclosure reasons
    "—",      # U+2014 em dash — USGS "no data"
    "–",      # U+2013 en dash variant
    "-",      # ASCII hyphen variant
})


# Pattern for numeric ranges like "50–300", "500–17,000", "330 - 390".
# Used by _parse_value to compute a midpoint instead of dropping the cell.
# MCS 2026 contains 8 such cells (typically in reserves columns where
# USGS knows production happens but can't pin down a single number).
_RANGE_PATTERN = re.compile(
    r"^\s*([\d,]+(?:\.\d+)?)\s*[–—-]\s*([\d,]+(?:\.\d+)?)\s*$"
)


def _parse_value(value: str) -> Optional[float]:
    """Parse an MCS Value cell.  Returns None for withheld/unavailable.

    Value patterns observed in MCS 2026 (count from full 8,886-row file):
      - numeric with commas:  ``"3,640"``       → 3640.0
      - withheld:             ``"W"`` (224)     → None
      - withheld (XX form):   ``"XX"`` (8)      → None
      - not applicable:       ``"NA"`` (353)    → None
      - sub-rounding:         ``"s"`` (48)      → None  (less than half unit shown)
      - estimated, no value:  ``"E"`` (92)      → None  (estimator marker; value missing)
      - dash variants:        ``"—"``, ``"–"``, ``"-"`` (313+) → None
      - greater-than:         ``">95"``         → 95.0  (lower-bound estimate)
      - less-than:            ``"<50"``         → 50.0  (upper-bound estimate)
      - numeric range:        ``"50–300"``      → 175.0 (midpoint, 2026-05-24)
      - numeric range w/ comma: ``"500–17,000"`` → 8750.0
      - numeric range w/ spaces: ``"330 - 390"`` → 360.0
      - qualitative:          ``"Large"``, ``"Variable, depending on…"`` → None
                              (no float() interpretation; signal of presence
                              without quantification is lost — known floor
                              on reserve_hhi accuracy.)

    Direction loss caveat: bare ``_parse_value`` strips ``<``/``>`` bounds
    rather than rejecting them.  ``<50`` and ``>50`` both return 50.0.
    Callers reading 0-100 percentages should use ``_parse_percent_with_bound``
    instead — it converts ``<X``/``>X`` to midpoint estimates that preserve
    direction.
    """
    if value is None:
        return None
    v = value.strip()
    if not v or v in _NO_DATA_SENTINELS:
        return None

    # Numeric range like "50–300" → midpoint.  Checked before bound stripping
    # because en-dash inside a range looks similar to em-dash sentinels.
    range_match = _RANGE_PATTERN.match(v)
    if range_match:
        try:
            lower = float(range_match.group(1).replace(",", ""))
            upper = float(range_match.group(2).replace(",", ""))
        except ValueError:
            return None
        return (lower + upper) / 2.0

    # Bounded estimate: ">95" or ">2,000,000".  We strip the prefix and
    # parse as the bound value (loses direction; see docstring caveat).
    v = v.lstrip(">").lstrip("<").strip()
    v = v.replace(",", "")
    try:
        return float(v)
    except ValueError:
        return None


def _parse_percent_with_bound(value: str) -> Optional[float]:
    """Parse an MCS Value cell that is known to be a 0–100 percentage.

    Same sentinel-handling as ``_parse_value`` but converts bounded
    estimates to midpoints so ``<50`` and ``>50`` no longer collapse to
    the same number:

      - ``"<25"`` → 12.5  (midpoint of 0–25)
      - ``"<50"`` → 25.0  (midpoint of 0–50)
      - ``">50"`` → 75.0  (midpoint of 50–100)
      - ``">95"`` → 97.5  (midpoint of 95–100)

    Result is clamped to ``[0.0, 100.0]`` so a malformed ``">120"`` cell
    can't produce an out-of-range 110.  Used for net-import-reliance and
    other percent values where direction matters more than precision.
    """
    if value is None:
        return None
    v = value.strip()
    if not v or v in _NO_DATA_SENTINELS:
        return None
    if v.startswith("<"):
        try:
            upper = float(v[1:].replace(",", "").strip())
        except ValueError:
            return None
        return max(0.0, min(100.0, upper / 2.0))
    if v.startswith(">"):
        try:
            lower = float(v[1:].replace(",", "").strip())
        except ValueError:
            return None
        return max(0.0, min(100.0, (lower + 100.0) / 2.0))
    try:
        return max(0.0, min(100.0, float(v.replace(",", ""))))
    except ValueError:
        return None


def _hhi(country_productions: dict[str, float]) -> float:
    """Raw Herfindahl-Hirschman Index from ``{country: production_volume}``.

    Returns the standard HHI: sum of squared market shares.  Mathematical
    range is ``[1/N, 1.0]`` where ``N`` is the number of producing
    countries with non-zero volume: ``1/N`` at perfect competition,
    ``1.0`` at monopoly.  This is intentional and matches DOJ/FTC and
    IMF supply-chain convention — the ``1/N`` floor itself carries
    supply-chain signal (a material produced equally across 3 countries
    is more concentrated than one produced equally across 30 countries,
    because losing any one producer hits the 3-country case harder).

    Empty input returns ``0.0``; an all-zero-volume dict also returns
    ``0.0``.  Caller is expected to gate the call on ``country_prod``
    truthiness when it wants ``None`` for no-data chapters.
    """
    total = sum(country_productions.values())
    if total == 0:
        return 0.0
    return sum((v / total) ** 2 for v in country_productions.values())


# ---------------------------------------------------------------------------
# Helpers for picking the right year and resolving country names
# ---------------------------------------------------------------------------

def _pick_latest_year(years: list[str]) -> Optional[int]:
    """Return the latest 4-digit year present, ignoring suffixes like
    ``"_estimated"`` or year-range strings like ``"2021–24"``.  None if
    no parseable year is found.
    """
    parsed: list[int] = []
    for y in years:
        if not y:
            continue
        # Match the first 4-digit prefix
        m = re.match(r"^\s*(\d{4})", y)
        if m:
            parsed.append(int(m.group(1)))
    return max(parsed) if parsed else None


def _resolve_country(name: str) -> Optional[str]:
    """Map MCS country name → ISO-2.  Returns None for unmapped names."""
    if not name:
        return None
    return _COUNTRY_ISO2.get(name.strip())


# ---------------------------------------------------------------------------
# Per-section extractors
# ---------------------------------------------------------------------------

def _extract_world_production_per_country(
    chapter_rows: list[dict],
    detail_substring: Optional[str] = None,
) -> tuple[dict[str, float], dict[str, float], dict[str, float], Optional[int], str]:
    """Extract per-country production tonnages from World Production sections.

    Walks all rows in the chapter whose ``Section`` starts with "World ".
    Filters to ``Statistics`` containing "production" (case-insensitive)
    and excludes "Capacity" rows (Capacity is extracted separately by
    ``_extract_world_capacity_per_country``).  Reserves rows are bucketed
    into ``country_reserves`` and filtered to the latest reserves year.

    Cross-detail aggregation
    ------------------------
    When ``detail_substring`` is None and a chapter has multiple production
    sub-types in the same year for the same country (e.g. Copper's
    "mine production" + "refinery production" rows under one chapter),
    this function SUMS them into a single material-level country total.
    Callers wanting per-stage breakdowns must call
    ``_extract_per_stage_world_production`` instead — that wrapper buckets
    rows by stage and calls this helper per-bucket so the cross-sum
    happens within one stage.

    detail_substring scope
    ----------------------
    When provided, ``detail_substring`` filters BOTH production AND
    reserves rows.  Calling with ``detail_substring="ferrosilicon"``
    returns ferrosilicon-specific production *and* ferrosilicon-specific
    reserves — correct for sub-type extraction, where production and
    reserves of the same sub-type should track together.

    Units
    -----
    Headline unit is taken from the first non-empty Unit cell encountered
    in iteration order.  This assumes all included rows share a unit
    (true for every tracked chapter in MCS 2026).  When the assumption
    is violated, a warning is logged and the headline unit reports the
    first one seen — the per-country sums silently mix units, so the
    chapter alias should be marked is_skipped=true rather than relying
    on this function to make sense of mixed-unit data.

    Returns:
        country_prod:     {iso2: production_volume}  for the latest production year
        country_reserves: {iso2: reserves}            for the latest reserves year
        all_country_prod_by_year: {iso2: {year: vol}} for time-series use
        latest_year:      int (4-digit year used for the headline production shares)
        unit:             str (unit of measurement; see "Units" caveat above)
    """
    country_prod: dict[str, float] = {}
    country_reserves: dict[str, float] = {}
    by_year: dict[str, dict[int, float]] = defaultdict(dict)
    unit = ""
    units_seen: set[str] = set()

    detail_low = detail_substring.lower() if detail_substring else None
    production_rows: list[dict] = []
    reserves_rows: list[dict] = []
    for r in chapter_rows:
        section = r.get("Section", "")
        stat = (r.get("Statistics") or "").lower()
        detail = (r.get("Statistics_detail") or "").lower()
        if not section.startswith(_WORLD_SECTION_PREFIX):
            continue
        if detail_low is not None and detail_low not in detail:
            continue
        # "rounded" rows are world totals — skip; we'll derive total from
        # country sum.
        if "rounded" in detail:
            continue
        if "production" in stat:
            production_rows.append(r)
        elif "reserves" in stat:
            reserves_rows.append(r)
        # Capacity rows fall through here; see _extract_world_capacity_per_country.

    # Latest production year — strips _estimated suffix via _pick_latest_year
    # but does not deprioritize estimated vs actual when both exist for the
    # same year (no estimated-suffix rows exist in MCS 2026 production data;
    # add a prefer-actual flag if a future file publishes both).
    available_years = [r.get("Year", "") for r in production_rows]
    latest_year = _pick_latest_year(available_years)

    # Build per-country production for the latest year + the time series.
    # Aggregate-row filter uses the shared _EXCLUDE_COUNTRIES set so we
    # catch "other countries" / "united states and canada" alongside the
    # "world total" rows the inline 'world' substring check handles.
    for r in production_rows:
        country_name_low = (r.get("Country") or "").strip().lower()
        if not country_name_low or country_name_low in _EXCLUDE_COUNTRIES:
            continue
        if "world" in country_name_low:
            continue
        iso2 = _resolve_country(r.get("Country", ""))
        if iso2 is None:
            continue
        val = _parse_value(r.get("Value", ""))
        if val is None:
            continue
        year_str = r.get("Year", "")
        m = re.match(r"^\s*(\d{4})", year_str)
        if not m:
            continue
        year = int(m.group(1))
        by_year[iso2][year] = val
        if year == latest_year:
            country_prod[iso2] = country_prod.get(iso2, 0.0) + val
        row_unit = (r.get("Unit") or "").strip()
        if row_unit:
            units_seen.add(row_unit)
            if not unit:
                unit = row_unit

    # Mixed-unit warning: HELIUM (the one chapter where this fires today)
    # is is_skipped=true in the alias table, so this branch never fires in
    # production today.  Logged so a future tracked chapter with mixed
    # units doesn't silently produce unit-incoherent per-country sums.
    if len(units_seen) > 1:
        log.warning(
            "mcs2026_parser.mixed_units_in_chapter",
            units=sorted(units_seen),
            kept_unit=unit,
            note=(
                "Per-country production sums mix units. Consider marking "
                "this chapter is_skipped=true in material_source_aliases."
            ),
        )

    # Reserves — filter to latest year before summing so multi-year
    # reserves data doesn't silently overcount.  Current MCS 2026 file
    # has all reserves at Year='2025' so this is a no-op today, but
    # protects against the bug pattern fixed 2026-05-31.
    reserves_years = [r.get("Year", "") for r in reserves_rows]
    latest_reserves_year = _pick_latest_year(reserves_years)
    for r in reserves_rows:
        year_str = r.get("Year", "")
        m = re.match(r"^\s*(\d{4})", year_str)
        if not m or int(m.group(1)) != latest_reserves_year:
            continue
        country_name_low = (r.get("Country") or "").strip().lower()
        if not country_name_low or country_name_low in _EXCLUDE_COUNTRIES:
            continue
        if "world" in country_name_low:
            continue
        iso2 = _resolve_country(r.get("Country", ""))
        if iso2 is None:
            continue
        val = _parse_value(r.get("Value", ""))
        if val is not None:
            country_reserves[iso2] = country_reserves.get(iso2, 0.0) + val

    return country_prod, country_reserves, dict(by_year), latest_year, unit


def _extract_world_capacity_per_country(
    chapter_rows: list[dict],
) -> list[tuple[str, dict[str, float], Optional[int], str]]:
    """Extract per-country capacity values from World sections.

    Differentiated from production (Issue 4.1 fix, 2026-05-31): MCS publishes
    "Capacity" rows separately from "Production" rows for 8 tracked chapters
    today — ALUMINUM, BISMUTH, GALLIUM, INDIUM, MAGNESIUM METAL, SELENIUM,
    TELLURIUM, TITANIUM AND TITANIUM DIOXIDE.  Capacity = theoretical maximum
    a facility could produce; production = actual tonnes delivered.  Together
    they enable a spare-capacity / utilization-overhang signal that pure
    production HHI can't express.

    Returns a list of buckets, one per distinct Statistics_detail string:

        [(detail_type, {iso2: capacity_volume}, latest_year, unit), ...]

    where detail_type is the verbatim Statistics_detail (e.g. "Smelter
    capacity", "Titanium sponge metal Capacity").  Multiple buckets per
    chapter when a material has multiple capacity types — TITANIUM has both
    "Titanium sponge metal Capacity" and "TiO2 Pigment Capacity" as distinct
    capacity streams.  Callers store these as separate
    ``material_capacity_shares`` rows keyed on detail_type.

    Skips "rounded" aggregate rows (world totals — caller derives total
    from country sum).  Same country-resolution + ``_EXCLUDE_COUNTRIES``
    aggregate-row filter as the production extractor.
    """
    by_detail: dict[str, list[dict]] = defaultdict(list)
    for r in chapter_rows:
        section = r.get("Section", "") or ""
        if not section.startswith(_WORLD_SECTION_PREFIX):
            continue
        stat = (r.get("Statistics") or "").lower()
        if "capacity" not in stat:
            continue
        detail = (r.get("Statistics_detail") or "").strip()
        if not detail or "rounded" in detail.lower():
            continue
        by_detail[detail].append(r)

    buckets: list[tuple[str, dict[str, float], Optional[int], str]] = []
    for detail_type, bucket_rows in by_detail.items():
        years = [r.get("Year", "") for r in bucket_rows]
        latest_year = _pick_latest_year(years)
        country_cap: dict[str, float] = {}
        unit = ""
        for r in bucket_rows:
            year_str = r.get("Year", "")
            m = re.match(r"^\s*(\d{4})", year_str)
            if not m or int(m.group(1)) != latest_year:
                continue
            country_name_low = (r.get("Country") or "").strip().lower()
            if not country_name_low or country_name_low in _EXCLUDE_COUNTRIES:
                continue
            if "world" in country_name_low:
                continue
            iso2 = _resolve_country(r.get("Country", ""))
            if iso2 is None:
                continue
            val = _parse_value(r.get("Value", ""))
            if val is None:
                continue
            country_cap[iso2] = country_cap.get(iso2, 0.0) + val
            row_unit = (r.get("Unit") or "").strip()
            if not unit and row_unit:
                unit = row_unit
        if country_cap:
            buckets.append((detail_type, country_cap, latest_year, unit))
    return buckets


def _classify_detail_stage(detail: str) -> Optional[str]:
    """Return the supply_chain_stage for a Statistics_detail value.

    Walks ``_DETAIL_STAGE_PATTERNS`` in order; returns the first match.

    Returns None for:
      * rows whose detail contains ``"rounded"`` (USGS world-total rounding
        rows — caller derives world total from the country sum)
      * rows whose detail contains ``"all forms"`` (USGS aggregate rows
        like BORON's "Production—All forms" or SULFUR's "Production, all
        forms" — summing the per-form rows below would double-count)
      * details that don't match any pattern (silently skipped — see
        section 3 audit of mcs2026_parser.py for a periodic check that
        none of the unclassified strings belong to tracked materials).
    """
    if not detail:
        return None
    detail_low = detail.lower()
    # Aggregate rows that double-count if summed alongside per-form rows.
    if "rounded" in detail_low or "all forms" in detail_low:
        return None
    for pattern, stage in _DETAIL_STAGE_PATTERNS:
        if pattern in detail_low:
            return stage
    return None


# Data-quality flag values emitted by _extract_per_stage_world_production.
# ``None`` means a stage had a single source-detail bucket (no consolidation
# happened); explicit values flag stages where the consolidation made a
# judgment call partner may want to review.
_DQ_NOT_CONSOLIDATED = None
_DQ_ADDITIVE = "additive"           # multiple details, ≤30% country overlap
_DQ_DUPLICATE_SUSPECT = "duplicate_suspect"  # multiple details, >30% country overlap


def _extract_per_stage_world_production(
    chapter_rows: list[dict],
) -> list[tuple[str, str, dict[str, float], Optional[int], str, Optional[str]]]:
    """Group all "World *" production rows by detected supply_chain_stage.

    Replaces the per-substring loop in ``parse_mcs2026_csv`` with auto-
    detection.  For each supply_chain_stage that has at least one resolvable
    country production row in the latest year, returns ONE entry:

        (stage, detail_substring, country_prod, latest_year, unit, data_quality_flag)

    where ``country_prod`` is ``{iso2: production_volume}`` for the latest
    available year.  Multiple stages are returned when a chapter publishes
    mixed-stage data in one section (Cu mine + refinery → ore + refined;
    BAUXITE alumina + bauxite → ore + intermediate).

    Ambiguity handling (data_quality_flag, added 2026-05-31)
    --------------------------------------------------------
    Some chapters publish multiple Statistics_detail strings that all
    classify to the same supply_chain_stage.  Three patterns observed:

      * **Additive** — different products at the same stage that sum into
        a country's total supply.  Example: PGM "palladium" + "platinum"
        (same mine produces both); BORON's per-mineral-form ore details;
        TITANIUM "ilmenite" + "rutile".  Country overlap is high (often
        100%) but the values represent distinct flows.  flag=_DQ_ADDITIVE.

      * **Duplicate / asymmetric overlap** — one detail covers all major
        producers while a sub-detail (e.g. ``": concentrate"``,
        ``": iron content"``, ``": copper telluride"``) covers a subset
        of them measuring the same flow differently.  TELLURIUM is the
        only currently-tracked battery material where this fires.  When
        country overlap is >30% between sub-buckets, flag=_DQ_DUPLICATE_SUSPECT
        so partner-review can decide per-chapter how to interpret it.

      * **Stage-mixed within ore** — DIATOMITE "mine production" + "mine
        production: processed".  Both classify to the ``ore`` stage; the
        processed row is conservatively summed.  DIATOMITE is is_skipped
        so this doesn't fire for tracked materials today.

    The current implementation sums all three patterns per (stage × country).
    The data_quality_flag surfaces consolidation events to downstream code
    (and partner review) without changing the math.  Adjudication is a
    methodology decision — see docs/scoring-audit-2026-05.md.

    Unit consistency
    ----------------
    When multiple buckets share a stage, their units must match.  Mixed
    units within a stage are flagged with ``log.warning`` and the consolidated
    output unit is the most common one; the inconsistent bucket still
    contributes its values but the sum is unit-incoherent and unreliable.
    The chapter alias should be marked ``is_skipped=true`` if this fires.

    Returns:
        list of 6-tuples (stage, detail_low, country_prod, latest_year,
        unit, data_quality_flag).  Previous callers using 5-tuple
        destructuring need updating.
    """
    # First pass — build (stage, detail) buckets and extract per-country
    # production per bucket.
    by_bucket: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in chapter_rows:
        section = r.get("Section", "") or ""
        if not section.startswith(_WORLD_SECTION_PREFIX):
            continue
        stat = (r.get("Statistics") or "").lower()
        if "production" not in stat:
            continue
        detail = (r.get("Statistics_detail") or "").strip()
        stage = _classify_detail_stage(detail)
        if stage is None:
            continue
        by_bucket[(stage, detail.lower())].append(r)

    # For each bucket, get its per-country production.  Empty buckets
    # (all values withheld / qualitative) are logged so investigations
    # can distinguish "no data" from "parser bug" when chapter coverage
    # looks thin.
    bucket_extractions: list[
        tuple[str, str, dict[str, float], Optional[int], str]
    ] = []
    for (stage, detail_low), bucket_rows in by_bucket.items():
        country_prod, _r, _by_year, latest_year, unit = (
            _extract_world_production_per_country(bucket_rows, detail_substring=None)
        )
        if not country_prod:
            log.debug(
                "mcs2026_parser.empty_bucket_skipped",
                stage=stage,
                detail=detail_low,
                row_count=len(bucket_rows),
            )
            continue
        bucket_extractions.append(
            (stage, detail_low, country_prod, latest_year, unit or "")
        )

    # Cross-bucket unit consistency check (Issue 5.1).  Sum-based
    # consolidation is only well-defined if the buckets share a unit.
    units_per_stage: dict[str, Counter] = defaultdict(Counter)
    countries_per_bucket: dict[tuple[str, str], set[str]] = {}
    for stage, detail_low, country_prod, _yr, unit in bucket_extractions:
        if unit:
            units_per_stage[stage][unit] += 1
        countries_per_bucket[(stage, detail_low)] = set(country_prod.keys())

    mixed_unit_stages: set[str] = set()
    for stage, units_seen in units_per_stage.items():
        if len(units_seen) > 1:
            mixed_unit_stages.add(stage)
            log.warning(
                "mcs2026_parser.mixed_units_in_stage_consolidation",
                stage=stage,
                units=dict(units_seen),
                note=(
                    "Per-country sums across buckets with different units "
                    "are unit-incoherent. Mark this chapter is_skipped=true "
                    "in material_source_aliases."
                ),
            )

    # Compute the data_quality_flag per stage based on country-overlap
    # geometry.  Single-bucket stages get flag=None (no consolidation).
    # Multi-bucket stages: high overlap (>30%) → DUPLICATE_SUSPECT;
    # low overlap → ADDITIVE.
    stage_to_buckets: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for stage, detail_low, *_ in bucket_extractions:
        stage_to_buckets[stage].append((stage, detail_low))

    def _stage_overlap_pct(stage_buckets: list[tuple[str, str]]) -> float:
        """Highest pairwise country overlap across buckets in a stage,
        normalised to the smaller bucket's size (so a 1-country bucket
        overlapping fully with a 7-country bucket scores 100%, exposing
        asymmetric duplication)."""
        sets = [countries_per_bucket[b] for b in stage_buckets if countries_per_bucket.get(b)]
        if len(sets) < 2:
            return 0.0
        max_overlap = 0.0
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                inter = sets[i] & sets[j]
                smaller = min(len(sets[i]), len(sets[j]))
                if smaller > 0:
                    pct = len(inter) / smaller
                    if pct > max_overlap:
                        max_overlap = pct
        return max_overlap

    stage_dq_flag: dict[str, Optional[str]] = {}
    for stage, buckets in stage_to_buckets.items():
        if len(buckets) <= 1:
            stage_dq_flag[stage] = _DQ_NOT_CONSOLIDATED
        elif _stage_overlap_pct(buckets) > 0.30:
            stage_dq_flag[stage] = _DQ_DUPLICATE_SUSPECT
        else:
            stage_dq_flag[stage] = _DQ_ADDITIVE

    # Consolidate: SUM per (stage × country) across all buckets sharing
    # a stage.  Tiebreak in by_stage_meta uses strict ``>`` on country_count,
    # so the first-seen bucket wins on ties; dict iteration is insertion
    # order (Python 3.7+) so this is deterministic but coupled to row
    # order in the CSV.
    by_stage_country: dict[str, dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    by_stage_meta: dict[str, dict] = {}
    by_stage_details: dict[str, list[str]] = defaultdict(list)
    for stage, detail_low, country_prod, latest_year, unit in bucket_extractions:
        for iso2, vol in country_prod.items():
            by_stage_country[stage][iso2] += vol
        prev = by_stage_meta.get(stage)
        if prev is None or len(country_prod) > prev["country_count"]:
            by_stage_meta[stage] = {
                "detail_low": detail_low,
                "latest_year": latest_year,
                "unit": unit,
                "country_count": len(country_prod),
            }
        by_stage_details[stage].append(detail_low)

    # Per-stage consolidation log — promoted to INFO 2026-05-31 so
    # data-quality events surface in standard logs without spam (one
    # event per consolidated stage, not per bucket).
    for stage, details in by_stage_details.items():
        if len(details) > 1:
            log.info(
                "mcs2026_parser.same_stage_bucket_consolidated",
                stage=stage,
                source_details=sorted(details),
                kept_meta_detail=by_stage_meta[stage]["detail_low"],
                country_count=len(by_stage_country[stage]),
                data_quality_flag=stage_dq_flag[stage],
                mixed_units=stage in mixed_unit_stages,
            )

    return [
        (
            stage,
            by_stage_meta[stage]["detail_low"],
            dict(by_stage_country[stage]),
            by_stage_meta[stage]["latest_year"],
            by_stage_meta[stage]["unit"],
            stage_dq_flag[stage],
        )
        for stage in by_stage_country
    ]


# ── Unit normalisation for USGS Salient Price observations ─────────────────
# USGS reports prices in commodity-specific units (cents/lb for Cu/Al/Ni,
# $/MT for Li/Ni LME, $/lb for Co, DMTU for W, etc.).  We normalise to
# USD per metric ton for Financial Pressure pillar scoring so CV /
# % change calculations are unit-consistent across materials.
#
# Constants:
#   1 metric ton = 2204.622 lb = 1000 kg = 32 150.7 troy oz = 1.10231 short ton
#
# DMTU (dry metric ton unit) and MTU are contained-element pricing
# (1 DMTU = 10 kg of contained WO3 or Mn in the gross-tonnage sense).
# These don't convert linearly without the contained-element fraction
# from the same chapter — left unnormalised.

_LB_PER_MT = 2204.622
_KG_PER_MT = 1000.0
_TROY_OZ_PER_MT = 32_150.7
_SHORT_TON_PER_MT = 1.10231


def _to_usd_per_metric_ton(value: float, unit_raw: str) -> Optional[float]:
    """Normalise a USGS Salient Price observation to USD per metric ton.

    Returns ``None`` for DMTU / MTU units (contained-element pricing —
    needs a contained-element fraction we don't have to hand here).  The
    raw value + unit are always preserved separately so the partner can
    do a contained-element conversion offline if needed.
    """
    if value is None:
        return None
    u = (unit_raw or "").lower()
    # Order matters: "metric ton unit" must be checked BEFORE "metric ton"
    # to avoid a substring collision on "dollars per metric ton unit"
    # falsely matching the plain-MT branch.  DMTU / MTU don't convert
    # linearly without contained-element context, so they return None.
    if "metric ton unit" in u:
        return None
    if "cents per pound" in u:
        return value / 100.0 * _LB_PER_MT
    if "dollars per pound" in u:
        return value * _LB_PER_MT
    if "dollars per metric ton" in u:
        return value
    if "dollars per kilogram" in u:
        return value * _KG_PER_MT
    if "dollars per troy ounce" in u:
        return value * _TROY_OZ_PER_MT
    if "dollars per short ton" in u:
        return value * _SHORT_TON_PER_MT
    return None


def _extract_prices_from_salient(chapter_rows: list[dict]) -> list[dict]:
    """Extract every annual Price observation from Salient Statistics.

    Companion to ``_extract_price_unit_from_salient`` which only returns
    the unit string of the first Price row.  This function returns the
    full time series — one entry per ``(Statistics_detail, Year)`` tuple
    so that multi-benchmark commodities (Cobalt US-spot + LME, Copper 3
    benchmarks, Nickel $/MT + $/lb LME quotes) preserve every datum.

    Returns a list of dicts in CSV order::

        {
            "year": int,                           # 2021, 2022, ...
            "value_raw": float,                    # as published
            "unit_raw": str,                       # e.g. "dollars per metric ton"
            "statistics_detail": str,              # full benchmark descriptor
            "value_usd_per_mt": float | None,      # normalised, None for DMTU/MTU
        }

    Caller is expected to write each entry into ``commodity_prices`` with
    ``source='usgs_mcs'`` and ``price_form=statistics_detail`` so the
    benchmark identity is preserved.  Pick the first Statistics_detail
    (the "primary" benchmark USGS leads with) when deriving growth-rate
    signals to match ``_extract_price_unit_from_salient``'s convention.
    """
    out: list[dict] = []
    for r in chapter_rows:
        if not _is_salient_section(r.get("Section")):
            continue
        if (r.get("Statistics") or "").strip().lower() != "price":
            continue
        year_raw = (r.get("Year") or "").strip()
        value_str = (r.get("Value") or "").strip().replace(",", "")
        unit_raw = (r.get("Unit") or "").strip()
        detail = (r.get("Statistics_detail") or "").strip()
        if not (year_raw and value_str and detail):
            continue
        try:
            year = int(year_raw)
            value = float(value_str)
        except (TypeError, ValueError):
            continue
        out.append({
            "year": year,
            "value_raw": value,
            "unit_raw": unit_raw,
            "statistics_detail": detail,
            "value_usd_per_mt": _to_usd_per_metric_ton(value, unit_raw),
        })
    return out


def _derive_yoy_and_cagr_from_prices(
    prices: list[dict],
) -> tuple[Optional[float], Optional[float]]:
    """Derive ``(YoY %, CAGR %)`` from a chapter's primary price benchmark.

    Uses the FIRST ``Statistics_detail`` in the price list — same
    convention as ``_extract_price_unit_from_salient`` for the unit.
    Output schema matches the existing
    ``material_criticality_signals.{price_yoy_pct, price_cagr_5yr_pct}``
    columns that the Fig 10 parser writes today: signed fractions, e.g.
    ``-0.24`` for a 24% YoY drop, ``+0.18`` for an 18% CAGR.

    Why this matters: the Fig 10 CSV only carries growth rates for
    commodities with MULTIPLE price sources in their Salient table.  For
    SINGLE-source commodities (most of them) the Fig 10 CSV is silent,
    leaving the price-volatility sub-signal at zero.  This function fills
    that gap by deriving the same metrics from the same Salient prices
    USGS already gave us.

    Returns ``(None, None)`` when fewer than 2 observations are present.
    YoY uses the latest pair; CAGR uses the full ``(first, last)`` span.
    """
    if not prices:
        return (None, None)
    primary_detail = prices[0]["statistics_detail"]
    series = sorted(
        (p for p in prices if p["statistics_detail"] == primary_detail),
        key=lambda x: x["year"],
    )
    if len(series) < 2:
        return (None, None)

    def _v(p: dict) -> Optional[float]:
        # Prefer the normalised USD/MT value so cross-material math is
        # unit-consistent.  Fall back to raw for DMTU/MTU benchmarks —
        # YoY and CAGR are dimensionless so the unit choice doesn't
        # change the answer as long as we're internally consistent
        # within the series.
        return p["value_usd_per_mt"] if p["value_usd_per_mt"] is not None else p["value_raw"]

    first = _v(series[0])
    prior = _v(series[-2])
    last = _v(series[-1])
    if last is None or first is None or first <= 0:
        return (None, None)

    yoy: Optional[float] = None
    if prior is not None and prior > 0:
        yoy = (last - prior) / prior

    years_span = series[-1]["year"] - series[0]["year"]
    cagr: Optional[float] = None
    if years_span >= 1 and last > 0:
        cagr = (last / first) ** (1.0 / years_span) - 1.0

    return (yoy, cagr)


def _extract_price_unit_from_salient(chapter_rows: list[dict]) -> Optional[str]:
    """Read the first Price row in Salient Statistics and return the unit.

    Returns one of: ``per_lb``, ``per_kg``, ``per_mt``, ``per_st``,
    ``per_oz``, ``per_dmtu``, ``per_mtu``, ``per_carat``, or ``None`` if
    no Price row is present or no unit phrase is recognised.

    Why "first row": chapters often publish multiple Price rows (Cobalt
    has US-spot AND LME; Copper has 3 benchmarks; Nickel has both per-mt
    and per-pound LME quotes).  The FIRST Price row in the CSV is
    consistently the primary benchmark USGS leads with, so it's the
    most defensible default.  Partner can override via a manual seed
    if a different convention is preferred (e.g., trade desks may
    quote Li in $/kg LCE while USGS reports $/MT).
    """
    salient_prices = [
        r for r in chapter_rows
        if _is_salient_section(r.get("Section"))
        and (r.get("Statistics") or "").strip().lower() == "price"
    ]
    if not salient_prices:
        return None

    # Use the first row's Statistics_detail.
    detail = (salient_prices[0].get("Statistics_detail") or "").lower()
    if not detail:
        return None

    # Match longest unit phrases first to avoid partial-string collisions
    # ("metric ton unit" must beat "metric ton"; "dry metric ton unit"
    # must beat "metric ton unit").
    if "dry metric ton unit" in detail:
        return "per_dmtu"   # Tungsten — DMTU = dry metric ton unit of WO3
    if "metric ton unit" in detail:
        return "per_mtu"    # Manganese — MTU
    if "metric ton" in detail:
        return "per_mt"
    if "short ton" in detail:
        return "per_st"     # Soda Ash sometimes
    if "kilogram" in detail:
        return "per_kg"
    if "troy ounce" in detail:
        return "per_oz"
    if "pound" in detail:
        return "per_lb"     # covers "cents per pound" and "dollars per pound"
    if "carat" in detail:
        return "per_carat"  # not battery-relevant; included for completeness
    return None


# Word-boundary regex for the NIR "Total" sub-type detection.  Avoids
# accidental matches inside compound detail strings — though MCS 2026
# only uses "Total" as a whole-word qualifier today, this guards against
# future formats that might say "subtotal", "totalised", etc.
_NIR_TOTAL_RE = re.compile(r"\btotal\b", re.IGNORECASE)


# Reserve-life-index sanity floor: world_reserves / world_prod ratios
# below this value are almost certainly a unit-scale mismatch (e.g.,
# reserves in tonnes vs production in thousand tonnes — would give an
# RLI of ~0.001 when the real value is ~1000 years).  Parser returns
# None for any RLI below this floor rather than emitting a misleadingly
# tiny "years of reserves" number.  Issue 8.3 fix (2026-05-31): hoisted
# from inside the per-chapter loop to module level so it's defined once.
_RLI_MIN_PLAUSIBLE = 2.0


def _extract_us_salient_signals(
    chapter_rows: list[dict],
) -> dict[str, Optional[float]]:
    """Extract US-domestic signals from Salient Statistics—United States.

    Returns a dict with keys we care about for material-level scoring:
        - capacity_utilization: US-production ÷ US-capacity for latest year
        - production_yoy_pct:   (latest - prior) / prior for production sum
        - apparent_consumption: latest year US apparent consumption
        - net_import_reliance:  pct (0–100) for latest year, primary stat

    Unavailable signals return None.

    Sub-type aggregation
    --------------------
    Sub-type splits (Silicon's ferrosilicon + silicon metal + Total;
    ALUMINUM's Primary + Secondary; ANTIMONY's Mine + Smelter) are
    present as separate ``Statistics_detail`` rows; we sum across
    sub-types per (stat, year).  This is correct when sub-types are
    additive components of one supply chain (ALUMINUM Primary +
    Secondary = total US Al supply).  Chapters where sub-types mix
    distinct supply-chain stages (BAUXITE AND ALUMINA "mine" + "refinery"
    in Salient Production) are aliased with ``writes_material_signals=False``
    so the cross-stage sum doesn't poison material-level signals.

    Capacity-utilization data source (Issue 6.1 fix, 2026-05-31)
    -----------------------------------------------------------
    MCS 2026 publishes Capacity rows in WORLD sections, NOT in Salient.
    A pre-fix implementation looked for ``"capacity" in stat`` within
    salient rows and always found zero, yielding None for every chapter.
    The fix filters World-section Capacity rows to ``Country='United
    States'`` and uses that sum as the capacity denominator against the
    Salient Production numerator.  Works for the 8 tracked chapters
    with US capacity (ALUMINUM, BISMUTH, GALLIUM, INDIUM, MAGNESIUM
    METAL, SELENIUM, TELLURIUM, TITANIUM) when their World capacity
    section includes a US row.

    YoY zero-handling (Issue 6.2 fix, 2026-05-31)
    --------------------------------------------
    Previous version filtered ``_latest_two`` to ``v > 0`` years, which
    hid production-dropout signals (mine closures going to zero).  Now
    uses the latest 2 actual years regardless of zero; the outer
    compute guards against ``prior_v == 0`` to avoid divide-by-zero.
    """
    salient = [r for r in chapter_rows if _is_salient_section(r.get("Section"))]
    if not salient:
        log.debug("mcs2026_parser.salient_no_rows",
                  chapter=(chapter_rows[0].get("MCS chapter") if chapter_rows else None))
        return {
            "capacity_utilization": None,
            "production_yoy_pct": None,
            "apparent_consumption": None,
            "net_import_reliance": None,
        }

    # Bucket rows by Statistics + year.  Sum across sub-types
    # (Statistics_detail).  Filter "rounded" detail rows (Issue 6.7)
    # to be defensive against double-counting if USGS ever publishes a
    # rounded-aggregate alongside its per-sub-type breakdown in salient.
    by_stat_year: dict[tuple[str, int], float] = defaultdict(float)
    for r in salient:
        stat = (r.get("Statistics") or "").lower()
        detail = (r.get("Statistics_detail") or "").lower()
        if "rounded" in detail:
            continue
        year_str = r.get("Year", "")
        m = re.match(r"^\s*(\d{4})", year_str)
        if not m:
            continue
        year = int(m.group(1))
        val = _parse_value(r.get("Value", ""))
        if val is None:
            continue
        by_stat_year[(stat, year)] += val

    def _latest_two(stat_low: str) -> Optional[tuple[int, float, int, float]]:
        """Return (latest_year, latest_val, prior_year, prior_val) for ``stat``
        — the two most recent years for which a value exists, zeros included.
        None if fewer than 2 years of data.  Caller is responsible for
        guarding against prior_val == 0 when computing rates.
        """
        years = sorted(
            (y for (s, y) in by_stat_year if s == stat_low),
            reverse=True,
        )
        if len(years) < 2:
            return None
        latest, prior = years[0], years[1]
        return (latest, by_stat_year[(stat_low, latest)],
                prior,  by_stat_year[(stat_low, prior)])

    # ── Capacity utilization (Issue 6.1) ─────────────────────────────────
    # Numerator: latest US production from Salient (already summed across
    # sub-types in by_stat_year).
    # Denominator: latest US capacity from World-section Capacity rows.
    capacity_utilization = None
    prod_years_avail = sorted(
        {y for (s, y) in by_stat_year if "production" in s},
        reverse=True,
    )
    us_capacity_by_year: dict[int, float] = defaultdict(float)
    for r in chapter_rows:
        section = r.get("Section", "") or ""
        if not section.startswith(_WORLD_SECTION_PREFIX):
            continue
        if "capacity" not in (r.get("Statistics") or "").lower():
            continue
        detail = (r.get("Statistics_detail") or "").lower()
        if "rounded" in detail:
            continue
        if (r.get("Country") or "").strip().lower() != "united states":
            continue
        year_str = r.get("Year", "")
        m = re.match(r"^\s*(\d{4})", year_str)
        if not m:
            continue
        val = _parse_value(r.get("Value", ""))
        if val is None:
            continue
        us_capacity_by_year[int(m.group(1))] += val

    if prod_years_avail and us_capacity_by_year:
        common = set(prod_years_avail) & set(us_capacity_by_year.keys())
        if common:
            latest_common = max(common)
            prod_us = sum(
                v for (s, y), v in by_stat_year.items()
                if "production" in s and y == latest_common
            )
            cap_us = us_capacity_by_year[latest_common]
            if cap_us > 0:
                raw_ratio = prod_us / cap_us
                # Sanity gate: utilization > 1.0 means the numerator and
                # denominator are scoped differently.  Most common cause:
                # Salient Production sums Primary + Secondary (recycled
                # scrap) while World Capacity is primary smelter only —
                # ALUMINUM and MAGNESIUM METAL trigger this.  Return None
                # rather than emit an impossible value; partner can
                # refine the numerator filter (e.g. to "primary"-only
                # sub-types) when methodology resolves it.
                if raw_ratio > 1.0:
                    log.warning(
                        "mcs2026_parser.salient_capacity_utilization_over_unity",
                        raw_ratio=round(raw_ratio, 4),
                        prod_us=prod_us,
                        cap_us=cap_us,
                        year=latest_common,
                        note=(
                            "Numerator likely includes recycled / secondary "
                            "production not covered by primary capacity. "
                            "Returning None until partner refines the "
                            "production-sub-type filter."
                        ),
                    )
                else:
                    capacity_utilization = round(raw_ratio, 4)

    if capacity_utilization is None:
        log.debug(
            "mcs2026_parser.salient_capacity_utilization_unavailable",
            has_us_capacity=bool(us_capacity_by_year),
            has_us_production=bool(prod_years_avail),
        )

    # ── Production YoY % ─────────────────────────────────────────────────
    # Uses the latest two ACTUAL years (zeros allowed).  Outer guard
    # against prior_v == 0 protects against divide-by-zero.
    production_yoy_pct = None
    pair = _latest_two("production")
    if pair:
        _latest_y, latest_v, _prior_y, prior_v = pair
        if prior_v > 0:
            production_yoy_pct = round((latest_v - prior_v) / prior_v, 4)
        else:
            log.debug(
                "mcs2026_parser.salient_yoy_undefined_prior_zero",
                latest_value=latest_v, prior_value=prior_v,
            )
    else:
        log.debug("mcs2026_parser.salient_yoy_unavailable_too_few_years")

    # ── Apparent consumption — latest value ──────────────────────────────
    apparent_consumption = None
    cons_years = sorted(
        ({y for (s, y) in by_stat_year if "consumption" in s}),
        reverse=True,
    )
    if cons_years:
        latest_y = cons_years[0]
        apparent_consumption = sum(
            v for (s, y), v in by_stat_year.items()
            if "consumption" in s and y == latest_y
        )
    else:
        log.debug("mcs2026_parser.salient_consumption_unavailable")

    # ── Net import reliance ──────────────────────────────────────────────
    # NIR uses bounded estimates ("<50", ">50") that need midpoint-aware
    # parsing.  Re-parse the raw rows directly with
    # `_parse_percent_with_bound` rather than relying on the sum/count
    # buckets above (which used the bound-stripping `_parse_value`).
    # Prefer the "Total" sub-type if present (Silicon has ferrosilicon +
    # silicon metal + Total); fall back to the average across sub-types
    # when no Total row exists.  Total detection uses a word-boundary
    # regex (Issue 6.4) so substrings like "subtotal" don't accidentally
    # match.
    nir = None
    nir_rows = [
        r for r in salient
        if "net import reliance" in (r.get("Statistics") or "").lower()
    ]
    if nir_rows:
        nir_by_detail_year: dict[tuple[str, int], float] = {}
        for r in nir_rows:
            year_str = r.get("Year", "")
            m = re.match(r"^\s*(\d{4})", year_str)
            if not m:
                continue
            year = int(m.group(1))
            val = _parse_percent_with_bound(r.get("Value", ""))
            if val is None:
                continue
            detail_low = (r.get("Statistics_detail") or "").lower()
            nir_by_detail_year[(detail_low, year)] = val

        if nir_by_detail_year:
            latest_y = max(y for (_, y) in nir_by_detail_year)
            total_keys = [
                k for k in nir_by_detail_year
                if k[1] == latest_y and _NIR_TOTAL_RE.search(k[0])
            ]
            if total_keys:
                nir = round(nir_by_detail_year[total_keys[0]], 2)
            else:
                latest_vals = [
                    v for (d, y), v in nir_by_detail_year.items()
                    if y == latest_y
                ]
                nir = round(sum(latest_vals) / len(latest_vals), 2)
    if nir is None:
        log.debug("mcs2026_parser.salient_nir_unavailable")

    return {
        "capacity_utilization": capacity_utilization,
        "production_yoy_pct": production_yoy_pct,
        "apparent_consumption": apparent_consumption,
        "net_import_reliance": nir,
    }


# Aggregate-row labels in the Import Sources Country column.
# Verified against MCS 2026: "Other countries" is the only non-Total
# aggregate value in real data (~159 rows).  Kept as a frozenset so
# membership lookup is O(1).
_IMPORT_SOURCE_AGGREGATE_COUNTRIES: frozenset[str] = frozenset({
    "total",
    "other",
    "other countries",        # Issue 7.1 — real MCS value, plural + capitalised
    "world total",
    "",
})


def _extract_us_import_sources(
    chapter_rows: list[dict],
    chapter: str,
) -> list[dict]:
    """Extract US import sources from the Import Sources section.

    Each row gives a country's % share of US imports for a given sub-type
    (``Statistics_detail``).  Sub-types translate to HS prefixes via
    ``_DETAIL_TO_HS_PREFIX``; unmapped sub-types fall through to the
    material's primary HS prefix (caller resolves the default).

    Field naming caveat (Issue 7.3): the output field is named
    ``production_share`` for column-name parity with the parser's other
    share outputs, but the value here is a fraction of *US imports*,
    NOT global production.  Downstream this lands in
    ``HsCodeProductionShare.production_share`` with ``market_scope='us'``
    — the table column shares the naming compromise.  Treat as
    "share of supply through this channel" rather than literal production.

    Bounded values (Issue 7.5): shares are parsed with
    ``_parse_percent_with_bound`` so bounded estimates like ``"<10"``
    resolve to a midpoint (5.0%) rather than collapsing to the bound
    value via ``_parse_value``.  Real MCS 2026 import-source data
    doesn't include bounded values today, so this is defensive against
    future format variation.

    Per-product methodology gap (Issue 7.7 — DEFERRED): for tracked
    chapters other than SILICON, the per-sub-type HS resolution falls
    through to ``hs_prefix=""``.  The CLI then collapses to the
    material's primary HS prefix, so per-product import attribution
    (e.g. ANTIMONY's Ore vs Oxide vs Unwrought metal) is lost in
    storage.  Closing this gap requires partner-curated
    ``_DETAIL_TO_HS_PREFIX`` entries for top-impact chapters (ANTIMONY,
    NIOBIUM, RARE EARTHS, TITANIUM, TUNGSTEN).  Tracked as a follow-up
    task; not blocking material-level geopolitical scoring today.

    Returns a list of:
        {
          "hs_code_prefix": str,
          "country_code": str (ISO-2),
          "production_share": float (0.0–1.0),
          "reference_year_range": str (e.g. "2021–24"),
          "type_substring": str (verbatim Statistics_detail),
        }
    """
    rows = [r for r in chapter_rows if r.get("Section") == _IMPORT_SECTION]
    if not rows:
        return []

    sources: list[dict] = []
    for r in rows:
        country = r.get("Country", "").strip()
        # Issue 7.1 — explicit aggregate-row filter (was an accidental
        # fall-through via the country-resolution step previously).
        if country.lower() in _IMPORT_SOURCE_AGGREGATE_COUNTRIES:
            continue
        iso2 = _resolve_country(country)
        if iso2 is None:
            continue
        share_pct = _parse_percent_with_bound(r.get("Value", ""))
        if share_pct is None:
            continue
        # Issue 7.4 — clamp to [0.0, 1.0] so corrupted or
        # bound-defying values can't propagate impossible shares.
        share_fraction = max(0.0, min(1.0, share_pct / 100.0))

        detail = (r.get("Statistics_detail") or "").lower()
        # Resolve sub-type to HS prefix.
        hs_prefix = None
        for (cfg_chapter, cfg_detail), prefix in _DETAIL_TO_HS_PREFIX.items():
            if cfg_chapter == chapter and cfg_detail in detail:
                hs_prefix = prefix
                break
        if hs_prefix is None:
            # No sub-type-specific mapping; signal "unresolved" so the
            # CLI's keyword resolver + primary-fallback chain can pick a
            # default.  Empty string (not None) for backwards compat
            # with the CLI's ``if hs_prefix:`` truthy check; see Issue
            # 7.6 follow-up for a cleaner sentinel.
            hs_prefix = ""

        # Issue 7.2 — MCS 2026 always publishes Year='2021–24' here, so
        # the previous hardcoded fallback is unreachable in real data.
        # Drop the literal fallback; if Year is somehow missing, pass an
        # empty string and let the caller decide what to do.
        year_range = r.get("Year", "").strip()
        sources.append({
            "hs_code_prefix": hs_prefix,
            "country_code": iso2,
            "production_share": round(share_fraction, 6),
            "reference_year_range": year_range,
            "type_substring": detail or "(unspecified)",
        })

    return sources


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse_mcs2026_csv(filepath: str | Path) -> list[dict]:
    """Parse MCS 2026 long-format CSV and return one record per chapter.

    Phase B refactor (May 2026): no longer carries chapter→canonical
    mapping (that's in ``material_source_aliases`` now) or static
    material metadata (that's seeded directly into the ``materials``
    table).  This function emits raw signal records keyed by
    ``source_name``; the CLI resolves to canonical materials via the
    alias table, fills in fallback HS prefixes from
    ``materials.hs_codes``, and writes the upserts.

    Reserve_life_index unit assumption (Issue 8.7): the function
    computes ``world_reserves / world_prod`` and assumes both come from
    the same unit-of-measure.  MCS consistently publishes both in
    tonnes for tracked materials, but the function does not validate
    this.  ``_RLI_MIN_PLAUSIBLE`` catches gross unit-scale mismatches
    (returns None when the ratio looks unphysically small) but doesn't
    detect a numerator/denominator unit-cross.

    Output contract — one record per chapter present in the CSV.

    NOTE (Issue 8.5): the field list documented below must match the
    dict literal in ``results.append({...})`` at the bottom of the
    per-chapter loop.  Adding or renaming a key needs to happen in
    both places.

    CLI-consumed fields (cli.py ingest_usgs_cmd reads each of these into
    a downstream DB write)::

        {
          "source_system": "mcs_2026_csv",
          "source_name":   str,    # raw chapter heading e.g. "ALUMINUM"
          "criticality_score":     float | None,
          "hhi_score":             float | None,
          "reserve_hhi_score":     float | None,
          "reserve_life_index":    float | None,
          "production_yoy_pct":    float | None,
          "capacity_utilization":  float | None,
          "us_net_import_reliance": float | None,
          "apparent_consumption":  float | None,
          "price_unit_usgs":       str | None,    # "per_lb" / "per_kg" / etc.
          "prices":                list[dict],     # full Salient Price observations (2026-06-14)
          "price_yoy_pct_derived":   float | None, # signed fraction from primary benchmark
          "price_cagr_5yr_pct_derived": float | None,  # signed fraction over chapter's full span
          "production_shares":     list[dict],     # material-level country shares
          "hs_production_shares":  list[dict],     # sub-type splits per `_DETAIL_TO_HS_PREFIX`
          "us_import_sources":     list[dict],     # raw — hs_code_prefix may be ""
          "capacity_shares":       list[dict],     # per-country capacity rows (Issue 4.1 fix, 2026-05-31).  One entry per (country × detail_type).  detail_type carries the verbatim MCS Statistics_detail string (e.g. "Smelter capacity", "Titanium sponge metal Capacity") so TITANIUM's sponge-metal vs TiO2-pigment capacity remain distinct in the DB.
        }

    Audit-only fields (kept in the output for debugging / sanity-checking
    parser behaviour; verified 2026-05-24 to be NOT consumed by the CLI
    write path; safe to ignore but cheap to produce so they stay)::

        {
          "ranked_countries":      list[str],     # top-N producer ISO-2s
          "latest_year":           int | None,    # year used for headline shares
          "world_total":           float | None,  # sum of country production
          "world_unit":            str | None,    # unit of measure for world_total
          "notes":                 str,           # human-readable parse summary
        }

    Skipped chapters (e.g. ABRASIVES, ARSENIC, ASBESTOS — non-battery)
    are NOT filtered here.  The alias table tells the CLI which to
    resolve and which to skip.  The CLI logs unknown chapters so partner
    can add aliases as needed.
    """
    filepath = Path(filepath)

    # Encoding: try UTF-8 first (future-proofs against a USGS publication
    # format switch) then fall back to cp1252 for the current 2026 file
    # which has em-dashes / en-dashes in section names + detail strings.
    # The full file is read once during sniffing; the second open() reuses
    # whichever encoding succeeded.
    try:
        with open(filepath, encoding="utf-8") as f:
            all_rows = list(csv.DictReader(f))
    except UnicodeDecodeError:
        log.debug("mcs2026_parser.encoding_fallback_cp1252", path=str(filepath))
        with open(filepath, encoding="cp1252") as f:
            all_rows = list(csv.DictReader(f))

    # Index rows by chapter for fast filtering.
    by_chapter: dict[str, list[dict]] = defaultdict(list)
    for r in all_rows:
        by_chapter[(r.get("MCS chapter") or "").strip()].append(r)

    results: list[dict] = []
    for chapter, chapter_rows in by_chapter.items():
        if not chapter or not chapter_rows:
            continue

        # ── Material-level production shares (aggregated across sub-types) ──
        country_prod, country_reserves, _by_year, latest_year, unit = (
            _extract_world_production_per_country(chapter_rows, detail_substring=None)
        )

        criticality = round(_hhi(country_prod), 4) if country_prod else None
        reserve_hhi = round(_hhi(country_reserves), 4) if country_reserves else None

        ranked_countries = [
            iso2 for iso2, _ in sorted(country_prod.items(), key=lambda x: -x[1])
        ]

        world_prod = sum(country_prod.values()) if country_prod else None
        world_reserves = sum(country_reserves.values()) if country_reserves else None

        production_shares: list[dict] = []
        if country_prod and world_prod and world_prod > 0:
            for iso2, vol in country_prod.items():
                production_shares.append({
                    "country_code": iso2,
                    "production_volume": vol,
                    "production_share": round(vol / world_prod, 6),
                    "unit_of_measure": unit or None,
                })

        # Reserve life index — guard against unit-scale mismatches that
        # produce implausibly low values (same logic as the 2025 parser).
        # Floor constant ``_RLI_MIN_PLAUSIBLE`` is module-level (Issue 8.3
        # fix 2026-05-31; was previously redeclared inside this loop).
        reserve_life_index: Optional[float] = None
        if world_reserves and world_prod and world_prod > 0:
            rli_candidate = round(world_reserves / world_prod, 1)
            if rli_candidate >= _RLI_MIN_PLAUSIBLE:
                reserve_life_index = rli_candidate

        # ── Salient (US-domestic) signals ─────────────────────────────────
        salient = _extract_us_salient_signals(chapter_rows)

        # ── Price unit derived from USGS Salient Price row ───────────────
        price_unit_usgs = _extract_price_unit_from_salient(chapter_rows)
        # ── 2026-06-14: capture full Salient Price observations + derived
        # growth rates.  Was only capturing the unit string; the actual
        # year-by-year price values were discarded even though the parser
        # already iterated them.
        prices = _extract_prices_from_salient(chapter_rows)
        price_yoy_pct_derived, price_cagr_5yr_pct_derived = (
            _derive_yoy_and_cagr_from_prices(prices)
        )

        # ── Per-HS-node production shares ─────────────────────────────────
        # Two-path build (refactored 2026-05-09):
        #
        #   Path A — Stage auto-detection (covers ~all launch-list materials):
        #     Walk every world-production row, classify by Statistics_detail
        #     into a supply_chain_stage, group by stage, emit one entry per
        #     (stage × country).  Entries carry ``stage`` and an empty
        #     ``hs_code_prefix``; the CLI resolves the prefix via
        #     ``(material_id, stage)`` lookup at write time.
        #
        #   Path B — Sub-type prefix override (Silicon only):
        #     ``_DETAIL_TO_HS_PREFIX`` still routes ferrosilicon vs silicon
        #     metal to specific prefixes because both are at the same stage
        #     and stage-based lookup can't disambiguate.  These entries
        #     carry an explicit ``hs_code_prefix`` and no ``stage``.
        #
        # Path A overrides the stage-detected output with Path B for any
        # (chapter, detail) pair that's in ``_DETAIL_TO_HS_PREFIX`` — so
        # Silicon's ferrosilicon vs metal split still works without
        # double-writing.  No-op for chapters not in the override dict.
        hs_production_shares: list[dict] = []
        path_b_overrides = {
            cfg_detail
            for (cfg_chapter, cfg_detail) in _DETAIL_TO_HS_PREFIX
            if cfg_chapter == chapter
        }

        # ── Path A: stage auto-detection ──
        # 6-tuple unpacking (was 5-tuple pre-2026-05-31).  data_quality_flag
        # is None for single-bucket stages and 'additive' / 'duplicate_suspect'
        # for stages where the consolidation made a judgment call.
        #
        # Loop variables prefixed ``stage_`` (Issue 8.1/8.2 fix 2026-05-31)
        # to avoid shadowing the material-level ``country_prod`` and
        # ``unit`` bindings from the outer scope.  Pre-fix, ``unit``
        # silently took the last per-stage loop iteration's value and
        # the orchestrator's ``world_unit`` output would report the
        # wrong value if a chapter ever had mixed-stage units.
        for (
            stage, detail_low, stage_country_prod, _yr, stage_unit, dq_flag
        ) in _extract_per_stage_world_production(chapter_rows):
            # Skip if this detail substring is handled by the explicit
            # sub-type override below (Silicon ferrosilicon/silicon metal).
            if any(ovr in detail_low for ovr in path_b_overrides):
                continue
            sub_world = sum(stage_country_prod.values())
            if sub_world <= 0:
                continue
            for iso2, vol in stage_country_prod.items():
                hs_production_shares.append({
                    # Empty prefix signals stage-based lookup at write time.
                    "hs_code_prefix": "",
                    "stage":          stage,
                    "country_code":   iso2,
                    "production_volume": vol,
                    "production_share":  round(vol / sub_world, 6),
                    "unit_of_measure":   stage_unit or None,
                    "type_substring":    detail_low,
                    # Issue 5.2 (2026-05-31): surface the consolidation
                    # judgment so downstream review / future scoring can
                    # gate on TELLURIUM-style duplicate_suspect cases.
                    "data_quality_flag": dq_flag,
                })

        # ── Path B: explicit sub-type prefix overrides ──
        # Iterates ALL _DETAIL_TO_HS_PREFIX entries (not first-match-wins
        # like the import-source loop).  When a chapter has multiple
        # matching entries — e.g. SILICON's ferrosilicon AND silicon
        # metal — each emits its own hs_production_shares row.  This is
        # intentional: the entries route to different HS prefixes so
        # they're disjoint in storage (Issue 8.6 comment 2026-05-31).
        #
        # Path A above skips details handled by Path B via the
        # ``path_b_overrides`` set, so the two paths emit disjoint
        # rows for the same chapter — no double-write (Issue 8.8
        # comment 2026-05-31).
        for (cfg_chapter, cfg_detail), hs_prefix in _DETAIL_TO_HS_PREFIX.items():
            if cfg_chapter != chapter:
                continue
            sub_country_prod, _sub_reserves, _sub_year, _sub_y, sub_unit = (
                _extract_world_production_per_country(
                    chapter_rows, detail_substring=cfg_detail,
                )
            )
            sub_world = sum(sub_country_prod.values()) if sub_country_prod else 0.0
            if sub_country_prod and sub_world > 0:
                for iso2, vol in sub_country_prod.items():
                    hs_production_shares.append({
                        "hs_code_prefix": hs_prefix,
                        # No ``stage`` field — caller uses the explicit prefix.
                        "country_code": iso2,
                        "production_volume": vol,
                        "production_share": round(vol / sub_world, 6),
                        "unit_of_measure": sub_unit or None,
                        "type_substring": cfg_detail,
                    })

        # ── US import sources ─────────────────────────────────────────────
        # Rows where the sub-type doesn't match `_DETAIL_TO_HS_PREFIX`
        # come back with hs_code_prefix="" — the CLI fills the fallback
        # from material.hs_codes[0] after canonical resolution.
        us_import_sources = _extract_us_import_sources(chapter_rows, chapter)

        # ── Per-country capacity (Issue 4.1 fix) ──────────────────────────
        # Differentiated from production: MCS publishes Capacity rows
        # for 8 tracked chapters (ALUMINUM, BISMUTH, GALLIUM, INDIUM,
        # MAGNESIUM METAL, SELENIUM, TELLURIUM, TITANIUM).  One bucket
        # per Statistics_detail; TITANIUM has two (sponge metal + TiO2
        # pigment) which remain distinct in the output and the DB.
        capacity_buckets = _extract_world_capacity_per_country(chapter_rows)
        capacity_shares: list[dict] = []
        for detail_type, cap_country, cap_year, cap_unit in capacity_buckets:
            bucket_world = sum(cap_country.values())
            if bucket_world <= 0:
                continue
            for iso2, vol in cap_country.items():
                capacity_shares.append({
                    "country_code":     iso2,
                    "reference_year":   cap_year,
                    "detail_type":      detail_type,
                    "capacity_volume":  vol,
                    "capacity_share":   round(vol / bucket_world, 6),
                    "unit_of_measure":  cap_unit or None,
                })

        notes_parts = [
            "Source: USGS Mineral Commodity Summaries 2026 long-format CSV.",
            f"Latest production year used: {latest_year}.",
        ]
        # Issue 8.4 fix (2026-05-31): explicit ``is not None`` so a
        # legitimate zero world_total still surfaces in notes.  The
        # other notes branches already use this pattern; this one was
        # inconsistent with a truthy check.
        if world_prod is not None:
            notes_parts.append(f"World total: {world_prod:,.0f} {unit}.")
        if salient["production_yoy_pct"] is not None:
            notes_parts.append(
                f"Production YoY: {salient['production_yoy_pct']:+.1%}."
            )
        if salient["net_import_reliance"] is not None:
            notes_parts.append(
                f"US net import reliance: {salient['net_import_reliance']:.0f}%."
            )
        notes_parts.append(
            "criticality_score = raw HHI of country production shares "
            "(sum of squared shares; range 1/N to 1.0)."
        )
        if ranked_countries:
            notes_parts.append(
                f"Top producing countries: {', '.join(ranked_countries[:5])}."
            )

        # Chapter-level HS prefix re-routing was removed 2026-05-24 along
        # with the now-deleted ``_CHAPTER_HS_PREFIX`` dict.  ``_DETAIL_STAGE_PATTERNS``
        # handles the cases this block used to cover (BAUXITE AND ALUMINA's
        # bauxite mine + alumina refinery rows auto-route via ``stage``).
        # If a future MCS chapter publishes production data the auto-
        # detector can't classify, re-add a small chapter-override
        # mechanism here rather than reviving the empty-dict no-op.

        results.append({
            "source_system":           "mcs_2026_csv",
            "source_name":             chapter,
            # criticality_score and hhi_score intentionally carry the same
            # value today.  criticality_score is the column scoring code
            # consumes; hhi_score is preserved so partner-curated
            # composites (e.g. HHI × import_dependency × strategic-mineral
            # flag) can land in criticality_score without losing the raw
            # HHI signal.  When that divergence happens, do not silently
            # drop the alias — update the CLI's persistence path too.
            "criticality_score":       criticality,
            "hhi_score":               criticality,
            "reserve_hhi_score":       reserve_hhi,
            "reserve_life_index":      reserve_life_index,
            "production_yoy_pct":      salient["production_yoy_pct"],
            "capacity_utilization":    salient["capacity_utilization"],
            "us_net_import_reliance":  salient["net_import_reliance"],
            "apparent_consumption":    salient["apparent_consumption"],
            "price_unit_usgs":         price_unit_usgs,
            # 2026-06-14: full Salient Price time series + derived growth
            # rates.  See ``_extract_prices_from_salient`` for the schema
            # of each entry in ``prices``.  CLI uses these to upsert
            # ``commodity_prices`` rows (source='usgs_mcs') and to fill
            # ``material_criticality_signals.{price_yoy_pct,
            # price_cagr_5yr_pct}`` when the dedicated Fig 10 CSV doesn't
            # carry growth rates for this commodity (most single-source
            # chapters).
            "prices":                  prices,
            "price_yoy_pct_derived":   price_yoy_pct_derived,
            "price_cagr_5yr_pct_derived": price_cagr_5yr_pct_derived,
            "production_shares":       production_shares,
            "hs_production_shares":    hs_production_shares,
            "us_import_sources":       us_import_sources,
            "capacity_shares":         capacity_shares,
            "ranked_countries":        ranked_countries,
            "latest_year":             latest_year,
            "world_total":             world_prod,
            "world_unit":              unit or None,
            "notes":                   " ".join(notes_parts),
        })

    # Per-run consolidation summary (Issue 5.3) — counts how often the
    # same-stage bucket consolidation made a judgment call this run.
    # ``duplicate_suspect`` cases are the ones partner-review should
    # adjudicate (TELLURIUM is the only tracked battery material that
    # currently fires this today).
    dq_counts: Counter = Counter()
    for rec in results:
        for hs in rec.get("hs_production_shares") or []:
            flag = hs.get("data_quality_flag")
            if flag:
                dq_counts[flag] += 1
    log.info(
        "mcs2026_parser.run_summary",
        chapter_records=len(results),
        total_hs_production_shares=sum(
            len(r.get("hs_production_shares") or []) for r in results
        ),
        consolidation_additive_rows=dq_counts[_DQ_ADDITIVE],
        consolidation_duplicate_suspect_rows=dq_counts[_DQ_DUPLICATE_SUSPECT],
        total_capacity_shares=sum(
            len(r.get("capacity_shares") or []) for r in results
        ),
    )

    return results


__all__ = [
    "parse_mcs2026_csv",
    "_DETAIL_TO_HS_PREFIX",
]
