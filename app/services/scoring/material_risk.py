"""Material Concentration Risk component scoring (v2).

Sub-weights: criticality 35%, concentration 35%, trade_volatility 30%.
Concentration raised to equal criticality — a highly concentrated supply of a
critical material is as dangerous as the material's intrinsic criticality.

2026-06 — JRC-aligned HHI cliff mapping
=======================================
The original linear HHI → risk mapping produced systematically compressed
scores: Cobalt (DRC 75% → HHI ≈ 0.58) contributed only 0.45 × 0.58 ≈ 0.26
to the concentration sub-input, then through the 0.35 pillar weight and
the 100x scaling that's ~9 points of the final pillar score.  Same math
gave Lithium (well-diversified, HHI ≈ 0.20) ≈ 3 points.  Eight-point
differentiation is far too narrow given the actual structural difference
between these two materials.

Validation: Spearman ρ between our overall risk score and the published
USGS criticality_score across 39 materials was -0.16 (slightly inverse).
Rare earths (Tb, Dy, Nd) sat in our bottom 5 despite topping every public
critical-minerals list.

``hhi_concentration_risk`` below replaces the linear mapping with a
piecewise-linear curve aligned with the DOJ Horizontal Merger Guidelines
HHI tiers (used by FTC/DOJ for antitrust analysis):

    < 0.15: unconcentrated (mapped to 0.0 → 0.40)
    0.15-0.25: moderately concentrated (0.40 → 0.65)
    0.25-0.40: highly concentrated (0.65 → 0.85)
    0.40-0.55: very highly concentrated (0.85 → 0.95)
    > 0.55: extreme / single-supplier dominance (0.95 → 1.0)

Same input — Cobalt's 0.58 — now maps to ~0.97 instead of 0.58, restoring
the dynamic range the rest of the math expected.

References for the methodology page:
- US DOJ/FTC Horizontal Merger Guidelines §5.3 (HHI tiers)
- EU JRC, Methodology for Establishing the EU List of Critical Raw
  Materials (Blengini et al., 2017) — supply concentration component
- USGS, Methodology for the 2022 Critical Minerals List (NSTC)
"""

from __future__ import annotations


def hhi_concentration_risk(hhi: float) -> float:
    """Map raw Herfindahl-Hirschman Index [0, 1] to concentration risk [0, 1].

    Piecewise linear with breakpoints at the DOJ Horizontal Merger Guidelines
    HHI thresholds (0.15 / 0.25 / 0.40 / 0.55).  The breakpoints in raw HHI
    correspond roughly to "top single producer share" of 39% / 50% / 63% / 74%
    in a typical critical-minerals supply distribution — i.e. the cliffs land
    where industry analysts already draw qualitative tiers ("moderate" vs.
    "high" vs. "extreme" concentration).

    Tests of correspondence at the breakpoints:
        hhi = 0.00 → risk = 0.00 (theoretical perfectly fragmented market)
        hhi = 0.15 → risk = 0.40 (DOJ "unconcentrated" upper bound)
        hhi = 0.25 → risk = 0.65 (DOJ "moderate" upper bound)
        hhi = 0.40 → risk = 0.85 (DOJ "highly concentrated")
        hhi = 0.55 → risk = 0.95 (very-high / dominant single supplier)
        hhi = 0.75 → risk ≈ 0.97 (monopolistic territory)
        hhi = 1.00 → risk = 1.00 (theoretical pure monopoly)

    Why piecewise linear and not a smooth power curve: transparency.  Analysts
    and customers can read the mapping off a table; smooth power exponents
    invite "why this curve?" questions that don't have a clean answer.  The
    DOJ HHI tiers are public, well-known, and defensible.

    Args:
        hhi: Raw HHI in [0, 1] (i.e. ``Σ share_i²`` where shares are
             0-1 fractions).  Negative inputs are clamped to 0.

    Returns:
        Concentration risk on [0, 1].
    """
    if hhi <= 0.0:
        return 0.0
    if hhi <= 0.15:
        # Unconcentrated band: linear 0 → 0.40.
        return (hhi / 0.15) * 0.40
    if hhi <= 0.25:
        # Moderate band: linear 0.40 → 0.65.
        return 0.40 + ((hhi - 0.15) / 0.10) * 0.25
    if hhi <= 0.40:
        # Highly concentrated band: linear 0.65 → 0.85.
        return 0.65 + ((hhi - 0.25) / 0.15) * 0.20
    if hhi <= 0.55:
        # Very highly concentrated band: linear 0.85 → 0.95.
        return 0.85 + ((hhi - 0.40) / 0.15) * 0.10
    # Extreme / monopolistic band: linear 0.95 → 1.0 over the remaining
    # range, with a cap at 1.0 so HHI > 1 (impossible but defensive)
    # doesn't blow past the upper bound.
    return min(1.0, 0.95 + ((hhi - 0.55) / 0.45) * 0.05)


def score_material_exposure(
    criticality: float,       # 0-1.0: inherent supply criticality of the material
    concentration: float,     # 0-1.0: geographic/supplier concentration of supply
    trade_volatility: float,  # 0-1.0: price/flow volatility signal
) -> float:
    """
    Material Concentration Risk component score (0-100).

    Args:
        criticality:      Inherent supply criticality of the material on [0, 1].
        concentration:    Geographic/supplier concentration of supply on [0, 1].
        trade_volatility: Price/flow volatility signal on [0, 1].

    Returns:
        Component score on [0, 100].
    """
    return min(100.0, (0.35 * criticality + 0.35 * concentration + 0.30 * trade_volatility) * 100)

# NOTE: apply_material_hhi_lift (the Step-2B material-HHI lift) was
# removed in V1/4.x — the max-across-stages concentration pillar makes
# the lift structurally unnecessary (spec §8). score_material_exposure
# and hhi_concentration_risk above are retained (still used).
