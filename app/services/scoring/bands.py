"""Risk-band mapping used by the admin dashboard list rows.

Bands (2026-05-11 calibration):

    0   - 29   LOW    (low risk — green)
    30  - 59   MOD    (moderate — amber)
    60  - 79   HIGH   (high — orange)
    80  - 100  CRIT   (critical — red)

Prior calibration was 0-34 / 35-54 / 55-74 / 75-100; loosened on 2026-05-11
after the Overview page review surfaced that scores in the 50-60 range
felt over-pessimistic against the dev-DB demo set.  The new cutoffs:

  * Give MOD enough headroom that scores in the high-50s stay yellow
    rather than flipping to orange.
  * Keep CRIT reserved for genuinely extreme scores (80+) rather than the
    previous 75+ which was triggering on every materially-concentrated
    mineral.
  * Stay within industry-standard risk-band conventions (Gartner / Verisk
    Maplecroft both use ~60 as the HIGH inflection on 0-100 scales).

This module + ``lib/utils/risk-band.ts`` on the frontend are the SOLE
source of truth.  If you change one, change the other in the same commit.

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
    if normalized < 30.0:
        return "LOW"
    if normalized < 60.0:
        return "MOD"
    if normalized < 80.0:
        return "HIGH"
    return "CRIT"
