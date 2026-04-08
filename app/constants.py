"""Shared constants and risk taxonomy (reports-first; extend for SaaS later)."""

from enum import Enum


class RiskCategory(str, Enum):
    """Primary risk dimensions used in scoring and event tagging."""

    MATERIAL_SUPPLY = "material_supply"
    REGULATORY_POLICY = "regulatory_policy"
    SUPPLIER_OPERATIONAL = "supplier_operational"
    MARKET_DEMAND = "market_demand"
    INFRASTRUCTURE_ECOSYSTEM = "infrastructure_ecosystem"
