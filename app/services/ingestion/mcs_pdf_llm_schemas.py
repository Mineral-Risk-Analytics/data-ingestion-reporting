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


# Section 10 cleanup (2026-06): ``SectionType`` Literal + ``CommoditySection``
# class removed.  The LLM locator only emits chapter-level bounds now;
# sub-section identification is done by the deterministic regex extractors
# in ``mcs_pdf_parser.py``.  The historical ``"production"`` SectionType
# entry was also dead post Section 5.1 (per-country world-production
# extractor deleted; CSV path is sole populator).  If a future feature
# needs sub-section routing tags, reinstate from git history.


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
    # ``sections`` field removed 2026-06 (Section 10 cleanup) — the LLM
    # never populated it (per the system prompt) and no downstream code
    # iterated it.  Pydantic v2 defaults to ``extra='ignore'`` so existing
    # cache files with a ``sections: []`` field still parse cleanly.

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
        """Return error strings — one per chapter whose line bounds exceed
        the actual PDF line count.  Empty list ⇒ all valid.

        Cheap bounds check that catches LLM hallucinations of line numbers
        before they cause IndexErrors downstream.  Section 10 cleanup
        (2026-06): the sub-section loop was removed along with
        ``CommodityChapter.sections``.
        """
        errors: list[str] = []
        for chapter in self.chapters:
            if chapter.end_line > total_lines:
                errors.append(
                    f"Chapter {chapter.canonical_material!r}: end_line "
                    f"{chapter.end_line} exceeds PDF line count {total_lines}"
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


# section_text helper removed 2026-06 (Section 10 cleanup) along with
# ``CommoditySection`` — no remaining caller.


__all__ = [
    "CommodityChapter",
    "SkippedCommodity",
    "LocatorResult",
    "chapter_text",
]
