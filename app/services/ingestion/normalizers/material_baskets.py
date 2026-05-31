"""Shared material-basket definitions and basket-attribution helpers.

A "basket" is a curated set of canonical material names that share a
downstream context — e.g. the LIB cathode/anode chemistry basket, the
wind-turbine permanent-magnet basket, the PV-cell silicon basket.  Multiple
ingesters need to attribute a single source-text match (a tech category, a
product wording) to a set of materials at once.

Before this module existed, basket definitions were duplicated across:
  * ``iea_policy_tracker.py::_TECH_MINERAL_BASKETS`` — IEA tech-category
    → mineral list (e.g., "battery technologies" → 5 minerals)
  * ``ingest_federal_register.py::_COMMODITY_TITLE_PATTERNS`` — FR title
    pattern → mineral list (e.g., "lithium-ion batteries" → 5 minerals)

Centralising here:
  * Eliminates the silent drift risk where one ingester adds a mineral to
    "battery technologies" and the other doesn't.
  * Makes adding a NEW ingester that needs basket attribution a 1-line
    change (import + call).
  * Documents the semantic intent of each basket in one place so future
    edits have explicit context.

The basket constants are deliberately CANONICAL-NAME LISTS (not
material_id lists).  Resolution to IDs happens at call time via the
``MaterialResolver`` passed by the caller — this keeps the module free of
DB / ORM dependencies and importable in isolation for unit testing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.ingestion.normalizers.material_resolver import MaterialResolver


# ---------------------------------------------------------------------------
# Basket definitions — canonical material names
# ---------------------------------------------------------------------------
# Each basket maps to the downstream context where attributing all listed
# materials at once reflects real supply-chain co-dependence.  Keep these
# focused — over-broad baskets re-introduce the same incidental-attribution
# noise we built the Haiku classifier to remove.

# LIB cathode + anode chemistry — the standard 5 minerals every lithium-ion
# battery touches.  Iron Ore + Phosphate are LFP-specific and live in a
# separate basket below.
BATTERY_BASKET: list[str] = [
    "Lithium",
    "Cobalt",
    "Nickel",
    "Manganese",
    "Natural Graphite",
]

# LFP variant — LiFePO4 cathode chemistry adds iron ore + phosphate.  Use
# this when the source text explicitly names LFP / lithium-iron-phosphate
# chemistry rather than blanket "lithium-ion".
EV_BATTERY_BASKET_LFP: list[str] = BATTERY_BASKET + [
    "Phosphate (Battery Grade)",
    "Iron Ore (LFP Grade)",
]

# Cathode-only — when the source names the cathode active material (CAM)
# specifically rather than the whole cell.  Graphite (anode) is excluded.
CATHODE_BASKET: list[str] = [
    "Lithium",
    "Cobalt",
    "Nickel",
    "Manganese",
]

# Anode-only — when the source names the anode material.  Silicon entries
# here are battery-anode-grade specifically.
ANODE_BASKET: list[str] = [
    "Natural Graphite",
    "Silicon (Anode Grade)",
]

# Pre-Mn LIB chemistry — most pre-2018 lithium-ion battery formulations
# (NCA, early NCM, LCO) used Li-Co-Ni cathodes with graphite anodes but
# without Mn.  Used when the source explicitly references battery recycling
# or older LIB generations.  Mn was added to mainstream LIB chemistries
# (NCM, NCA-with-Mn) around 2018 but recycling streams skew toward older
# packs.
BATTERY_BASKET_NO_MN: list[str] = [
    "Lithium",
    "Cobalt",
    "Nickel",
    "Natural Graphite",
]

# Wind turbine — REE for permanent magnets, Copper for windings/transmission.
# Iron ore optional; some IEA categories include it for tower steel — we
# leave it out by default and let the LFP variant include iron explicitly
# if needed.
WIND_BASKET: list[str] = [
    "Rare Earth Elements",
    "Copper",
]

# Wind-offshore — same as wind for materials currently; placeholder for
# future divergence (e.g., specialised PGM-bearing alloys for offshore).
WIND_OFFSHORE_BASKET: list[str] = WIND_BASKET

# Solar PV — crystalline-silicon base (Silicon) plus thin-film
# semiconductor materials (Gallium for CIGS / GaAs cells).
SOLAR_BASKET: list[str] = [
    "Silicon (Anode Grade)",
    "Gallium",
]

# Fuel cells — PGM catalysts (mainly Pt) are the primary critical input.
FUEL_CELL_BASKET: list[str] = [
    "Platinum-Group Metals",
]

# Hydrogen electrolysis — PGM catalysts (Pt for PEM, Ni for alkaline).
HYDROGEN_BASKET: list[str] = [
    "Platinum-Group Metals",
    "Nickel",
]

# Energy storage (non-LIB) — vanadium-flow batteries are the main non-LIB
# grid-scale storage chemistry.  Lithium included because some IEA
# "energy storage" categories still mean LIB-stationary.
ENERGY_STORAGE_BASKET: list[str] = [
    "Lithium",
    "Vanadium",
]

# Permanent magnets — neodymium/dysprosium magnets are the dominant high-
# performance type; samarium-cobalt magnets are the secondary variant.
# Used by both wind and EV-motor contexts.
PERMANENT_MAGNET_BASKET: list[str] = [
    "Rare Earth Elements",
    "Cobalt",
]


# ---------------------------------------------------------------------------
# Resolution helper — basket canonical names → material IDs
# ---------------------------------------------------------------------------

def resolve_basket_to_ids(
    canonical_names: list[str],
    mat_resolver: "MaterialResolver",
) -> list[int]:
    """Resolve a basket (list of canonical material names) to material IDs.

    Materials that don't exist in the materials table (e.g., "Silicon (Anode
    Grade)" before the silicon material is added) are silently skipped —
    cheaper than raising for the caller, and matches the existing pattern
    in IEA / FR ingesters.

    Deduplicates: a single material id appears at most once in the output
    even if the basket lists the same name twice or two names resolve to
    the same material.

    Order is preserved — first-occurrence wins.
    """
    seen: set[int] = set()
    ids: list[int] = []
    for name in canonical_names:
        material = mat_resolver.resolve_by_canonical_name(name)
        if material is None or material.id in seen:
            continue
        seen.add(material.id)
        ids.append(material.id)
    return ids


def resolve_basket_to_attributions(
    canonical_names: list[str],
    mat_resolver: "MaterialResolver",
    *,
    relevance: float,
    match_reason_prefix: str,
    matched_text: str = "",
) -> list[tuple[int, float, str, int | None]]:
    """Resolve a basket and shape the output as (id, relevance, reason, None)
    tuples — the same shape ``MaterialCache.detect`` produces so callers
    can merge basket attributions into the same downstream junction-write
    code path.

    Args:
        canonical_names:      The basket itself.
        mat_resolver:         For canonical-name → material_id lookup.
        relevance:            Fixed relevance score for every attribution in
                              this basket.  Callers typically use 0.40 for
                              category-inference baskets (IEA tech basket,
                              FR commodity-title basket) and 0.85 for
                              direct-mention baskets (rare).
        match_reason_prefix:  Prefix for the match_reason field, e.g.
                              ``"technology_basket"`` for IEA or
                              ``"title_basket"`` for FR.  The full reason
                              becomes ``"{prefix}:{matched_text}"`` when
                              ``matched_text`` is provided, or just
                              ``prefix`` when empty.
        matched_text:         Optional source-text excerpt (≤32 chars) that
                              triggered the basket.  Helps with auditing
                              attributions later.

    Returns:
        List of ``(material_id, relevance, match_reason, hs_mapping_id)``
        tuples.  ``hs_mapping_id`` is always ``None`` — basket attribution
        is stage-ambiguous.

    Example:
        >>> resolve_basket_to_attributions(
        ...     BATTERY_BASKET, mat_resolver,
        ...     relevance=0.40,
        ...     match_reason_prefix="technology_basket",
        ...     matched_text="battery technologies",
        ... )
        [(296, 0.40, "technology_basket:battery technologies", None),
         (289, 0.40, "technology_basket:battery technologies", None),
         ...]
    """
    suffix = f":{matched_text[:32]}" if matched_text else ""
    reason = f"{match_reason_prefix}{suffix}"

    ids = resolve_basket_to_ids(canonical_names, mat_resolver)
    return [(mid, relevance, reason, None) for mid in ids]


__all__ = [
    "BATTERY_BASKET",
    "BATTERY_BASKET_NO_MN",
    "EV_BATTERY_BASKET_LFP",
    "CATHODE_BASKET",
    "ANODE_BASKET",
    "WIND_BASKET",
    "WIND_OFFSHORE_BASKET",
    "SOLAR_BASKET",
    "FUEL_CELL_BASKET",
    "HYDROGEN_BASKET",
    "ENERGY_STORAGE_BASKET",
    "PERMANENT_MAGNET_BASKET",
    "resolve_basket_to_ids",
    "resolve_basket_to_attributions",
]
