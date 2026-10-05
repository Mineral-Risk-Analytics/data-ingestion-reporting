"""Regulatory & Compliance Risk component scoring (v3 — DB-driven obligations).

Two-part score:
  1. Event-driven rollup — top-3 computed event_impact values, scaled to 0-60.
  2. Obligation uplift — hard legal obligations add additive points, SOFT-CAPPED
     to the 0-40 range via a saturating curve (4.3, 2026-07-26):

         uplift = 40 × (1 − e^(−raw / 35))

     The old hard min(40, raw) pinned every high-obligation combination to
     exactly 40 (REE×CN raw 101.9 and raw 68 both scored 40), erasing the
     differentiation the workbook curation exists to provide. The curve
     preserves ordering at any raw value (26→21, 68→34, 102→38), never
     reaches 40, and keeps scaling as regulations accumulate.
     These are not regular policy events; they carry enforcement deadlines and are
     scored separately to avoid dilution.

Obligation uplift is weighted by compliance_status severity (company path):
  non_compliant × 1.00  (confirmed violation — full statutory risk)
  unknown       × 0.50  (unassessed — conservative half-weight)
  partial       × 0.40  (in transition — meaningful but not full exposure)

For market-level scoring (no company), the weight comes from the regulation's
geography_compliance_weights JSONB column (resolved by _resolve_compliance_weight
in market_aggregator.py) rather than a compliance_status lookup.

Build 2 (2026-07-24, migration 063): obligation base points moved from the
hardcoded ``COMPLIANCE_OBLIGATIONS`` dict into ``regulations.obligation_points``
(+ ``is_obligation``), so curation can add a new landmark regulation's
obligation weight without a code change. Callers pass ``obligation_points``
(regulation_key → base points, from the DB); a key missing from the mapping
contributes 0 — exact parity with the old ``dict.get(key, 0)``. The original
tier calibration (UFLPA 25 … EU_CONFLICT_MINERALS 3; SEC_CLIMATE_2024
deliberately point-less while court-stayed) lives in migration 063 and
``seed_regulations.py``.
"""

from __future__ import annotations

import math
from typing import Mapping, Optional

# Soft-cap parameters (4.3): asymptote and e-folding scale of the obligation
# uplift saturation curve. K=35 puts the old single-heavyweight case (UFLPA
# alone, raw 25) at ~20 and a stacked-regime case (raw ~100) at ~38.
OBLIGATION_UPLIFT_MAX = 40.0
OBLIGATION_SOFTCAP_SCALE = 35.0


def score_regulatory_profile(
    top_event_impacts: list[float],
    active_obligations: list[tuple[str, float]],
    policy_proximity_adjustment: float = 1.0,
    *,
    obligation_points: Optional[Mapping[str, float]] = None,
) -> float:
    """
    Regulatory & Compliance Risk component score (0-100).

    Args:
        top_event_impacts:          Pre-computed event_impact values (from event_impact.py).
                                    May be any length; only the top-3 are used.
        active_obligations:         List of (regulation_key, weight_multiplier) tuples,
                                    e.g. [("UFLPA", 1.0), ("IRA_DOMESTIC", 0.40)].
                                    Weight reflects compliance_status severity (company
                                    path) or the resolved geography weight (market path).
        policy_proximity_adjustment: Scalar applied to the event-driven portion to
                                    account for geographic or sectoral proximity.
                                    Defaults to 1.0 (no adjustment).
        obligation_points:          regulation_key → obligation base points, read from
                                    ``regulations.obligation_points`` by the caller
                                    (Build 2). Missing key ⇒ 0 points. ``None`` ⇒ no
                                    uplift at all (data-honest: no points known).

    Returns:
        Component score on [0, 100].
    """
    # Event-driven rollup: average the top-3 impacts, scale to 0-60 range
    top_3 = sorted(top_event_impacts, reverse=True)[:3]
    event_score = (sum(top_3) / len(top_3)) * policy_proximity_adjustment * 60 if top_3 else 0.0

    # Obligation uplift: base points × status weight, additive, soft-capped
    # via a saturating curve (4.3) — see module docstring.
    points = obligation_points or {}
    raw_uplift = sum(
        float(points.get(ob_key, 0) or 0) * weight
        for ob_key, weight in active_obligations
    )
    obligation_score = OBLIGATION_UPLIFT_MAX * (
        1.0 - math.exp(-raw_uplift / OBLIGATION_SOFTCAP_SCALE)
    ) if raw_uplift > 0 else 0.0

    return min(100.0, event_score + obligation_score)
