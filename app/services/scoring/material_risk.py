"""Material Concentration Risk component scoring (v2).

Sub-weights: criticality 35%, concentration 35%, trade_volatility 30%.
Concentration raised to equal criticality — a highly concentrated supply of a
critical material is as dangerous as the material's intrinsic criticality.
"""

from __future__ import annotations


def score_material_exposure(
    criticality: float,       # 0-1.0: inherent supply criticality of the material
    concentration: float,     # 0-1.0: geographic/supplier concentration of supply
    trade_volatility: float,  # 0-1.0: price/flow volatility signal
) -> float:
    """
    Material Concentration Risk component score (0-100).

    Args:
        criticality:      Inherent supply criticality of the material on [0, 1].
        concentration:    Geographic/supplier concentration of supply on [0, 1].
        trade_volatility: Price/flow volatility signal on [0, 1].

    Returns:
        Component score on [0, 100].
    """
    return min(100.0, (0.35 * criticality + 0.35 * concentration + 0.30 * trade_volatility) * 100)
