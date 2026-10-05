"""SEC filing body-text section parser (Workstream B).

Sits next to ``filing_parser.py`` (which handles the submissions-JSON
metadata) and produces the narrative content that the metadata parser
intentionally leaves as a stub.  See ``alembic/versions/049_filing_body_sections.py``
for the storage rationale and the ``section_code`` taxonomy.

Three-layer extraction strategy
-------------------------------
For each (filing, target_section) pair we try in order:

  1. ``edgartools`` — primary path.  iXBRL-aware section extraction for
     10-K and 20-F.  Catches >80% of well-formed filings.  Imported
     lazily inside ``_extract_via_edgartools`` so this module remains
     importable if the dep isn't installed yet.

  2. Regex over plain text — fallback.  BeautifulSoup-strip the HTML,
     find "Item N." anchors, slice between consecutive anchors.
     Handles the long tail of formatting quirks that edgartools chokes
     on but produces less-clean text (may include navigation, page
     numbers, etc.).

  3. LLM locator (Haiku) — last-resort fallback for the long tail.
     STUBBED for v1; the parser logs "needs_llm_locator" and returns
     None for that section.  Implement in B2.1 once the v1 pipeline
     has a known set of failed cases to test against (premature LLM
     wiring is hard to evaluate without ground truth).

Each successful extraction records:
  - ``parser_method``: "edgartools" | "regex" | "llm_locator"
  - ``parser_version``: free-text version string defined here

Version bumps are how the fetcher decides what to re-parse.  Bump
``_PARSER_VERSION_*`` constants below when an extractor changes
meaningfully (different edgartools API, regex pattern fix, etc.).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Parser version constants — bump on meaningful change so the fetcher can
# target stale rows via WHERE parser_version != '<current>'.
# ---------------------------------------------------------------------------
_PARSER_VERSION_EDGARTOOLS = "edgartools-v1-2026-06-15"
_PARSER_VERSION_REGEX = "regex-v1-2026-06-15"
_PARSER_VERSION_LLM = "llm-locator-v0-stub"


# ---------------------------------------------------------------------------
# Section taxonomy — single source of truth for what we extract per form.
# Section codes match the convention in migration 049: `<form>.<item_slug>`.
# ---------------------------------------------------------------------------

# Map ``(form, section_code)`` → human-readable name.  Drives the regex
# anchor table below and the edgartools attribute lookup.
_SECTION_REGISTRY: dict[tuple[str, str], dict] = {
    # ── 10-K (US domestic annual) ────────────────────────────────────────
    # NOTE on digit-anchor specificity: every item digit is followed by
    # ``(?![a-z])`` so the anchor doesn't bleed into the next sub-item.
    # Without it ``item\s*7`` would happily match "Item 7A" too and the
    # "last occurrence" heuristic in _extract_via_regex would land on
    # the wrong line — observed during smoke-test development.
    ("10-K", "10-K.item_1a"): {
        "label": "Risk Factors",
        # Regex needle.  Matches "Item 1A" or "ITEM 1A" with arbitrary
        # whitespace + optional period; tolerates "Risk Factors" or just
        # the bare item number.  Anchor needs to be specific enough to
        # not match the table of contents on its own.
        "regex_anchor": r"item\s*1a(?![a-z])\.?\s*(risk\s+factors)?",
        # Anchors that mark the END of this section (next item starts).
        # Order matters — we use the FIRST matching following-anchor.
        "regex_terminators": [
            r"item\s*1b(?![a-z])\.?",
            r"item\s*2(?![a-z])\.?\s*(properties)?",
        ],
        "edgartools_attr": "risk_factors",
    },
    ("10-K", "10-K.item_2"): {
        "label": "Properties",
        "regex_anchor": r"item\s*2(?![a-z])\.?\s*properties",
        "regex_terminators": [r"item\s*3(?![a-z])\.?"],
        "edgartools_attr": "properties",
    },
    ("10-K", "10-K.item_7"): {
        "label": "Management's Discussion and Analysis",
        # ``7(?![a-z])`` is the critical guard against matching "Item 7A".
        "regex_anchor": (
            r"item\s*7(?![a-z])\.?\s*"
            r"(management['’]?s\s+discussion\s+and\s+analysis)?"
        ),
        "regex_terminators": [
            r"item\s*7a(?![a-z])\.?",
            r"item\s*8(?![a-z])\.?",
        ],
        "edgartools_attr": "mda",
    },

    # ── 20-F (foreign private issuer annual) ─────────────────────────────
    # 20-F item numbering is its own world — "Item 3.D Risk Factors",
    # "Item 4.B Business Overview", "Item 4.D Property, Plants and
    # Equipment", "Item 5 Operating and Financial Review".  Anchors here
    # have to handle "Item 3.D", "Item 3D", "ITEM 3.D" forms.
    ("20-F", "20-F.item_3d"): {
        "label": "Risk Factors (20-F)",
        "regex_anchor": r"item\s*3\.?\s*d(?![a-z])\.?\s*(risk\s+factors)?",
        "regex_terminators": [r"item\s*4(?![a-z])\.?\s*[a-d]?"],
        "edgartools_attr": "risk_factors",  # edgartools maps 20-F too
    },
    ("20-F", "20-F.item_4d"): {
        "label": "Property, Plants and Equipment (20-F)",
        "regex_anchor": (
            r"item\s*4\.?\s*d(?![a-z])\.?\s*"
            r"(property,?\s+plants?\s+and\s+equipment)?"
        ),
        "regex_terminators": [
            r"item\s*4a(?![a-z])\.?",
            r"item\s*5(?![a-z])\.?",
        ],
        "edgartools_attr": "properties",
    },
    ("20-F", "20-F.item_5"): {
        "label": "Operating and Financial Review (20-F)",
        "regex_anchor": (
            r"item\s*5(?![a-z])\.?\s*"
            r"(operating\s+and\s+financial\s+review)?"
        ),
        "regex_terminators": [
            r"item\s*5a(?![a-z])\.?",
            r"item\s*6(?![a-z])\.?",
        ],
        "edgartools_attr": "mda",
    },
}


# Public helper for callers that want to know what sections exist for a form.
def target_sections_for_form(form: str) -> list[str]:
    """Return the list of ``section_code`` strings we extract for a form."""
    return [code for (f, code) in _SECTION_REGISTRY.keys() if f == form]


# Minimum useful section length.  Sections below this are treated as
# extraction failures (table-of-contents anchor matches that didn't
# reach actual content, mostly).  Tunable — 200 chars is a couple
# sentences which is the floor for "the parser found something real".
_MIN_SECTION_CHARS = 200


# ---------------------------------------------------------------------------
# Parse-result dataclass
# ---------------------------------------------------------------------------

@dataclass
class SectionParseResult:
    """One extracted section ready to land in ``filing_body_sections``.

    ``text`` is whitespace-normalised plain text (HTML stripped).
    ``method`` is the path that succeeded ("edgartools" | "regex" |
    "llm_locator").  ``version`` is the extractor's version string.
    """

    section_code: str
    text: str
    method: str
    version: str

    @property
    def char_count(self) -> int:
        return len(self.text)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_filing_sections(
    *,
    raw_html: str,
    form: str,
    accession_number: Optional[str] = None,
) -> list[SectionParseResult]:
    """Extract narrative sections from a fetched SEC filing.

    Tries each extractor in order: edgartools → regex → (LLM stub).
    Returns one ``SectionParseResult`` per section actually found.
    Sections below ``_MIN_SECTION_CHARS`` are dropped — extracting an
    "Item 1A" anchor from the table of contents but no following body
    is treated as a miss.

    Args:
        raw_html: Filing HTML/XHTML body text.  Caller is responsible
            for fetching from ``primary_document_url`` with the
            SEC-required ``User-Agent`` header.
        form: SEC form code ("10-K" | "20-F").  Determines which
            section taxonomy applies.
        accession_number: Optional, used only for log breadcrumbs.

    Returns:
        List of successful ``SectionParseResult`` rows.  Empty list if
        no sections could be extracted (caller should log and move on).
    """
    target_codes = target_sections_for_form(form)
    if not target_codes:
        log.warning(
            "filing_body_parser.unsupported_form",
            form=form,
            accession=accession_number,
            note="No section taxonomy for this form — caller should skip.",
        )
        return []

    results: list[SectionParseResult] = []
    extracted_codes: set[str] = set()

    # ── Layer 1: edgartools ──────────────────────────────────────────────
    try:
        edgar_results = _extract_via_edgartools(
            raw_html=raw_html,
            form=form,
            target_codes=target_codes,
            accession_number=accession_number,
        )
        for r in edgar_results:
            if r.char_count >= _MIN_SECTION_CHARS:
                results.append(r)
                extracted_codes.add(r.section_code)
    except _EdgartoolsUnavailable:
        # Not installed — log once and fall straight through to regex.
        log.info(
            "filing_body_parser.edgartools_unavailable",
            accession=accession_number,
            note="Falling back to regex extractor.",
        )
    except Exception as exc:  # pragma: no cover — defensive
        # Defensive catch: edgartools' parsers throw a range of types
        # (lxml errors, KeyError when iXBRL is missing, etc.).  We
        # don't want a single bad filing to crash the batch.
        log.warning(
            "filing_body_parser.edgartools_exception",
            accession=accession_number,
            form=form,
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )

    # ── Layer 2: regex on plain text ─────────────────────────────────────
    missing_codes = [c for c in target_codes if c not in extracted_codes]
    if missing_codes:
        regex_results = _extract_via_regex(
            raw_html=raw_html,
            form=form,
            target_codes=missing_codes,
            accession_number=accession_number,
        )
        for r in regex_results:
            if r.char_count >= _MIN_SECTION_CHARS:
                results.append(r)
                extracted_codes.add(r.section_code)

    # ── Layer 3: LLM locator (stubbed for v1) ────────────────────────────
    still_missing = [c for c in target_codes if c not in extracted_codes]
    if still_missing:
        log.info(
            "filing_body_parser.needs_llm_locator",
            accession=accession_number,
            form=form,
            missing_sections=still_missing,
            note=(
                "edgartools + regex both failed for these sections.  "
                "LLM locator is stubbed in v1 — these filings are logged "
                "and skipped for now.  Wire ``_extract_via_llm_locator`` "
                "in B2.1 once we have enough real failures to test against."
            ),
        )

    return results


# ---------------------------------------------------------------------------
# Layer 1 — edgartools
# ---------------------------------------------------------------------------

class _EdgartoolsUnavailable(Exception):
    """Raised when edgartools is not importable in the current env."""


def _extract_via_edgartools(
    *,
    raw_html: str,
    form: str,
    target_codes: list[str],
    accession_number: Optional[str],
) -> list[SectionParseResult]:
    """Use edgartools' Filing-form objects to pull each target section.

    Lazy import so the rest of the parser module — and anything that
    imports it — works when edgartools isn't installed.

    edgartools exposes form-specific accessors on the parsed Filing
    object: ``TenK.risk_factors``, ``TenK.properties``, ``TenK.mda``.
    20-F is handled by ``TwentyF`` with the same attribute names.
    """
    try:
        # edgartools' Documents module accepts raw HTML strings via
        # its ``HtmlDocument`` constructor; we use the form-specific
        # parser to get section accessors.
        from edgar.documents import HtmlDocument  # type: ignore
    except ImportError as exc:
        raise _EdgartoolsUnavailable() from exc

    # Defensive: edgartools' high-level parsers expect a Filing
    # object (which itself wraps an SEC HTTP fetch).  Since we already
    # have the raw HTML in hand, we use the lower-level HtmlDocument
    # path and project to the form-specific accessors.
    try:
        from edgar.documents import HtmlDocument as _HD  # noqa: F811
        doc = _HD(raw_html)  # parses to internal tree
    except Exception as exc:  # pragma: no cover — depends on lib version
        log.debug(
            "filing_body_parser.edgartools_doc_construct_failed",
            accession=accession_number,
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return []

    results: list[SectionParseResult] = []
    for code in target_codes:
        meta = _SECTION_REGISTRY.get((form, code))
        if meta is None:
            continue
        attr = meta["edgartools_attr"]
        try:
            # The edgartools API exposes section text via attribute
            # access on the parsed document.  Different library
            # versions wrap this differently; we try the most common
            # accessors in order.  All return ``None`` or empty on
            # miss rather than raising.
            text: Optional[str] = None
            getter = getattr(doc, attr, None)
            if callable(getter):
                text = getter()
            elif isinstance(getter, str):
                text = getter
            elif getter is not None and hasattr(getter, "text"):
                text = getter.text

            if not text or not isinstance(text, str):
                continue
            text = _normalise_whitespace(text)
            if not text:
                continue
            results.append(
                SectionParseResult(
                    section_code=code,
                    text=text,
                    method="edgartools",
                    version=_PARSER_VERSION_EDGARTOOLS,
                )
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "filing_body_parser.edgartools_section_failed",
                accession=accession_number,
                section=code,
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            continue
    return results


# ---------------------------------------------------------------------------
# Layer 2 — regex over plain text
# ---------------------------------------------------------------------------

# Reuse the bs4/lxml stack already used by other parsers in the codebase.
# Lazy-imported so this module stays importable without bs4 installed.
def _strip_html(raw_html: str) -> str:
    """HTML → plain text, normalised whitespace.  Best-effort."""
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        # No bs4 — strip with a brute-force regex.  Worse fidelity but
        # the regex anchor scan below still works for plain-anchor
        # filings (small minority of modern 10-Ks).
        plain = re.sub(r"<[^>]+>", " ", raw_html)
    else:
        soup = BeautifulSoup(raw_html, "lxml")
        for el in soup(["script", "style"]):
            el.decompose()
        plain = soup.get_text(separator=" ")
    return _normalise_whitespace(plain)


def _normalise_whitespace(text: str) -> str:
    """Collapse whitespace runs to single spaces, strip ends."""
    if not text:
        return ""
    # &nbsp; → space (bs4's get_text usually leaves the unicode char)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*\n+", "\n\n", text)
    return text.strip()


def _extract_via_regex(
    *,
    raw_html: str,
    form: str,
    target_codes: list[str],
    accession_number: Optional[str],
) -> list[SectionParseResult]:
    """Anchor-based extractor over plain-text-stripped HTML.

    For each target section we find its anchor (case-insensitive) and
    slice from there to the FIRST terminator anchor that follows.

    Known limitations:
      * The table of contents typically contains every item anchor.
        We pick the LAST occurrence of the start anchor as a heuristic
        (TOC mentions come first; the actual section comes later).
        Imperfect — some 10-Ks include "see Item 1A" cross-references
        that throw this off.  edgartools handles those cleanly; this
        path is the fallback.
      * Page headers / footers that repeat "Item 1A — Risk Factors"
        will pollute the result.  Acceptable for downstream consumers
        (material attribution, facility extraction) — the noise is
        immaterial at the LLM-input scale.
    """
    plain = _strip_html(raw_html)
    if len(plain) < 500:
        # Filing is suspiciously short — probably an exhibit list or a
        # malformed primary doc.  Don't waste cycles.
        return []

    plain_lower = plain.lower()
    results: list[SectionParseResult] = []

    for code in target_codes:
        meta = _SECTION_REGISTRY.get((form, code))
        if meta is None:
            continue

        # Find ALL occurrences of the start anchor; pick the LAST one
        # (heuristic: TOC mentions come first, body comes later).
        anchor_re = re.compile(meta["regex_anchor"], re.IGNORECASE)
        starts = list(anchor_re.finditer(plain_lower))
        if not starts:
            continue
        body_start = starts[-1].start()

        # Find the nearest terminator after body_start.
        body_end = len(plain)
        for term_pattern in meta["regex_terminators"]:
            term_re = re.compile(term_pattern, re.IGNORECASE)
            m = term_re.search(plain_lower, pos=body_start + 50)
            # +50 so the start anchor itself doesn't immediately
            # match its own terminator (anchor "Item 7" terminator
            # "Item 7A" would otherwise match the same position).
            if m and m.start() < body_end:
                body_end = m.start()

        section_text = plain[body_start:body_end]
        section_text = _normalise_whitespace(section_text)
        if not section_text:
            continue
        results.append(
            SectionParseResult(
                section_code=code,
                text=section_text,
                method="regex",
                version=_PARSER_VERSION_REGEX,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Layer 3 — LLM locator (stubbed for v1)
# ---------------------------------------------------------------------------

def _extract_via_llm_locator(  # pragma: no cover — stub
    *,
    raw_html: str,
    form: str,
    target_codes: list[str],
    accession_number: Optional[str],
) -> list[SectionParseResult]:
    """STUB — to be implemented in B2.1.

    Intent: feed the plain-stripped text to Haiku with a structured-
    output schema asking for byte offsets of each target section's
    start and end.  Slice the text at the returned offsets.

    Why deferred to B2.1: we want to evaluate against a real set of
    filings where edgartools + regex both failed.  Without ground
    truth from those cases the prompt design is guesswork.  The
    in-DB ``parser_method='regex'`` rows (plus the logged
    ``needs_llm_locator`` events) will provide that test set after
    v1 runs.
    """
    return []


__all__ = [
    "SectionParseResult",
    "parse_filing_sections",
    "target_sections_for_form",
]
