"""Explainable rule-based risk scoring — v3.0 six-pillar framework.

Pillars: material exposure, geopolitical trade, regulatory profile,
operational risk, financial pressure, supply-chain propagation.

Pure-function pillar scorers re-exported here for convenience. The
per-company orchestrator lives in ``orchestrator.py``; the market-level
aggregator lives in ``market_aggregator.py``.
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
