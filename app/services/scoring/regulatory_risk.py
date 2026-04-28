"""Regulatory & Compliance Risk component scoring (v2).

Two-part score:
  1. Event-driven rollup — top-3 computed event_impact values, scaled to 0-60.
  2. Obligation uplift — hard legal obligations add additive points capped at 40.
     These are not regular policy events; they carry enforcement deadlines and are
     scored separately to avoid dilution.

Obligation uplift is weighted by compliance_status severity:
  non_compliant × 1.00  (confirmed violation — full statutory risk)
  unknown       × 0.50  (unassessed — conservative half-weight)
  partial       × 0.40  (in transition — meaningful but not full exposure)

This prevents partially-compliant OEMs from scoring identically to confirmed FEOC
entities with full non-compliance on the same obligations.

For market-level scoring (no company), the weight comes from the regulation's
geography_compliance_weights JSONB column (resolved by _resolve_compliance_weight
in market_aggregator.py) rather than a compliance_status lookup.
"""

from __future__ import annotations

# Base uplift points per active compliance obligation (before weight multiplier).
#
# Calibrated so a fully non_compliant UFLPA entity reaches 25/100 from obligations
# alone before any event signal.  Hard-capped at 40 to reserve headroom for events.
#
# Ordering reflects enforcement severity and battery supply chain directness:
#   Tier 1 (import prohibition / credit disqualification): UFLPA, EU_BATTERY_REG_2023,
#           CRMA_2024, IRA_DOMESTIC
#   Tier 2 (broad due diligence mandates, not yet fully effective): EU_CSDDD,
#           EU_REACH_COBALT
#   Tier 3 (indirect / narrow battery relevance): EU_CBAM, EU_CONFLICT_MINERALS
#   Excluded: SEC_CLIMATE_2024 (stayed by federal court as of 2025 — disclosure
#             rule only, no sourcing prohibition)
#
# Geography-specific weights for each regulation are stored in
# regulations.geography_compliance_weights (seeded in migration 017).
COMPLIANCE_OBLIGATIONS: dict[str, int] = {
    # Tier 1 — direct enforcement consequences for battery supply chains
    "UFLPA":               25,   # rebuttable presumption: Xinjiang goods = forced labour, import ban
    "EU_BATTERY_REG_2023": 20,   # due diligence + battery passport; non-compliance = EU market exclusion
    "CRMA_2024":           15,   # 65% single-country cap on strategic raw materials
    "IRA_DOMESTIC":        15,   # FEOC materials disqualify battery components from IRA credits
    # Tier 2 — significant but not yet fully effective or more general scope
    "EU_CSDDD":            10,   # human rights + environmental due diligence across full value chain (effective 2027)
    "EU_REACH_COBALT":      8,   # cobalt SVHC authorisation requirements for EU manufacturers
    # Tier 3 — indirect battery relevance
    "EU_CBAM":              5,   # carbon border certificates for copper/aluminium from 2026
    "EU_CONFLICT_MINERALS": 3,   # 3TG responsible sourcing (covers BMS electronics, not cathode materials)
}


def score_regulatory_profile(
    top_event_impacts: list[float],
    active_obligations: list[tuple[str, float]],
    policy_proximity_adjustment: float = 1.0,
) -> float:
    """
    Regulatory & Compliance Risk component score (0-100).

    Args:
        top_event_impacts:          Pre-computed event_impact values (from event_impact.py).
                                    May be any length; only the top-3 are used.
        active_obligations:         List of (obligation_key, weight_multiplier) tuples,
                                    e.g. [("UFLPA", 1.0), ("IRA_DOMESTIC", 0.40)].
                                    Weight reflects compliance_status severity.
        policy_proximity_adjustment: Scalar applied to the event-driven portion to
                                    account for geographic or sectoral proximity.
                                    Defaults to 1.0 (no adjustment).

    Returns:
        Component score on [0, 100].
    """
    # Event-driven rollup: average the top-3 impacts, scale to 0-60 range
    top_3 = sorted(top_event_impacts, reverse=True)[:3]
    event_score = (sum(top_3) / len(top_3)) * policy_proximity_adjustment * 60 if top_3 else 0.0

    # Obligation uplift: base points × status weight, additive, hard-capped at 40
    obligation_score = min(40.0, sum(
        COMPLIANCE_OBLIGATIONS.get(ob_key, 0) * weight
        for ob_key, weight in active_obligations
    ))

    return min(100.0, event_score + obligation_score)
