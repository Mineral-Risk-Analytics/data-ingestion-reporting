"""
Core event-impact formula for the v2 scoring engine.

Pure functions only — no DB reads, no side effects.
All inputs must already be validated to their stated ranges before calling.
"""

from __future__ import annotations


def compute_effective_confidence(severity: float, confidence: float) -> float:
    """
    Apply confidence floor for high-severity events.

    A genuine high-risk signal should not be over-discounted by parser uncertainty.
    Floor only activates when severity >= 0.80; below that threshold the raw
    confidence value is used unchanged so low-severity noise is not artificially boosted.
    """
    if severity >= 0.80:
        return max(confidence, 0.60)
    return confidence


def relevance_score_to_multiplier(relevance_score: float) -> float:
    """Map a [0, 1] relevance probability to a [0.70, 1.30] multiplier.

    Background (2026-05-09): ``RiskEventMaterial.relevance_score`` and
    ``RiskEventGeography.relevance_score`` are stored on [0, 1] (probability-
    style — 1.0 = directly attributed, ~0.1 = weakly keyword-detected).  But
    ``compute_event_impact`` accepts ``relevance_multiplier`` on [0.70,
    1.30] (Bayesian-style, centered on 1.0).  Passing a raw relevance_score
    directly raised ``ValueError: relevance_multiplier must be 0.70-1.30``
    for any event with relevance < 0.70.  Pre-G6 the rollup path almost
    never fired in production so the bug stayed hidden; lowering
    _STAGE_ROLLUP_MIN_NODES to 1 surfaced it.

    Mapping:
        relevance_score = 0.0  →  0.70  (slight discount — weakly attached)
        relevance_score = 0.5  →  1.00  (neutral)
        relevance_score = 1.0  →  1.30  (boost — directly attributed)

    Inputs outside [0, 1] are clamped before mapping.
    """
    clamped = max(0.0, min(1.0, relevance_score))
    return 0.70 + 0.60 * clamped


def compute_event_impact(
    severity: float,
    confidence: float,
    recency_multiplier: float = 1.0,
    relevance_multiplier: float = 1.0,
) -> float:
    """
    Compute a normalized event impact value.

    Theoretical max: 1.0 × 1.0 × 1.20 × 1.30 = 1.56.
    All multipliers are bounded to prevent unbounded compounding.

    Args:
        severity:            Intrinsic severity of the event on [0.0, 1.0].
        confidence:          Parser/classifier confidence in the signal on [0.0, 1.0].
        recency_multiplier:  Category-specific decay output from decay.py on [0.60, 1.20].
        relevance_multiplier: Supplier-event relevance factor on [0.70, 1.30].

    Returns:
        event_impact on [0.0, 1.56].
    """
    if not (0.0 <= severity <= 1.0):
        raise ValueError(f"severity must be 0-1.0, got {severity}")
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"confidence must be 0-1.0, got {confidence}")
    if not (0.60 <= recency_multiplier <= 1.20):
        raise ValueError(f"recency_multiplier must be 0.60-1.20, got {recency_multiplier}")
    if not (0.70 <= relevance_multiplier <= 1.30):
        raise ValueError(f"relevance_multiplier must be 0.70-1.30, got {relevance_multiplier}")

    eff_conf = compute_effective_confidence(severity, confidence)
    return severity * eff_conf * recency_multiplier * relevance_multiplier
