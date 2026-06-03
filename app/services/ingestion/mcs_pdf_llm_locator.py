"""LLM-driven section locator for the USGS MCS PDF (Option A, step 2).

Replaces the brittle parts of regex-based section detection with an
Anthropic LLM call.  The LLM is responsible for *where* the data is
(commodity chapter boundaries, sub-section locations).  Deterministic
post-processing in this file converts the LLM's page-and-anchor output
into the line-number bounds required by ``mcs_pdf_llm_schemas.LocatorResult``.

The deterministic regex extractors in ``mcs_pdf_parser.py`` continue to
be responsible for *what the values are* (HS codes, tonnages, country
names).  Hallucination risk is bounded to "wrong section was located,"
not "wrong number was extracted."

Architecture
------------
1. ``locate_commodity_chapters()`` is the public entry point.
2. The LLM returns an internal "anchor-shaped" payload (page numbers +
   textual anchors), NOT the public schema.  This avoids asking the LLM
   to count lines in a 12K-line document, which is hallucination-prone.
3. A deterministic resolver converts page numbers → line indices using
   the ``--- PAGE BREAK ---`` markers, and converts textual anchors →
   line offsets via literal string match within the bounded chapter text.
4. The resolved data is constructed as a public ``LocatorResult``.
   ``CommodityChapter`` / ``CommoditySection`` Pydantic constraints
   enforce non-negative line indices and ``end_line > start_line``;
   beyond that, the safety net is ``_heading_in_chapter_text`` (drops
   chapters whose LLM-named heading doesn't appear within the resolved
   bounds — catches page-number hallucinations).  Materials outside the
   canonical list are filtered by the LLM (via the ``skipped`` field
   per the system prompt), not by a post-step here.
5. Optionally cached to disk as JSON.

Caching
-------
**Anthropic prompt caching:** the system prompt and the full PDF text are
both marked with ``cache_control``.  The first call writes them to
Anthropic's cache (5 min default, 1 hour with ``ttl="1h"``).  Subsequent
calls within the window read from the cache at ~10% of input cost.
Useful while iterating on the system prompt during development.

**Disk caching:** if ``cache_path`` is provided, the validated
``LocatorResult`` is written as JSON.  Subsequent runs load from disk
without calling the LLM at all.  This makes the parser deterministic in
CI and lets you diff year-over-year structural changes by checking the
JSON file into git.

Required dependency
-------------------
``pip install anthropic`` (or ``uv add anthropic``).  Imported lazily so
the schemas in ``mcs_pdf_llm_schemas.py`` remain usable without it.

Cost (annual run)
-----------------
~190K input tokens + ~5K output tokens on Claude Sonnet 4.6 ≈ $0.65
without caching.  With prompt caching during development: ~$0.13 per
re-run after the first.
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional


def _ensure_anthropic_key_in_env() -> None:
    """Best-effort load of ANTHROPIC_API_KEY from a project-root ``.env`` file
    when it's not already in the environment.

    Why: the locator can be invoked from multiple call sites — the standalone
    ``scripts/run_mcs_locator.py`` (which calls ``load_dotenv()`` itself), the
    CLI (``bdi-ingest ingest-mcs-pdf``), Inngest jobs, and ad-hoc REPL use.
    Some of those load ``.env`` automatically; some don't.  Putting the
    fallback here makes the locator self-sufficient — if the key is in
    ``.env`` and ``python-dotenv`` is installed, it works regardless of
    caller.

    No-op when the key is already set, when python-dotenv isn't installed,
    or when no ``.env`` file is found.  Never raises.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    # `load_dotenv()` with no argument walks up from cwd looking for `.env`.
    # Falls back to walking up from this file's location.
    if not load_dotenv():
        here = Path(__file__).resolve()
        for parent in here.parents:
            env_path = parent / ".env"
            if env_path.exists():
                load_dotenv(env_path, override=False)
                break

import structlog
from pydantic import BaseModel, Field, ValidationError

from app.services.ingestion.mcs_pdf_llm_schemas import (
    CommodityChapter,
    LocatorResult,
    SkippedCommodity,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Section 7.1 fix (2026-06): allow override via env so future model
# rotations (deprecation of claude-sonnet-4-6, A/B testing against a
# newer Sonnet or Opus) don't require a source edit + redeploy.
_MODEL = os.environ.get("MCS_LOCATOR_MODEL", "claude-sonnet-4-6")

# Output budget.  Empirical history:
#   - 8K: hit cap exactly, returned empty {}
#   - 16K: hit cap exactly again, returned empty {}
# Both failures were caused by the LLM trying to fit ~25 chapters × 5
# sub-sections × 2 long anchor strings + JSON syntactic overhead — well
# more than my naive estimate.  The fix is two-pronged:
#   (a) tell the LLM NOT to populate sub-sections (handled by post-
#       processor regex extractors against chapter-bounded text), and
#   (b) raise the cap to 32K as a safety cushion in case the model still
#       misjudges its budget on future MCS editions.
# With sub-sections omitted, the realistic budget is ~500-1000 output
# tokens for 25 chapters + skipped list — 32K is wildly over-provisioned
# but cheap insurance.
_MAX_OUTPUT_TOKENS = 32768

_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral", "ttl": "1h"}

# Sentinel marker used by `MCSPdfParser` when joining pages.  The LLM
# doesn't see this directly (the pdf_text we send strips it), but we need
# it to map page numbers to line indices in the joined text.
_PAGE_BREAK = "--- PAGE BREAK ---"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
#
# The prompt is intentionally explicit about the BAUXITE AND ALUMINA scope
# decision, the footnote-marker convention, and the section-type
# vocabulary.  Drift in any of these would silently produce wrong data —
# better to be over-specific here than discover the issue when scoring.

_SYSTEM_PROMPT = """You are a structural parser for the USGS Mineral Commodity Summaries (MCS) PDF.

You will receive:
  1. The full extracted text of an MCS PDF.  Pages are joined with the
     literal string "--- PAGE BREAK ---" on its own line.  The printed
     page number appears as a digit-only line near each page break.
  2. A list of canonical material names that our system scores.

Your job: for each canonical material, identify which PDF pages contain
that material's commodity chapter.  Report results via the
`report_chapters` tool.  That's the entire job — you do NOT need to
identify sub-sections inside each chapter; downstream regex extractors
handle that automatically once they have the chapter bounds.

Chapter rules:
  - Only return chapters whose commodity maps to a canonical material in
    the list provided.  List others in `skipped` with a short reason.
  - Each MCS chapter typically appears in two locations:
      (a) a one-page front-matter summary that contains the Tariff codes
          and US Import Sources, and
      (b) a one-to-two page detailed body that contains the World Mine /
          Smelter / Refinery Production tables.
    Combine BOTH into a single chapter record.  `start_page`/`end_page`
    should span the union — i.e. earliest page to latest page.
  - The chapter heading on its first page often has a footnote marker
    (e.g. "ALUMINUM¹" rendered as "ALUMINUM1").  Strip the trailing
    digit when matching against the canonical name list, but include
    the raw heading (with marker) in `pdf_heading`.

Out-of-scope (do NOT include in `chapters`):
  - "BAUXITE AND ALUMINA" — bauxite mining and alumina refining are
    upstream of the battery-relevant aluminum stage.  Add to `skipped`
    with reason "bauxite/alumina is upstream of battery-relevant aluminum".
    The "ALUMINUM" chapter (smelter production, etc.) IS in scope.
  - Commodities not in the canonical materials list (CLAYS, GYPSUM, MICA,
    GEMSTONES, SALT, SULFUR, IODINE, etc.).  Add to `skipped` with reason
    "not in canonical materials list".

Page numbers are 1-indexed PDF page numbers (the printed page numbers
inside the document footer/header, NOT the PDF reader's 1-of-N counter).

OUTPUT FORMAT — STRICT
Respond ONLY by calling the `report_chapters` tool exactly once.  Do
NOT produce any text, thinking, preamble, summary, explanation, or
commentary before the tool call.  Begin the tool call as your very first
output token.

Each chapter record needs only `canonical_material`, `pdf_heading`,
`start_page`, and `end_page` — the schema doesn't accept sub-sections.
Downstream regex extractors locate Tariff / Production / Import Sources /
Salient Statistics blocks within each chapter automatically.
"""


# ---------------------------------------------------------------------------
# Internal types — LLM-shaped payload (anchors instead of line numbers)
# ---------------------------------------------------------------------------

# _LLMSection class removed 2026-06 (Section 10 cleanup) — chapter-only
# locator; sub-section identification handled by regex extractors.


class _LLMChapter(BaseModel):
    """Chapter as returned by the LLM — page numbers only.

    Section 10 cleanup (2026-06): ``sections`` field removed.  The system
    prompt asks the LLM for chapter bounds only.
    """

    canonical_material: str
    pdf_heading: str
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)


class _LLMResponse(BaseModel):
    """Top-level LLM-shaped response.  Resolved into ``LocatorResult`` by
    the deterministic post-step in ``locate_commodity_chapters``.
    """

    chapters: list[_LLMChapter] = Field(default_factory=list)
    skipped: list[SkippedCommodity] = Field(default_factory=list)
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Tool definition for Anthropic
# ---------------------------------------------------------------------------

def _tool_definition() -> dict[str, Any]:
    """Build the Anthropic tool spec for `report_chapters`.

    Pydantic's ``model_json_schema()`` produces standard JSON Schema; the
    Anthropic tool-use API accepts that with minimal massaging.  We do not
    pass ``additionalProperties: false`` because Anthropic models tend to
    add explanatory keys when uncertain, and we'd rather drop them than
    fail the whole call.
    """
    return {
        "name": "report_chapters",
        "description": (
            "Report all commodity chapters found in the MCS PDF, with each "
            "chapter's page range and the textual anchors that delimit its "
            "sub-sections."
        ),
        "input_schema": _LLMResponse.model_json_schema(),
    }


# ---------------------------------------------------------------------------
# Deterministic resolution: page → line, anchor → line offset
# ---------------------------------------------------------------------------

# Match a standalone line that is just a 1–4 digit number — used as the
# page-number marker that USGS prints on each page.  Combined with the
# `--- PAGE BREAK ---` separator from MCSPdfParser, this gives us a
# reliable mapping from printed page number to line index.
_PAGE_NUMBER_RE = re.compile(r"^\s*(\d{1,4})\s*$")

# Trailing footnote marker on commodity headings — regular digits (when
# pdfplumber renders unicode superscripts as plain digits) and the unicode
# superscripts themselves (when it doesn't).  Used by `_normalize_heading`.
_HEADING_FOOTNOTE_RE = re.compile(r"[\d¹²³⁴⁵⁶⁷⁸⁹⁰]+\s*$")


def _normalize_heading(heading: str) -> str:
    """Strip trailing footnote markers and return uppercased heading text.

        'ALUMINUM1'             → 'ALUMINUM'
        'BAUXITE AND ALUMINA¹'  → 'BAUXITE AND ALUMINA'
        'PLATINUM-GROUP METALS' → 'PLATINUM-GROUP METALS'
    """
    return _HEADING_FOOTNOTE_RE.sub("", heading).strip().upper()


def _heading_in_chapter_text(
    pdf_heading: str,
    lines: list[str],
    start_line: int,
    end_line: int,
    *,
    search_first_n_nonempty: int = 30,
) -> bool:
    """Return True if the LLM-reported ``pdf_heading`` (footnote-stripped)
    appears in the first few non-empty lines of the resolved chapter text.

    Safety net for LLM page-number hallucinations.  If the LLM says a
    chapter spans pages 65–66 but those pages actually contain CESIUM,
    not COBALT, the heading won't appear in the resolved range and we
    drop the chapter.  Without this check, the resolver would silently
    attribute CESIUM content to Cobalt.

    Implementation note: searches the first ``search_first_n_nonempty``
    NON-EMPTY lines (rather than the first N lines of any kind) so blank
    lines, page-number markers, and byline preambles don't consume the
    search budget before the heading appears.  USGS MCS chapters
    typically have:
        --- PAGE BREAK ---
        (blank)
        Prepared by …                 ← byline
        109                            ← printed page number
        IRON ORE                       ← actual heading (here at the 3rd
                                         non-empty line after the break)
    A budget of 30 non-empty lines comfortably covers any preamble.

    Both sides are normalised by ``_normalize_heading`` so footnote
    markers don't cause spurious mismatches.
    """
    clean = _normalize_heading(pdf_heading)
    if not clean:
        return False
    seen = 0
    end = min(end_line, len(lines))
    for i in range(start_line, end):
        line = lines[i].strip()
        if not line:
            continue
        if clean in _normalize_heading(line):
            return True
        seen += 1
        if seen >= search_first_n_nonempty:
            break
    return False


def _build_page_index(lines: list[str]) -> dict[int, int]:
    """Return ``{printed_page_number: line_index}`` mapping.

    Walks the joined PDF text looking for ``--- PAGE BREAK ---`` markers,
    and for each break records the printed page number that appears in
    the surrounding lines (USGS prints the number on its own line).  The
    line index is the FIRST non-empty content line of that page (i.e.
    the line just after the break marker).

    Pages that don't have a clean printed page number adjacent to their
    break (typically the cover page and a few intros) are simply skipped;
    the LLM doesn't reference those pages anyway since they don't contain
    chapters.
    """
    page_to_line: dict[int, int] = {}
    n = len(lines)

    for i, line in enumerate(lines):
        if line.strip() != _PAGE_BREAK:
            continue

        # Section 8.1 fix (2026-06): only consider numeric lines AFTER
        # the break.  Page numbers that appear BEFORE a break are
        # footers of the page ENDING (the content for that page started
        # at the prior break, not at this one).  The previous code
        # scanned ±5 lines symmetrically and picked the first match,
        # which mis-indexed any page using footer-style numbering —
        # observed in MCS 2026 intro pages (1-15).  Commodity chapters
        # all use header-style numbering (number AFTER break), so this
        # narrower scan keeps every chapter page correctly indexed
        # while dropping the off-by-one-page intro entries (which the
        # LLM never requests anyway).
        page_num: Optional[int] = None
        for j in range(i + 1, min(n, i + 6)):
            m = _PAGE_NUMBER_RE.match(lines[j])
            if m:
                page_num = int(m.group(1))
                break
        if page_num is None:
            continue

        # First non-empty content line AFTER the break is the start of
        # this printed page.
        start = i + 1
        while start < n and not lines[start].strip():
            start += 1
        # Don't overwrite — first occurrence wins, in case the page
        # number appears more than once (e.g. footnote references).
        page_to_line.setdefault(page_num, start)

    return page_to_line


# _find_anchor_line removed 2026-06 (Section 10 cleanup) — sub-section
# anchor resolution is no longer part of the contract; downstream regex
# extractors locate sub-sections within chapter-bounded text.


def _resolve_chapter(
    llm_chapter: _LLMChapter,
    lines: list[str],
    page_to_line: dict[int, int],
) -> Optional[CommodityChapter]:
    """Convert one LLM-shaped chapter to the public schema.

    Returns None (and logs) when the chapter cannot be resolved — typically
    because the LLM returned a page number we couldn't index, or because
    the heading-presence safety net rejects the resolved bounds.
    """
    start_line_opt = page_to_line.get(llm_chapter.start_page)
    if start_line_opt is None:
        log.warning(
            "mcs_pdf_llm_locator.unresolved_start_page",
            material=llm_chapter.canonical_material,
            start_page=llm_chapter.start_page,
        )
        return None

    # End-line: the start of the FIRST indexed page strictly after end_page.
    # Falling back to len(lines) on the very next page is wrong — many MCS
    # pages lack a clean numeric page-number marker where _build_page_index
    # expects one, so they're not in page_to_line.  If we extend to EOF
    # whenever end_page+1 is missing, chapters near layout-irregular regions
    # silently inherit hundreds or thousands of lines of unrelated content.
    # Observed in MCS 2026 with Fluorspar (pp.80-81 → L4493-12111, the
    # rest of the document) and Zirconium (pp.214-215 → L11601-12111).
    candidate_pages = [p for p in page_to_line if p > llm_chapter.end_page]
    if candidate_pages:
        end_line = page_to_line[min(candidate_pages)]
    else:
        end_line = len(lines)

    if end_line <= start_line_opt:
        log.warning(
            "mcs_pdf_llm_locator.invalid_chapter_bounds",
            material=llm_chapter.canonical_material,
            start_line=start_line_opt,
            end_line=end_line,
        )
        return None

    # Safety net: confirm the LLM-reported heading actually appears in
    # the resolved chapter text.  Catches cases where the LLM
    # hallucinates page numbers and the resolver pulls a different
    # commodity's content (e.g. "COBALT pages 65-66" when those pages
    # are actually CESIUM).  Without this, the resolver would silently
    # attribute the wrong content to the named material.
    if not _heading_in_chapter_text(
        llm_chapter.pdf_heading, lines, start_line_opt, end_line,
    ):
        first_line_preview = (
            lines[start_line_opt][:80] if start_line_opt < len(lines) else "<eof>"
        )
        log.warning(
            "mcs_pdf_llm_locator.heading_not_in_chapter",
            material=llm_chapter.canonical_material,
            pdf_heading=llm_chapter.pdf_heading,
            start_page=llm_chapter.start_page,
            end_page=llm_chapter.end_page,
            start_line=start_line_opt,
            end_line=end_line,
            first_line=first_line_preview,
            note=(
                "LLM-reported heading not found in resolved chapter text — "
                "likely wrong page numbers in the LLM response.  Dropping "
                "chapter."
            ),
        )
        return None

    # Section 10 cleanup (2026-06): sub-section anchor resolution removed
    # along with ``_find_anchor_line`` — the LLM no longer emits anchors
    # and downstream regex extractors locate sub-sections automatically.

    try:
        return CommodityChapter(
            canonical_material=llm_chapter.canonical_material,
            pdf_heading=llm_chapter.pdf_heading,
            start_page=llm_chapter.start_page,
            end_page=llm_chapter.end_page,
            start_line=start_line_opt,
            end_line=end_line,
        )
    except ValidationError as exc:
        log.warning(
            "mcs_pdf_llm_locator.chapter_validation_failed",
            material=llm_chapter.canonical_material,
            error=str(exc),
        )
        return None


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _call_llm_sync(
    pdf_text: str,
    canonical_material_names: list[str],
    *,
    client: Any,
) -> _LLMResponse:
    """Make a synchronous ``messages.create`` call and return the parsed response.

    Kept as a fallback / debugging path.  Hits the per-minute rate limit
    on lower account tiers (Tier 1 caps Sonnet at 30K input tokens/min,
    so a 190K-token MCS run will fail with ``RateLimitError`` here).
    Use ``_call_llm_batch`` instead for production runs.
    """
    params = _build_request_params(pdf_text, canonical_material_names)
    response = client.messages.create(**params)
    return _parse_message_response(response)


# ---------------------------------------------------------------------------
# LLM call — Batch API path (preferred for the annual MCS run)
# ---------------------------------------------------------------------------
#
# The Batch API has separate (much higher) limits than the per-minute
# sync rate limit on lower tiers, accepts 190K+ token inputs in a single
# request without throttling, and applies a 50% pricing discount.  For
# annual offline ingestion this is the right tool.
#
# Trade-off: the batch is async.  Anthropic typically completes batches
# in 5–30 minutes (rarely longer).  This function polls every
# `poll_interval_seconds` and surfaces structured progress logs so the
# caller can see the run isn't hung.

# How requests are tagged in the batch payload — only matters because
# `messages.batches.results` returns results keyed by this id.
_BATCH_CUSTOM_ID = "mcs-locate"

# Default polling cadence + overall timeout.  30s polling matches
# Anthropic's recommended cadence (avoids rate-limiting on the polling
# endpoint itself).  30 min timeout covers the typical case + headroom;
# `use_batch=False` is available as an escape hatch.
_DEFAULT_POLL_INTERVAL = 30
_DEFAULT_TIMEOUT_SECONDS = 30 * 60


def _build_request_params(
    pdf_text: str,
    canonical_material_names: list[str],
) -> dict[str, Any]:
    """Construct the params dict that goes into both sync `messages.create`
    AND batch request entries — single source of truth so the two paths
    can't drift.
    """
    materials_listing = "\n".join(
        f"  - {n}" for n in sorted(canonical_material_names)
    )
    user_text = (
        "Canonical material names (case-sensitive, must match exactly):\n"
        f"{materials_listing}\n\n"
        "MCS PDF text follows.  Identify all commodity chapters and call "
        "the report_chapters tool exactly once."
    )

    return {
        "model": _MODEL,
        "max_tokens": _MAX_OUTPUT_TOKENS,
        "system": [
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": _CACHE_CONTROL,
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": pdf_text,
                        "cache_control": _CACHE_CONTROL,
                    },
                    {"type": "text", "text": user_text},
                ],
            },
        ],
        "tools": [_tool_definition()],
        "tool_choice": {"type": "tool", "name": "report_chapters"},
    }


def _parse_message_response(response: Any) -> _LLMResponse:
    """Extract and validate the report_chapters tool input from a Message.

    Shared by the sync and batch paths — the post-processing is identical
    once we have a Message object in hand.
    """
    usage = getattr(response, "usage", None)
    if usage is not None:
        log.info(
            "mcs_pdf_llm_locator.usage",
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None),
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None),
        )

    tool_input: Optional[dict[str, Any]] = None
    for block in response.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "report_chapters"
        ):
            tool_input = block.input
            break

    if tool_input is None:
        raise RuntimeError(
            "Anthropic response did not contain a `report_chapters` tool_use block. "
            f"stop_reason={getattr(response, 'stop_reason', None)!r}"
        )

    try:
        parsed = _LLMResponse.model_validate(tool_input)
    except ValidationError as exc:
        log.error(
            "mcs_pdf_llm_locator.response_validation_failed",
            errors=str(exc),
            raw_input=json.dumps(tool_input)[:2000],
        )
        raise RuntimeError(
            f"LLM response failed Pydantic validation: {exc}"
        ) from exc

    # If the parsed result is empty, dump diagnostics about the raw response
    # so we can tell whether the model truncated mid-tool-call (output cap),
    # produced text-only, or genuinely returned an empty tool input.
    if not parsed.chapters and not parsed.skipped:
        # Summarise content blocks: type + size of each
        block_summary = []
        for b in response.content:
            btype = getattr(b, "type", "unknown")
            if btype == "text":
                text = getattr(b, "text", "")
                block_summary.append({
                    "type": "text",
                    "chars": len(text),
                    "preview": text[:200],
                })
            elif btype == "tool_use":
                inp = getattr(b, "input", None)
                inp_json = json.dumps(inp) if inp is not None else None
                block_summary.append({
                    "type": "tool_use",
                    "name": getattr(b, "name", None),
                    "input_chars": len(inp_json) if inp_json else 0,
                    "input_keys": list(inp.keys()) if isinstance(inp, dict) else None,
                })
            else:
                block_summary.append({"type": btype})

        log.error(
            "mcs_pdf_llm_locator.empty_result_diagnostic",
            stop_reason=getattr(response, "stop_reason", None),
            content_block_count=len(response.content),
            blocks=block_summary,
            tool_input_preview=json.dumps(tool_input)[:500],
            note=(
                "Tool call returned empty result.  If stop_reason is "
                "'max_tokens', increase _MAX_OUTPUT_TOKENS.  If text blocks "
                "appeared before tool_use, tighten the system prompt's "
                "'no preamble' instruction.  If neither, the model is "
                "genuinely refusing to emit data — review the prompt."
            ),
        )

    return parsed


def _call_llm_batch(
    pdf_text: str,
    canonical_material_names: list[str],
    *,
    client: Any,
    poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> _LLMResponse:
    """Submit a one-request batch, poll until done, return the parsed response.

    Raises:
        RuntimeError if the batch fails to complete within ``timeout_seconds``,
        if the result entry is missing, or if the result is anything other
        than ``succeeded``.
    """
    params = _build_request_params(pdf_text, canonical_material_names)

    log.info(
        "mcs_pdf_llm_locator.batch.submitting",
        pdf_chars=len(pdf_text),
        canonical_materials=len(canonical_material_names),
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
    )

    batch = client.messages.batches.create(
        requests=[
            {
                "custom_id": _BATCH_CUSTOM_ID,
                "params": params,
            },
        ],
    )

    batch_id = batch.id
    log.info(
        "mcs_pdf_llm_locator.batch.submitted",
        batch_id=batch_id,
        processing_status=batch.processing_status,
    )

    # Poll until done or timeout
    deadline = time.time() + timeout_seconds
    elapsed = 0
    while True:
        if time.time() > deadline:
            raise RuntimeError(
                f"Batch {batch_id} did not complete within {timeout_seconds} seconds. "
                f"You can resume by calling `client.messages.batches.results({batch_id!r})` "
                f"directly once it finishes."
            )

        time.sleep(poll_interval_seconds)
        elapsed += poll_interval_seconds

        batch = client.messages.batches.retrieve(batch_id)
        # `request_counts` is an object on newer SDKs; coerce to a dict for logging.
        rc = getattr(batch, "request_counts", None)
        rc_dict = (
            {k: getattr(rc, k, None) for k in ("processing", "succeeded", "errored", "canceled", "expired")}
            if rc is not None
            else None
        )
        log.info(
            "mcs_pdf_llm_locator.batch.poll",
            batch_id=batch_id,
            processing_status=batch.processing_status,
            elapsed_seconds=elapsed,
            request_counts=rc_dict,
        )
        if batch.processing_status == "ended":
            break

    # Pull results — the SDK returns a streaming iterator over JSONL.
    log.info("mcs_pdf_llm_locator.batch.retrieving", batch_id=batch_id)
    target = None
    for entry in client.messages.batches.results(batch_id):
        if getattr(entry, "custom_id", None) == _BATCH_CUSTOM_ID:
            target = entry
            break

    if target is None:
        raise RuntimeError(
            f"Batch {batch_id} returned no result entry for "
            f"custom_id={_BATCH_CUSTOM_ID!r}.  Check the batch in the "
            f"Anthropic console for diagnostic details."
        )

    result = target.result
    result_type = getattr(result, "type", None)
    if result_type != "succeeded":
        # `result.error` exists when type=="errored"; fall through to the
        # repr for any other unexpected state.
        err = getattr(result, "error", None)
        raise RuntimeError(
            f"Batch {batch_id} request {_BATCH_CUSTOM_ID!r} did not succeed: "
            f"type={result_type!r}, detail={err!r}"
        )

    return _parse_message_response(result.message)


# ---------------------------------------------------------------------------
# LLM call — sync path (kept as fallback / testing aid)
# ---------------------------------------------------------------------------

def _load_cached(cache_path: Path) -> Optional[LocatorResult]:
    """Load a previously-persisted LocatorResult from JSON, or None.

    Treats an empty result (zero chapters AND zero skipped) as if no cache
    exists — it's almost always the residue of an aborted or failed run
    that wrote a default-empty payload, not a legitimate "the LLM saw
    nothing" outcome.  Refusing to load these prevents the silent
    "everything keeps coming back empty" trap.
    """
    if not cache_path.exists():
        return None
    try:
        result = LocatorResult.model_validate_json(cache_path.read_text())
    except (ValidationError, json.JSONDecodeError, OSError) as exc:
        log.warning(
            "mcs_pdf_llm_locator.cache_load_failed",
            cache_path=str(cache_path),
            error=str(exc),
        )
        return None

    if not result.chapters and not result.skipped:
        log.warning(
            "mcs_pdf_llm_locator.cache_empty_treated_as_miss",
            cache_path=str(cache_path),
            note=(
                "Cache file has zero chapters AND zero skipped — treating "
                "as a miss and re-calling the LLM.  Delete the file or pass "
                "force_refresh=False to suppress this if you actually want "
                "an empty cached result."
            ),
        )
        return None

    return result


def _save_cached(cache_path: Path, result: LocatorResult) -> None:
    """Persist a LocatorResult to JSON.  Indented for human-readable diffs."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(result.model_dump_json(indent=2))
    log.info(
        "mcs_pdf_llm_locator.cache_saved",
        cache_path=str(cache_path),
        chapters=len(result.chapters),
        skipped=len(result.skipped),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def locate_commodity_chapters(
    pdf_text: str,
    canonical_material_names: list[str],
    *,
    client: Any = None,
    cache_path: Optional[Path] = None,
    force_refresh: bool = False,
    use_batch: bool = True,
    poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> LocatorResult:
    """Locate commodity chapters in the MCS PDF text via Anthropic LLM.

    Returns a fully-validated ``LocatorResult`` whose ``chapters`` carry
    line-number bounds resolved from the LLM's page+anchor output.

    Args:
        pdf_text:
            Full PDF text, joined with ``"\\n\\n--- PAGE BREAK ---\\n\\n"``
            between pages.  Same convention ``MCSPdfParser`` uses today.
        canonical_material_names:
            Material names from ``materials.canonical_name``.  Used both
            as the LLM filter (only return chapters mapping to one of
            these) and as the runtime validator (reject responses with
            unknown canonical_material values).
        client:
            An ``anthropic.Anthropic`` instance.  When None, a fresh
            client is created with default credentials.  Passed in
            explicitly by tests to inject mocks.
        cache_path:
            If provided, the result is loaded from this path on a hit and
            saved on a miss.  Recommended path:
            ``Path("data") / f"mcs{year}_locator_result.json"``.
        force_refresh:
            When True, ignore any existing disk cache and re-call the LLM.
            Use after MCS releases a new edition or after editing the
            system prompt.
        use_batch:
            When True (default), submit the request via Anthropic's
            Batch API — slower (5–30 min wait) but bypasses the per-
            minute rate limit and applies a 50% pricing discount.  When
            False, uses the synchronous ``messages.create`` path, which
            is faster but throttled by the per-minute input-token cap on
            lower account tiers.
        poll_interval_seconds / timeout_seconds:
            Batch polling controls.  Only consulted when ``use_batch``.

    Raises:
        RuntimeError if the LLM response cannot be parsed or validated,
        AND no usable disk cache exists.  The caller (typically
        ``MCSPdfParser.parse()``) is expected to fall back to the regex
        path on this error.
    """
    # Disk-cache hit?
    if cache_path is not None and not force_refresh:
        cached = _load_cached(cache_path)
        if cached is not None:
            log.info(
                "mcs_pdf_llm_locator.cache_hit",
                cache_path=str(cache_path),
                chapters=len(cached.chapters),
            )
            return cached

    # Lazy import so the rest of the codebase doesn't require anthropic
    if client is None:
        # Best-effort load of ANTHROPIC_API_KEY from .env when missing.
        # Idempotent — no-op if already set or if dotenv isn't installed.
        _ensure_anthropic_key_in_env()
        try:
            import anthropic  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "The `anthropic` package is required to call the MCS PDF "
                "LLM locator.  Install with `uv add anthropic` or "
                "`pip install anthropic`."
            ) from exc
        client = anthropic.Anthropic()

    log.info(
        "mcs_pdf_llm_locator.calling_llm",
        pdf_chars=len(pdf_text),
        canonical_materials=len(canonical_material_names),
        force_refresh=force_refresh,
        mode="batch" if use_batch else "sync",
    )

    if use_batch:
        llm_response = _call_llm_batch(
            pdf_text=pdf_text,
            canonical_material_names=canonical_material_names,
            client=client,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
        )
    else:
        llm_response = _call_llm_sync(
            pdf_text=pdf_text,
            canonical_material_names=canonical_material_names,
            client=client,
        )

    # Resolve anchors to line indices
    lines = pdf_text.splitlines()
    page_to_line = _build_page_index(lines)

    log.info(
        "mcs_pdf_llm_locator.resolution_input",
        pages_indexed=len(page_to_line),
        total_lines=len(lines),
        chapters_from_llm=len(llm_response.chapters),
    )

    resolved_chapters: list[CommodityChapter] = []
    for llm_ch in llm_response.chapters:
        ch = _resolve_chapter(llm_ch, lines, page_to_line)
        if ch is not None:
            resolved_chapters.append(ch)

    result = LocatorResult(
        chapters=resolved_chapters,
        skipped=llm_response.skipped,
        notes=llm_response.notes,
    )

    # Runtime validators
    valid_set = set(canonical_material_names)
    canonical_errors = result.validate_canonical_materials(valid_set)
    bounds_errors = result.validate_line_bounds(len(lines))

    if canonical_errors:
        log.warning(
            "mcs_pdf_llm_locator.canonical_material_drift",
            errors=canonical_errors[:10],
            total_errors=len(canonical_errors),
        )
        # Drop chapters with bad canonical_material rather than failing
        # the whole call.  The LLM occasionally invents close-but-wrong
        # material names; rejecting those rows is better than rejecting
        # the entire run.
        result.chapters = [
            c for c in result.chapters if c.canonical_material in valid_set
        ]

    if bounds_errors:
        # Bounds errors after our own resolution should be impossible
        # unless the page index is wrong; surface as ERROR for visibility.
        log.error(
            "mcs_pdf_llm_locator.line_bounds_violation",
            errors=bounds_errors[:10],
            total_errors=len(bounds_errors),
        )
        # Drop offending chapters
        valid_chapters = []
        for c in result.chapters:
            if c.end_line > len(lines):
                continue
            valid_chapters.append(c)
        result.chapters = valid_chapters

    log.info(
        "mcs_pdf_llm_locator.complete",
        chapters=len(result.chapters),
        skipped=len(result.skipped),
        canonical_drift_dropped=len(canonical_errors),
        bounds_violations_dropped=len(bounds_errors),
    )

    # Persist to disk on success
    if cache_path is not None:
        _save_cached(cache_path, result)

    return result


__all__ = [
    "locate_commodity_chapters",
]
