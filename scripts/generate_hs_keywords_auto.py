"""Regenerate ``app/services/ingestion/seed_hs_keywords_auto.py``.

Run this whenever the partner-curated "Comprehensive HS_HTS codes" CSV
gets refreshed.  The generated file is checked into the repo and
consumed at seed time by ``seed_hs_mappings._merged_keywords_for()``.

Usage
-----
::

    python scripts/generate_hs_keywords_auto.py \\
        --csv-path  data/curation/comprehensive_hs_hts_codes.csv \\
        --out-path  app/services/ingestion/seed_hs_keywords_auto.py

Defaults to the canonical paths above when the args are omitted.
After running, review the diff and run ``seed-hs-mappings --force``
to push the refreshed keyword arrays to the DB.

What this generates (Tier 2-4 of the keyword pipeline)
------------------------------------------------------

For every (hs_prefix, canonical_name) pair in
``seed_hs_mappings._MAPPINGS`` with ``market_scope='global'``:

  Tier 2  Canonical material name (lowercased) + symbol/code (when ≥2 chars)
  Tier 3  Supply-chain stage label + stage-generic tags
          (e.g. for stage='ore': "ore", "concentrate", "concentrates")
  Tier 4  Tokens extracted from the partner CSV's Description column
          for any HTS Code matching this prefix.  Split on em-dashes,
          parens, slashes, semicolons; lowercased; deduped; noise
          tokens (≤2 chars, "the", "and", etc.) dropped.

Tier 1 (hand-curated ``_HS_KEYWORDS_BY_MAPPING``) is NOT regenerated
— it stays in ``seed_hs_mappings.py`` and is merged with the output
of this script at seed time, with curated entries appearing first
in each keyword array (lookup-priority winner).

Determinism
-----------
Output is sorted by (prefix, canonical) and keywords are emitted in
the order they were first seen, so the generated file diffs cleanly
across runs against the same input.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

# Ensure we can import from app/ when running this script directly
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.services.ingestion.seed_hs_mappings import _MAPPINGS  # noqa: E402
from app.services.ingestion.seed_materials import _MATERIALS  # noqa: E402


# ---------------------------------------------------------------------------
# Canonical default paths
# ---------------------------------------------------------------------------

_DEFAULT_CSV = _REPO_ROOT / "data" / "curation" / "comprehensive_hs_hts_codes.csv"
_DEFAULT_OUT = _REPO_ROOT / "app" / "services" / "ingestion" / "seed_hs_keywords_auto.py"


# ---------------------------------------------------------------------------
# Tier 3 — stage-specific generic tag terms.
# Add to these only when a new supply-chain stage is introduced.
# ---------------------------------------------------------------------------

# Stage-specific tag terms — INTENTIONALLY only multi-word phrases.
#
# The previous version included bare singletons like "ore", "metal",
# "scrap", "concentrate" as keywords on every material at the matching
# stage.  At runtime that meant an article saying "ore" once tagged
# ~60 materials.  Word-boundary matching (now enforced in
# MaterialCache.detect) doesn't fix that — the right answer is to
# never seed bare generic tokens in the first place.
#
# Multi-word combinations like "ore and concentrates" survive because
# they're inherently more specific and rarely appear in unrelated
# contexts.  Inverse-frequency weighting still down-scores them when
# they appear in many materials' arrays, but the floor of relevance is
# higher than a bare word would deserve.
#
# When adding a new stage, ONLY include phrases of 2+ words OR
# distinctive single tokens (e.g. "ferrochromium" is fine —
# unambiguous; "ferro" alone is not).
_STAGE_KEYWORDS: dict[str, list[str]] = {
    "ore":           ["ore and concentrates"],
    "concentrate":   [],  # subsumed by 'ore' multi-word; nothing distinctive single-word
    "intermediate":  [],
    "refined":       [],
    "battery_grade": ["battery grade", "battery-grade", "high purity"],
    "fabricated":    [],
    "scrap":         ["waste and scrap"],
}

# Tokens to drop from CSV-derived description tokenisation.  Includes
# stop-words AND bare generic stage words — same rationale as
# _STAGE_KEYWORDS above.  When a description splits into "Aluminum,
# not alloyed – Unwrought (in coils)", we want "aluminum, not alloyed"
# and "in coils" but NOT bare "unwrought" (matches every refined
# material).
_NOISE_TOKENS = {
    # Stop words / connectives
    "the", "and", "or", "of", "for", "in", "with", "from", "by", "all",
    # Bare generic stage tokens (same reason _STAGE_KEYWORDS dropped them)
    "ore", "ores", "concentrate", "concentrates",
    "metal", "metals", "ingot", "unwrought", "wrought", "refined",
    "scrap", "waste", "recycled",
    "fabricated", "articles", "wire", "rod", "powder",
    "intermediate", "precursor", "compound",
    "high", "purity", "high purity",  # 'high purity' alone too generic without material
    # USGS phrasing fragments that aren't material-specific on their own
    "other", "primary", "secondary", "natural", "synthetic",
}


# ---------------------------------------------------------------------------
# Description tokenisation
# ---------------------------------------------------------------------------

def _clean(token: str) -> str | None:
    """Normalise a description-derived token; return None to drop it."""
    t = token.strip().strip(",.;:").lower()
    if len(t) < 3 or t in _NOISE_TOKENS:
        return None
    if not any(c.isalnum() for c in t):
        return None
    return t


def _tokenize_description(desc: str) -> list[str]:
    """Pull useful keyword tokens out of a description.

    Splits on em-dashes, parens, semicolons, slashes; lowercases; drops
    very short / noise tokens; preserves multi-word forms.  Parenthesised
    content is extracted as separate tokens (so "Aluminum (in coils)"
    yields ``["aluminum", "in coils"]``).
    """
    s = desc.lower()
    for ch in "–—-/;":
        s = s.replace(ch, "\n")
    paren_parts = re.findall(r"\(([^)]+)\)", s)
    s = re.sub(r"\([^)]*\)", "\n", s)
    parts = [_clean(p) for p in s.split("\n")]
    parts.extend(_clean(p) for p in paren_parts)

    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate(csv_path: Path) -> dict[tuple[str, str], list[str]]:
    """Build the {(prefix, canonical) -> keyword list} dict from the CSV."""
    if not csv_path.exists():
        raise FileNotFoundError(
            f"Partner CSV not found at {csv_path}.  "
            f"Pass --csv-path to point at the actual file."
        )

    mat_by_name = {m["canonical_name"]: m for m in _MATERIALS}

    # Aggregate descriptions per (prefix, canonical) — match both 4-digit
    # and 6-digit prefixes against the HTS Code column.
    desc_by_pair: dict[tuple[str, str], list[str]] = defaultdict(list)
    with csv_path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            canonical = (r.get("canonical_name") or "").strip()
            if not canonical:
                continue
            code = (r.get("HTS Code") or "").replace(".", "")
            desc = (r.get("Description") or "").strip()
            if not desc:
                continue
            if len(code) >= 4:
                desc_by_pair[(code[:4], canonical)].append(desc)
            if len(code) >= 6:
                desc_by_pair[(code[:6], canonical)].append(desc)

    merged: dict[tuple[str, str], list[str]] = {}
    for prefix, canonical, _desc, _conf, stage, _digits, scope in _MAPPINGS:
        if scope != "global":
            continue
        key = (prefix, canonical)
        keywords: list[str] = []
        seen: set[str] = set()

        def _add(k: str | None) -> None:
            kl = (k or "").strip().lower()
            if kl and kl not in seen:
                seen.add(kl)
                keywords.append(kl)

        # Tier 2 — canonical name + symbol/code (when ≥2 chars to skip noise).
        _add(canonical)
        sym = (mat_by_name.get(canonical, {}).get("symbol_or_code") or "")
        if len(sym) >= 2:
            _add(sym)

        # Tier 3 — stage-specific multi-word phrases ONLY.  We deliberately
        # do NOT add the bare stage label as a keyword: stage names like
        # "ore", "refined", "intermediate", "scrap" are too generic on
        # their own (would tag every material with that stage when an
        # article merely uses the word once).  Multi-word phrases live in
        # ``_STAGE_KEYWORDS`` and stay specific enough to be useful.
        if stage:
            for sk in _STAGE_KEYWORDS.get(stage, []):
                _add(sk)

        # Tier 4 — partner CSV description tokens.  ``_tokenize_description``
        # already drops bare generic stage tokens via ``_NOISE_TOKENS``.
        for desc_str in desc_by_pair.get(key, []):
            for token in _tokenize_description(desc_str):
                _add(token)

        merged[key] = keywords

    return merged


def write_output(merged: dict[tuple[str, str], list[str]], out_path: Path) -> None:
    """Write the generated dict to ``out_path`` as a Python literal."""
    header = '''"""Auto-derived HS keyword aliases.

GENERATED FILE — do not hand-edit.  Regenerate via:

    python scripts/generate_hs_keywords_auto.py

Source layers (deterministic, in order — same as the seed-time merge):

  Tier 2  Canonical material name + symbol/code (when ≥2 chars)
  Tier 3  Supply-chain stage label + stage-specific generic terms
          (ore / concentrate / refined / metal / scrap / etc.)
  Tier 4  Tokens extracted from the partner-curated
          "Comprehensive HS_HTS codes" CSV's Description column,
          split on punctuation (em-dashes, parens, semicolons),
          lowercased, deduped.

These are MERGED with the hand-curated ``_HS_KEYWORDS_BY_MAPPING`` from
``seed_hs_mappings.py`` (Tier 1) at seed time — hand-curated keywords
appear first in each array so they win on lookup priority.

Why split into a separate file: the merged dict is ~284 entries × ~8
keywords each.  Inline in seed_hs_mappings.py it would balloon that
file by ~2,800 lines and obscure the partner-curated content.  Living
here lets the seed file stay readable while the auto-derived breadth
sits in one greppable place.

Confidence note (text-matching event attribution):
  Some keywords appear in many entries ("metal" → 73, "ore" → 59,
  "battery grade" → 36).  These low-specificity tokens are useful for
  stage-rolling but get down-weighted by inverse-frequency scoring
  inside ``normalizers.material_resolver.MaterialCache.build()``:

      relevance(kw) = 0.90 / sqrt(N_mappings_containing_kw)

  Result: ``"lithium hydroxide monohydrate"`` (in 1 mapping) keeps
  full 0.90 relevance; ``"metal"`` (in 73) drops to ~0.105.  Square
  root softens the decay so generic tokens stay informative when
  they're the only signal.
"""

from __future__ import annotations


_HS_KEYWORDS_AUTO: dict[tuple[str, str], list[str]] = {
'''

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        f.write(header)
        for (prefix, canonical), keywords in sorted(merged.items()):
            f.write(f'    ("{prefix}", "{canonical}"): [\n')
            for kw in keywords:
                f.write(f'        {kw!r},\n')
            f.write("    ],\n")
        f.write("}\n")


def _summary(merged: dict[tuple[str, str], list[str]]) -> str:
    sizes = [len(v) for v in merged.values()]
    if not sizes:
        return "  (no entries generated — check the CSV path)"
    sizes.sort()
    median = sizes[len(sizes) // 2]
    return (
        f"  entries:                {len(merged)}\n"
        f"  total keyword strings:  {sum(sizes)}\n"
        f"  per-entry min/med/max:  {sizes[0]} / {median} / {sizes[-1]}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else "",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--csv-path",
        type=Path,
        default=_DEFAULT_CSV,
        help=f"Partner CSV path (default: {_DEFAULT_CSV})",
    )
    parser.add_argument(
        "--out-path",
        type=Path,
        default=_DEFAULT_OUT,
        help=f"Output Python file (default: {_DEFAULT_OUT})",
    )
    args = parser.parse_args(argv)

    print(f"Reading {args.csv_path}")
    merged = generate(args.csv_path)
    write_output(merged, args.out_path)
    print(f"Wrote {args.out_path}")
    print(_summary(merged))
    print()
    print("Next steps:")
    print(f"  1. Review the diff:  git diff -- {args.out_path}")
    print( "  2. Push to the DB:   bdi-ingest seed-hs-mappings --force")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
