"""Material / supply-chain concentration heuristics."""

from __future__ import annotations

from app.services.scoring.types import ScoreResult


def score_material_exposure(
    *,
    exposure_score: float,
    num_distinct_sources: int,
    is_critical_mineral: bool,
) -> ScoreResult:
    """
    Weighted rule blend:
    - Higher declared exposure_score increases risk
    - Fewer distinct geographic/material sources increases risk
    - Critical minerals flag adds a bump
    """
    rationale: list[str] = []
    base = min(100.0, exposure_score * 0.6)
    rationale.append(f"Base exposure contribution: {base:.1f} (60% of exposure score)")

    geo_factor = max(0.0, 30.0 - num_distinct_sources * 8)
    rationale.append(
        f"Supply breadth adjustment: +{geo_factor:.1f} "
        f"({num_distinct_sources} distinct source hint(s))"
    )

    mineral_bump = 12.0 if is_critical_mineral else 0.0
    if mineral_bump:
        rationale.append("Critical mineral context adds +12.0")

    total = base + geo_factor + mineral_bump
    return ScoreResult(score=min(100.0, total), rationale=rationale)
