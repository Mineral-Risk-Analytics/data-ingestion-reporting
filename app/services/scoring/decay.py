"""
Category-specific recency decay for the v2 scoring engine.

Each pillar has its own evidence window and decay shape.  Returns a
recency_multiplier in [0.60, 1.20] for use in compute_event_impact().
"""

from __future__ import annotations

import math
from datetime import date
from typing import Optional

from app.constants import RiskCategory

# Evidence windows per category (days).  None = no fixed window cutoff;
# the decay shape itself drives the effective horizon.
#
# OPERATIONAL is flagged ``None`` rather than ``180``: the 90-day half-life
# exponential clamps to the 0.60 floor by roughly day 125, so a hard
# 180-day window would have no effect on the multiplier output.  Listing
# it as None makes the single-source-of-truth on operational decay the
# half-life constant in compute_recency_multiplier — partner reviewing
# the calibration should adjust that one value, not this dict's entry.
#
# FINANCIAL_PRESSURE is also None — the caller controls the evidence
# pool by limiting to the most recent N quarterly filings (default 4).
EVIDENCE_WINDOWS: dict[RiskCategory, Optional[int]] = {
    RiskCategory.GEOPOLITICAL_TRADE:     730,   # 24 months — structural facts decay slowly
    RiskCategory.REGULATORY_COMPLIANCE:  365,   # 365 days before/after effectivity
    RiskCategory.OPERATIONAL:            None,  # driven by 90-day half-life — no fixed cutoff
    RiskCategory.MATERIAL_CONCENTRATION: 365,   # aligned with regulatory/trade signals
    RiskCategory.FINANCIAL_PRESSURE:     None,  # held until superseded by newer filing
}


def compute_recency_multiplier(
    category: RiskCategory,
    event_date: date,
    as_of_date: date,
    effective_date: Optional[date] = None,
) -> float:
    """
    Return a recency_multiplier in [0.60, 1.20].

    Rules per category:
    - GEOPOLITICAL_TRADE: slow linear decay from 1.0 (day 0) to 0.60 (day 730+).
      Structural concentration facts should be passed with event_date = today
      so they always produce ~1.0.  Previously the formula decayed to 0.70 over
      the window then stepped to 0.60 at the boundary — that 0.10 discontinuity
      was smoothed in 2026-06-06 so the decay reaches 0.60 continuously.
    - REGULATORY_COMPLIANCE: step-up to 1.10–1.20 when enforcement is within 90 days;
      otherwise linear decay over 365 days.
    - OPERATIONAL: exponential decay with a 90-day half-life clamped to [0.60, 1.20].
    - FINANCIAL_PRESSURE: always returns 1.0; the caller controls the evidence window
      by only passing the 4 most recent quarterly filings.
    - MATERIAL_CONCENTRATION: linear decay over 365 days, floor 0.60.

    Args:
        category:       RiskCategory pillar for this event.
        event_date:     Date the underlying event was published/filed.
        as_of_date:     Evaluation date (typically today).
        effective_date: For REGULATORY_COMPLIANCE events only — the date the rule
                        becomes enforceable.  Ignored for other categories.

    Returns:
        Float in [0.60, 1.20].
    """
    age_days = (as_of_date - event_date).days

    if category == RiskCategory.FINANCIAL_PRESSURE:
        return 1.0

    if category == RiskCategory.GEOPOLITICAL_TRADE:
        window = EVIDENCE_WINDOWS[category]
        assert window is not None
        # Linear decay from 1.0 (day 0) to 0.60 (day window+); continuous
        # at the boundary.  Removed the step-down discontinuity that
        # previously dropped the multiplier from 0.70 → 0.60 at the
        # window edge.
        #
        # 2026-06-14: clamp upper bound at 1.20.  Future-dated events
        # (e.g. GTA's announced-but-not-yet-implemented interventions
        # carrying event_date months or years past as_of_date) make
        # age_days negative, which the linear formula would extrapolate
        # past 1.20.  Treating future / imminent events as "fully recent"
        # at the 1.20 ceiling matches the docstring contract.
        return max(0.60, min(1.20, 1.0 - (age_days / window) * 0.40))

    if category == RiskCategory.REGULATORY_COMPLIANCE:
        if effective_date is not None:
            days_to_effective = (effective_date - as_of_date).days
            if 0 <= days_to_effective <= 90:
                # Step-up: multiplier rises toward 1.20 as enforcement approaches
                return min(1.20, 1.10 + (1 - days_to_effective / 90) * 0.10)
        # Normal linear decay over 365 days
        if age_days >= 365:
            return 0.60
        # 2026-06-14: clamp upper bound at 1.20 for future-dated events.
        return max(0.60, min(1.20, 1.0 - (age_days / 365) * 0.40))

    if category == RiskCategory.OPERATIONAL:
        # 90-day half-life: after one half-life multiplier = 0.5, after two = 0.25, etc.
        half_life = 90
        multiplier = 1.0 * (0.5 ** (age_days / half_life))
        return max(0.60, min(1.20, multiplier))

    if category == RiskCategory.MATERIAL_CONCENTRATION:
        if age_days >= 365:
            return 0.60
        # 2026-06-14: clamp upper bound at 1.20 for future-dated events.
        return max(0.60, min(1.20, 1.0 - (age_days / 365) * 0.40))

    # Fallback for any future category values before they get an explicit rule
    return 1.0
