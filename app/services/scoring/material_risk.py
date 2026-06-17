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


# ---------------------------------------------------------------------------
# Step 2B (2026-06) — Material-level HHI lift for the stage-rollup path
# ---------------------------------------------------------------------------
# Background
# ----------
# When market_aggregator falls into the stage-weighted Level-0 rollup, the
# aggregated ``mat_score`` depends on the per-node ``score_method`` mix.
# Coverage measurement (2026-06-15) showed that for the launch-10 minerals
# the dominant per-node method is ``event_only_no_hhi`` — i.e. the rollup
# is built from nodes that have no HHI signal of their own, only tariff /
# export-restriction event signals.  That means the structural country-
# concentration story (DRC for cobalt, China for graphite, etc.) is
# invisible to the score even though it's clearly real.
#
# Step 2A (MCS same-stage propagation) helps but doesn't close the gap —
# MCS publishes shares almost exclusively at the mine stage, so refined /
# battery-grade nodes still fall to event-only scoring.
#
# This helper computes a *blended* concentration score that mixes the
# stage-rollup result with the legacy material-level estimate
# ``score_material_exposure(crit, conc, trade_vol)``.  The blend weight is
# the fraction of nodes that DID have HHI signal: when that fraction is
# high (data-rich case), the stage rollup dominates; when low (event-only
# dominates), the material-level estimate provides the structural floor.
#
# The result is *floored* — we never lower the stage rollup result even
# if the material estimate is lower.  Otherwise a flurry of recent tariff
# events on a low-concentration material would be wiped out by the floor.
# ---------------------------------------------------------------------------

# Fraction-of-nodes-with-HHI-signal at or above this and no lift is
# applied.  Tuned conservatively: 80% means the rollup is built from
# overwhelmingly HHI-anchored nodes and the material-level estimate is
# more likely to be the cruder signal.  Below 80% the lift kicks in and
# scales linearly with the actual coverage fraction.
_MATERIAL_HHI_LIFT_NO_OP_THRESHOLD: float = 0.80

# score_method labels (from hs_node_scorer.py) that count as "has HHI"
# for the purpose of the blend.  Anything not in this set is treated as
# event-/operational-only signal.
_HHI_BEARING_SCORE_METHODS: frozenset[str] = frozenset({
    "hhi_anchored",
    "hhi_anchored_with_operational",
})


def apply_material_hhi_lift(
    *,
    stage_rollup_score: float,
    material_floor_score: float,
    score_method_breakdown: dict[str, int],
    has_material_hhi_signal: bool,
) -> tuple[float, dict]:
    """Blend the stage-rollup concentration score with a material-level floor.

    Args:
        stage_rollup_score: 0-100 result from the stage-weighted rollup.
        material_floor_score: 0-100 result from ``score_material_exposure``
            using the same (criticality, concentration, trade_volatility)
            sub-inputs.  Caller is responsible for computing this — keeps
            the helper pure.
        score_method_breakdown: histogram of per-node ``score_method``
            labels across the nodes that contributed to the rollup.
            Same dict that lands in ``rationale_json.sub_inputs.material
            .node_score_method_breakdown``.
        has_material_hhi_signal: True iff the material has a usable
            ``MaterialCriticalitySignal.hhi_score``.  Required because
            without it the floor is essentially noise — we'd be lifting
            toward an estimate that has no real concentration content.

    Returns:
        ``(lifted_score, diagnostic_dict)``.  ``diagnostic_dict`` is
        always populated so callers can store it in rationale_json
        regardless of whether the lift actually fired.
    """
    total_nodes = sum(score_method_breakdown.values()) if score_method_breakdown else 0
    hhi_nodes = sum(
        score_method_breakdown.get(m, 0) for m in _HHI_BEARING_SCORE_METHODS
    )
    hhi_node_fraction = (hhi_nodes / total_nodes) if total_nodes > 0 else 0.0

    diag: dict = {
        "applied":              False,
        "reason":               None,
        "hhi_node_count":       hhi_nodes,
        "total_eligible_nodes": total_nodes,
        "hhi_node_fraction":    round(hhi_node_fraction, 4),
        "stage_rollup_score":   round(stage_rollup_score, 3),
        "material_floor_score": round(material_floor_score, 3),
        "blend_alpha":          None,
        "lifted_score":         round(stage_rollup_score, 3),
    }

    if not has_material_hhi_signal:
        diag["reason"] = "no_material_hhi_signal"
        return stage_rollup_score, diag

    if total_nodes == 0:
        diag["reason"] = "no_contributing_nodes"
        return stage_rollup_score, diag

    if hhi_node_fraction >= _MATERIAL_HHI_LIFT_NO_OP_THRESHOLD:
        diag["reason"] = "rollup_is_hhi_dominated"
        return stage_rollup_score, diag

    # Blend: high HHI-node fraction → stage rollup dominates.
    # Low HHI-node fraction → material floor dominates.
    blend_alpha = hhi_node_fraction
    blended = blend_alpha * stage_rollup_score + (1.0 - blend_alpha) * material_floor_score
    # Floor: never lower the stage rollup result.  If the material floor
    # is lower than the rollup (e.g. recent events pushed it up legitimately),
    # the max() keeps the higher signal.
    lifted_score = max(stage_rollup_score, blended)

    diag.update({
        "applied":      True,
        "reason":       "low_hhi_coverage_lifted_to_material_floor",
        "blend_alpha":  round(blend_alpha, 4),
        "lifted_score": round(lifted_score, 3),
    })
    return lifted_score, diag
