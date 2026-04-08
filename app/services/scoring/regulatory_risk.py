"""Regulatory / policy pressure from recent events and rule density."""

from __future__ import annotations

from app.services.scoring.types import ScoreResult


def score_regulatory_profile(
    *,
    recent_high_severity_events: int,
    active_regulation_count: int,
) -> ScoreResult:
    """Transparent weighted sum; tune weights as you calibrate to analyst review."""
    rationale: list[str] = []
    ev = min(60.0, recent_high_severity_events * 12.0)
    rationale.append(
        f"Recent high-signal regulatory events: {recent_high_severity_events} → +{ev:.1f}"
    )
    reg = min(40.0, active_regulation_count * 6.0)
    rationale.append(f"Active tracked regulations: {active_regulation_count} → +{reg:.1f}")
    total = min(100.0, ev + reg)
    return ScoreResult(score=total, rationale=rationale)
