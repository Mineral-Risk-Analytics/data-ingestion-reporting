"""Pydantic contract for the LLM-driven MCS PDF section locator.

This module defines the data shape returned by
``locate_commodity_chapters()`` in ``mcs_pdf_llm_locator.py``.  It contains
only schemas and validation helpers — no LLM client code, no DB code, no
parsing logic — so the contract can be unit-tested without network or LLM
dependencies, and can be imported by tooling that wants the JSON Schema
without paying for any of the heavier dependencies.

Why this contract exists
------------------------
The brittle parts of MCS PDF ingestion are:
  1. **Section boundary detection** — every year USGS tweaks layout, adds
     footnote markers, renames variant table headers ("World Smelter
     Production" vs "World Mine Production").  Regex pattern accumulation
     is unsustainable here.
  2. **Material attribution at multi-stage chapters** — "BAUXITE AND
     ALUMINA" covers two stages of Aluminum; the regex parser would need a
     dedicated entry for every chapter-name variant.

Both are semantic-structure problems that LLMs handle well.  This contract
is the boundary at which we hand the LLM responsibility for *where* the
data is, while keeping deterministic regex extractors responsible for
*what the values are* (HS codes, tonnages, country names).  Hallucination
risk is bounded to "wrong section was located," not "wrong code was
extracted."

Line-number convention
----------------------
Line indices in this contract refer to the joined PDF text:

    pages = [page.extract_text() or "" for page in pdf.pages]
    full = "\\n\\n--- PAGE BREAK ---\\n\\n".join(pages)
    lines = full.splitlines()

That is the same convention ``MCSPdfParser`` uses today, so callers can
slice ``lines[start_line:end_line]`` to recover any section's text.

Page numbers are 1-indexed (matching the PDF's printed page numbers).

Note on `from __future__ import annotations`
--------------------------------------------
Intentionally NOT used here.  Pydantic v2 + future-annotations does not
resolve Literal type aliases at model-construction time without an explicit
``model_rebuild()`` step, which is awkward and error-prone.  This module is
small and self-contained, so eager evaluation of type hints is the simpler
and more reliable choice.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Section vocabulary — fixed so downstream extractors can route by tag
# ---------------------------------------------------------------------------

SectionType = Literal[
    "tariff",            # tariff codes table → _parse_tariff_table
    "production",        # world production / reserves table → _parse_production_leaders
    "import_sources",    # US import sources percentages → _parse_import_sources
    "salient_notes",     # quantitative narrative section → _extract_salient_notes
    "events_trends",     # qualitative narrative; not currently parsed (reserved)
]


# ---------------------------------------------------------------------------
# Sub-section
# ---------------------------------------------------------------------------

class CommoditySection(BaseModel):
    """One sub-section inside a commodity chapter.

    ``start_line`` is inclusive. ``end_line`` is exclusive (Python slice
    semantics: ``lines[start_line:end_line]``).
    """

    section_type: SectionType = Field(
        description=(
            "Type of sub-section.  Restricted to a fixed vocabulary so that "
            "downstream code can route to the correct extractor by tag rather "
            "than by raw heading text.  Use 'tariff' for the tariff-table "
            "section, 'production' for World Mine/Smelter/Refinery Production "
            "tables, 'import_sources' for US import-source percentage lists, "
            "'salient_notes' for the Salient Statistics block, and "
            "'events_trends' for the Events, Trends, and Issues narrative."
        ),
    )
    start_line: int = Field(
        ge=0,
        description="Inclusive starting line index in the joined PDF text.",
    )
    end_line: int = Field(
        ge=1,
        description=(
            "Exclusive ending line index — `lines[start_line:end_line]` "
            "yields exactly the section's text."
        ),
    )

    @model_validator(mode="after")
    def _check_line_order(self) -> "CommoditySection":
        if self.end_line <= self.start_line:
            raise ValueError(
                f"end_line ({self.end_line}) must be > start_line "
                f"({self.start_line}) for section {self.section_type!r}"
            )
        return self


# ---------------------------------------------------------------------------
# Chapter
# ---------------------------------------------------------------------------

class CommodityChapter(BaseModel):
    """One commodity chapter located in the MCS PDF.

    A chapter spans the full logical content for a single canonical material:
    front-matter summary AND detailed body, if those are split across
    different page ranges.  The line bounds should cover the union of those
    ranges; if there is a gap, the LLM may either:

      * pick the larger range and let the parser ignore the gap, or
      * report two separate ``CommodityChapter`` records under the same
        ``canonical_material`` (the downstream caller will concatenate them).

    Either approach is acceptable as long as ``sections`` accurately point
    to the sub-sections that contain the data.
    """

    canonical_material: str = Field(
        description=(
            "The canonical material name from `materials.canonical_name`. "
            "MUST be a member of the list provided in the LLM prompt.  "
            "If the PDF heading covers multiple stages of one material "
            "(e.g. 'BAUXITE AND ALUMINA' covers ore + intermediate of "
            "Aluminum), use the parent material name ('Aluminum').  "
            "If no canonical match exists, the chapter belongs in "
            "`LocatorResult.skipped`, NOT here."
        ),
    )
    pdf_heading: str = Field(
        description=(
            "Raw heading text as it appears in the PDF, uppercased, including "
            "any footnote marker (e.g. 'BAUXITE AND ALUMINA1').  Used for "
            "human-facing logs only — downstream code routes on "
            "`canonical_material`."
        ),
    )
    start_page: int = Field(
        ge=1,
        description="1-indexed first PDF page of the chapter (printed page number).",
    )
    end_page: int = Field(
        ge=1,
        description="1-indexed last PDF page of the chapter, inclusive.",
    )
    start_line: int = Field(
        ge=0,
        description="Inclusive starting line index in the joined PDF text.",
    )
    end_line: int = Field(
        ge=1,
        description=(
            "Exclusive ending line index.  `lines[start_line:end_line]` "
            "yields the chapter's full text."
        ),
    )
    sections: list[CommoditySection] = Field(
        default_factory=list,
        description=(
            "Sub-sections within this chapter.  May be empty if the LLM cannot "
            "identify any standard sub-section.  Sub-sections need not cover "
            "the chapter completely — gaps between sub-sections are unparsed "
            "by design (e.g. inter-section narrative or footnotes).  Each "
            "sub-section's line range MUST be fully contained inside this "
            "chapter's line range; the validator enforces this."
        ),
    )

    @model_validator(mode="after")
    def _check_consistency(self) -> "CommodityChapter":
        if self.end_line <= self.start_line:
            raise ValueError(
                f"Chapter {self.canonical_material!r}: end_line "
                f"({self.end_line}) must be > start_line ({self.start_line})"
            )
        if self.end_page < self.start_page:
            raise ValueError(
                f"Chapter {self.canonical_material!r}: end_page "
                f"({self.end_page}) must be >= start_page ({self.start_page})"
            )
        for sec in self.sections:
            if sec.start_line < self.start_line or sec.end_line > self.end_line:
                raise ValueError(
                    f"Chapter {self.canonical_material!r}: sub-section "
                    f"{sec.section_type!r} bounds "
                    f"({sec.start_line}, {sec.end_line}) are outside the "
                    f"chapter bounds ({self.start_line}, {self.end_line})."
                )
        return self


# ---------------------------------------------------------------------------
# Skipped commodity (diagnostic only)
# ---------------------------------------------------------------------------

class SkippedCommodity(BaseModel):
    """A commodity the LLM identified in the PDF but didn't include in
    ``chapters`` because it doesn't match a canonical material we score.

    Used for monitoring (was a new battery-relevant commodity added to MCS
    that we haven't seeded?) and for confirming the LLM is correctly
    filtering — not for any downstream processing.
    """

    pdf_heading: str = Field(
        description="Raw heading text from the PDF.",
    )
    reason: str = Field(
        description=(
            "Why this commodity was skipped.  Typical values: "
            "'no canonical match' (e.g. CLAYS, GYPSUM — not battery-relevant); "
            "'covered by another chapter' (e.g. duplicate / cross-reference); "
            "'ambiguous attribution' (multiple plausible canonical materials)."
        ),
    )


# ---------------------------------------------------------------------------
# Top-level result
# ---------------------------------------------------------------------------

class LocatorResult(BaseModel):
    """Top-level response from ``locate_commodity_chapters()``.

    Even if the locator chunks the PDF internally for cost or context-window
    reasons, the final returned object merges all chunks into one
    ``LocatorResult``.  Callers should treat this as the authoritative,
    fully-merged view.
    """

    chapters: list[CommodityChapter] = Field(
        default_factory=list,
        description=(
            "Commodity chapters that mapped to a known canonical material. "
            "May be empty (e.g. on parser failure); callers should handle "
            "the empty case as a hard error and log."
        ),
    )
    skipped: list[SkippedCommodity] = Field(
        default_factory=list,
        description=(
            "Commodities found in the PDF but not mapped to any canonical "
            "material.  Diagnostic only."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description=(
            "Optional free-form notes from the LLM about ambiguities, edge "
            "cases, or anything worth a human's attention.  Surfaced in logs."
        ),
    )

    # ------------------------------------------------------------------
    # Runtime validators (called by mcs_pdf_llm_locator after Pydantic parse)
    # ------------------------------------------------------------------

    def validate_canonical_materials(
        self,
        valid_materials: set[str],
    ) -> list[str]:
        """Return error strings — one per chapter whose ``canonical_material``
        isn't in ``valid_materials``.  Empty list ⇒ all valid.

        We don't enforce this inside the Pydantic model because the valid
        set comes from the DB at runtime, not from the schema.  The locator
        function calls this immediately after parsing the LLM response,
        before returning to its caller.
        """
        errors: list[str] = []
        for chapter in self.chapters:
            if chapter.canonical_material not in valid_materials:
                errors.append(
                    f"Unknown canonical_material "
                    f"{chapter.canonical_material!r} for PDF heading "
                    f"{chapter.pdf_heading!r}"
                )
        return errors

    def validate_line_bounds(self, total_lines: int) -> list[str]:
        """Return error strings — one per chapter or sub-section whose
        line bounds exceed the actual PDF line count.  Empty list ⇒ all
        valid.

        Cheap bounds check that catches LLM hallucinations of line numbers
        before they cause IndexErrors downstream.
        """
        errors: list[str] = []
        for chapter in self.chapters:
            if chapter.end_line > total_lines:
                errors.append(
                    f"Chapter {chapter.canonical_material!r}: end_line "
                    f"{chapter.end_line} exceeds PDF line count {total_lines}"
                )
            for sec in chapter.sections:
                if sec.end_line > total_lines:
                    errors.append(
                        f"Chapter {chapter.canonical_material!r}, section "
                        f"{sec.section_type!r}: end_line {sec.end_line} "
                        f"exceeds PDF line count {total_lines}"
                    )
        return errors


# ---------------------------------------------------------------------------
# Convenience helpers — text slicing
# ---------------------------------------------------------------------------

def chapter_text(chapter: CommodityChapter, lines: list[str]) -> str:
    """Return the chapter's text from the joined PDF text lines.

    Equivalent to ``"\\n".join(lines[chapter.start_line:chapter.end_line])``
    — wrapped for readability and a stable callsite the test suite can mock.
    """
    return "\n".join(lines[chapter.start_line:chapter.end_line])


def section_text(section: CommoditySection, lines: list[str]) -> str:
    """Return the sub-section's text from the joined PDF text lines."""
    return "\n".join(lines[section.start_line:section.end_line])


__all__ = [
    "SectionType",
    "CommoditySection",
    "CommodityChapter",
    "SkippedCommodity",
    "LocatorResult",
    "chapter_text",
    "section_text",
]
