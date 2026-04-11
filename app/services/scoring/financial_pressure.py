"""Financial Pressure component scoring (v2).

Three explicitly bounded sub-components:
  base_filing_signal    (0-40): keyword/sentiment frequency in filing language
  leverage_warning_bonus (0-30): covenant stress, high-yield debt language
  liquidity_stress_bonus (0-30): going concern, suspended capex, cash runway warnings

When evidence is sparse (fewer than 2 filings), the maximum achievable score is
capped proportionally to avoid false precision from a single data point.
"""

from __future__ import annotations


def score_financial_pressure(
    base_filing_signal: float,
    leverage_warning_bonus: float,
    liquidity_stress_bonus: float,
    filing_count: int = 4,
) -> float:
    """
    Financial Pressure component score (0-100).

    Args:
        base_filing_signal:     Keyword/sentiment signal derived from filing language on [0, 40].
        leverage_warning_bonus: Covenant/debt-load language signal on [0, 30].
        liquidity_stress_bonus: Going-concern / cash-runway signal on [0, 30].
        filing_count:           Number of recent quarterly filings available.
                                Used only to apply a sparse-evidence cap.

    Returns:
        Component score on [0, 100].

    Raises:
        ValueError: if any component is outside its declared range.
    """
    if not (0 <= base_filing_signal <= 40):
        raise ValueError(f"base_filing_signal must be 0-40, got {base_filing_signal}")
    if not (0 <= leverage_warning_bonus <= 30):
        raise ValueError(f"leverage_warning_bonus must be 0-30, got {leverage_warning_bonus}")
    if not (0 <= liquidity_stress_bonus <= 30):
        raise ValueError(f"liquidity_stress_bonus must be 0-30, got {liquidity_stress_bonus}")

    raw_score = base_filing_signal + leverage_warning_bonus + liquidity_stress_bonus

    # Sparse-evidence cap: scale score linearly when fewer than 2 filings exist.
    # A single filing does not constitute a trend; avoid treating it as one.
    if filing_count < 2:
        raw_score = raw_score * (filing_count / 2.0)

    return min(100.0, raw_score)
