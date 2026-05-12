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
* **5-year time series** (2020–2024 plus 2025 estimate).  YoY trends
  computed from real years instead of just two.
* **US import sources are structured** in the CSV (``Import Sources``
  section).  Previously only the PDF parser had this data.
* **Critical-mineral flag** (``Is critical mineral 2025`` column)
  available as structured input rather than hardcoded.

Encoding
--------
The 2026 CSV uses Windows-1252 (cp1252) — em-dash, en-dash, and other
characters fail under UTF-8.  Always read with ``encoding='cp1252'``.

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
from collections import defaultdict
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# Reuse country name → ISO-2 mapping from the legacy 2025 parser.  USGS
# country names are stable across editions; no need to duplicate.
from app.services.ingestion.seeds.usgs_mcs_parser import _COUNTRY_ISO2


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
# Chapter-level HS prefix attribution
# ---------------------------------------------------------------------------
# When a chapter describes a single supply-chain stage of a canonical
# material that's primarily defined by another chapter (e.g. BAUXITE
# AND ALUMINA describes the ore stage of Aluminum, while the ALUMINUM
# chapter describes refined metal), the chapter's per-country production
# data should land in ``hs_code_production_shares`` keyed by the
# stage-specific HS prefix instead of the material-level
# ``material_production_shares`` table.
#
# Mechanically: when the parser sees a chapter listed here, it ROUTES
# the chapter's per-country production into ``hs_production_shares``
# (with this prefix and ``type_substring="(chapter-level)"``) and
# leaves ``production_shares`` empty for that record.  This avoids
# collision with the primary chapter's material-level shares.
#
# Pair these with ``writes_material_signals=False`` on the alias for
# the same chapter so the CLI also skips material-level signal upserts.

_CHAPTER_HS_PREFIX: dict[str, str] = {
    # Deprecated 2026-05-09: previously routed BAUXITE AND ALUMINA → 2606
    # (Aluminum ore stage).  Now handled by ``_DETAIL_STAGE_PATTERNS``
    # below — "Bauxite, mine production" auto-routes to ore stage and
    # "Alumina, refinery production" auto-routes to intermediate stage,
    # so the chapter no longer needs an explicit override.  Kept the
    # dict empty for future per-chapter overrides.
}


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
    # ── Specific patterns first (must match before the generic catches) ──
    ("alumina, refinery",   "intermediate"),  # BAUXITE → Al2O3 oxide intermediate
    ("bauxite, mine",       "ore"),           # BAUXITE → bauxite ore
    # ── Generic patterns ─────────────────────────────────────────────────
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

_SALIENT_SECTION = "Salient Statistics—United States"  # cp1252 em-dash
_IMPORT_SECTION = "Import Sources"
_WORLD_SECTION_PREFIX = "World "

# Salient Statistics_detail strings we extract for material-level signals.
# Substring match, case-insensitive.
_SALIENT_PATTERNS = {
    # Returns a single-year value for the year in `Year`; we use the
    # latest non-estimated year for capacity / consumption.
    "capacity":          "capacity",                  # production capacity
    "consumption":       "apparent",                  # apparent consumption
    "import_reliance":   "net import reliance",       # net import reliance %
    "yearend_stocks":    "stocks",                    # producer stocks yearend
    "import_volume":     "imports for consumption",   # imports volume
}


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

def _parse_value(value: str) -> Optional[float]:
    """Parse an MCS Value cell.  Returns None for withheld/unavailable.

    Value patterns observed in MCS 2026:
      - numeric with commas: ``"3,640"`` → 3640.0
      - withheld:            ``"W"``    → None
      - zero / unavailable:  ``"—"``    → None  (em-dash)
                             ``"–"``    → None  (en-dash)
                             ``"NA"``   → None
      - greater-than:        ``">95"``  → 95.0  (lower-bound estimate)
      - exponent / status:   ``"E"``    → None  (estimated marker; value missing)

    For bounded percentages (``<25``, ``<50``, ``>50`` etc.) callers that
    know the value is a 0–100 percentage should use
    ``_parse_percent_with_bound`` instead — it converts ``<X`` to a
    midpoint estimate.  The bare ``_parse_value`` strips bounds rather
    than rejecting them, which preserves coarse signal at the cost of
    losing direction (``<50`` and ``>50`` both return 50).
    """
    if value is None:
        return None
    v = value.strip()
    if not v or v in ("W", "NA", "N/A", "E", "—", "–", "-"):
        return None
    # Handle ">95" or ">2,000,000" — strip the ">" and parse as lower bound
    v = v.lstrip(">").lstrip("<").strip()
    v = v.replace(",", "")
    try:
        return float(v)
    except ValueError:
        return None


def _parse_percent_with_bound(value: str) -> Optional[float]:
    """Parse an MCS Value cell that is known to be a 0–100 percentage.

    Same as ``_parse_value`` but converts bounded estimates to midpoints
    so ``<50`` and ``>50`` no longer collapse to the same number:

      - ``"<25"`` → 12.5  (midpoint of 0–25)
      - ``"<50"`` → 25.0  (midpoint of 0–50)
      - ``">50"`` → 75.0  (midpoint of 50–100)
      - ``">95"`` → 97.5  (midpoint of 95–100)

    Used for net-import-reliance and other percent values where direction
    matters more than precision.
    """
    if value is None:
        return None
    v = value.strip()
    if not v or v in ("W", "NA", "N/A", "E", "—", "–", "-"):
        return None
    if v.startswith("<"):
        try:
            upper = float(v[1:].replace(",", "").strip())
        except ValueError:
            return None
        return upper / 2.0
    if v.startswith(">"):
        try:
            lower = float(v[1:].replace(",", "").strip())
        except ValueError:
            return None
        return (lower + 100.0) / 2.0
    try:
        return float(v.replace(",", ""))
    except ValueError:
        return None


def _hhi(country_productions: dict[str, float]) -> float:
    """Normalised HHI from {country: production_volume}.  Range 0–1."""
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
    and excludes "Reserves" / "Capacity" rows.

    When ``detail_substring`` is provided, additionally filters
    ``Statistics_detail`` to rows containing that substring (used for
    sub-type extraction — e.g. ``"ferrosilicon"`` to extract just the
    ferrosilicon sub-type).  When None, aggregates ALL sub-types.

    Returns:
        country_prod:     {iso2: production_volume}  for the latest year
        country_reserves: {iso2: reserves}            for the latest year
        all_country_prod_by_year: {iso2: {year: vol}} for time-series use
        latest_year:      int (4-digit year used for the headline shares)
        unit:             str (unit of measurement, taken from any data row)
    """
    country_prod: dict[str, float] = {}
    country_reserves: dict[str, float] = {}
    by_year: dict[str, dict[int, float]] = defaultdict(dict)
    unit = ""

    # Identify candidate rows (excluding "Reserves" detail_substring matches
    # which carry the reserves data instead of production).
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
        elif "reserves" in stat or "reserves" in detail:
            reserves_rows.append(r)

    # Find the latest year in the production rows (skip estimated for
    # the headline shares — `_pick_latest_year` strips the suffix).
    available_years = [r.get("Year", "") for r in production_rows]
    latest_year = _pick_latest_year(available_years)

    # Build per-country production for the latest year + the time series.
    for r in production_rows:
        iso2 = _resolve_country(r.get("Country", ""))
        if iso2 is None or iso2 == "":
            continue
        # Skip "World total" rows
        country_name_low = (r.get("Country") or "").strip().lower()
        if "world" in country_name_low:
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
        if not unit:
            unit = (r.get("Unit") or "").strip()

    # Reserves — usually a single year (often 2024 in MCS 2026).
    for r in reserves_rows:
        iso2 = _resolve_country(r.get("Country", ""))
        if iso2 is None:
            continue
        if "world" in (r.get("Country") or "").strip().lower():
            continue
        val = _parse_value(r.get("Value", ""))
        if val is not None:
            country_reserves[iso2] = country_reserves.get(iso2, 0.0) + val

    return country_prod, country_reserves, dict(by_year), latest_year, unit


def _classify_detail_stage(detail: str) -> Optional[str]:
    """Return the supply_chain_stage for a Statistics_detail value.

    Walks ``_DETAIL_STAGE_PATTERNS`` in order; returns the first match.
    Returns None if no pattern matches — caller skips the row (typically
    rounding-total rows like "Mine production: rounded").
    """
    if not detail:
        return None
    detail_low = detail.lower()
    if "rounded" in detail_low:
        return None  # totals — caller derives world total from country sum
    for pattern, stage in _DETAIL_STAGE_PATTERNS:
        if pattern in detail_low:
            return stage
    return None


def _extract_per_stage_world_production(
    chapter_rows: list[dict],
) -> list[tuple[str, str, dict[str, float], Optional[int], str]]:
    """Group all "World *" production rows by detected supply_chain_stage.

    Replaces the per-substring loop in ``parse_mcs2026_csv`` with auto-
    detection.  For each supply_chain_stage that has at least one resolvable
    country production row in the latest year, returns ONE entry:

        (stage, detail_substring, country_prod, latest_year, unit)

    where ``country_prod`` is ``{iso2: production_volume}`` for the latest
    available year.  Multiple stages are returned when a chapter publishes
    mixed-stage data in one section (Cu mine + refinery → ore + refined;
    BAUXITE alumina + bauxite → ore + intermediate).

    Ambiguity handling (added 2026-05-09):
    Some chapters publish multiple Statistics_detail strings that all
    classify to the same supply_chain_stage.  Three patterns observed:

      * **Additive** — different products at the same stage that sum into
        a country's total supply.  Example: SODA ASH "natural" +
        "synthetic"; PGM "palladium" + "platinum"; CLAYS "bentonite" +
        "kaolin".
      * **Duplicate** — same physical supply measured under different
        unit conventions.  Example: IRON ORE "iron content" + "usable";
        SELENIUM / TELLURIUM "refinery production" + "concentrate
        equivalent".  Summing here double-counts.
      * **Stage-mixed within ore** — DIATOMITE "mine production" + "mine
        production: processed".  Both technically ore-stage in our
        taxonomy; processed is downstream of mine.

    We sum per (stage × country) across all detail strings in the same
    stage.  Rationale: additive cases are handled correctly; duplicate
    cases double-count both the numerator and denominator equally per
    country, so the resulting country *share* (which is what HHI consumes)
    stays roughly accurate.  Stage-mixed cases get conservatively
    over-counted but they're not in the launch-10 today.  Tradeoff
    documented in ``docs/scoring-audit-2026-05.md``.

    The previous implementation emitted one entry per (stage × detail)
    pair, which caused unique-constraint violations downstream because
    multiple buckets resolve to the same hs_mapping_id at write time and
    try to insert duplicate rows for the same
    (hs_mapping_id × country × year × scope × source) key.
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

    # For each bucket, get its per-country production.
    bucket_extractions: list[
        tuple[str, str, dict[str, float], Optional[int], str]
    ] = []
    for (stage, detail_low), bucket_rows in by_bucket.items():
        country_prod, _r, _by_year, latest_year, unit = (
            _extract_world_production_per_country(bucket_rows, detail_substring=None)
        )
        if not country_prod:
            continue
        bucket_extractions.append(
            (stage, detail_low, country_prod, latest_year, unit or "")
        )

    # Consolidate: SUM per (stage × country) across all buckets sharing
    # a stage.  See docstring for tradeoff rationale.
    by_stage_country: dict[str, dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    by_stage_meta: dict[str, dict] = {}
    by_stage_details: dict[str, list[str]] = defaultdict(list)
    for stage, detail_low, country_prod, latest_year, unit in bucket_extractions:
        for iso2, vol in country_prod.items():
            by_stage_country[stage][iso2] += vol
        # Keep the meta from the bucket with the most countries (most
        # comprehensive geographic coverage) — used for unit + year on the
        # consolidated row, and for the type_substring reported back to
        # the caller.
        prev = by_stage_meta.get(stage)
        if prev is None or len(country_prod) > prev["country_count"]:
            by_stage_meta[stage] = {
                "detail_low": detail_low,
                "latest_year": latest_year,
                "unit": unit,
                "country_count": len(country_prod),
            }
        by_stage_details[stage].append(detail_low)

    # Audit log: when a stage has more than one source detail string, the
    # consolidation is non-trivial.  Recorded so we can review later.
    for stage, details in by_stage_details.items():
        if len(details) > 1:
            log.debug(
                "mcs2026_parser.same_stage_bucket_consolidated",
                stage=stage,
                source_details=sorted(details),
                kept_meta_detail=by_stage_meta[stage]["detail_low"],
                country_count=len(by_stage_country[stage]),
            )

    return [
        (
            stage,
            by_stage_meta[stage]["detail_low"],
            dict(by_stage_country[stage]),
            by_stage_meta[stage]["latest_year"],
            by_stage_meta[stage]["unit"],
        )
        for stage in by_stage_country
    ]


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
        if r.get("Section") == _SALIENT_SECTION
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


def _extract_us_salient_signals(
    chapter_rows: list[dict],
) -> dict[str, Optional[float]]:
    """Extract US-domestic signals from Salient Statistics—United States.

    Returns a dict with keys we care about for material-level scoring:
        - capacity_utilization: production / capacity ratio (0–1) for latest year
        - production_yoy_pct:   (latest − prior) / prior for ANY production stat
        - apparent_consumption: latest year value
        - net_import_reliance:  pct (0–100) for latest year, primary stat

    Unavailable signals return None.  Sub-type splits (e.g. Silicon's
    ferrosilicon vs metal) are present as separate ``Statistics_detail``
    rows; we aggregate by summing across sub-types when computing YoY.
    """
    salient = [r for r in chapter_rows if r.get("Section") == _SALIENT_SECTION]
    if not salient:
        return {
            "capacity_utilization": None,
            "production_yoy_pct": None,
            "apparent_consumption": None,
            "net_import_reliance": None,
        }

    # Bucket rows by Statistics + year.  Sum across sub-types (Statistics_detail).
    by_stat_year: dict[tuple[str, int], float] = defaultdict(float)
    by_stat_year_count: dict[tuple[str, int], int] = defaultdict(int)
    for r in salient:
        stat = (r.get("Statistics") or "").lower()
        year_str = r.get("Year", "")
        m = re.match(r"^\s*(\d{4})", year_str)
        if not m:
            continue
        year = int(m.group(1))
        val = _parse_value(r.get("Value", ""))
        if val is None:
            continue
        by_stat_year[(stat, year)] += val
        by_stat_year_count[(stat, year)] += 1

    def _latest_two(stat_low: str) -> Optional[tuple[int, float, int, float]]:
        """Return (latest_year, latest_val, prior_year, prior_val) for ``stat`` —
        latest non-zero, prior most recent.  None if <2 data points."""
        years = sorted(
            (y for (s, y), v in by_stat_year.items() if s == stat_low and v > 0),
            reverse=True,
        )
        if len(years) < 2:
            return None
        latest, prior = years[0], years[1]
        return (latest, by_stat_year[(stat_low, latest)],
                prior,  by_stat_year[(stat_low, prior)])

    # Capacity utilization — production ÷ capacity for the latest year
    # where both are present.
    capacity_utilization = None
    cap_years = sorted(
        ({y for (s, y) in by_stat_year if "capacity" in s}),
        reverse=True,
    )
    prod_years = sorted(
        ({y for (s, y) in by_stat_year if "production" in s}),
        reverse=True,
    )
    common = set(cap_years) & set(prod_years)
    if common:
        latest_common = max(common)
        cap = sum(v for (s, y), v in by_stat_year.items()
                  if "capacity" in s and y == latest_common)
        prod = sum(v for (s, y), v in by_stat_year.items()
                   if "production" in s and y == latest_common)
        if cap > 0:
            capacity_utilization = round(prod / cap, 4)

    # Production YoY %
    production_yoy_pct = None
    pair = _latest_two("production")
    if pair:
        latest_y, latest_v, _prior_y, prior_v = pair
        if prior_v > 0:
            production_yoy_pct = round((latest_v - prior_v) / prior_v, 4)

    # Apparent consumption — latest value
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

    # Net import reliance — value can be 0–100 in the CSV.
    # NIR uses bounded estimates ("<50", ">50") that need midpoint-aware
    # parsing.  Re-parse the raw rows directly with `_parse_percent_with_bound`
    # rather than relying on the sum/count buckets above (which used the
    # bound-stripping `_parse_value`).  Prefer the "Total" sub-type if
    # present (Silicon has ferrosilicon + silicon metal + Total); fall
    # back to the average across sub-types when no Total row exists.
    nir = None
    nir_rows = [
        r for r in salient
        if "net import reliance" in (r.get("Statistics") or "").lower()
    ]
    if nir_rows:
        # Bucket: {(detail_low, year): value}
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
            # Prefer "Total" sub-type if present.
            total_keys = [
                k for k in nir_by_detail_year
                if k[1] == latest_y and "total" in k[0]
            ]
            if total_keys:
                nir = round(nir_by_detail_year[total_keys[0]], 2)
            else:
                latest_vals = [
                    v for (d, y), v in nir_by_detail_year.items()
                    if y == latest_y
                ]
                nir = round(sum(latest_vals) / len(latest_vals), 2)

    return {
        "capacity_utilization": capacity_utilization,
        "production_yoy_pct": production_yoy_pct,
        "apparent_consumption": apparent_consumption,
        "net_import_reliance": nir,
    }


def _extract_us_import_sources(
    chapter_rows: list[dict],
    chapter: str,
) -> list[dict]:
    """Extract US import sources from the Import Sources section.

    Each row gives a country's % share of US imports for a given sub-type
    (Statistics_detail).  We translate sub-types to HS prefixes via
    `_DETAIL_TO_HS_PREFIX`; unmapped sub-types fall through to the
    material's primary HS prefix.

    Returns a list of:
        {
          "hs_code_prefix": str,
          "country_code": str (ISO-2),
          "production_share": float (0.0–1.0),
          "reference_year_range": str (e.g. "2021–24"),
        }
    """
    rows = [r for r in chapter_rows if r.get("Section") == _IMPORT_SECTION]
    if not rows:
        return []

    sources: list[dict] = []
    for r in rows:
        country = r.get("Country", "").strip()
        # Skip "Total" / "Other" rows
        if country.lower() in ("total", "other", "world total", ""):
            continue
        iso2 = _resolve_country(country)
        if iso2 is None:
            continue
        share_pct = _parse_value(r.get("Value", ""))
        if share_pct is None:
            continue

        detail = (r.get("Statistics_detail") or "").lower()
        # Resolve sub-type to HS prefix.
        hs_prefix = None
        for (cfg_chapter, cfg_detail), prefix in _DETAIL_TO_HS_PREFIX.items():
            if cfg_chapter == chapter and cfg_detail in detail:
                hs_prefix = prefix
                break
        if hs_prefix is None:
            # No sub-type-specific mapping; fall back to the material's
            # primary HS prefix.  Caller resolves which prefix in the
            # caller's hs_codes list.
            hs_prefix = ""  # signal "unresolved" — caller picks default

        year_range = r.get("Year", "").strip() or "2021–24"
        sources.append({
            "hs_code_prefix": hs_prefix,
            "country_code": iso2,
            "production_share": round(share_pct / 100.0, 6),
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

    Output contract — one record per chapter present in the CSV::

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
          "production_shares":     list[dict],     # material-level country shares
          "hs_production_shares":  list[dict],     # sub-type splits per `_DETAIL_TO_HS_PREFIX`
          "us_import_sources":     list[dict],     # raw — hs_code_prefix may be ""
          "ranked_countries":      list[str],
          "latest_year":           int | None,
          "world_total":           float | None,
          "world_unit":            str | None,
          "notes":                 str,
        }

    Skipped chapters (e.g. ABRASIVES, ARSENIC, ASBESTOS — non-battery)
    are NOT filtered here.  The alias table tells the CLI which to
    resolve and which to skip.  The CLI logs unknown chapters so partner
    can add aliases as needed.
    """
    filepath = Path(filepath)

    # cp1252 encoding due to em-dash / en-dash characters in section names
    # and detail strings.
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
        _RLI_MIN_PLAUSIBLE = 2.0
        reserve_life_index: Optional[float] = None
        if world_reserves and world_prod and world_prod > 0:
            rli_candidate = round(world_reserves / world_prod, 1)
            if rli_candidate >= _RLI_MIN_PLAUSIBLE:
                reserve_life_index = rli_candidate

        # ── Salient (US-domestic) signals ─────────────────────────────────
        salient = _extract_us_salient_signals(chapter_rows)

        # ── Price unit derived from USGS Salient Price row ───────────────
        price_unit_usgs = _extract_price_unit_from_salient(chapter_rows)

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
        for stage, detail_low, country_prod, _yr, unit in (
            _extract_per_stage_world_production(chapter_rows)
        ):
            # Skip if this detail substring is handled by the explicit
            # sub-type override below (Silicon ferrosilicon/silicon metal).
            if any(ovr in detail_low for ovr in path_b_overrides):
                continue
            sub_world = sum(country_prod.values())
            if sub_world <= 0:
                continue
            for iso2, vol in country_prod.items():
                hs_production_shares.append({
                    # Empty prefix signals stage-based lookup at write time.
                    "hs_code_prefix": "",
                    "stage":          stage,
                    "country_code":   iso2,
                    "production_volume": vol,
                    "production_share":  round(vol / sub_world, 6),
                    "unit_of_measure":   unit or None,
                    "type_substring":    detail_low,
                })

        # ── Path B: explicit sub-type prefix overrides ──
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

        notes_parts = [
            "Source: USGS Mineral Commodity Summaries 2026 long-format CSV.",
            f"Latest production year used: {latest_year}.",
        ]
        if world_prod:
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
            "criticality_score = normalised HHI of country production shares."
        )
        if ranked_countries:
            notes_parts.append(
                f"Top producing countries: {', '.join(ranked_countries[:5])}."
            )

        # ── Chapter-level HS prefix re-routing (deprecated 2026-05-09) ───
        # ``_CHAPTER_HS_PREFIX`` is now empty by default — stage auto-
        # detection handles former entries (BAUXITE AND ALUMINA's bauxite
        # mine + alumina refinery rows now route via ``stage`` to the
        # appropriate Aluminum HS mappings without an explicit override).
        # Block kept in place for future per-chapter overrides if a
        # chapter publishes production data the auto-detector can't
        # classify.  Empty dict → block is a no-op today.
        chapter_prefix = _CHAPTER_HS_PREFIX.get(chapter)
        if chapter_prefix and production_shares:
            for ps in production_shares:
                hs_production_shares.append({
                    "hs_code_prefix":   chapter_prefix,
                    "country_code":     ps["country_code"],
                    "production_volume": ps["production_volume"],
                    "production_share": ps["production_share"],
                    "unit_of_measure":  ps.get("unit_of_measure"),
                    "type_substring":   "(chapter-level)",
                })
            production_shares = []  # don't double-write at the material level

        results.append({
            "source_system":           "mcs_2026_csv",
            "source_name":             chapter,
            "criticality_score":       criticality,
            "hhi_score":               criticality,
            "reserve_hhi_score":       reserve_hhi,
            "reserve_life_index":      reserve_life_index,
            "production_yoy_pct":      salient["production_yoy_pct"],
            "capacity_utilization":    salient["capacity_utilization"],
            "us_net_import_reliance":  salient["net_import_reliance"],
            "apparent_consumption":    salient["apparent_consumption"],
            "price_unit_usgs":         price_unit_usgs,
            "production_shares":       production_shares,
            "hs_production_shares":    hs_production_shares,
            "us_import_sources":       us_import_sources,
            "ranked_countries":        ranked_countries,
            "latest_year":             latest_year,
            "world_total":             world_prod,
            "world_unit":              unit or None,
            "notes":                   " ".join(notes_parts),
        })

    return results


__all__ = [
    "parse_mcs2026_csv",
    "_DETAIL_TO_HS_PREFIX",
]
