"""Shared constants and risk taxonomy (reports-first; extend for SaaS later).

Scoring v2 five-pillar taxonomy.  Previous values (MATERIAL_SUPPLY, REGULATORY_POLICY,
SUPPLIER_OPERATIONAL, MARKET_DEMAND, INFRASTRUCTURE_ECOSYSTEM) are retired:
  - MATERIAL_SUPPLY → MATERIAL_CONCENTRATION
  - REGULATORY_POLICY → REGULATORY_COMPLIANCE
  - SUPPLIER_OPERATIONAL → OPERATIONAL
  - MARKET_DEMAND → FINANCIAL_PRESSURE (financial signals) or GEOPOLITICAL_TRADE
    (trade/demand geo signals); mapped per call site.
  - INFRASTRUCTURE_ECOSYSTEM → GEOPOLITICAL_TRADE (infrastructure context
    feeds country-concentration risk, not a standalone scoring pillar).
"""

from enum import Enum


class RiskCategory(str, Enum):
    """Five-pillar scoring taxonomy.  Weights: 30/20/20/15/15."""

    MATERIAL_CONCENTRATION = "material_concentration"
    GEOPOLITICAL_TRADE     = "geopolitical_trade"
    REGULATORY_COMPLIANCE  = "regulatory_compliance"
    OPERATIONAL            = "operational"
    FINANCIAL_PRESSURE     = "financial_pressure"
