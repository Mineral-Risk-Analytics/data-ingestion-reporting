"""Launch-list materials — the 10 minerals the product focuses on for v1.

This is a hardcoded constant for the dev / analyst phase.  Once partner
review settles on the final list, promote to a ``materials.is_launch_list``
boolean column via migration and have callers read from the DB instead of
this module.  Until then, every page / endpoint that needs "the core set
of minerals" should import :data:`LAUNCH_LIST_CANONICAL_NAMES` from here so
there's exactly one source of truth.

The list mirrors:
  - The partner-curated battery supply chain seed (Li, Co, Ni, Graphite,
    Phosphate, Cu, REE — the original seven)
  - Plus three recommended adds from the 2026-05-09 audit scoping
    conversation (Mn, Al, W)

Phosphate is currently a *launch-blocker* — zero facilities seeded; the
G4c partner template is being filled in.  Other launch-list minerals
have at least sparse coverage today.

If you change this list, also revisit:
  - docs/scoring-audit-2026-05-addendum.md (launch-list references)
  - frontend dashboard KPI strip (core_minerals_scored)
  - any coverage-matrix endpoints once those land
"""

from __future__ import annotations


# Canonical names match Material.canonical_name exactly — joins on this
# string work without conversion.  Order is presentation-friendly:
# partner-tier-1 first (in cathode-stack importance order), then the
# anode + structural + recommended adds.
LAUNCH_LIST_CANONICAL_NAMES: tuple[str, ...] = (
    "Lithium",
    "Cobalt",
    "Nickel",
    "Manganese",
    "Natural Graphite",
    "Phosphate",
    "Iron Ore",
    "Copper",
    "Aluminum",
    "Rare Earth Elements",
)


# Convenience: lowercase set for case-insensitive membership tests.
LAUNCH_LIST_CANONICAL_NAMES_LOWER: frozenset[str] = frozenset(
    name.lower() for name in LAUNCH_LIST_CANONICAL_NAMES
)


def is_launch_list_material(canonical_name: str | None) -> bool:
    """Return True if ``canonical_name`` is in the launch-list set."""
    if not canonical_name:
        return False
    return canonical_name.lower() in LAUNCH_LIST_CANONICAL_NAMES_LOWER
