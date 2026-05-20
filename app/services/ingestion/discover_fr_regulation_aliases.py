"""Discover Federal Register doc numbers for tracked US regulations.

Surfaces candidate FR document numbers that should be added to
``seed_regulation_aliases.py:_FEDERAL_REGISTER`` so that risk events
ingested from those documents link to their canonical regulation rows
via ``RiskEventRegulation``.

Why we need this
----------------
The Federal Register ingester (``ingest_federal_register.py``) creates
RiskEvent rows for every document it pulls — but it does NOT link those
events back to canonical Regulation rows unless an explicit alias row
maps the FR doc number to a regulation_key.  Today the
``_FEDERAL_REGISTER`` alias list is empty.  That means:

  * UFLPA-related FR notices (Entity List adds, CBP rulings) don't
    drive the UFLPA event_score path in the Regulatory pillar.
  * IRA Domestic Content FR rules (§45X final rule, FEOC guidance)
    don't accumulate under the IRA_DOMESTIC obligation.
  * SEC climate-disclosure FR documents have no canonical regulation
    to link to (regulation defined in seed_regulations.py but no
    external IDs map to it).

This script closes that gap by searching the FR API for documents
referencing each tracked regulation, surfacing them as structured
suggestions partner can review and copy into seed_regulation_aliases.py.

Architecture — follows existing FR ingester patterns
----------------------------------------------------
The script reuses the same FR API conventions as ``ingest_federal_register.py``:

  * Same base URL (``https://www.federalregister.gov/api/v1``)
  * Same agency-slug filter shape (``conditions[agencies][]=...``)
  * Same doc-type code filter (RULE / PRESDOCU / NOTICE / PRORULE)
  * Same free-text filter (``conditions[term]=...``)
  * Same date filter (``conditions[publication_date][gte]=...``)
  * Same rate-limit delay (0.5s between calls)
  * Same per-query lookback semantics

Each tracked regulation gets a ``DiscoveryQuery`` config describing
which agency / doc-type / term combinations to search.  The script
runs each query in sequence, deduplicates document_number values
across queries (a regulation often surfaces under multiple search
patterns), and emits a structured suggestion list.

Haiku benefit analysis
----------------------
**Would Haiku improve discovery quality?** Yes, modestly.  The FR API's
free-text search is AND-style — every word must appear in the doc
title/abstract.  That's already a fairly strict filter, but it returns
false positives:

  * Documents that MENTION UFLPA in a "background" or "preamble"
    section without being UFLPA enforcement actions
  * Documents about unrelated trade matters that happen to cite UFLPA
    as legislative context
  * Documents that match an agency + keyword filter but cover a
    different sub-topic (e.g., BIS export controls that mention
    "critical minerals" but aren't about CRMA-style policy)

Haiku could classify each candidate as "this document IS an
enforcement action / implementation rule for regulation X" vs "this
document REFERENCES regulation X tangentially."  Cost would be
~$0.005-0.008 per document × ~50-200 candidates per discovery run
× ~3-5 tracked regulations = roughly $1-8 per full discovery run.

**Recommendation:** make Haiku classification opt-in via the
``--use-haiku`` flag (defaults to off).  Run cheap heuristics first
(title/abstract keyword presence, doc_type filtering); enable Haiku
when partner wants to triage a large candidate list down to high-
precision matches.  Without Haiku, the script still produces useful
output — partner just gets more candidates per regulation to review
manually.

A future enhancement could add a Haiku-extraction path that returns
the structured "this document affects regulation X at scope_type Y
for material list Z" output, but that's beyond the alias-discovery
scope.

Usage
-----
    bdi-ingest discover-fr-regulation-aliases \\
        --regulations UFLPA,IRA_DOMESTIC,SEC_CLIMATE_2024 \\
        --lookback-days 1095 \\
        [--use-haiku]

Output: JSON list of suggested ``("federal_register", doc_number,
regulation_key)`` tuples grouped by regulation_key, ready for partner
to paste into seed_regulation_aliases.py.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional
from urllib.parse import quote, urlencode

import httpx
import structlog

log = structlog.get_logger(__name__)

# Mirror the existing FR ingester's API constants (kept identical so
# behaviour stays consistent — DRY would be cleaner but the ingester's
# constants live behind a thicker wall of inheritance).
_BASE_URL = "https://www.federalregister.gov/api/v1"
_PER_PAGE = 100
_RATE_LIMIT_DELAY = 0.5
_API_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=10.0, pool=10.0)


@dataclass
class DiscoveryQuery:
    """Configuration for one alias-discovery search.

    Each tracked regulation gets one or more DiscoveryQuery configs
    describing which FR API filter combinations should surface
    relevant documents.  Search results are deduplicated by
    document_number across queries — a document found via multiple
    queries surfaces only once.
    """
    regulation_key: str
    description: str
    term: Optional[str] = None
    agencies: Optional[list[str]] = None
    doc_types: Optional[list[str]] = None
    lookback_days: int = 1095  # 3 years default
    # Higher confidence_hint = stronger signal that documents matching
    # this query are genuine references to the regulation.  Used to
    # rank suggestions in the output.
    confidence_hint: float = 0.7


# Per-regulation discovery configurations.  Each regulation may have
# multiple queries to capture different document classes (e.g., UFLPA
# surfaces in both CBP enforcement notices AND BIS export-control
# rules).
_DISCOVERY_QUERIES: dict[str, list[DiscoveryQuery]] = {
    "UFLPA": [
        DiscoveryQuery(
            regulation_key="UFLPA",
            description="UFLPA Entity List + DHS enforcement documents",
            term="UFLPA",
            agencies=["homeland-security-department", "u-s-customs-and-border-protection"],
            confidence_hint=0.95,
        ),
        DiscoveryQuery(
            regulation_key="UFLPA",
            description="Uyghur Forced Labor Prevention Act-related rules across agencies",
            term="Uyghur Forced Labor Prevention Act",
            doc_types=["RULE", "NOTICE", "PRORULE"],
            confidence_hint=0.90,
        ),
    ],
    "IRA_DOMESTIC": [
        DiscoveryQuery(
            regulation_key="IRA_DOMESTIC",
            description="FEOC-related Treasury/IRS rules (battery component + critical mineral)",
            term="Foreign Entity of Concern",
            agencies=["treasury-department", "internal-revenue-service"],
            confidence_hint=0.95,
        ),
        DiscoveryQuery(
            regulation_key="IRA_DOMESTIC",
            description="Section 45X advanced manufacturing production credit",
            term="Section 45X advanced manufacturing",
            agencies=["treasury-department", "internal-revenue-service"],
            doc_types=["RULE", "PRORULE"],
            confidence_hint=0.90,
        ),
        DiscoveryQuery(
            regulation_key="IRA_DOMESTIC",
            description="Domestic content critical-mineral rules under IRA",
            term="domestic content critical mineral",
            doc_types=["RULE", "PRORULE"],
            confidence_hint=0.80,
        ),
    ],
    "SEC_CLIMATE_2024": [
        DiscoveryQuery(
            regulation_key="SEC_CLIMATE_2024",
            description="SEC climate-related disclosure rule (final + amendments + stays)",
            term="climate-related disclosure",
            agencies=["securities-and-exchange-commission"],
            doc_types=["RULE", "PRORULE", "NOTICE"],
            lookback_days=1095,  # 3-year lookback — rule was published
                                  # 2024-03-06, ~800 days ago.  Original
                                  # 730-day default missed the rule itself.
            confidence_hint=0.95,
        ),
        DiscoveryQuery(
            regulation_key="SEC_CLIMATE_2024",
            description="SEC Enhancement / Standardization climate disclosure rule by full title",
            term="Enhancement and Standardization of Climate-Related Disclosures",
            doc_types=["RULE", "PRORULE"],
            lookback_days=1095,
            confidence_hint=0.99,
        ),
    ],
}


@dataclass
class DiscoverySuggestion:
    """One suggested alias row, with provenance for partner review."""
    regulation_key: str
    document_number: str
    title: str
    publication_date: Optional[str]
    doc_type: Optional[str]
    agencies: list[str] = field(default_factory=list)
    html_url: Optional[str] = None
    matched_via_queries: list[str] = field(default_factory=list)
    confidence: float = 0.7

    def as_alias_tuple(self) -> tuple[str, str, str]:
        """Return the row in the format expected by seed_regulation_aliases."""
        return ("federal_register", self.document_number, self.regulation_key)

    def to_dict(self) -> dict:
        return {
            "regulation_key": self.regulation_key,
            "document_number": self.document_number,
            "title": self.title,
            "publication_date": self.publication_date,
            "doc_type": self.doc_type,
            "agencies": self.agencies,
            "html_url": self.html_url,
            "matched_via_queries": self.matched_via_queries,
            "confidence": self.confidence,
            "seed_tuple": list(self.as_alias_tuple()),
        }


def _fetch_page(
    client: httpx.Client,
    query: DiscoveryQuery,
    since_date: date,
    page: int,
) -> tuple[list[dict], int]:
    """Fetch one page of FR documents for the given discovery query.

    Identical query-building approach to ingest_federal_register._fetch_page
    so the FR API treats our requests consistently.  Returns
    (items, total_pages).
    """
    def _q(s: str, safe: str, encoding: Optional[str] = None, errors: Optional[str] = None) -> str:
        return quote(str(s), safe="[]", encoding=encoding, errors=errors)

    param_pairs: list[tuple[str, str]] = [
        ("conditions[publication_date][gte]", since_date.isoformat()),
        ("per_page", str(_PER_PAGE)),
        ("page", str(page)),
        ("order", "newest"),
    ]
    if query.term:
        param_pairs.append(("conditions[term]", query.term))
    if query.agencies:
        for slug in query.agencies:
            param_pairs.append(("conditions[agencies][]", slug))
    doc_types = query.doc_types or ["RULE", "PRORULE", "NOTICE", "PRESDOCU"]
    for code in doc_types:
        param_pairs.append(("conditions[type][]", code))
    for field_name in (
        "document_number", "title", "abstract", "publication_date",
        "agencies", "html_url", "type",
    ):
        param_pairs.append(("fields[]", field_name))

    url = f"{_BASE_URL}/documents.json?{urlencode(param_pairs, quote_via=_q)}"
    resp = client.get(url, timeout=_API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("results") or []
    total_pages = data.get("total_pages") or 1
    return [r for r in items if isinstance(r, dict)], int(total_pages)


def discover_fr_regulation_aliases(
    regulation_keys: Optional[list[str]] = None,
    *,
    max_pages: int = 5,
    use_haiku: bool = False,
) -> dict[str, list[DiscoverySuggestion]]:
    """Discover Federal Register doc numbers for tracked regulations.

    Args:
        regulation_keys:  List of regulation_keys to search.  Defaults
                          to all regulations in _DISCOVERY_QUERIES.
        max_pages:        Max pages to fetch per query (100 docs/page).
                          Default 5 = up to 500 candidates per query.
        use_haiku:        When True, classify each candidate via Haiku
                          to filter "is genuinely about this regulation"
                          from "tangentially mentions regulation."
                          Costs ~$0.005-0.008 per candidate; disabled
                          by default to keep discovery cheap.

    Returns:
        Dict mapping regulation_key → list of DiscoverySuggestion,
        deduplicated by document_number across all queries for that
        regulation, sorted by publication_date descending.
    """
    if regulation_keys is None:
        regulation_keys = list(_DISCOVERY_QUERIES.keys())

    # Filter to known regulations
    unknown = [k for k in regulation_keys if k not in _DISCOVERY_QUERIES]
    if unknown:
        log.warning(
            "discover_fr_regulation_aliases.unknown_regulation_keys",
            unknown=unknown,
            known=list(_DISCOVERY_QUERIES.keys()),
        )
    regulation_keys = [k for k in regulation_keys if k in _DISCOVERY_QUERIES]

    today = date.today()
    results: dict[str, list[DiscoverySuggestion]] = {}

    # Haiku classifier loaded once, only when requested
    classifier = None
    if use_haiku:
        from app.services.ingestion.material_classifier import MaterialClassifier
        from app.models.supply import Material
        from app.database import SessionLocal

        with SessionLocal() as s:
            mats = s.query(Material).all()
            materials_by_id = {m.id: m.canonical_name for m in mats}
        classifier = MaterialClassifier(materials_by_id=materials_by_id)
        if not classifier.enabled:
            log.warning("discover_fr_regulation_aliases.haiku_disabled_no_key")
            classifier = None

    with httpx.Client(follow_redirects=True) as client:
        for reg_key in regulation_keys:
            queries = _DISCOVERY_QUERIES[reg_key]
            log.info(
                "discover_fr_regulation_aliases.regulation_start",
                regulation_key=reg_key,
                query_count=len(queries),
            )

            seen_doc_numbers: dict[str, DiscoverySuggestion] = {}

            for query in queries:
                since_date = today - timedelta(days=query.lookback_days)
                for page in range(1, max_pages + 1):
                    time.sleep(_RATE_LIMIT_DELAY)
                    try:
                        items, total_pages = _fetch_page(
                            client, query, since_date, page,
                        )
                    except (httpx.HTTPError, ValueError) as exc:
                        log.warning(
                            "discover_fr_regulation_aliases.api_error",
                            regulation_key=reg_key,
                            query_description=query.description,
                            page=page,
                            error=str(exc),
                        )
                        break

                    for item in items:
                        doc_number = item.get("document_number")
                        if not doc_number:
                            continue

                        existing = seen_doc_numbers.get(doc_number)
                        if existing is not None:
                            # Already found via another query — record
                            # this query as additional provenance.
                            existing.matched_via_queries.append(query.description)
                            # Take the max confidence_hint across queries
                            existing.confidence = max(
                                existing.confidence, query.confidence_hint,
                            )
                            continue

                        agencies = []
                        for a in item.get("agencies", []) or []:
                            if isinstance(a, dict) and a.get("name"):
                                agencies.append(str(a["name"]))

                        suggestion = DiscoverySuggestion(
                            regulation_key=reg_key,
                            document_number=doc_number,
                            title=str(item.get("title") or "")[:300],
                            publication_date=item.get("publication_date"),
                            doc_type=item.get("type"),
                            agencies=agencies[:5],
                            html_url=item.get("html_url"),
                            matched_via_queries=[query.description],
                            confidence=query.confidence_hint,
                        )
                        seen_doc_numbers[doc_number] = suggestion

                    if page >= total_pages:
                        break

            # ── Optional Haiku classification pass ──────────────────
            # When enabled, ask Haiku per-candidate: "is this document
            # genuinely an enforcement / implementation / rulemaking
            # document for regulation X, or just a tangential mention?"
            # Filter out candidates the model rejects.
            if classifier is not None and seen_doc_numbers:
                filtered: dict[str, DiscoverySuggestion] = {}
                for doc_number, suggestion in seen_doc_numbers.items():
                    relevance_text = (
                        f"Federal Register document: {suggestion.title}\n"
                        f"Doc type: {suggestion.doc_type}\n"
                        f"Agencies: {', '.join(suggestion.agencies)}\n"
                        f"Question: is this document an enforcement, "
                        f"implementation, or rulemaking action for {reg_key}, "
                        f"or does it merely reference {reg_key} tangentially?"
                    )
                    try:
                        is_relevant, conf, reason = (
                            classifier.assess_battery_industry_relevance(
                                relevance_text,
                            )
                        )
                    except Exception as exc:
                        log.warning(
                            "discover_fr_regulation_aliases.haiku_error",
                            doc_number=doc_number,
                            error=str(exc),
                        )
                        filtered[doc_number] = suggestion
                        continue
                    # Haiku's relevance is calibrated for battery industry
                    # rather than regulation-specific.  Use it as a coarse
                    # confidence multiplier: relevant → keep, not relevant
                    # with high confidence → drop, otherwise keep with
                    # caveat note.
                    if not is_relevant and conf >= 0.7:
                        log.debug(
                            "discover_fr_regulation_aliases.haiku_dropped",
                            doc_number=doc_number,
                            confidence=conf,
                            reason=reason,
                        )
                        continue
                    suggestion.confidence = (
                        suggestion.confidence * 0.5 + conf * 0.5
                    )
                    filtered[doc_number] = suggestion
                seen_doc_numbers = filtered

            # Sort by publication_date descending (newest first)
            suggestions = list(seen_doc_numbers.values())
            suggestions.sort(
                key=lambda s: (s.publication_date or "1900-01-01"),
                reverse=True,
            )
            results[reg_key] = suggestions

            log.info(
                "discover_fr_regulation_aliases.regulation_done",
                regulation_key=reg_key,
                candidates_found=len(suggestions),
            )

    return results


def format_seed_tuples(
    results: dict[str, list[DiscoverySuggestion]],
    *,
    min_confidence: float = 0.0,
) -> str:
    """Format discovery results as Python tuple literals ready to paste
    into ``seed_regulation_aliases.py:_FEDERAL_REGISTER``.

    Filters by min_confidence (default 0.0 = include all).  Set higher
    (e.g., 0.85) when reviewing a large candidate list.
    """
    lines: list[str] = []
    for reg_key, suggestions in results.items():
        kept = [s for s in suggestions if s.confidence >= min_confidence]
        if not kept:
            continue
        lines.append(f"    # ── {reg_key} ────────────────────────────")
        for s in kept:
            comment = (
                f"  # {s.publication_date} | {s.doc_type or '?'} | "
                f"conf={s.confidence:.2f} | "
                f"{(s.title[:60] + '...') if len(s.title) > 60 else s.title}"
            )
            lines.append(
                f'    ("federal_register", "{s.document_number}", "{reg_key}"),'
                f"{comment}"
            )
        lines.append("")
    return "\n".join(lines)
