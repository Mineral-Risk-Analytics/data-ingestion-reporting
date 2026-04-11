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

# Evidence windows per category (days).  None = held until superseded.
EVIDENCE_WINDOWS: dict[RiskCategory, Optional[int]] = {
    RiskCategory.GEOPOLITICAL_TRADE:     730,   # 24 months — structural facts decay slowly
    RiskCategory.REGULATORY_COMPLIANCE:  365,   # 365 days before/after effectivity
    RiskCategory.OPERATIONAL:            180,   # 180 days, 90-day half-life
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
    - GEOPOLITICAL_TRADE: slow linear decay from 1.0 to 0.70 over 24 months; drops
      to 0.60 once the window is exhausted.  Structural concentration facts should be
      passed with event_date = today so they always produce ~1.0.
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
        if age_days >= window:
            return 0.60
        # Linear decay from 1.0 to 0.70 across the full 24-month window
        return max(0.70, 1.0 - (age_days / window) * 0.30)

    if category == RiskCategory.REGULATORY_COMPLIANCE:
        if effective_date is not None:
            days_to_effective = (effective_date - as_of_date).days
            if 0 <= days_to_effective <= 90:
                # Step-up: multiplier rises toward 1.20 as enforcement approaches
                return min(1.20, 1.10 + (1 - days_to_effective / 90) * 0.10)
        # Normal linear decay over 365 days
        if age_days >= 365:
            return 0.60
        return max(0.60, 1.0 - (age_days / 365) * 0.40)

    if category == RiskCategory.OPERATIONAL:
        # 90-day half-life: after one half-life multiplier = 0.5, after two = 0.25, etc.
        half_life = 90
        multiplier = 1.0 * (0.5 ** (age_days / half_life))
        return max(0.60, min(1.20, multiplier))

    if category == RiskCategory.MATERIAL_CONCENTRATION:
        if age_days >= 365:
            return 0.60
        return max(0.60, 1.0 - (age_days / 365) * 0.40)

    # Fallback for any future category values before they get an explicit rule
    return 1.0
