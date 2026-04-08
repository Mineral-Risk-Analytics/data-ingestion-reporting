"""Macro / demand / infrastructure context (placeholder calibration layer)."""

from __future__ import annotations

from app.services.scoring.types import ScoreResult


def macro_context_score(
    *,
    demand_index: float,
    infrastructure_stress_index: float,
) -> ScoreResult:
    """
    Phase 1: simple average of two 0–100 indices (provided by caller).

    Later: ingest charging density, OEM build rates, grid interconnection backlog.
    """
    rationale = [
        f"Demand index input: {demand_index:.1f}",
        f"Infrastructure stress index input: {infrastructure_stress_index:.1f}",
    ]
    avg = (demand_index + infrastructure_stress_index) / 2.0
    rationale.append(f"Equally weighted average → {avg:.1f}")
    return ScoreResult(score=avg, rationale=rationale)
