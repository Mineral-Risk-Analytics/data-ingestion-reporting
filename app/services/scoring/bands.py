"""Risk-band mapping — SOLE source of truth with frontend lib/utils/risk-band.ts.

Bands (2026-07-26 recalibration vs SCORING_VERSION 4.3 / rollup 1.2):

    0  - 29    LOW    (measured, diversified — green)
    30 - 49    MOD    (real concentration/trade exposure, mitigated — amber)
    50 - 64    HIGH   (severe chokepoint on at least one stage — orange)
    65 - 100   CRIT   (extreme concentration + weaponization exposure — red)

4.3 recalibration rationale: the 4.2 avg→max geopolitical fix raised the
whole distribution ~5-13 points as a LEVEL effect (strongest active
restriction now defines exposure instead of being averaged away), and the
4.3 obligation soft-cap plus the 25-regulation workbook re-shaped the
regulatory pillar. Under the old 25/45/60 cuts, 24/40 materials read
HIGH-or-worse and CRIT held 10 — the cuts were calibrated to the old
level, not the old risk. 30/50/65 restores the intended shape on the 4.3
distribution: 7 CRIT / 13 HIGH / 11 MOD / 9 LOW.

CRITICAL (>=65) membership check 2026-07-26: Gallium 69.9, Natural
Graphite 69.1, Magnesium 67.7, Silicon (anode) 66.8, Bismuth 66.7,
Cobalt 66.2, REE 65.6 — same seven-material set as the 4.1 calibration.
Near-misses documented: Nickel 63.9, Tungsten 63.1, Niobium 60.4.

Prior calibration (2026-05-11: 30/60/80) was set against 3.x scores: under
4.1 it left CRIT structurally empty (weights cap overall ~72 even at
conc=100), lumped 21/40 materials into MOD, and showed false-green LOW for
materials whose concentration pillar simply has no data.  New cuts are FIXED
ABSOLUTE (Verisk-Maplecroft-style categories, not FEMA-NRI-style
percentiles): a material's band must not change because another material
moved.  Calibrated against the rollup-1.2 distribution; revisit on
methodology changes only.

CRITICAL (>=60) membership check 2026-07-20: Gallium, Natural Graphite,
Cobalt, Magnesium, Bismuth, Silicon (anode), REE — 4 of 7 in IEA GCMO
2026's top-8 risk ranking; divergences documented in the proposal.

**Insufficient-data gate:** pass ``sufficient=False`` when the material's
concentration pillar is unscored (no share data; e.g. Germanium) — returns
``None`` so the UI renders "Insufficient data" instead of a false-green LOW.

This module + ``lib/utils/risk-band.ts`` on the frontend are the SOLE
source of truth.  If you change one, change the other in the same commit.

``score_to_band`` accepts a raw score on the 0-100 axis. It also accepts
``None`` (returns ``None``) and values on the 0-1 axis (rescaled x100), which
is common in older evidence rollups.
"""

from __future__ import annotations

from typing import Literal, Optional

RiskBand = Literal["LOW", "MOD", "HIGH", "CRIT"]

# 2026-07-26 cuts (4.3 recalibration) — keep in lockstep with frontend risk-band.ts.
BAND_CUT_MOD: float = 30.0
BAND_CUT_HIGH: float = 50.0
BAND_CUT_CRIT: float = 65.0


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
