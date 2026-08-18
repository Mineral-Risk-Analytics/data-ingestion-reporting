"""Risk-band mapping — SOLE source of truth with frontend lib/utils/risk-band.ts.

Bands (2026-08-11 recalibration for the CONCENTRATION-ONLY launch):

    0  - 34    LOW    (measured, diversified — green)
    35 - 59    MOD    (real concentration exposure, mitigated — amber)
    60 - 89    HIGH   (severe chokepoint on at least one stage — orange)
    90 - 100   CRIT   (binding chokepoint ~80%+ single-country; no
                       meaningful alternative supply — red)

2026-08-11 recalibration rationale (Option B of
docs/design/band_recalibration_proposal.md; sign-off: Nicole 2026-08-11):
the concentration-first launch publishes the concentration pillar ALONE,
and stage-max scores sit structurally higher than the 4.3 five-pillar
blend — one concentrated stage sets the number.  Under the old 30/50/65
cuts the post-USGS-fix launch distribution read 8 CRIT / 0 HIGH / 2 MOD
and the corpus 81% CRIT.  35/60/90 against the 2026-08-10 distribution
(all launch binding stages at 2025 data):

  Launch: Iron Ore LOW · Copper MOD · Aluminum/Phosphate/Lithium/Nickel
  HIGH · Cobalt/REE/Natural Graphite/Manganese CRIT (1/1/4/4).
  Corpus (37 scored): 5 LOW / 5 MOD / 12 HIGH / 16 CRIT (43% CRIT).

Boundary cases recorded in the proposal: Cobalt 90.01 lands CRIT by 0.01
— deliberately, because its published score is UNDERSTATED by the stale
battery-grade stage (would-be 89.4; on-page banner), so the placement is
conservative in the right direction.  Tantalum 60.51 sits 0.5 above the
HIGH cut (churn-risk on revision; disclosed, accepted).  Lower cuts sit
in natural corpus gaps (32.8→37.2 and 56.5→60.5) to minimize revision
churn.  Cuts remain FIXED ABSOLUTE (Verisk-Maplecroft-style categories,
not percentiles): a material's band must not change because another
material moved.  Revisit on methodology changes only.

Prior calibrations, kept for the audit trail:
- 2026-07-26 (4.3 five-pillar): 30/50/65 → 7 CRIT / 13 HIGH / 11 MOD /
  9 LOW; CRIT set Ga/NG/Mg/Si/Bi/Co/REE.
- 2026-05-11 (3.x): 30/60/80.

**Insufficient-data gate:** pass ``sufficient=False`` when the material's
concentration pillar is unscored (no share data; e.g. Rhenium, Sodium) —
returns ``None`` so the UI renders "Insufficient data" instead of a
false-green LOW.

This module + ``lib/utils/risk-band.ts`` on the frontend are the SOLE
source of truth.  If you change one, change the other in the same commit.

``score_to_band`` accepts a raw score on the 0-100 axis. It also accepts
``None`` (returns ``None``) and values on the 0-1 axis (rescaled x100), which
is common in older evidence rollups.
"""

from __future__ import annotations

from typing import Literal, Optional

RiskBand = Literal["LOW", "MOD", "HIGH", "CRIT"]

# 2026-08-11 cuts (concentration-only launch, proposal Option B) — keep in
# lockstep with frontend risk-band.ts.
BAND_CUT_MOD: float = 35.0
BAND_CUT_HIGH: float = 60.0
BAND_CUT_CRIT: float = 90.0


def _normalize(score: float) -> float:
    # If the score looks like a fraction (0-1), rescale to 0-100.
    if 0.0 <= score <= 1.0:
        return score * 100.0
    return score


def score_to_band(
    score: Optional[float],
    *,
    sufficient: bool = True,
) -> Optional[RiskBand]:
    """Map a numeric risk score to its band label.

    Returns ``None`` when the score is ``None`` OR when ``sufficient=False``
    (concentration pillar unscored — "Insufficient data", never LOW), so
    callers can render a dash / neutral chip.
    """
    if score is None or not sufficient:
        return None
    normalized = _normalize(float(score))
    if normalized < BAND_CUT_MOD:
        return "LOW"
    if normalized < BAND_CUT_HIGH:
        return "MOD"
    if normalized < BAND_CUT_CRIT:
        return "HIGH"
    return "CRIT"
