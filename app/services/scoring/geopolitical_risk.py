"""Geopolitical / Trade Risk component scoring (v2) — fifth pillar at 20%.

Sub-weights: country_concentration 40%, export_restriction_exposure 35%, tariff_exposure 25%.
Country concentration carries the highest weight because structural geographic dependency
is the dominant risk for EV battery supply chains (China ~70-80% of cell production and
refining capacity as of 2024-2026).
"""

from __future__ import annotations


def score_geopolitical_trade(
    country_concentration: float,
    export_restriction_exposure: float,
    tariff_exposure: float,
) -> float:
    """
    Geopolitical / Trade Risk component score (0-100).

    Args:
        country_concentration:         Share of supply chain in high-risk geographies on [0, 1].
        export_restriction_exposure:   Historical export-restriction risk for source countries
                                       on [0, 1].
        tariff_exposure:               Current tariff burden plus pending policy signals on [0, 1].

    Returns:
        Component score on [0, 100].
    """
    return min(100.0, (
        0.40 * country_concentration
        + 0.35 * export_restriction_exposure
        + 0.25 * tariff_exposure
    ) * 100)
