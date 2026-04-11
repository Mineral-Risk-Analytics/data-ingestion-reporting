"""Regulatory & Compliance Risk component scoring (v2).

Two-part score:
  1. Event-driven rollup — top-3 computed event_impact values, scaled to 0-60.
  2. Obligation uplift — hard legal obligations (UFLPA, EU Battery Reg, IRA) add
     additive points capped at 40.  These are not regular policy events; they carry
     enforcement deadlines and are scored separately to avoid dilution.
"""

from __future__ import annotations

# Uplift points per active compliance obligation.
# Calibrated so UFLPA alone pushes a supplier to ~25/100 before any event signal.
COMPLIANCE_OBLIGATIONS: dict[str, int] = {
    "UFLPA":        25,   # supplier has known Xinjiang exposure
    "EU_BATTERY_REG": 20, # sells into EU but lacks required compliance documentation
    "IRA_DOMESTIC": 15,   # materials do not qualify for IRA domestic content credits
}


def score_regulatory_profile(
    top_event_impacts: list[float],
    active_obligations: list[str],
    policy_proximity_adjustment: float = 1.0,
) -> float:
    """
    Regulatory & Compliance Risk component score (0-100).

    Args:
        top_event_impacts:          Pre-computed event_impact values (from event_impact.py).
                                    May be any length; only the top-3 are used.
        active_obligations:         List of obligation keys present for this supplier,
                                    e.g. ["UFLPA", "EU_BATTERY_REG"].
        policy_proximity_adjustment: Scalar applied to the event-driven portion to
                                    account for geographic or sectoral proximity.
                                    Defaults to 1.0 (no adjustment).

    Returns:
        Component score on [0, 100].
    """
    # Event-driven rollup: average the top-3 impacts, scale to 0-60 range
    top_3 = sorted(top_event_impacts, reverse=True)[:3]
    event_score = (sum(top_3) / len(top_3)) * policy_proximity_adjustment * 60 if top_3 else 0.0

    # Obligation uplift: additive, hard-capped at 40 points
    obligation_score = min(40.0, sum(
        COMPLIANCE_OBLIGATIONS.get(ob, 0) for ob in active_obligations
    ))

    return min(100.0, event_score + obligation_score)
