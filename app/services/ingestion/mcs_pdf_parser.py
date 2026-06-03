"""
MCS PDF Parser — Phase 2 of the HS code redesign.

Parses USGS Mineral Commodity Summaries annual PDF using pdfplumber.
Extracts per-commodity:
  1. Tariff tables (10-digit US HTS codes) → hs_code_material_mappings (market_scope='us')
  2. Derived 6-digit global rows → hs_code_material_mappings (market_scope='global')
  3. Salient notes → MaterialCriticalitySignal.metadata_json (write-only today;
     no downstream reader, retained as a forward-compatibility hook)

Insert order per commodity (must follow this sequence):
  1. 10-digit US HTS rows (INSERT ... ON CONFLICT DO NOTHING)
  2. Derived 6-digit global rows (INSERT ... ON CONFLICT DO NOTHING)
  3. SELECT back hs_mapping_id for every inserted or pre-existing row

Steps 1–2 use ON CONFLICT DO NOTHING so re-runs are safe.  Pre-existing rows
must still have their IDs retrieved via a follow-up SELECT — do NOT assume
the ID from the initial insert.

Section 5 cleanup (2026-06)
----------------------------
Removed the per-country world-production extractor (``_parse_production_leaders``)
and its DB write path (Step 4a + ``_pick_production_hs_id`` +
``_upsert_production_share``).  Real-PDF audit confirmed the extractor
produced 0 rows against MCS 2026 layout, and the CSV path
(``mcs2026_parser._hs_production_shares`` → 413 production-share rows in
``hs_code_production_shares`` with market_scope='global') has been the
sole populator of that table since May 2026.  The PDF parser's promise
to deliver per-country shares predated the CSV refactor and was
vestigial.  Tariff codes (Steps 1-2 above) are still uniquely the PDF
parser's job; the CSV path doesn't capture HS codes.

Removed in the same pass: ``_parse_import_sources`` continues to run for
diagnostic purposes (Step 4b stays disabled — see in-line note).

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

from app.models.supply import HsCodeMaterialMapping, Material
from app.models.criticality_signal import MaterialCriticalitySignal

if TYPE_CHECKING:
    from app.services.ingestion.material_resolver import MaterialAliasResolver

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Commodity name normalisation — PDF all-caps headings → materials.canonical_name
# ---------------------------------------------------------------------------
# REMOVED 2026-06 (Section 4.3 fix): the legacy ``_MCS_COMMODITY_MAP`` dict
# was deleted.  The single source of truth for "PDF heading → canonical
# material" is now ``material_source_aliases`` (source_system='mcs_pdf').
# Resolution happens via ``MaterialAliasResolver``, which the regex
# fallback path (Path B) requires.  Path B will raise ``RuntimeError`` if
# constructed without a resolver — matching the docstring contract.
# Path A (LLM locator) gets canonical names directly from the LLM result
# and does not need a resolver.
#
# See app/services/ingestion/seed_material_source_aliases.py for the
# current alias rows (source_system='mcs_pdf').

# ---------------------------------------------------------------------------
# Removed 2026-06 (Section 5.1 cleanup):
#   ``_MCS_PRODUCTION_STAGE_PREFERENCE`` + ``_MCS_PRODUCTION_STAGE_PREFERENCE_DEFAULT``
# The PDF parser no longer writes per-country world-production rows
# (``_parse_production_leaders`` + ``_pick_production_hs_id`` deleted).
# The CSV path (``mcs2026_parser._hs_production_shares``) is the sole
# populator of ``hs_code_production_shares`` for market_scope='global'.
#
# If a future schema change reintroduces per-country PDF extraction,
# the stage-preference dict can be reinstated; until then it's dead
# configuration.
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

# Import sources section header — year range varies by material and edition,
# AND some chapters add qualifiers inside the parens:
#   "Import Sources (2021–24):"            ← most common
#   "Import Sources (2020-2024):"          ← 4-digit end year variant
#   "Import Sources (2021–24, by value):"  ← 2 occurrences in MCS 2026
# Permissive content match (Section 1.1 fix 2026-05-31): accept any
# non-empty parenthesised content so the "by value" variant + future
# qualifier additions don't silently drop import-source data.  The
# tighter validation lives downstream in ``_parse_import_sources``
# which extracts the year range via _IMPORT_PCT_RE-adjacent patterns.
_IMPORT_SOURCES_RE = re.compile(
    r'Import Sources\s*\([^)]+\)\s*:',
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
# the footnote so callers can use it for material_source_aliases lookup
# (source_system='mcs_pdf').
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


# ProductionShare dataclass removed 2026-06 (Section 5.1 cleanup) —
# see file header for rationale.


@dataclass
class ImportSource:
    """One country from MCS import sources section."""
    country_name: str
    share: float           # 0.0–1.0
    reference_year: int    # end year of the YYYY–YY range


@dataclass
class CommoditySection:
    """All parsed data for one MCS commodity section.

    Section 5.1 cleanup (2026-06): ``production_leaders`` removed —
    see file header.
    """
    heading: str                         # all-caps heading as found in PDF
    canonical_name: str                  # resolved via material_source_aliases (source_system='mcs_pdf')
    tariff_entries: list[TariffEntry] = field(default_factory=list)
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
      - import_sources: list[ImportSource(country_name, share, year)]
                        (parsed for diagnostics; DB write disabled May 2026)
      - salient_notes: str (raw text from "Salient Statistics" section;
                        written to MaterialCriticalitySignal.metadata_json
                        but no current reader)

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
        (``_parse_tariff_table``, ``_parse_import_sources``,
        ``_extract_salient_notes``) — they only
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
        # Silent fall-through cases — make the path choice visible so
        # operators / debug-script callers can see why their LLM cache
        # isn't being read (2.2 fix) or why their canonical list was
        # ignored (2.3 fix).
        if use_llm_sections and not canonical_materials:
            log.warning(
                "mcs_pdf_parser.path_fallback",
                requested="llm_locator",
                used="regex",
                reason="canonical_materials missing — LLM path requires it",
            )
        elif not use_llm_sections and canonical_materials:
            log.debug(
                "mcs_pdf_parser.canonical_materials_ignored",
                path="regex",
                count=len(canonical_materials),
                reason="Path B (regex) discovers commodities from PDF headings; "
                       "the canonical_materials argument is informational only here",
            )
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
            section.import_sources = self._parse_import_sources(chapter_text)
            section.salient_notes = self._extract_salient_notes(chapter_text)

            if not section.tariff_entries:
                # Section 5.1 cleanup (2026-06): tariff entries are the
                # sole DB-write contribution from this chapter (US HTS
                # 10-digit + derived global 6-digit).  An empty list
                # means the LLM-located bounds didn't include the tariff
                # block — likely a chapter-bounds error worth surfacing.
                log.warning(
                    "mcs_pdf_parser.empty_section",
                    canonical_name=chapter.canonical_material,
                    heading=chapter.pdf_heading,
                    note="LLM-located chapter produced no tariff rows; "
                         "verify the chapter bounds in the cache file are correct.",
                )

            sections.append(section)
            log.info(
                "mcs_pdf_parser.section_parsed",
                commodity=chapter.canonical_material,
                source="llm_locator",
                tariff_entries=len(section.tariff_entries),
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
            section.import_sources = self._parse_import_sources(body)
            section.salient_notes = self._extract_salient_notes(body)

            if not section.tariff_entries:
                # 2.8 fix (2026-05-31): include a path-specific actionable
                # note so the empty-section log shape matches Path A's.
                # Section 5.1 cleanup (2026-06): production-leaders criterion
                # dropped — tariff entries are the only DB-write contribution.
                log.warning(
                    "mcs_pdf_parser.empty_section",
                    canonical_name=canonical,
                    heading=heading,
                    note="Regex-detected heading produced no tariff rows; "
                         "verify the heading matches a real MCS commodity "
                         "chapter (not an Appendix / Intro / subsection label).",
                )

            sections.append(section)
            log.info(
                "mcs_pdf_parser.section_parsed",
                commodity=canonical,
                source="regex",
                tariff_entries=len(section.tariff_entries),
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

        Returns a summary dict ``{canonical_name: counters}`` plus a special
        ``__run_meta__`` key carrying the run-level counters (chapters
        skipped due to missing material, chapters that raised during
        per-section processing).

        Per-section error handling (3.5 fix 2026-05-31): each section is
        processed inside try/except so that a failure parsing or writing
        one chapter doesn't abort the whole seed run.  The failing chapter
        is logged via ``log.exception`` and counted in
        ``__run_meta__.skipped_on_error``; the rest of the chapters
        continue.  ``parent_stage_cache`` (3.6 deferred concern) is shared
        across all sections and survives raises — its state at the point
        of an exception may leave a partially-populated entry for the
        failing chapter's HS code, but since the cache is just a per-run
        memo of DB SELECTs there's no correctness risk; subsequent sections
        either hit the cache (correct value, no re-query) or miss and
        re-query (correct value, slight overhead).

        Dry-run note (3.10): the material map is still loaded in dry-run
        mode because it's needed for per-section logging (material_id
        lookup happens before the dry-run short-circuit).
        ``_upsert_mapping_row`` and friends short-circuit on the flag,
        but the read-side queries (``_assign_stage`` stage lookups,
        salient-notes destination probes) still execute.
        """
        # Section 6.2 cleanup (2026-06): ``country_map`` removed.  Its
        # only consumer was the deleted Step 4a (world production shares)
        # and the disabled Step 4b (US import sources).  Country resolution
        # is no longer the PDF parser's job.
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
        # this seed run.  Populated lazily by `_assign_stage`.  Survives
        # per-section exceptions (see 3.5 / 3.6 in seed_to_db docstring).
        parent_stage_cache: dict[tuple[str, int], tuple[Optional[str], Optional[int]]] = {}

        stats: dict[str, dict[str, int]] = {}
        # Run-level meta counters (3.2 + 3.5 fix 2026-05-31).  Surfaced
        # via the special ``__run_meta__`` key in the return dict.
        run_meta: dict[str, int] = {
            "skipped_no_material": 0,
            "skipped_on_error": 0,
            "chapters_processed": 0,
        }

        for section in sections:
            material_id = material_map.get(section.canonical_name)
            if material_id is None:
                log.warning(
                    "mcs_pdf_parser.material_not_found",
                    canonical_name=section.canonical_name,
                )
                run_meta["skipped_no_material"] += 1
                continue

            # 3.1 counter rename (2026-05-31): ``us_rows_inserted`` /
            # ``global_rows_inserted`` were misleading — they counted
            # every processed row (including pre-existing ON CONFLICT
            # DO NOTHING rows) because ``_upsert_mapping_row`` always
            # returns -1 (placeholder).  Renamed to ``*_processed`` so
            # CLI summary reflects actual semantics.
            #
            # 3.4: ``import_source_shares_inserted`` renamed to
            # ``import_source_shares_skipped`` since Step 4b is disabled
            # — the counter now reflects how many rows the parser SAW
            # and intentionally did NOT write.
            #
            # 3.7: ``salient_notes_appended`` added so CLI can report it.
            # Section 5.1 cleanup (2026-06): ``production_shares_inserted``
            # counter removed along with the Step 4a write block (CSV path
            # now sole populator of ``hs_code_production_shares`` for
            # market_scope='global').
            s: dict[str, int] = {
                "us_rows_processed": 0,
                "global_rows_processed": 0,
                "import_source_shares_skipped": 0,
                "stage_assigned_us": 0,
                "stage_assigned_global": 0,
                "stage_unresolved_us": 0,
                "stage_unresolved_global": 0,
                "salient_notes_appended": 0,
            }

            # 3.5 fix (2026-05-31): wrap per-section processing in
            # try/except so one chapter's failure doesn't abort the whole
            # seed run.  Failing chapter is logged + counted in
            # run_meta; subsequent chapters continue.
            try:
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
                        s["us_rows_processed"] += 1

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
                        s["global_rows_processed"] += 1

                # Per-section flush required so Step 3 SELECT-back sees
                # the inserts.  The end-of-function flush is redundant
                # in steady state but defensive against any future write
                # added after Step 4 — see 3.9 in seed_to_db docstring.
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

                # --- Step 4a: world production shares — DELETED 2026-06 ---
                # See file header for rationale.  Summary: the CSV path
                # (mcs2026_parser) is the sole populator of
                # ``hs_code_production_shares`` for market_scope='global';
                # the PDF parser's extractor was redundant and never
                # actually wrote rows against MCS 2026 layout (real-PDF
                # audit confirmed 0 rows extracted).

                # --- Step 4b: US import sources — DISABLED May 2026 ---
                # See seed_to_db docstring for the trade-off rationale.
                # 3.3 (2026-05-31): import_sources is parsed by the
                # extractor for diagnostics + forward compatibility, but
                # nothing is written here.  Counter renamed to
                # ``import_source_shares_skipped`` for accuracy.
                if section.import_sources and us_id_map:
                    s["import_source_shares_skipped"] = len(section.import_sources)
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
                    s["salient_notes_appended"] = 1

                stats[section.canonical_name] = s
                run_meta["chapters_processed"] += 1

            except Exception as exc:  # noqa: BLE001 — intentional broad catch
                # 3.5 fix (2026-05-31): per-section error isolation.  Log
                # full traceback so partner can see WHAT failed; continue
                # with the next section rather than aborting the whole
                # seed run.  Failing chapter is NOT added to ``stats`` —
                # only fully-processed chapters appear there.  The CLI
                # summary should compare ``len(stats)`` against
                # ``run_meta['chapters_processed']`` to spot mismatches.
                run_meta["skipped_on_error"] += 1
                log.exception(
                    "mcs_pdf_parser.section_failed",
                    canonical_name=section.canonical_name,
                    error_type=type(exc).__name__,
                    note=(
                        "Section processing raised; chapter skipped.  "
                        "Subsequent chapters continue.  Inspect the "
                        "traceback above to identify the failure point."
                    ),
                )

        if not dry_run:
            # Redundant in steady state — every Step 2 already flushed
            # the per-section work — but defensive against any future
            # write added after Step 4 that wouldn't otherwise hit the
            # DB before the CLI's commit.
            session.flush()

        # 3.2 + 3.7 (2026-05-31): surface run-level counters via the
        # __run_meta__ key.  Existing CLI iterates ``stats.values()`` to
        # sum per-chapter counters; that iteration naturally excludes
        # __run_meta__ via the meta-prefix convention, but explicit
        # ``skip if k.startswith('__')`` in the CLI would be cleaner.
        stats["__run_meta__"] = run_meta
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
             Both known (registered in ``material_source_aliases`` under
             source_system='mcs_pdf') and unknown headings count.
          2. For each known heading, the section body is the text between its
             line and the NEXT heading (known or unknown).  This means a CHROMIUM
             section terminates at the next CLAYS heading even though CLAYS is
             not a battery material — its tariff codes are not pulled into the
             chromium section.
          3. Unknown headings act as section terminators only; their content is
             discarded.

        Body text contains ``--- PAGE BREAK ---`` separator lines emitted by
        ``_read_pdf_text``.  Downstream extractors operate on regex patterns
        that don't match the marker, so they're harmless — but salient-notes
        extraction does include the marker if it falls between anchors.
        That's tolerated; the marker is stripped in human-facing surfaces.

        Why this fixes the previous misattribution
        -------------------------------------------
        The previous implementation only treated *known* headings as section
        boundaries.  A line "CLAYS AND SHALE" between CHROMIUM and COPPER was
        appended to the CHROMIUM body because it didn't match a known heading.
        All clay tariff codes were then parsed and attributed to Chromium —
        confirmed in the 2026-05 reseed (14 chromium parser rows, all clay).
        Treating ANY all-caps heading as a boundary eliminates this leak path.

        Returns ``{heading: section_body_text}`` keyed by uppercase heading.
        Only headings registered in ``material_source_aliases`` (source_system
        ='mcs_pdf') appear in the result.

        Raises ``RuntimeError`` if invoked without a resolver — matches the
        Path B contract documented in ``__init__``.  Section 4.3 fix
        (2026-06): the previous fall-through to the deleted
        ``_MCS_COMMODITY_MAP`` dict has been removed.
        """
        if self._resolver is None:
            # Section 4.3 fix (2026-06): align code with __init__ docstring.
            # Path B can't resolve headings → canonicals without the alias
            # table.  Callers must construct the parser with a resolver
            # (the CLI does so at cli.py:3761).
            raise RuntimeError(
                "MCSPdfParser._split_into_commodity_sections requires a "
                "MaterialAliasResolver. Construct the parser with "
                "MCSPdfParser(path, reference_year, resolver=resolver)."
            )
        # Section 4.4 fix (2026-06): use the public list_for_source method
        # instead of reaching into resolver._session — keeps the parser
        # decoupled from the resolver's internal storage.
        known_headings = self._resolver.list_for_source("mcs_pdf")
        lines = full_text.splitlines()

        # Step 1: locate every plausible heading line (known + unknown).
        # `_commodity_heading_text` returns the canonical heading string with
        # any trailing footnote marker stripped (e.g. "ALUMINUM1" → "ALUMINUM"),
        # so the heading-positions list operates on the form that matches the
        # alias-table source_name values.
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
            # MCS prints each chapter heading at the top of EVERY page of the
            # chapter — so a 2-page commodity appears in `heading_positions`
            # twice.  The concatenation path stitches page-1 and page-2 bodies
            # back together.  Section 4.1 fix (2026-06): the previous warning
            # log fired on every chapter (~26/run) because page-2 continuation
            # is the dominant case, not an anomaly.  Downgraded to debug.
            if upper_text in sections:
                sections[upper_text] = sections[upper_text] + "\n" + body
                log.debug(
                    "mcs_pdf_parser.duplicate_heading",
                    heading=upper_text,
                    note="Section appears more than once in PDF — concatenating "
                         "bodies (expected for multi-page chapters)",
                )
            else:
                sections[upper_text] = body

        if not sections:
            log.warning(
                "mcs_pdf_parser.no_sections_found",
                heading_candidates_detected=len(heading_positions),
                known_heading_keys=len(known_headings),
                note="No matched sections — verify material_source_aliases "
                     "(source_system='mcs_pdf') against PDF headings",
            )

        return sections

    def _resolve_pdf_heading(self, heading: str) -> Optional[str]:
        """Resolve a PDF heading to a canonical material name.

        Uses ``MaterialAliasResolver`` (source_system='mcs_pdf') as the
        single source of truth.  Returns the canonical name on success;
        returns None when the heading is skipped (alias is_skipped=True)
        or unknown (no alias row).

        Section 4.3 fix (2026-06): the deprecated ``_MCS_COMMODITY_MAP``
        fallback was removed; this method now raises ``RuntimeError`` if
        called without a resolver — matching ``__init__``'s contract.
        """
        if self._resolver is None:
            raise RuntimeError(
                "MCSPdfParser._resolve_pdf_heading requires a "
                "MaterialAliasResolver. Construct the parser with "
                "MCSPdfParser(path, reference_year, resolver=resolver)."
            )
        result = self._resolver.resolve("mcs_pdf", heading)
        if result.status == "ok":
            assert result.material is not None
            return result.material.canonical_name
        return None

    def _commodity_heading_text(self, line: str) -> Optional[str]:
        """Return the canonical heading text (without footnote marker) or None.

        Heuristic — needs to be permissive enough to catch all MCS commodity
        headings (known + unknown) but tight enough not to fire on body text.

        Rules:
          - Stripped length 2–60 chars (long lines aren't headings)
          - Matches ``_COMMODITY_HEADING_RE`` (2+ uppercase letters, allowed
            spaces / parens / hyphens, optional trailing footnote digits)
          - Not one of the known false-positive labels found in MCS body text
            (in-chapter subsection titles like "TARIFF", document-structure
            labels like "APPENDIX A", and observed cross-column artifacts
            from MCS 2026 such as "IRZ IRZ")

        Returns the captured heading text (group 1 of the regex), uppercased
        and with trailing footnote digits stripped — e.g. ``ALUMINUM1`` →
        ``ALUMINUM``.  This is the form callers should match against
        ``material_source_aliases.source_name`` for source_system='mcs_pdf'.

        The deny-list is conservative — when in doubt, treat a line as a
        heading and let the unknown-heading branch in
        ``_split_into_commodity_sections`` discard its content.  Missing a
        commodity heading is worse than over-segmenting; over-segmenting
        just drops some non-battery body text.

        Section 4.7 fix (2026-06): demoted from ``@staticmethod`` to an
        instance method — the call site in ``_split_into_commodity_sections``
        already uses ``self._commodity_heading_text(line)``, and dropping
        the decorator matches Python convention for instance-method
        access patterns.
        """
        stripped = line.strip()
        if not (2 <= len(stripped) <= 60):
            return None
        match = _COMMODITY_HEADING_RE.match(stripped)
        if not match:
            return None
        heading = match.group(1).upper()
        # Common in-section labels that match the heading regex but aren't
        # commodity titles.  Extend if false positives are observed in new
        # MCS editions.  Last extended Section 4.2/4.5 fix (2026-06) with
        # entries surfaced by real-PDF audit against MCS 2026.
        _NOT_HEADINGS = {
            # In-chapter subsection titles
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
            # Document-structure labels surfaced by 2026-06 audit
            # (Section 4.2 fix).  These currently terminate sections
            # correctly (not in the alias table) but emitting them as
            # heading candidates adds log noise.
            "APPENDIX A",
            "APPENDIX B",
            "APPENDIX C",
            "APPENDIX D",
            "CONTENTS",
            "EXPLANATION",
            "FOREWORD",
            "INSTANT INFORMATION",
            "INTRODUCTION",
            "KEY PUBLICATIONS",
            "MINERAL COMMODITY",
            "WHERE TO OBTAIN PUBLICATIONS",
            # Cross-column / cross-region layout artifacts.  "IRZ IRZ"
            # appears in MCS 2026 as a region-code cluster repeated in
            # two columns of the appendix tables; "ARAB" and "ASIA AND
            # EURASIA SAUDI" similarly fragment from the regional
            # production tables.  Section 4.5 fix (2026-06).
            "ARAB",
            "ASIA AND EURASIA SAUDI",
            "IRZ IRZ",
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

            # Section 5.4 fix (2026-06): MCS sometimes wraps a long
            # description onto a continuation line AFTER the HTS code.
            # Example from COBALT chapter:
            #   "Cobalt mattes and other intermediate products; 8105.20.9000 Free."
            #   "cobalt powders"
            # The pre-code line ends with `;` (or `,`) and the trailing
            # clause "cobalt powders" sits on the line below — invisible
            # to a pre-code lookback.  Capture the immediate next line
            # only when the description ends in a continuation indicator.
            if description.rstrip().endswith((";", ",")):
                after_start = match.end()
                after = block[after_start:after_start + 200]
                lines_after = after.splitlines()
                # First line after the match contains the rest of the
                # tariff row (rate value); the SECOND non-empty line is
                # the wrapped continuation candidate.  Only fold it in
                # if it's plainly lowercase prose and not another row
                # (no HTS code, no leading whitespace block).
                if len(lines_after) >= 2:
                    cont = lines_after[1].strip()
                    if (
                        cont
                        and cont[0].islower()
                        and not _HTS_CODE_RE.search(cont)
                    ):
                        description = f"{description} {cont}"

            entries.append(TariffEntry(
                description=description[:512],
                hts_code=normalised,
                hts_code_raw=raw_code,
                confidence=1.0,
            ))

        return entries

    # _parse_production_leaders removed 2026-06 (Section 5.1 cleanup).
    # CSV path (mcs2026_parser._hs_production_shares) is the sole populator
    # of hs_code_production_shares for market_scope='global'.

    def _parse_import_sources(self, section_text: str) -> list[ImportSource]:
        """
        Extract US import source country percentages.

        The section header format is "Import Sources (YYYY–YY[, qualifier]):"
        where the year range varies by material and edition.  Uses regex to
        locate the header and extracts the inline country/percentage list
        that follows.

        Section 5.2 fix (2026-06): the year-parse regex no longer requires
        a closing ``)`` immediately after the end year — that anchor broke
        on the ``(2021–24, by value)`` variant introduced in MCS 2026
        (2 occurrences).  Falling back to ``reference_year - 1`` produced
        the right answer by coincidence today but would diverge if USGS
        published a different range with a qualifier.
        """
        sources: list[ImportSource] = []

        header_match = _IMPORT_SOURCES_RE.search(section_text)
        if not header_match:
            return sources

        # Parse reference year from header (end year of range).
        # Use a word boundary on the end year instead of requiring ``)`` so
        # qualifier-suffixed variants like ``(2021–24, by value):`` parse.
        year_match = re.search(r'\((\d{4})[–\-](\d{2,4})\b', header_match.group(0))
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
        """Extract the 'Salient Statistics' narrative text block.

        Section 5.3 fix (2026-06): stop-anchor regex now requires the
        colon-terminated section-header form.  The previous version matched
        bare ``Import`` inside ``Imports for consumption`` (a data row of the
        Salient Statistics table itself), truncating salient text to ~120
        chars of ~1500.  The new anchor list explicitly enumerates the
        post-Salient section headers documented in the MCS layout:

          - ``Recycling:``
          - ``Import Sources (...):``
          - ``Tariff:``
          - ``Depletion Allowance:``
          - ``Government Stockpile:``
          - ``Events, Trends, and Issues:``
          - ``Substitutes:``
          - ``World <Mine|Smelter|Refinery> Production``
            (these column headers don't always have a trailing colon)

        The captured block still contains pdfplumber's ``--- PAGE BREAK ---``
        markers; downstream consumers may strip them.  No reader of
        ``MaterialCriticalitySignal.metadata_json['salient_notes']`` exists
        today (write-only, forward-compat hook) — fixing this is so the
        first reader gets honest data rather than truncated snippets.
        """
        match = re.search(r'Salient\s+Statistics', section_text, re.IGNORECASE)
        if not match:
            return ""
        # Take up to 4000 chars after the header — salient tables are
        # multi-paragraph and the prior 2000-char window truncated
        # rows mid-table even after the anchor fix.
        block = section_text[match.end():match.end() + 4000]
        # Stop at next major section.  Each alternative carries its own
        # tail because "Events, Trends, and Issues:" has variable text
        # between "Trends" and the colon, and "Import Sources" carries
        # a parenthesised year qualifier before the colon.
        stop = re.search(
            r'\n('
            r'Recycling\s*:'
            r'|Import Sources\s*\([^)]+\)\s*:'
            r'|Tariff\s*:'
            r'|Depletion Allowance\s*:'
            r'|Government Stockpile\s*:'
            r'|Events,\s+Trends[^:\n]*:'
            r'|Substitutes\s*:'
            r'|World\s+(?:Mine|Smelter|Refinery)\s+Production'
            r')',
            block,
            re.IGNORECASE,
        )
        if stop:
            block = block[:stop.start()]
        return block.strip()[:4000]

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
        # Section 6.1 fix (2026-06): skip the exact-prefix lookup for
        # 10-digit codes — they're stored with market_scope='us' but this
        # lookup filters market_scope='global' (the resolver is only
        # interested in stage propagation from the global stage graph),
        # so the query always misses for digit_count=10.  Go straight to
        # the 6-digit truncation, which is where 10-digit stages actually
        # come from in practice.  6-digit and 4-digit lookups still hit
        # Step 1 normally.
        if digit_count != 10:
            stage, seq = _lookup(hs_code_prefix)
            if stage is not None:
                return stage, seq

        # 10-digit codes try the 6-digit truncation as their first lookup.
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

    # _build_country_map + _resolve_country removed 2026-06 (Section 6.2
    # cleanup).  Their only callers (deleted Step 4a + disabled Step 4b)
    # are gone.  If a future schema change reintroduces per-country writes
    # from the PDF path, the MCS-specific override list lived here in
    # source control — recover via `git log`.

    @staticmethod
    def _build_material_map(session: Session) -> dict[str, int]:
        """Build canonical_name → material_id lookup."""
        rows = session.execute(
            select(Material.canonical_name, Material.id)
        ).all()
        return {name: mid for name, mid in rows}

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

    # _pick_production_hs_id + _upsert_production_share removed 2026-06
    # (Section 5.1 cleanup) along with the Step 4a write block they served.

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
