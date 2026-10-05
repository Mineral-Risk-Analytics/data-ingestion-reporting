"""Keyword-based HS resolution for free-text price benchmark descriptors.

Companion to ``material_resolver.MaterialResolver`` which goes the other
way (HS code → material).  This module goes (material, descriptor_text)
→ HS code, using ``hs_code_material_mappings.description`` and
``hs_code_material_mappings.keywords`` as the single source of truth.

Use cases
---------
- USGS MCS Salient Price rows: ``"Price, annual average-real,
  battery-grade lithium carbonate"`` → HS 2836.91 via the description
  match on the Lithium row carrying ``"Lithium carbonate (Li₂CO₃)
  battery-grade precursor"``.
- World Bank Pink Sheet column resolution: cheaper than maintaining a
  parallel ``_HEADER_TO_HS_PREFIX`` dict, especially as Pink Sheet
  evolves.
- Future commercial price feeds (Asian Metal, Fastmarkets) — same
  pattern.

Why not a separate alias table
-----------------------------
``hs_code_material_mappings`` already encodes the canonical
"material × stage → HS code" relationship for the platform.  Adding a
parallel USGS-specific or feed-specific alias table would create two
sources of truth that drift over time.  Reusing the existing keyword
mechanism means partner-curation work on HS mappings (adding a keyword
to the seed file) automatically improves price-form resolution for
every price feed.

Scoring contract
----------------
``resolve_hs_for_price_descriptor`` returns ``(hs_mapping_id, score,
matched_via)`` where:

- ``hs_mapping_id`` is ``None`` when no candidate scored above the
  confidence floor.  Callers should fall back to material-level
  attribution in that case (i.e. ``CommodityPrice.hs_mapping_id = None``).
- ``score`` is a non-negative float — partner can inspect in
  ``CommodityPrice.metadata_json["hs_resolution"]`` to debug why a row
  landed at a particular HS.
- ``matched_via`` is one of ``"keywords"``, ``"description"``, or
  ``"mixed"`` — tells the partner which signal pushed the match over
  the line.

Confidence floor (``_MIN_RESOLUTION_SCORE``) is intentionally
conservative: prefer ``None`` over a wrong attribution.  Partner can
lower the floor or add curated keywords as needed.
"""

from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.supply import HsCodeMaterialMapping

# Confidence floor — require at least this many "match points" before
# attributing an HS code.  Empirically: 1 substantive description word
# scores 1.0, one curated keyword scores 2.0.  A score >= 2.0 means
# either "one solid keyword match" or "two non-trivial description
# words" — enough signal to differentiate "Lithium carbonate" from
# "Lithium hydroxide" but conservative enough that "Lithium" alone
# won't pick a random HS row.
_MIN_RESOLUTION_SCORE = 2.0

# Description-word matches weight less than curated keywords.  Keywords
# represent partner intent ("here's exactly what to look for"); raw
# description words might be incidental.
_KEYWORD_WEIGHT = 2.0
_DESCRIPTION_WORD_WEIGHT = 1.0

# Minimum word length to consider from descriptions.  Filters out
# articles / prepositions / units that would otherwise add noise.
# "of" / "by" / "the" / "per" / "mt" / "lb" / "usd" all get dropped.
_MIN_DESC_WORD_LEN = 4

# Tokens to discard even when they're long enough — common
# descriptive boilerplate that matches everything.
_STOPWORDS = frozenset({
    "price", "prices", "average", "annual", "value", "values", "spot",
    "metric", "dollars", "cents", "pound", "tonne", "tonnes", "imports",
    "exports", "report", "reported", "mine", "mines", "real", "fees",
    "freight", "insurance", "cost", "costs", "market", "markets",
    "kilogram", "kilograms", "estimated", "official", "from", "with",
    "into", "this", "that", "year", "data", "type",
})


def _tokenize_description(text: str) -> set[str]:
    """Extract lowercased alpha tokens worth matching against."""
    out = set()
    for word in re.findall(r"[a-z]{%d,}" % _MIN_DESC_WORD_LEN, text.lower()):
        if word in _STOPWORDS:
            continue
        out.add(word)
    return out


def _stem(token: str) -> str:
    """Crude singular-form normaliser for English nouns.

    Plural / singular variants of the same noun were causing
    description tokens like "cathodes" (in
    ``"Cobalt cathodes (refined) — battery current collector input"``)
    to miss USGS descriptors like ``"U.S. spot, cathode"`` because
    substring matching treats them as distinct strings.  This
    de-pluraliser doesn't try to be a real lemmatiser — just strips a
    trailing ``s`` from tokens longer than four characters.  Handles the
    common cases (cathode/cathodes, oxide/oxides, alloy/alloys,
    concentrate/concentrates, ore/ores) without false positives on
    short stopwords or tokens like ``"plus"`` / ``"gas"``.
    """
    if len(token) > 4 and token.endswith("s"):
        return token[:-1]
    return token


def resolve_hs_for_price_descriptor(
    db: Session,
    material_id: int,
    descriptor: str,
    market_scope: str = "global",
) -> tuple[Optional[int], float, Optional[str]]:
    """Match a free-text price descriptor to an HS code mapping.

    Args:
        db:            SQLAlchemy session for querying
                       ``hs_code_material_mappings``.
        material_id:   Scope candidates to a single material — avoids
                       cross-material misattribution (e.g. "London Metal
                       Exchange" appears in Cobalt and Nickel descriptors;
                       scoping by material prevents the Cobalt benchmark
                       from being matched against a Nickel HS row).
        descriptor:    The free-text price benchmark string.  USGS
                       Salient ``Statistics_detail`` values, Pink Sheet
                       column headers, Asian Metal product names, etc.
        market_scope:  Defaults to ``"global"``.  Only mappings tagged
                       with this scope are eligible; partner-curated
                       jurisdictional mappings (US/EU) can be added
                       later by passing a different scope.

    Returns:
        ``(hs_mapping_id, score, matched_via)`` — see module docstring.
        When no candidate clears ``_MIN_RESOLUTION_SCORE``, returns
        ``(None, best_score_seen, None)`` so the caller can log the
        miss for partner review.
    """
    if not descriptor or not descriptor.strip():
        return (None, 0.0, None)

    descriptor_lower = descriptor.lower()
    # Pre-tokenise + stem the descriptor once so we can do set
    # intersection against each candidate's description tokens.
    # Stemming covers the plural/singular gap (e.g. description
    # "cathodes" matches USGS descriptor "cathode") which a raw
    # substring check misses in one direction.
    descriptor_tokens = {_stem(t) for t in _tokenize_description(descriptor_lower)}

    candidates = db.scalars(
        select(HsCodeMaterialMapping).where(
            HsCodeMaterialMapping.material_id == material_id,
            HsCodeMaterialMapping.market_scope == market_scope,
        )
    ).all()
    if not candidates:
        return (None, 0.0, None)

    best_id: Optional[int] = None
    best_score = 0.0
    best_via: Optional[str] = None

    for c in candidates:
        keyword_score = 0.0
        description_score = 0.0

        # Curated-keyword matches — partner intent, weighted higher.
        # Keywords are phrases (e.g. "battery-grade lithium carbonate")
        # so a substring check on the raw descriptor is the right
        # semantic — phrases should match whole, not by token overlap.
        for kw in (c.keywords or []):
            if kw and kw.lower() in descriptor_lower:
                keyword_score += _KEYWORD_WEIGHT

        # Description-word matches — stemmed set intersection so
        # plural/singular variants of the same noun count as one match.
        desc_tokens = {_stem(t) for t in _tokenize_description(c.description or "")}
        common = desc_tokens & descriptor_tokens
        description_score = len(common) * _DESCRIPTION_WORD_WEIGHT

        total = keyword_score + description_score
        if total > best_score:
            best_score = total
            best_id = c.id
            if keyword_score > 0 and description_score > 0:
                best_via = "mixed"
            elif keyword_score > 0:
                best_via = "keywords"
            else:
                best_via = "description"

    if best_score < _MIN_RESOLUTION_SCORE:
        # Surface the candidate-floor miss to the caller so partner can
        # see why the row stayed at material-level.  matched_via stays
        # None so admin queries can distinguish "tried and failed" from
        # "didn't try" (which would be a code-path bug).
        return (None, best_score, None)

    return (best_id, best_score, best_via)


__all__ = ["resolve_hs_for_price_descriptor"]
