"""Explainable rule-based risk scoring (v2, five-pillar framework).

Removed in v2: macro_context_score — its demand_index input feeds
score_material_exposure(trade_volatility=...) and its infrastructure_stress_index
input feeds score_geopolitical_trade(country_concentration=...).
"""

from app.services.scoring.decay import compute_recency_multiplier
from app.services.scoring.event_impact import compute_effective_confidence, compute_event_impact
from app.services.scoring.financial_pressure import score_financial_pressure
from app.services.scoring.geopolitical_risk import score_geopolitical_trade
from app.services.scoring.material_risk import score_material_exposure
from app.services.scoring.regulatory_risk import score_regulatory_profile
from app.services.scoring.supplier_risk import aggregate_supplier_risk

__all__ = [
    "aggregate_supplier_risk",
    "compute_effective_confidence",
    "compute_event_impact",
    "compute_recency_multiplier",
    "score_financial_pressure",
    "score_geopolitical_trade",
    "score_material_exposure",
    "score_regulatory_profile",
]
