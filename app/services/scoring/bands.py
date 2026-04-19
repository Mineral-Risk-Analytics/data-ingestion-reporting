"""Risk-band mapping used by the admin dashboard list rows.

Bands follow the Phase 1 product spec:

    0   - 34   LOW
    35  - 54   MOD
    55  - 74   HIGH
    75  - 100  CRIT

``score_to_band`` accepts a raw score on the 0-100 axis. It also accepts
``None`` (returns ``None``) and values on the 0-1 axis (rescaled x100), which
is common in older evidence rollups.
"""

from __future__ import annotations

from typing import Literal, Optional

RiskBand = Literal["LOW", "MOD", "HIGH", "CRIT"]


def _normalize(score: float) -> float:
    # If the score looks like a fraction (0-1), rescale to 0-100.
    if 0.0 <= score <= 1.0:
        return score * 100.0
    return score


def score_to_band(score: Optional[float]) -> Optional[RiskBand]:
    """Map a numeric risk score to its band label. Returns ``None`` when the
    score is ``None`` so callers can render a dash."""
    if score is None:
        return None
    normalized = _normalize(float(score))
    if normalized < 35.0:
        return "LOW"
    if normalized < 55.0:
        return "MOD"
    if normalized < 75.0:
        return "HIGH"
    return "CRIT"
