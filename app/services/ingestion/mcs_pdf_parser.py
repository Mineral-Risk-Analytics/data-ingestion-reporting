"""
MCS PDF Parser — Phase 2 of the HS code redesign.

Parses USGS Mineral Commodity Summaries annual PDF using pdfplumber.
Extracts per-commodity:
  1. Tariff tables (10-digit US HTS codes) → hs_code_material_mappings (market_scope='us')
  2. Derived 6-digit global rows → hs_code_material_mappings (market_scope='global')
  3. Production leaders → hs_code_production_shares (market_scope='global')
  4. Import sources → hs_code_production_shares (market_scope='us')
  5. Salient notes → MaterialCriticalitySignal.metadata_json

Insert order per commodity (must follow this sequence):
  1. 10-digit US HTS rows (INSERT ... ON CONFLICT DO NOTHING)
  2. Derived 6-digit global rows (INSERT ... ON CONFLICT DO NOTHING)
  3. SELECT back hs_mapping_id for every inserted or pre-existing row
  4. Insert hs_code_production_shares using those IDs

Steps 1–2 must complete before step 4 because hs_code_production_shares.hs_mapping_id
is a non-nullable FK.  An ON CONFLICT DO NOTHING row that already exists must still have
its ID retrieved via a follow-up SELECT — do NOT assume the ID from the initial insert.

See docs/hs-code-redesign.md § MCS PDF Parser Plan for the full specification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.supply import HsCodeMaterialMapping, HsCodeProductionShare, Material
from app.models.criticality_signal import MaterialCriticalitySignal

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Commodity name normalisation — PDF all-caps headings → materials.canonical_name
# ---------------------------------------------------------------------------

_MCS_COMMODITY_MAP: dict[str, str] = {
    "COBALT":                   "Cobalt",
    "LITHIUM":                  "Lithium",
    "NICKEL":                   "Nickel",
    "MANGANESE":                "Manganese",
    "GRAPHITE (NATURAL)":       "Natural Graphite",
    "RARE EARTHS":              "Rare Earth Elements",
    "PLATINUM-GROUP METALS":    "Platinum-Group Metals",
    "ALUMINUM":                 "Aluminum",
    "COPPER":                   "Copper",
    "SILICON":                  "Silicon (Anode Grade)",
    "TITANIUM":                 "Titanium",
    "CHROMIUM":                 "Chromium",
    "TUNGSTEN":                 "Tungsten",
    "MOLYBDENUM":               "Molybdenum",
    "VANADIUM":                 "Vanadium",
    "NIOBIUM":                  "Niobium",
    "TANTALUM":                 "Tantalum",
    "TIN":                      "Tin",
    "ZINC":                     "Zinc",
    "BORON":                    "Boron",
    "FLUORSPAR":                "Fluorspar",
    "MAGNESIUM":                "Magnesium",
    "IRON ORE":                 "Iron Ore (LFP Grade)",
    "SILVER":                   "Silver",
    "GALLIUM":                  "Gallium",
    "GERMANIUM":                "Germanium",
    "INDIUM":                   "Indium",
    "ANTIMONY":                 "Antimony",
    "ZIRCONIUM AND HAFNIUM":    "Zirconium",
    # Add entries as new commodities are covered by MCS
}

# ---------------------------------------------------------------------------
# Stage preference for world mine production share linkage.
# Maps canonical material name → preferred supply_chain_stage for the
# hs_code_production_shares row that represents world mine production.
# 'ore' is correct for most mining commodities.  A few processed materials
# are reported at the 'refined' or 'battery_grade' stage instead.
# ---------------------------------------------------------------------------

_MCS_PRODUCTION_STAGE_PREFERENCE: dict[str, str] = {
    "Cobalt": "ore",
    "Lithium": "ore",           # spodumene / brine extraction
    "Nickel": "ore",
    "Manganese": "ore",
    "Natural Graphite": "ore",
    "Rare Earth Elements": "ore",
    "Platinum-Group Metals": "ore",
    "Aluminum": "ore",          # bauxite
    "Copper": "ore",
    "Silicon (Anode Grade)": "refined",
    "Titanium": "ore",
    "Chromium": "ore",
    "Tungsten": "ore",
    "Molybdenum": "ore",
    "Vanadium": "ore",
    "Niobium": "ore",
    "Tantalum": "ore",
    "Tin": "ore",
    "Zinc": "ore",
    "Boron": "ore",
    "Fluorspar": "ore",
    "Magnesium": "ore",
    "Iron Ore (LFP Grade)": "ore",
    "Silver": "ore",
    "Gallium": "refined",
    "Germanium": "refined",
    "Indium": "refined",
    "Antimony": "ore",
    "Zirconium": "ore",
}

# ---------------------------------------------------------------------------
# Stage assignment for parser-derived 6-digit and 10-digit rows
# ---------------------------------------------------------------------------
# Resolution order at insert time:
#   1. Look up (hs_code_prefix, canonical_material_name) in _HS_6DIGIT_STAGE_OVERRIDE.
#      For 10-digit prefixes the truncated 6-digit form is also tried.
#   2. Fall back to the 4-digit parent's stage in hs_code_material_mappings
#      filtered by the same material_id.
#   3. If neither resolves, the row is written with stage=NULL and a structured
#      log line is emitted so coverage gaps are visible.
#
# Override scope rule: only declare an entry when the 6-digit (or 10-digit) form
# resolves to a more specific stage than its 4-digit parent in `_MAPPINGS`,
# OR when the 4-digit parent does not exist in `_MAPPINGS` for this material.
# Otherwise leave it to inheritance — fewer entries to maintain, less drift risk.
#
# Confidence buckets:
#   ▶ "high"          — major battery materials, supply chain stage well established
#   ▶ "needs_review"  — listed for completeness; partner should confirm before
#                       this is the authoritative stage on a persisted row
# Marking is informal — both buckets get applied; the comment is a flag for
# downstream review, not a runtime branch.
# ---------------------------------------------------------------------------

_HS_6DIGIT_STAGE_OVERRIDE: dict[tuple[str, str], tuple[str, int]] = {
    # ── Lithium ─────────────────────────────────────────── (high confidence)
    # 282520 = lithium hydroxide. Parent 2825 already battery_grade in seed,
    # so this is a no-op-but-explicit entry to make the intent obvious to readers.
    ("282520", "Lithium"): ("battery_grade", 5),
    # 283691 = lithium carbonate. Parent 2836 (carbonates) is mixed across
    # materials; explicit override needed.
    ("283691", "Lithium"): ("battery_grade", 5),
    # 282739 = lithium chloride. Parent 2827 (chlorides) is mixed.
    ("282739", "Lithium"): ("intermediate", 3),
    # 280512 = lithium metal. Parent 2805 in seed at refined; explicit for clarity.
    ("280512", "Lithium"): ("refined", 4),

    # ── Cobalt ──────────────────────────────────────────── (high confidence)
    # 282200 = cobalt oxides and hydroxides. Parent 2822 not in 4-digit seed
    # for Cobalt → must override.
    ("282200", "Cobalt"): ("battery_grade", 5),
    # 283329 = cobalt sulfate. Parent 2833 (sulfates) is mixed across materials.
    ("283329", "Cobalt"): ("battery_grade", 5),
    # 810520 = cobalt mattes / unwrought. Parent 8105 in seed at intermediate.
    ("810520", "Cobalt"): ("intermediate", 3),
    # 260500 = cobalt ores. Parent 2605 in seed at ore.
    ("260500", "Cobalt"): ("ore", 1),

    # ── Nickel ──────────────────────────────────────────── (high confidence)
    # 283324 = nickel sulfate. Parent 2833 (sulfates) mixed → override.
    ("283324", "Nickel"): ("battery_grade", 5),
    # 750100 = nickel mattes / oxide sinters. Parent 7501 in seed at intermediate.
    ("750100", "Nickel"): ("intermediate", 3),
    # 750210 / 750220 = unwrought nickel. Parent 7502 in seed at refined.
    ("750210", "Nickel"): ("refined", 4),
    ("750220", "Nickel"): ("refined", 4),
    # 260400 = nickel ores. Parent 2604 in seed at ore.
    ("260400", "Nickel"): ("ore", 1),

    # ── Manganese ───────────────────────────────────────── (high confidence)
    # 283329 = manganese sulfate. Note: same prefix as Cobalt sulfate; this
    # works because the override key is (prefix, canonical_name).
    ("283329", "Manganese"): ("battery_grade", 5),
    # 260200 = manganese ores.
    ("260200", "Manganese"): ("ore", 1),

    # ── Copper ──────────────────────────────────────────── (high confidence)
    ("260300", "Copper"): ("ore", 1),
    ("740200", "Copper"): ("intermediate", 3),
    ("740311", "Copper"): ("refined", 4),  # cathode
    ("740319", "Copper"): ("refined", 4),

    # ── Aluminum ────────────────────────────────────────── (high confidence)
    ("260600", "Aluminum"): ("ore", 1),    # bauxite
    ("281820", "Aluminum"): ("intermediate", 3),  # alumina (Al2O3)
    ("760110", "Aluminum"): ("refined", 4),       # primary unwrought
    ("760120", "Aluminum"): ("refined", 4),       # alloyed unwrought

    # ── Iron Ore (LFP Grade) ────────────────────────────── (high confidence)
    ("260111", "Iron Ore (LFP Grade)"): ("ore", 1),
    ("260112", "Iron Ore (LFP Grade)"): ("ore", 1),

    # ── Natural Graphite ────────────────────────────────── (high confidence)
    ("250410", "Natural Graphite"): ("ore", 1),
    ("250490", "Natural Graphite"): ("ore", 1),

    # ── Rare Earth Elements ─────────────────────────────── (high confidence)
    ("280530", "Rare Earth Elements"): ("refined", 4),       # REE metals unwrought
    ("284690", "Rare Earth Elements"): ("battery_grade", 5),  # REE compounds
    ("261790", "Rare Earth Elements"): ("ore", 1),           # other ores

    # ── Titanium ────────────────────────────────────────── (high confidence)
    ("261400", "Titanium"): ("ore", 1),
    ("810820", "Titanium"): ("refined", 4),
    ("720291", "Titanium"): ("intermediate", 3),  # ferrotitanium

    # ── Tungsten ────────────────────────────────────────── (high confidence)
    ("261100", "Tungsten"): ("ore", 1),
    ("810194", "Tungsten"): ("intermediate", 3),  # APT
    ("810199", "Tungsten"): ("refined", 4),

    # ── Silicon (Anode Grade) ───────────────────────────── (needs_review)
    # MCS does not cleanly separate metallurgical Si vs polysilicon vs
    # battery-grade Si at the 6-digit level. 280461 covers metallurgical Si.
    # Real battery-grade Si is a small slice of refined production. Partner
    # should confirm whether stage=refined is appropriate or if it should be
    # battery_grade in the battery context.
    ("280461", "Silicon (Anode Grade)"): ("refined", 4),     # needs_review
    ("280469", "Silicon (Anode Grade)"): ("refined", 4),     # needs_review

    # ── Vanadium / Niobium / Tantalum / Zirconium ───────── (high confidence)
    # All four share 4-digit ore prefix 2615 (in seed at ore for each).
    # 6-digit subdivisions break out by material — each is still ore at this stage.
    ("261500", "Vanadium"): ("ore", 1),
    ("261500", "Niobium"): ("ore", 1),
    ("261500", "Tantalum"): ("ore", 1),
    ("261500", "Zirconium"): ("ore", 1),
    # Refined / intermediate forms
    ("810292", "Molybdenum"): ("intermediate", 3),  # ferromolybdenum
    ("720270", "Molybdenum"): ("intermediate", 3),
    ("720292", "Vanadium"): ("intermediate", 3),    # ferrovanadium
    ("720241", "Chromium"): ("intermediate", 3),    # ferrochromium
    ("720249", "Chromium"): ("intermediate", 3),

    # ── Boron ─────────────────────────────────────────── (needs_review)
    # 252810 / 252890 are natural borate concentrates (ore).
    # 281000 oxides of boron / boric acid is processed = intermediate.
    # Battery-grade boron compounds (LiBOB precursors) are niche; not seeded.
    # Partner should confirm whether processed borates should ever be marked
    # battery_grade in the battery scoring context.
    ("252810", "Boron"): ("ore", 1),                # natural borate
    ("252890", "Boron"): ("ore", 1),
    ("281000", "Boron"): ("intermediate", 3),       # processed boric oxide

    # ── PGMs / Silver / Antimony / Tin / Zinc / Mg / Fluorspar ─
    # These materials' 6-digit subdivisions inherit cleanly from their
    # 4-digit parent's seed assignment in `_MAPPINGS`. No overrides needed.
    # Add entries here if the parser produces 6-digit codes whose stage is
    # genuinely different from the 4-digit parent for the same material.

    # ── Gallium / Germanium / Indium / Tellurium / Selenium ─ (needs_review)
    # All trade through chapter 8112 (minor metals, refined) or 2804 (non-metals).
    # 6-digit subdivisions like 811292 (gallium unwrought) inherit refined from
    # 8112 in seed. Add explicit entries only if the parser produces sub-prefixes
    # the seed doesn't cover. Partner should confirm 6-digit Ga/Ge/In codes.
}

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# 10-digit US HTS code in HHHH.SS.XXXX format
_HTS_CODE_RE = re.compile(r'\b(\d{4}\.\d{2}\.\d{4})\b')

# Import sources section header — year range varies by material and edition
# e.g. "Import Sources (2021–24):" or "Import Sources (2020-2024):"
_IMPORT_SOURCES_RE = re.compile(
    r'Import Sources\s*\(\d{4}[–\-]\d{2,4}\)\s*:',
    re.IGNORECASE,
)

# Country percentage in import sources list  e.g. "South Africa, 28%"
_IMPORT_PCT_RE = re.compile(r'([A-Za-z][A-Za-z\s\(\)\-\.\']+?),\s*(\d+(?:\.\d+)?)\s*%')

# Commodity section heading — a line of ALL CAPS text (2+ words or known single-word).
#
# The optional trailing ``[\d¹²³⁴⁵⁶⁷⁸⁹⁰]+`` group tolerates USGS footnote markers
# that appear on the first page of every commodity chapter (e.g. ``ALUMINUM¹``,
# ``BAUXITE AND ALUMINA¹``, ``IRON ORE¹``).  When ``pdfplumber`` extracts the
# text, the unicode superscript usually renders as a regular digit, so the
# anchor ends up looking like ``ALUMINUM1`` — without this tolerance the regex
# rejects it and the parser silently skips the entire chapter, picking up only
# the second-page continuation header (which sits AFTER the Tariff and Salient
# Statistics sections, so the parsed body is the back-half of the chapter).
# This was the root cause of the 2026-05 zero-row results for Aluminum,
# Bauxite/Alumina, and Iron Ore.  Group 1 captures the heading text without
# the footnote so callers can use it for `_MCS_COMMODITY_MAP` lookup.
_COMMODITY_HEADING_RE = re.compile(
    r'^([A-Z][A-Z\s\(\)\-]+[A-Z])(?:[\d¹²³⁴⁵⁶⁷⁸⁹⁰]+)?\s*$'
)

# Country+tonnage row in world mine production table
# Handles numbers with commas and optional trailing 'e' (estimated)
_PRODUCTION_ROW_RE = re.compile(
    r'^([A-Za-z][A-Za-z\s\(\)\-\.\']+?)\s{2,}([\d,]+(?:,\d{3})*(?:\.\d+)?)\s*e?\s*'
    r'([\d,]+(?:,\d{3})*(?:\.\d+)?)?\s*e?\s*$'
)

# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------

@dataclass
class TariffEntry:
    """One row from a MCS commodity tariff table."""
    description: str
    hts_code: str          # normalised, no dots: e.g. "2605000000"
    hts_code_raw: str      # as printed: e.g. "2605.00.0000"
    confidence: float = 1.0


@dataclass
class ProductionShare:
    """One country row from MCS world mine production table."""
    country_name: str      # raw name from PDF; resolved to ISO-2 by the seeder
    production_value: float
    reference_year: int
    is_estimate: bool = False


@dataclass
class ImportSource:
    """One country from MCS import sources section."""
    country_name: str
    share: float           # 0.0–1.0
    reference_year: int    # end year of the YYYY–YY range


@dataclass
class CommoditySection:
    """All parsed data for one MCS commodity section."""
    heading: str                         # all-caps heading as found in PDF
    canonical_name: str                  # resolved via _MCS_COMMODITY_MAP
    tariff_entries: list[TariffEntry] = field(default_factory=list)
    production_leaders: list[ProductionShare] = field(default_factory=list)
    import_sources: list[ImportSource] = field(default_factory=list)
    salient_notes: str = ""


# ---------------------------------------------------------------------------
# Main parser class
# ---------------------------------------------------------------------------

class MCSPdfParser:
    """
    Parses USGS Mineral Commodity Summaries annual PDF.

    Page layout: each commodity occupies 2 pages in consistent order.
    Extraction targets per commodity:
      - tariff_table: list[TariffEntry(description, hts_code, confidence)]
      - production_leaders: list[ProductionShare(country_name, production_value, year)]
      - import_sources: list[ImportSource(country_name, share, year)]
      - salient_notes: str (raw text from "Salient Statistics" section)

    Usage::

        parser = MCSPdfParser(Path("mcs2026.pdf"), reference_year=2025)
        sections = parser.parse()
        for section in sections:
            print(section.canonical_name, len(section.tariff_entries))

    For DB seeding, call :meth:`seed_to_db` directly.
    """

    def __init__(self, path: Path, reference_year: int) -> None:
        self._path = path
        self._reference_year = reference_year

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self) -> list[CommoditySection]:
        """
        Parse all commodity sections from the PDF.

        Returns one :class:`CommoditySection` per recognised commodity heading.
        Headings not in ``_MCS_COMMODITY_MAP`` are silently skipped.
        """
        try:
            import pdfplumber  # noqa: PLC0415 — optional heavy dep
        except ImportError as exc:
            raise RuntimeError(
                "pdfplumber is required for MCS PDF parsing.  "
                "Install with: pip install pdfplumber"
            ) from exc

        sections: list[CommoditySection] = []
        full_text_pages: list[str] = []

        with pdfplumber.open(str(self._path)) as pdf:
            for page in pdf.pages:
                full_text_pages.append(page.extract_text() or "")

        combined = "\n\n--- PAGE BREAK ---\n\n".join(full_text_pages)
        raw_sections = self._split_into_commodity_sections(combined)

        for heading, body in raw_sections.items():
            canonical = _MCS_COMMODITY_MAP.get(heading.strip())
            if canonical is None:
                log.debug("mcs_pdf_parser.unknown_heading", heading=heading)
                continue

            section = CommoditySection(heading=heading, canonical_name=canonical)
            section.tariff_entries = self._parse_tariff_table(body)
            section.production_leaders = self._parse_production_leaders(body)
            section.import_sources = self._parse_import_sources(body)
            section.salient_notes = self._extract_salient_notes(body)

            if not section.tariff_entries and not section.production_leaders:
                log.warning(
                    "mcs_pdf_parser.empty_section",
                    canonical_name=canonical,
                    heading=heading,
                )

            sections.append(section)
            log.info(
                "mcs_pdf_parser.section_parsed",
                commodity=canonical,
                tariff_entries=len(section.tariff_entries),
                production_leaders=len(section.production_leaders),
                import_sources=len(section.import_sources),
            )

        return sections

    def seed_to_db(self, session: Session, *, dry_run: bool = False) -> dict[str, Any]:
        """
        Parse the PDF and seed the extracted data into the database.

        Insert order per commodity (enforced here):
          1. 10-digit US HTS rows → hs_code_material_mappings (market_scope='us')
          2. Derived 6-digit global rows → hs_code_material_mappings (market_scope='global',
             ON CONFLICT DO NOTHING)
          3. SELECT back hs_mapping_id for every row (including pre-existing)
          4. Insert hs_code_production_shares using those IDs

        Country name resolution uses Country.common_names loaded from the DB once at
        the start of seeding.

        Returns a summary dict with counts per commodity.
        """
        sections = self.parse()
        country_map = self._build_country_map(session)
        material_map = self._build_material_map(session)

        # Cached 4-digit-parent stage lookups, shared across all materials in
        # this seed run.  Populated lazily by `_assign_stage`.
        parent_stage_cache: dict[tuple[str, int], tuple[Optional[str], Optional[int]]] = {}

        stats: dict[str, dict[str, int]] = {}

        for section in sections:
            material_id = material_map.get(section.canonical_name)
            if material_id is None:
                log.warning(
                    "mcs_pdf_parser.material_not_found",
                    canonical_name=section.canonical_name,
                )
                continue

            s: dict[str, int] = {
                "us_rows_inserted": 0,
                "global_rows_inserted": 0,
                "production_shares_inserted": 0,
                "import_source_shares_inserted": 0,
                "stage_assigned_us": 0,
                "stage_assigned_global": 0,
                "stage_unresolved_us": 0,
                "stage_unresolved_global": 0,
            }

            # --- Step 1: insert 10-digit US HTS rows ---
            us_id_map: dict[str, int] = {}  # hts_code (no dots) → hs_mapping_id
            for entry in section.tariff_entries:
                hts_nodots = entry.hts_code   # already normalised
                stage, stage_seq = self._assign_stage(
                    session,
                    hs_code_prefix=hts_nodots,
                    digit_count=10,
                    material_id=material_id,
                    canonical_name=section.canonical_name,
                    parent_stage_cache=parent_stage_cache,
                )
                if stage is not None:
                    s["stage_assigned_us"] += 1
                else:
                    s["stage_unresolved_us"] += 1
                row_id = self._upsert_mapping_row(
                    session,
                    hs_code_prefix=hts_nodots,
                    material_id=material_id,
                    description=entry.description,
                    confidence=entry.confidence,
                    digit_count=10,
                    market_scope="us",
                    supply_chain_stage=stage,
                    stage_sequence=stage_seq,
                    dry_run=dry_run,
                )
                if row_id is not None:
                    us_id_map[hts_nodots] = row_id
                    s["us_rows_inserted"] += 1

            # --- Step 2: derive 6-digit global rows and insert ON CONFLICT DO NOTHING ---
            # Build: 6-digit prefix → max confidence among all 10-digit codes that share it
            six_digit_conf: dict[str, float] = {}
            six_digit_desc: dict[str, str] = {}
            for entry in section.tariff_entries:
                six = entry.hts_code[:6]
                if six not in six_digit_conf or entry.confidence > six_digit_conf[six]:
                    six_digit_conf[six] = entry.confidence
                    six_digit_desc[six] = entry.description

            global_id_map: dict[str, int] = {}  # 6-digit prefix → hs_mapping_id
            for six_prefix, conf in six_digit_conf.items():
                stage, stage_seq = self._assign_stage(
                    session,
                    hs_code_prefix=six_prefix,
                    digit_count=6,
                    material_id=material_id,
                    canonical_name=section.canonical_name,
                    parent_stage_cache=parent_stage_cache,
                )
                if stage is not None:
                    s["stage_assigned_global"] += 1
                else:
                    s["stage_unresolved_global"] += 1
                row_id = self._upsert_mapping_row(
                    session,
                    hs_code_prefix=six_prefix,
                    material_id=material_id,
                    description=six_digit_desc[six_prefix],
                    confidence=conf,
                    digit_count=6,
                    market_scope="global",
                    supply_chain_stage=stage,
                    stage_sequence=stage_seq,
                    dry_run=dry_run,
                )
                if row_id is not None:
                    global_id_map[six_prefix] = row_id
                    s["global_rows_inserted"] += 1

            if not dry_run:
                session.flush()

            # --- Step 3: SELECT back IDs for all rows (covers ON CONFLICT DO NOTHING) ---
            us_id_map = self._select_back_ids(
                session, material_id=material_id, market_scope="us",
                prefixes=list(us_id_map.keys()), dry_run=dry_run,
            )
            global_id_map = self._select_back_ids(
                session, material_id=material_id, market_scope="global",
                prefixes=list(global_id_map.keys()), dry_run=dry_run,
            )

            # --- Step 4a: world production shares → linked to the most-raw 6-digit row ---
            if section.production_leaders and global_id_map:
                prod_hs_id = self._pick_production_hs_id(
                    session,
                    canonical_name=section.canonical_name,
                    material_id=material_id,
                    global_id_map=global_id_map,
                )
                if prod_hs_id is not None:
                    world_total = sum(
                        p.production_value for p in section.production_leaders
                    )
                    for prod in section.production_leaders:
                        iso2 = self._resolve_country(prod.country_name, country_map)
                        if iso2 is None:
                            continue
                        share = prod.production_value / world_total if world_total else 0.0
                        written = self._upsert_production_share(
                            session,
                            hs_mapping_id=prod_hs_id,
                            country_code=iso2,
                            production_share=share,
                            production_volume=prod.production_value,
                            reference_year=prod.reference_year,
                            market_scope="global",
                            source="usgs_mcs",
                            dry_run=dry_run,
                        )
                        s["production_shares_inserted"] += written

                    # NOTE: previously this block also wrote a cached HHI back to
                    # `hs_code_material_mappings.{hhi_score, hhi_reference_year,
                    # hhi_source}`.  Migration 035 dropped those columns — the
                    # cache was written-only and never read.  Runtime HHI is
                    # computed by `hs_node_scorer` directly from
                    # `hs_code_production_shares` and persisted on
                    # `hs_code_geography_risk_scores.hhi_at_stage`.

            # --- Step 4b: US import sources → linked to all 10-digit US rows ---
            if section.import_sources and us_id_map:
                for imp in section.import_sources:
                    iso2 = self._resolve_country(imp.country_name, country_map)
                    if iso2 is None:
                        continue
                    for hs_id in us_id_map.values():
                        written = self._upsert_production_share(
                            session,
                            hs_mapping_id=hs_id,
                            country_code=iso2,
                            production_share=imp.share,
                            production_volume=None,
                            reference_year=imp.reference_year,
                            market_scope="us",
                            source="usgs_mcs",
                            dry_run=dry_run,
                        )
                        s["import_source_shares_inserted"] += written

            # --- Salient notes → append to MaterialCriticalitySignal ---
            if section.salient_notes and not dry_run:
                self._append_salient_notes(
                    session,
                    material_id=material_id,
                    notes=section.salient_notes,
                    reference_year=self._reference_year,
                )

            stats[section.canonical_name] = s

        if not dry_run:
            session.flush()

        return stats

    # ------------------------------------------------------------------
    # Text parsing helpers
    # ------------------------------------------------------------------

    def _split_into_commodity_sections(self, full_text: str) -> dict[str, str]:
        """
        Split full PDF text into per-commodity sections using a material-driven
        anchor approach.

        Algorithm:
          1. Walk the full text once and record the line index of EVERY
             commodity-style heading (any all-caps line that matches
             ``_COMMODITY_HEADING_RE`` and looks plausible — short, no digits).
             Both known (in ``_MCS_COMMODITY_MAP``) and unknown headings count.
          2. For each known heading, the section body is the text between its
             line and the NEXT heading (known or unknown).  This means a CHROMIUM
             section terminates at the next CLAYS heading even though CLAYS is
             not a battery material — its tariff codes are not pulled into the
             chromium section.
          3. Unknown headings act as section terminators only; their content is
             discarded.

        Why this fixes the previous misattribution
        -------------------------------------------
        The previous implementation only treated *known* headings as section
        boundaries.  A line "CLAYS AND SHALE" between CHROMIUM and COPPER was
        appended to the CHROMIUM body because it didn't match a known heading.
        All clay tariff codes were then parsed and attributed to Chromium —
        confirmed in the 2026-05 reseed (14 chromium parser rows, all clay).
        Treating ANY all-caps heading as a boundary eliminates this leak path.

        Returns ``{heading: section_body_text}`` keyed by uppercase heading.
        Only headings present in ``_MCS_COMMODITY_MAP`` appear in the result.
        """
        known_headings = set(_MCS_COMMODITY_MAP.keys())
        lines = full_text.splitlines()

        # Step 1: locate every plausible heading line (known + unknown).
        # `_commodity_heading_text` returns the canonical heading string with
        # any trailing footnote marker stripped (e.g. "ALUMINUM1" → "ALUMINUM"),
        # so the per-heading dict and `_MCS_COMMODITY_MAP` lookup operate on
        # the form that matches the curated map keys.
        heading_positions: list[tuple[int, str]] = []  # [(line_index, canonical_heading)]
        for i, line in enumerate(lines):
            heading = self._commodity_heading_text(line)
            if heading is not None:
                heading_positions.append((i, heading))

        # Step 2: build {known_heading_upper: body_text} bounded by adjacent
        # heading positions.  An unknown heading still terminates the previous
        # section but does not start a tracked one.
        sections: dict[str, str] = {}
        for idx, (line_idx, upper_text) in enumerate(heading_positions):
            if upper_text not in known_headings:
                continue  # unknown heading: acts as terminator only
            end_line = (
                heading_positions[idx + 1][0]
                if idx + 1 < len(heading_positions)
                else len(lines)
            )
            body = "\n".join(lines[line_idx + 1:end_line])
            # Multiple sections under the same heading should not happen in MCS,
            # but if it does, concatenate so we don't silently drop content.
            if upper_text in sections:
                sections[upper_text] = sections[upper_text] + "\n" + body
                log.warning(
                    "mcs_pdf_parser.duplicate_heading",
                    heading=upper_text,
                    note="Section appears more than once in PDF — concatenating bodies",
                )
            else:
                sections[upper_text] = body

        if not sections:
            log.warning(
                "mcs_pdf_parser.no_sections_found",
                heading_candidates_detected=len(heading_positions),
                known_heading_keys=len(known_headings),
                note="No matched sections — verify _MCS_COMMODITY_MAP keys against PDF headings",
            )

        return sections

    @staticmethod
    def _commodity_heading_text(line: str) -> Optional[str]:
        """Return the canonical heading text (without footnote marker) or None.

        Heuristic — needs to be permissive enough to catch all MCS commodity
        headings (known + unknown) but tight enough not to fire on body text.

        Rules:
          - Stripped length 2–60 chars (long lines aren't headings)
          - Matches ``_COMMODITY_HEADING_RE`` (2+ uppercase letters, allowed
            spaces / parens / hyphens, optional trailing footnote digits)
          - Not one of the known false-positive labels found in MCS body text
            ("ABOUT", "PREPARED BY", page numbers in caps form, etc.)

        Returns the captured heading text (group 1 of the regex), uppercased
        and with trailing footnote digits stripped — e.g. ``ALUMINUM1`` →
        ``ALUMINUM``.  This is the form callers should use for
        ``_MCS_COMMODITY_MAP`` lookup.

        The list of false-positive labels is intentionally conservative —
        when in doubt, treat a line as a heading and let the unknown-heading
        branch in ``_split_into_commodity_sections`` discard its content.
        Missing a commodity heading is worse than over-segmenting; over-
        segmenting just drops some non-battery body text.
        """
        stripped = line.strip()
        if not (2 <= len(stripped) <= 60):
            return None
        match = _COMMODITY_HEADING_RE.match(stripped)
        if not match:
            return None
        heading = match.group(1).upper()
        # Common in-section labels that match the heading regex but aren't
        # commodity titles.  Extend if false positives are observed.
        _NOT_HEADINGS = {
            "ABOUT",
            "PREPARED BY",
            "TARIFF",
            "DEPLETION ALLOWANCE",
            "GOVERNMENT STOCKPILE",
            "RECYCLING",
            "EVENTS, TRENDS, AND ISSUES",
            "WORLD MINE PRODUCTION AND RESERVES",
            "SALIENT STATISTICS",
            "IMPORT SOURCES",
            "DOMESTIC PRODUCTION",
            "SUBSTITUTES",
        }
        if heading in _NOT_HEADINGS:
            return None
        return heading

    def _parse_tariff_table(self, section_text: str) -> list[TariffEntry]:
        """
        Extract tariff entries from the commodity section text.

        Scans between "Tariff:" and the next major section anchor
        ("Depletion Allowance:" or "Government Stockpile:").
        Uses regex to find HTS codes rather than positional parsing because
        description text wraps unpredictably.
        """
        entries: list[TariffEntry] = []
        seen_codes: set[str] = set()

        # Narrow to the tariff block if possible
        tariff_start = re.search(r'Tariff\s*:', section_text, re.IGNORECASE)
        if tariff_start:
            tariff_end = re.search(
                r'(Depletion Allowance|Government Stockpile)\s*:',
                section_text[tariff_start.start():],
                re.IGNORECASE,
            )
            if tariff_end:
                block = section_text[
                    tariff_start.start():tariff_start.start() + tariff_end.start()
                ]
            else:
                block = section_text[tariff_start.start():]
        else:
            block = section_text

        # Find all HTS code occurrences with surrounding description context
        for match in _HTS_CODE_RE.finditer(block):
            raw_code = match.group(1)          # e.g. "2605.00.0000"
            normalised = raw_code.replace(".", "")  # e.g. "2605000000"

            if normalised in seen_codes:
                continue
            seen_codes.add(normalised)

            # Grab the text fragment immediately before the code as description
            start = max(0, match.start() - 200)
            context = block[start:match.start()].strip()
            # Take the last non-empty line as description
            desc_lines = [ln.strip() for ln in context.splitlines() if ln.strip()]
            description = desc_lines[-1] if desc_lines else raw_code

            entries.append(TariffEntry(
                description=description[:512],
                hts_code=normalised,
                hts_code_raw=raw_code,
                confidence=1.0,
            ))

        return entries

    def _parse_production_leaders(self, section_text: str) -> list[ProductionShare]:
        """
        Extract world mine production country data.

        Scans the "World Mine Production and Reserves" table.
        Derives production shares from raw tonnages (normalised against world total).
        Skips "World total" and "Other" rows — these are meta-rows, not countries.
        """
        leaders: list[ProductionShare] = []

        header_match = re.search(
            r'World\s+Mine\s+Production\s+and\s+Reserves',
            section_text,
            re.IGNORECASE,
        )
        if not header_match:
            return leaders

        # Extract the block that follows the header
        block_start = header_match.end()
        # Stop at the next major section (two blank lines or a known section header)
        next_section = re.search(
            r'\n(Salient|Import Sources|Tariff|Recycling|World\s+Smelter|Events)',
            section_text[block_start:],
            re.IGNORECASE,
        )
        if next_section:
            block = section_text[block_start:block_start + next_section.start()]
        else:
            block = section_text[block_start:block_start + 3000]

        # Detect the reference year from column header lines (e.g. "2024    2025(e)")
        year_col_match = re.search(
            r'\b(20\d{2})\s*\(e\)',
            block,
        )
        if year_col_match:
            reference_year = int(year_col_match.group(1))
        else:
            # Fall back to MCS publication year minus 1 (MCS 2026 → data year 2025)
            reference_year = self._reference_year - 1

        # Parse country/tonnage rows
        # Format: CountryName    NNNN    MMMM(e)
        # Numbers may include commas (thousands separator) and trailing 'e'
        world_total: float | None = None
        row_re = re.compile(
            r'^((?:[A-Z][a-z]+\s?)+(?:\([A-Za-z\s]+\))?)\s{2,}'  # country name
            r'([\d,]+)\s*e?\s*'                                     # col 1 value
            r'([\d,]+)\s*e?\s*$',                                   # col 2 value (estimated)
            re.MULTILINE,
        )
        for m in row_re.finditer(block):
            name = m.group(1).strip()
            # Second column is the more recent year (estimated); first is prior year
            try:
                value = float(m.group(3).replace(",", ""))
            except (ValueError, AttributeError):
                try:
                    value = float(m.group(2).replace(",", ""))
                except ValueError:
                    continue

            lower_name = name.lower()
            if "world total" in lower_name or "world (rounded)" in lower_name:
                world_total = value
                continue
            if lower_name.startswith("other") or name in ("e", "W"):
                continue

            leaders.append(ProductionShare(
                country_name=name,
                production_value=value,
                reference_year=reference_year,
            ))

        # If world total not found, derive it from the sum
        if world_total is None and leaders:
            world_total = sum(ldr.production_value for ldr in leaders)

        # Store world_total on the instance for use by seed_to_db (passed back via
        # the leaders list as a sentinel — caller computes share from values directly)
        return leaders

    def _parse_import_sources(self, section_text: str) -> list[ImportSource]:
        """
        Extract US import source country percentages.

        The section header format is "Import Sources (YYYY–YY):" where the year
        range varies by material and edition.  Uses regex to locate the header
        and extracts the inline country/percentage list that follows.
        """
        sources: list[ImportSource] = []

        header_match = _IMPORT_SOURCES_RE.search(section_text)
        if not header_match:
            return sources

        # Parse reference year from header (end year of range)
        year_match = re.search(r'\((\d{4})[–\-](\d{2,4})\)', header_match.group(0))
        if year_match:
            end_str = year_match.group(2)
            start_year = int(year_match.group(1))
            if len(end_str) == 2:
                reference_year = (start_year // 100) * 100 + int(end_str)
            else:
                reference_year = int(end_str)
        else:
            reference_year = self._reference_year - 1

        # Extract text block after the header (up to next paragraph break or 500 chars)
        after_header = section_text[header_match.end():header_match.end() + 500]
        # Collapse newlines within the sources block (text may wrap)
        sources_text = re.sub(r'\s*\n\s*', ' ', after_header.split('\n\n')[0])

        pct_sum = 0.0
        for m in _IMPORT_PCT_RE.finditer(sources_text):
            name = m.group(1).strip()
            pct = float(m.group(2))
            lower_name = name.lower()
            if "other" in lower_name or not name:
                pct_sum += pct
                continue
            share = pct / 100.0
            pct_sum += pct
            sources.append(ImportSource(
                country_name=name,
                share=share,
                reference_year=reference_year,
            ))

            if pct_sum >= 99.0:
                break

        return sources

    def _extract_salient_notes(self, section_text: str) -> str:
        """Extract the 'Salient Statistics' narrative text block."""
        match = re.search(r'Salient\s+Statistics', section_text, re.IGNORECASE)
        if not match:
            return ""
        # Take up to 2000 chars after the header
        block = section_text[match.end():match.end() + 2000]
        # Stop at next major section
        stop = re.search(
            r'\n(Events|Trends|Import|Tariff|World\s+Mine)',
            block,
            re.IGNORECASE,
        )
        if stop:
            block = block[:stop.start()]
        return block.strip()[:2000]

    # ------------------------------------------------------------------
    # DB seeding helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Stage assignment for parser-derived rows
    # ------------------------------------------------------------------

    @staticmethod
    def _assign_stage(
        session: Session,
        *,
        hs_code_prefix: str,
        digit_count: int,
        material_id: int,
        canonical_name: str,
        parent_stage_cache: dict[tuple[str, int], tuple[Optional[str], Optional[int]]],
    ) -> tuple[Optional[str], Optional[int]]:
        """
        Resolve ``(supply_chain_stage, stage_sequence)`` for a parser-derived row.

        Resolution order:
          1. ``_HS_6DIGIT_STAGE_OVERRIDE`` keyed by (prefix, canonical_name).
             For 10-digit prefixes, also try the 6-digit truncation.
          2. The 4-digit parent's stage in ``hs_code_material_mappings`` for
             this material_id (cached across calls in ``parent_stage_cache``).
          3. ``(None, None)`` with a structured warning so coverage gaps surface.

        ``parent_stage_cache``:
            Caller-supplied dict to avoid hitting the DB once per 6-digit child.
            Keyed by ``(prefix4, material_id)``; populated lazily.

        Returns:
            ``(stage, stage_sequence)`` — both Optional[]; both populated or
            both ``None``.
        """
        # ── Step 1: explicit override (high-confidence map at top of file) ──
        # Try the prefix as-given first
        override = _HS_6DIGIT_STAGE_OVERRIDE.get((hs_code_prefix, canonical_name))
        if override is not None:
            return override

        # For a 10-digit prefix, try its 6-digit truncation
        if digit_count == 10 and len(hs_code_prefix) >= 6:
            six = hs_code_prefix[:6]
            override = _HS_6DIGIT_STAGE_OVERRIDE.get((six, canonical_name))
            if override is not None:
                return override

        # ── Step 2: 4-digit parent inheritance from `_MAPPINGS` (DB) ────────
        if len(hs_code_prefix) >= 4:
            prefix4 = hs_code_prefix[:4]
            cache_key = (prefix4, material_id)
            if cache_key not in parent_stage_cache:
                row = session.execute(
                    select(
                        HsCodeMaterialMapping.supply_chain_stage,
                        HsCodeMaterialMapping.stage_sequence,
                    ).where(
                        HsCodeMaterialMapping.hs_code_prefix == prefix4,
                        HsCodeMaterialMapping.material_id == material_id,
                        HsCodeMaterialMapping.market_scope == "global",
                        HsCodeMaterialMapping.digit_count == 4,
                        HsCodeMaterialMapping.supply_chain_stage.is_not(None),
                    )
                ).first()
                if row is not None:
                    parent_stage_cache[cache_key] = (row[0], row[1])
                else:
                    parent_stage_cache[cache_key] = (None, None)
            stage, seq = parent_stage_cache[cache_key]
            if stage is not None:
                return stage, seq

        # ── Step 3: unresolved — log and return NULL ────────────────────────
        log.warning(
            "mcs_pdf_parser.stage_unresolved",
            hs_code_prefix=hs_code_prefix,
            digit_count=digit_count,
            material=canonical_name,
            note=(
                "No override entry and no 4-digit parent stage — row will be "
                "written with stage=NULL.  Add an override in "
                "_HS_6DIGIT_STAGE_OVERRIDE or seed the 4-digit parent row."
            ),
        )
        return (None, None)

    @staticmethod
    def _build_country_map(session: Session) -> dict[str, str]:
        """
        Build a lowercase name → ISO-2 lookup from the countries table.

        Includes name, iso2, and all entries in common_names JSONB array.
        """
        from app.models.country import Country  # noqa: PLC0415

        result: dict[str, str] = {}
        rows = session.execute(
            select(Country.iso2, Country.name, Country.common_names)
        ).all()

        for iso2, canonical, common_names in rows:
            result[canonical.lower()] = iso2
            result[iso2.lower()] = iso2
            if common_names and isinstance(common_names, list):
                for alias in common_names:
                    if isinstance(alias, str):
                        result[alias.lower()] = iso2

        # MCS-specific overrides not reliably covered by common_names
        _MCS_OVERRIDES: dict[str, str] = {
            "congo (kinshasa)": "CD",
            "congo, democratic republic of the": "CD",
            "drc": "CD",
            "russia": "RU",
            "russian federation": "RU",
            "south korea": "KR",
            "korea, republic of": "KR",
            "korea, south": "KR",
            "taiwan": "TW",
            "taiwan, province of china": "TW",
            "iran": "IR",
            "syria": "SY",
            "vietnam": "VN",
            "viet nam": "VN",
            "ivory coast": "CI",
            "côte d'ivoire": "CI",
        }
        result.update({k: v for k, v in _MCS_OVERRIDES.items()})
        return result

    @staticmethod
    def _build_material_map(session: Session) -> dict[str, int]:
        """Build canonical_name → material_id lookup."""
        rows = session.execute(
            select(Material.canonical_name, Material.id)
        ).all()
        return {name: mid for name, mid in rows}

    @staticmethod
    def _resolve_country(name: str, country_map: dict[str, str]) -> str | None:
        """Resolve a MCS country name to ISO-2.  Returns None if unresolvable."""
        if not name:
            return None
        key = name.strip().lower()
        iso2 = country_map.get(key)
        if iso2:
            return iso2
        # Fuzzy: try removing trailing parenthetical e.g. "Congo (Kinshasa)"
        bare = re.sub(r'\s*\([^)]+\)', '', key).strip()
        iso2 = country_map.get(bare)
        if iso2:
            return iso2
        log.debug("mcs_pdf_parser.country_unresolved", name=name)
        return None

    @staticmethod
    def _upsert_mapping_row(
        session: Session,
        *,
        hs_code_prefix: str,
        material_id: int,
        description: Optional[str],
        confidence: float,
        digit_count: int,
        market_scope: str,
        supply_chain_stage: Optional[str],
        stage_sequence: Optional[int],
        dry_run: bool,
    ) -> Optional[int]:
        """
        Insert a hs_code_material_mappings row, ignoring conflicts.

        Returns the row ID (from the INSERT or the pre-existing row), or None on dry run.
        Note: ON CONFLICT DO NOTHING returns no ID — callers must SELECT back after flush.
        """
        if dry_run:
            log.debug(
                "mcs_pdf_parser.dry_run.mapping",
                prefix=hs_code_prefix,
                scope=market_scope,
                digits=digit_count,
            )
            return -1  # sentinel for dry run

        stmt = (
            pg_insert(HsCodeMaterialMapping)
            .values(
                hs_code_prefix=hs_code_prefix,
                material_id=material_id,
                description=description,
                confidence=confidence,
                digit_count=digit_count,
                market_scope=market_scope,
                supply_chain_stage=supply_chain_stage,
                stage_sequence=stage_sequence,
            )
            .on_conflict_do_nothing(constraint="uq_hs_material_scope")
        )
        session.execute(stmt)
        # ID not available from ON CONFLICT DO NOTHING; caller uses _select_back_ids.
        return -1  # placeholder; replaced by _select_back_ids after flush

    @staticmethod
    def _select_back_ids(
        session: Session,
        *,
        material_id: int,
        market_scope: str,
        prefixes: list[str],
        dry_run: bool,
    ) -> dict[str, int]:
        """
        SELECT hs_code_material_mappings rows by prefix list.

        Returns {hs_code_prefix: id} for all matched rows.
        Needed because ON CONFLICT DO NOTHING returns no row — the only way to
        get the ID of a pre-existing row is to query after the flush.
        """
        if dry_run or not prefixes:
            return {}
        rows = session.execute(
            select(HsCodeMaterialMapping.hs_code_prefix, HsCodeMaterialMapping.id).where(
                HsCodeMaterialMapping.material_id == material_id,
                HsCodeMaterialMapping.market_scope == market_scope,
                HsCodeMaterialMapping.hs_code_prefix.in_(prefixes),
            )
        ).all()
        return {prefix: row_id for prefix, row_id in rows}

    @staticmethod
    def _pick_production_hs_id(
        session: Session,
        *,
        canonical_name: str,
        material_id: int,
        global_id_map: dict[str, int],
    ) -> Optional[int]:
        """
        Select the hs_mapping_id to link world production shares to.

        Priority:
          1. An existing 6-digit global row with the preferred production stage
             (from _MCS_PRODUCTION_STAGE_PREFERENCE) for this material — e.g. 'ore'
          2. The newly-inserted 6-digit global row with the lowest stage_sequence
          3. The first entry in global_id_map if no stage data exists yet

        This fallback chain is necessary because newly-inserted rows from this parser
        run have supply_chain_stage=NULL (stage is seeded later by seed_hs_mappings).
        The preferred-stage lookup covers rows already in the DB with stage metadata.
        """
        if not global_id_map:
            return None

        preferred_stage = _MCS_PRODUCTION_STAGE_PREFERENCE.get(canonical_name, "ore")

        # Try: find a global row for this material with the preferred stage
        existing = session.execute(
            select(HsCodeMaterialMapping.id).where(
                HsCodeMaterialMapping.material_id == material_id,
                HsCodeMaterialMapping.market_scope == "global",
                HsCodeMaterialMapping.supply_chain_stage == preferred_stage,
            )
            .order_by(HsCodeMaterialMapping.digit_count.desc())  # prefer 6-digit over 4-digit
            .limit(1)
        ).scalar_one_or_none()

        if existing is not None:
            return existing

        # Try: lowest stage_sequence among our newly-inserted global rows
        if global_id_map:
            prefix_list = list(global_id_map.keys())
            staged = session.execute(
                select(HsCodeMaterialMapping.id, HsCodeMaterialMapping.stage_sequence).where(
                    HsCodeMaterialMapping.material_id == material_id,
                    HsCodeMaterialMapping.market_scope == "global",
                    HsCodeMaterialMapping.hs_code_prefix.in_(prefix_list),
                    HsCodeMaterialMapping.stage_sequence.is_not(None),
                )
                .order_by(HsCodeMaterialMapping.stage_sequence)
                .limit(1)
            ).first()
            if staged:
                return staged[0]

        # Final fallback: first entry in global_id_map
        return next(iter(global_id_map.values()))

    @staticmethod
    def _upsert_production_share(
        session: Session,
        *,
        hs_mapping_id: int,
        country_code: str,
        production_share: float,
        production_volume: Optional[float],
        reference_year: int,
        market_scope: str,
        source: str,
        dry_run: bool,
    ) -> int:
        """
        Insert or update one hs_code_production_shares row.

        Uses ON CONFLICT DO UPDATE to refresh values on re-runs.
        Returns 1 if a row was written (insert or update), 0 on dry run.
        """
        if dry_run:
            log.debug(
                "mcs_pdf_parser.dry_run.share",
                hs_mapping_id=hs_mapping_id,
                country=country_code,
                share=production_share,
            )
            return 0

        stmt = (
            pg_insert(HsCodeProductionShare)
            .values(
                hs_mapping_id=hs_mapping_id,
                country_code=country_code,
                production_share=production_share,
                production_volume=production_volume,
                reference_year=reference_year,
                market_scope=market_scope,
                source=source,
            )
            .on_conflict_do_update(
                constraint="uq_hs_production_share",
                set_={
                    "production_share": production_share,
                    "production_volume": production_volume,
                },
            )
        )
        session.execute(stmt)
        return 1

    @staticmethod
    def _append_salient_notes(
        session: Session,
        *,
        material_id: int,
        notes: str,
        reference_year: int,
    ) -> None:
        """
        Append salient notes to the MaterialCriticalitySignal for this material/year.

        Does not overwrite existing signal rows — only appends the notes field.
        If no signal row exists for this year, creates a minimal one.
        """
        existing = session.execute(
            select(MaterialCriticalitySignal).where(
                MaterialCriticalitySignal.material_id == material_id,
                MaterialCriticalitySignal.source == "usgs_mcs",
                MaterialCriticalitySignal.reference_year == reference_year,
            )
        ).scalar_one_or_none()

        if existing is not None:
            meta = dict(existing.metadata_json or {})
            meta["salient_notes"] = notes
            existing.metadata_json = meta
        else:
            session.add(MaterialCriticalitySignal(
                material_id=material_id,
                source="usgs_mcs",
                reference_year=reference_year,
                metadata_json={"salient_notes": notes},
            ))
