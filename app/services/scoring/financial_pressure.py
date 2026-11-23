"""Financial Pressure component scoring (v2).

Three explicitly bounded sub-components:
  base_filing_signal    (0-40): material-level price volatility (Pink Sheet CV,
                                Fig 10 CAGR) + generic FINANCIAL_PRESSURE events
                                + SEC EDGAR company signal weighted by
                                production share (capped at 15 pts contribution).
  leverage_warning_bonus (0-30): material-level price spike + Fig 10 positive YoY
                                + PRICE_SURGE / MARKET_SQUEEZE events.
  liquidity_stress_bonus (0-30): material-level price crash + Fig 10 negative YoY
                                + PRODUCER_EXIT / MINE_CLOSURE / BANKRUPTCY events.

When evidence is sparse (fewer than 2 data points), the maximum achievable score
is capped proportionally to avoid false precision from a single data point.

11.4-Fin-B (2026-06-06): module docstring rewritten — the legacy v1 description
of "keyword/sentiment frequency in filing language" predated Tier 1.5 (Fig 10)
and Tier 3 (SEC EDGAR).  Today the signal sources are the four tiers documented
in ``market_aggregator._derive_market_financial_inputs``, not filing-language
mining.  The ``filing_count`` parameter was also renamed ``evidence_count``
because it counts price points + events + Fig 10 marker, not filings.
"""

from __future__ import annotations


def score_financial_pressure(
    base_filing_signal: float,
    leverage_warning_bonus: float,
    liquidity_stress_bonus: float,
    evidence_count: int = 4,
) -> float:
    """
    Financial Pressure component score (0-100).

    Args:
        base_filing_signal:     Material-level price/company signal on [0, 40].
        leverage_warning_bonus: Price-spike + leverage event signal on [0, 30].
        liquidity_stress_bonus: Price-crash + producer-exit signal on [0, 30].
        evidence_count:         Total evidence data points (price points + events
                                + Fig 10 marker if present).  Used to apply the
                                sparse-evidence cap when evidence is < 2.
                                Pre-11.4-Fin-B this was called ``filing_count``
                                — a stale v1 name from the days when the
                                Financial Pressure pillar mined SEC filing
                                language only.  See module docstring.

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

    # Sparse-evidence cap: scale score linearly when fewer than 2 data points
    # exist.  A single data point does not constitute a trend; avoid treating
    # it as one.  See ``sparse_evidence_cap_factor`` in the 11.4-Fin-A
    # diagnostic for partner-visible reporting of when this fires.
    if evidence_count < 2:
        raw_score = raw_score * (evidence_count / 2.0)

    return min(100.0, raw_score)
