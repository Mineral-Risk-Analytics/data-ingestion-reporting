"""Supply Chain Propagation Risk component scoring (v3 — sixth pillar).

Captures *second-party* risk: the rollup of *other companies'* persisted
``CompanyScore.overall_risk_score`` reachable via
``CompanySupplyRelationship``. This makes "Tesla's score moved because CATL
got hit" explicit, additive, and tunable.

Pure function — no DB. The orchestrator owns the supplier-chain BFS and the
``CompanyScore`` lookup; this module only does the math.

Tier-2 propagation uses a depth weight that decays from 1.00 (direct, tier-1)
to 0.40 (tier-2) by default, so a problem two hops upstream contributes
roughly half as much as the same problem at a direct supplier. Volume share
is multiplied in after depth weight so a 5%-volume tier-1 supplier and a
40%-volume tier-2 supplier with the same risk land on similar contributions
(the design intent — these *are* roughly comparable risk vectors).
"""

from __future__ import annotations

DEFAULT_DEPTH_WEIGHTS: tuple[float, ...] = (1.00, 0.40)
# Index 0 = tier-1 weight, index 1 = tier-2 weight, ... Tiers beyond the tuple
# length get the smallest weight (tail propagation is conservative).


def _depth_weight(depth: int, depth_weights: tuple[float, ...]) -> float:
    """Look up weight for ``depth`` (1-indexed); deeper tiers reuse the tail."""
    idx = max(0, depth - 1)
    if idx >= len(depth_weights):
        return depth_weights[-1] if depth_weights else 0.0
    return depth_weights[idx]


def score_propagation(
    supplier_contributions: list[tuple[float, float, int]],
    depth_weights: tuple[float, ...] = DEFAULT_DEPTH_WEIGHTS,
) -> float:
    """Volume-weighted, depth-decayed average of supplier overall scores.

    Args:
        supplier_contributions: Each tuple is
            ``(supplier_overall_score 0-100, edge_volume_share 0-1, depth)``.
            ``depth`` is 1-indexed (1 = direct supplier).
        depth_weights: Per-tier multipliers; defaults to
            :data:`DEFAULT_DEPTH_WEIGHTS` (tier-1 = 1.00, tier-2 = 0.40).

    Returns:
        Component score on [0, 100]. ``0.0`` when the input list is empty (the
        orchestrator decides whether to set the pillar to ``None`` rather than
        feeding an empty list).
    """
    if not supplier_contributions:
        return 0.0

    weighted_sum = 0.0
    weight_sum = 0.0
    for supplier_score, volume_share, depth in supplier_contributions:
        share = max(0.0, min(1.0, float(volume_share)))
        d_weight = _depth_weight(depth, depth_weights)
        combined = share * d_weight
        if combined <= 0:
            continue
        weighted_sum += float(supplier_score) * combined
        weight_sum += combined

    if weight_sum <= 0:
        return 0.0
    return min(100.0, max(0.0, weighted_sum / weight_sum))
