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
from typing import TYPE_CHECKING, Any, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.supply import HsCodeMaterialMapping, HsCodeProductionShare, Material
from app.models.criticality_signal import MaterialCriticalitySignal

if TYPE_CHECKING:
    from app.services.ingestion.material_resolver import MaterialAliasResolver

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Commodity name normalisation — PDF all-caps headings → materials.canonical_name
# ---------------------------------------------------------------------------
# This dict is now DEPRECATED — the same mappings live in
# ``material_source_aliases`` (source_system='mcs_pdf').  The regex
# fallback path uses ``MaterialAliasResolver``; this dict is retained
# only as a build-time fallback for callers that haven't been updated
# yet.  Will be removed once all callers pass a resolver.

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
    # Sodium-ion battery cathode precursor — MCS publishes Na2CO3 production
    # under the SODA ASH chapter.  After the May 2026 partner-CSV review the
    # canonical material was kept as "Sodium" with multiple HS prefixes
    # (NaOH, Na phosphate, Na2CO3, peroxometallates) rather than narrowed
    # to just sodium carbonate.
    "SODA ASH":                 "Sodium",
    # Add entries as new commodities are covered by MCS
}

# ---------------------------------------------------------------------------
# Stage preference for world mine production share linkage.
# ---------------------------------------------------------------------------
# Maps canonical material name → preferred supply_chain_stage for the
# hs_code_production_shares row that anchors world production shares from
# the MCS PDF.  Used by ``_pick_production_hs_id``.
#
# Default for any material NOT listed here: "ore" — most MCS commodities
# report mine production tonnages at the ore stage.  The dict carries
# ONLY the exceptions where USGS reports production at a different stage.
# Trimmed in May 2026 from 30 entries to 5; redundant defaults removed.
#
# When adding a new material that doesn't have an ore-stage HS prefix
# (e.g. a co-product like Gallium that's only recovered during refining),
# add an entry here.  Otherwise leave it out and the default takes over.

_MCS_PRODUCTION_STAGE_PREFERENCE_DEFAULT = "ore"

_MCS_PRODUCTION_STAGE_PREFERENCE: dict[str, str] = {
    # Co-products of zinc/copper refining — no standalone "ore" stage.
    # First appearance in the supply chain is as a refined metal
    # recovered during smelter operations.
    "Gallium":   "refined",
    "Germanium": "refined",
    "Indium":    "refined",
    # USGS reports silicon-metal world production at the refined stage
    # (HS 280461 — silicon-metal ≥99.99%).  Quartz mining is upstream of
    # MCS's silicon chapter, not reported there.
    "Silicon (Anode Grade)": "refined",
    # USGS reports under the SODA ASH chapter; soda ash (Na2CO3) is the
    # battery-grade input for Na-ion cathode synthesis (Na2CO3 → cathode
    # active material).  Without this override the function's lowest-
    # stage-sequence fallback would land production data on caustic-soda
    # intermediate (281511), which is the wrong stage for our scoring.
    "Sodium":    "battery_grade",
}

# ---------------------------------------------------------------------------
# Stage assignment for parser-derived 6-digit and 10-digit rows
# ---------------------------------------------------------------------------
# Resolution order at insert time (refactored May 2026 — single source of
# truth is now ``seed_hs_mappings._MAPPINGS`` populated into
# ``hs_code_material_mappings``):
#
#   1. Look up (hs_code_prefix, material_id) in hs_code_material_mappings.
#      The seed supplies stages for both 4-digit and 6-digit codes.
#   2. For 10-digit codes, also try the truncated 6-digit form.
#   3. Fall back to the 4-digit parent's stage in hs_code_material_mappings
#      filtered by the same material_id.
#   4. If neither resolves, write stage=NULL and emit a structured log.
#
# Removed: the previous ``_HS_6DIGIT_STAGE_OVERRIDE`` dict (84 entries)
# that duplicated seed_hs_mappings.  All entries were either consistent
# (78), or the 6 partner-resolved conflicts where the seed value wins.


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

    def __init__(
        self,
        path: Path,
        reference_year: int,
        resolver: Optional["MaterialAliasResolver"] = None,
    ) -> None:
        """``resolver``: ``MaterialAliasResolver`` instance the regex
        fallback path uses to translate PDF headings to canonical
        material names.  Optional only because the LLM path doesn't
        need it (canonical names come from the partner-curated
        materials list passed into ``parse``); the regex fallback path
        will raise if invoked without a resolver.
        """
        self._path = path
        self._reference_year = reference_year
        self._resolver = resolver

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(
        self,
        *,
        canonical_materials: Optional[list[str]] = None,
        use_llm_sections: bool = True,
    ) -> list[CommoditySection]:
        """
        Parse all commodity sections from the PDF.

        Two code paths share the same downstream extractors
        (``_parse_tariff_table``, ``_parse_production_leaders``,
        ``_parse_import_sources``, ``_extract_salient_notes``) — they only
        differ in how the per-commodity text bounds are determined.

        Path A — LLM-driven section detection (preferred):
            ``use_llm_sections=True`` (default) AND ``canonical_materials``
            is provided.  Calls ``locate_commodity_chapters()`` to get
            chapter bounds for each canonical material, slices the joined
            PDF text using those bounds, and runs the extractors on each
            slice.  Robust to MCS layout variants — handles footnoted
            headings, multi-chapter materials, and out-of-scope commodities
            without regex maintenance.  Result is cached at
            ``data/mcs<year>_locator_result.json``; subsequent calls within
            the cache window skip the LLM entirely.

        Path B — Regex-driven section detection (fallback):
            ``use_llm_sections=False`` OR ``canonical_materials`` not
            provided.  Walks the joined PDF text looking for all-caps
            headings via ``_split_into_commodity_sections()``.  Susceptible
            to layout edge cases (the issues we found in MCS 2026 with
            footnoted ALUMINUM¹, IRON ORE¹, etc.) but works without an
            LLM dependency.

        Args:
            canonical_materials:
                ``materials.canonical_name`` values from the DB.  Required
                for Path A.  When omitted, falls back to Path B.
            use_llm_sections:
                When True (default), prefer Path A.  When False, force
                Path B even if ``canonical_materials`` is provided —
                useful for testing the regex baseline.

        Returns one :class:`CommoditySection` per recognised commodity.
        """
        if use_llm_sections and canonical_materials:
            return self._parse_via_llm_locator(canonical_materials)
        return self._parse_via_regex()

    def _read_pdf_text(self) -> str:
        """Read the PDF and return joined text using the canonical convention.

        Pages are joined with ``"\\n\\n--- PAGE BREAK ---\\n\\n"``.  Both
        the regex path and the LLM locator depend on this exact format
        (the locator uses the page-break markers to build the page index).
        """
        try:
            import pdfplumber  # noqa: PLC0415 — optional heavy dep
        except ImportError as exc:
            raise RuntimeError(
                "pdfplumber is required for MCS PDF parsing.  "
                "Install with: pip install pdfplumber"
            ) from exc

        full_text_pages: list[str] = []
        with pdfplumber.open(str(self._path)) as pdf:
            for page in pdf.pages:
                full_text_pages.append(page.extract_text() or "")
        return "\n\n--- PAGE BREAK ---\n\n".join(full_text_pages)

    def _parse_via_llm_locator(
        self,
        canonical_materials: list[str],
    ) -> list[CommoditySection]:
        """LLM-driven Path A: get chapter bounds from the locator, then run
        the existing extractors on each chapter's bounded text.

        Disk cache lives at ``data/mcs<year>_locator_result.json``.  When
        present, the locator loads from disk without calling Anthropic.
        Check the file into git to make CI deterministic.
        """
        # Lazy import so the regex path doesn't pay the import cost
        from app.services.ingestion.mcs_pdf_llm_locator import (  # noqa: PLC0415
            locate_commodity_chapters,
        )

        full_text = self._read_pdf_text()
        cache_path = Path("data") / f"mcs{self._reference_year}_locator_result.json"

        log.info(
            "mcs_pdf_parser.llm_locator.starting",
            cache_path=str(cache_path),
            canonical_materials=len(canonical_materials),
            reference_year=self._reference_year,
        )

        result = locate_commodity_chapters(
            pdf_text=full_text,
            canonical_material_names=canonical_materials,
            cache_path=cache_path,
        )

        log.info(
            "mcs_pdf_parser.llm_locator.complete",
            chapters=len(result.chapters),
            skipped=len(result.skipped),
        )

        sections: list[CommoditySection] = []
        lines = full_text.splitlines()
        for chapter in result.chapters:
            chapter_text = "\n".join(lines[chapter.start_line:chapter.end_line])
            section = CommoditySection(
                heading=chapter.pdf_heading,
                canonical_name=chapter.canonical_material,
            )
            section.tariff_entries = self._parse_tariff_table(chapter_text)
            section.production_leaders = self._parse_production_leaders(chapter_text)
            section.import_sources = self._parse_import_sources(chapter_text)
            section.salient_notes = self._extract_salient_notes(chapter_text)

            if not section.tariff_entries and not section.production_leaders:
                log.warning(
                    "mcs_pdf_parser.empty_section",
                    canonical_name=chapter.canonical_material,
                    heading=chapter.pdf_heading,
                    note="LLM-located chapter produced no tariff or production rows; "
                         "verify the chapter bounds in the cache file are correct.",
                )

            sections.append(section)
            log.info(
                "mcs_pdf_parser.section_parsed",
                commodity=chapter.canonical_material,
                source="llm_locator",
                tariff_entries=len(section.tariff_entries),
                production_leaders=len(section.production_leaders),
                import_sources=len(section.import_sources),
            )

        return sections

    def _parse_via_regex(self) -> list[CommoditySection]:
        """Fallback Path B: regex-based heading detection on the joined PDF text.

        Used when ``use_llm_sections=False`` or when the caller cannot
        provide a canonical-materials list.  Susceptible to MCS layout
        edge cases (footnoted headings, multi-occurrence headings) — see
        ``docs/scoring-audit-2026-05.md`` § G3 for the failure modes that
        motivated the LLM path.
        """
        sections: list[CommoditySection] = []
        combined = self._read_pdf_text()
        raw_sections = self._split_into_commodity_sections(combined)

        for heading, body in raw_sections.items():
            canonical = self._resolve_pdf_heading(heading.strip())
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
                source="regex",
                tariff_entries=len(section.tariff_entries),
                production_leaders=len(section.production_leaders),
                import_sources=len(section.import_sources),
            )

        return sections

    def seed_to_db(
        self,
        session: Session,
        *,
        dry_run: bool = False,
        use_llm_sections: bool = True,
    ) -> dict[str, Any]:
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
        country_map = self._build_country_map(session)
        material_map = self._build_material_map(session)

        # ── Constrain the LLM to MCS-chapter materials only ──────────────
        # 2026-05-09: previously passed ``list(material_map.keys())`` — ALL
        # canonical materials in the DB.  That caused the LLM to fan out
        # the RARE EARTHS chapter to multiple individual REE breakouts (Nd,
        # Pr, Dy, Tb) because those names appeared in the allowed-list,
        # and to attribute Hafnium HTS rows to Zirconium because Hafnium
        # isn't a canonical.  Result: 8 noise rows per re-ingest.
        #
        # Fix: pass only canonical names that have a curated MCS chapter
        # alias (``material_source_aliases`` with ``source_system='mcs_pdf'``)
        # — i.e. the partner-reviewed 1:1 chapter→material mapping.  Each
        # MCS chapter still maps to exactly one canonical, but the LLM no
        # longer has individual REE / Hafnium names to fan out to.
        from app.models.supply import MaterialSourceAlias
        chapter_canonicals = list(session.scalars(
            select(Material.canonical_name)
            .join(
                MaterialSourceAlias,
                MaterialSourceAlias.canonical_material_id == Material.id,
            )
            .where(
                MaterialSourceAlias.source_system == "mcs_pdf",
                MaterialSourceAlias.is_skipped.is_(False),
            )
            .distinct()
        ).all())
        log.info(
            "mcs_pdf_parser.canonical_filter",
            total_materials=len(material_map),
            mcs_chapter_materials=len(chapter_canonicals),
            note="LLM section locator restricted to materials with curated mcs_pdf aliases",
        )

        sections = self.parse(
            canonical_materials=chapter_canonicals,
            use_llm_sections=use_llm_sections,
        )

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

            # --- Step 4b: US import sources — DISABLED May 2026 ---
            # The previous implementation fan-out each PDF sub-type's
            # country share across EVERY 10-digit US HTS row in the
            # chapter (e.g. "Chromite ores: South Africa 96%" was
            # written to ferrochromium and chromium-metal codes too —
            # incorrect attribution).
            #
            # Per-country US import shares now come exclusively from
            # ``ingest-usgs`` (MCS 2026 long-format CSV) using the
            # keyword-based sub-type resolver against
            # ``hs_code_material_mappings.keywords``.  That path
            # attributes each sub-type to its specific 6-digit prefix
            # (e.g. "Chromite (ores and concentrates)" → 261000) instead
            # of fan-outing to all chapter codes.
            #
            # Trade-offs:
            #   * 10-digit codes get no per-country share data attached
            #     — 10-digit nodes are still useful for tariff lookups
            #     (their primary purpose) but won't have a population
            #     of ``hs_code_production_shares`` rows.  If partner
            #     ever needs per-country attribution at 10-digit
            #     granularity, see the next-step plan: a sub-type →
            #     10-digit code-group keyword mapping that mirrors the
            #     6-digit one we already have for the CSV path.
            #   * ``section.import_sources`` is still parsed for
            #     diagnostics and forward compatibility; just not
            #     written.
            if section.import_sources and us_id_map:
                log.debug(
                    "mcs_pdf_parser.import_sources_skipped",
                    canonical=section.canonical_name,
                    count=len(section.import_sources),
                    note=(
                        "Per-country US import shares are now sourced "
                        "from ingest-usgs (CSV path) only.  PDF write "
                        "path disabled May 2026."
                    ),
                )

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
        # "Known headings" come from the alias table now — the resolver
        # tells us which PDF headings have a partner-curated mapping
        # under source_system='mcs_pdf'.  When no resolver is wired in
        # (legacy callers), fall back to the deprecated dict.
        if self._resolver is not None:
            from app.models.supply import MaterialSourceAlias as _MSA  # local import
            known_rows = self._resolver._session.scalars(  # type: ignore[attr-defined]
                select(_MSA).where(_MSA.source_system == "mcs_pdf")
            ).all()
            known_headings = {r.source_name.strip().upper() for r in known_rows}
        else:
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

    def _resolve_pdf_heading(self, heading: str) -> Optional[str]:
        """Resolve a PDF heading to a canonical material name.

        Uses ``MaterialAliasResolver`` (source_system='mcs_pdf') when
        the parser was constructed with a resolver, falls back to the
        deprecated ``_MCS_COMMODITY_MAP`` dict otherwise.  Returns the
        canonical name on success; returns None when the heading is
        skipped (alias is_skipped=True) or unknown (no alias row).
        """
        if self._resolver is None:
            return _MCS_COMMODITY_MAP.get(heading)
        result = self._resolver.resolve("mcs_pdf", heading)
        if result.status == "ok":
            assert result.material is not None
            return result.material.canonical_name
        return None

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
        Extract world production country data from the chapter's production table.

        MCS uses several variant headers depending on the commodity:
            "World Mine Production and Reserves"          (most ores: Co, Ni, Cu, …)
            "World Smelter Production and Capacity"       (Aluminum)
            "World Refinery Production and Reserves"      (some refined products)
            "World Mine Production"                       (some)
            "World Refinery Production"                   (some)
        The regex below matches any "World <Mine|Smelter|Refinery> Production"
        prefix.  Without this broadening, Aluminum (and other non-Mine
        production headers) silently produced zero rows because the previous
        anchor required the literal "Mine" word.

        Derives production shares from raw tonnages (normalised against world total).
        Skips "World total" and "Other" rows — these are meta-rows, not countries.
        """
        leaders: list[ProductionShare] = []

        header_match = re.search(
            r'World\s+(?:Mine|Smelter|Refinery)\s+Production',
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

        Resolution order (refactored May 2026 — single source of truth is now
        ``hs_code_material_mappings``, populated from
        ``seed_hs_mappings._MAPPINGS``):

          1. Exact-prefix lookup in ``hs_code_material_mappings`` for this
             material_id.  For a 10-digit prefix, also try the 6-digit
             truncation.
          2. 4-digit parent inheritance from ``hs_code_material_mappings``
             for this material_id (cached across calls).
          3. ``(None, None)`` with a structured warning so coverage gaps surface.

        ``parent_stage_cache``:
            Caller-supplied dict keyed by ``(prefix, material_id)`` so the
            same prefix isn't queried twice across the loop.

        Returns:
            ``(stage, stage_sequence)`` — both Optional; both populated or
            both ``None``.
        """
        # Helper: look up stage for a (prefix, material_id) from the DB,
        # caching the result.
        def _lookup(prefix: str) -> tuple[Optional[str], Optional[int]]:
            cache_key = (prefix, material_id)
            if cache_key not in parent_stage_cache:
                row = session.execute(
                    select(
                        HsCodeMaterialMapping.supply_chain_stage,
                        HsCodeMaterialMapping.stage_sequence,
                    ).where(
                        HsCodeMaterialMapping.hs_code_prefix == prefix,
                        HsCodeMaterialMapping.material_id == material_id,
                        HsCodeMaterialMapping.market_scope == "global",
                        HsCodeMaterialMapping.supply_chain_stage.is_not(None),
                    )
                ).first()
                if row is not None:
                    parent_stage_cache[cache_key] = (row[0], row[1])
                else:
                    parent_stage_cache[cache_key] = (None, None)
            return parent_stage_cache[cache_key]

        # ── Step 1: exact-prefix lookup ────────────────────────────────────
        stage, seq = _lookup(hs_code_prefix)
        if stage is not None:
            return stage, seq

        # 10-digit codes also try the 6-digit truncation.
        if digit_count == 10 and len(hs_code_prefix) >= 6:
            stage, seq = _lookup(hs_code_prefix[:6])
            if stage is not None:
                return stage, seq

        # ── Step 2: 4-digit parent inheritance ─────────────────────────────
        if len(hs_code_prefix) >= 4:
            stage, seq = _lookup(hs_code_prefix[:4])
            if stage is not None:
                return stage, seq

        # ── Step 3: unresolved — log and return NULL ───────────────────────
        log.warning(
            "mcs_pdf_parser.stage_unresolved",
            hs_code_prefix=hs_code_prefix,
            digit_count=digit_count,
            material=canonical_name,
            note=(
                "No exact-prefix, 6-digit truncation, or 4-digit parent "
                "stage in hs_code_material_mappings — row will be written "
                "with stage=NULL.  Add an entry to seed_hs_mappings._MAPPINGS "
                "and re-run seed-hs-mappings."
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

        preferred_stage = _MCS_PRODUCTION_STAGE_PREFERENCE.get(
            canonical_name, _MCS_PRODUCTION_STAGE_PREFERENCE_DEFAULT,
        )

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
