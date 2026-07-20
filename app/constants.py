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


# Known aliases for mis-typed / legacy category strings → canonical value.
# "geopolitical" was used on ~23 events (incl. the DRC cobalt export ban)
# where "geopolitical_trade" was meant, silently hiding them from the
# geopolitical pillar's event scan (2026-07-20).
_RISK_CATEGORY_ALIASES: dict[str, str] = {"geopolitical": "geopolitical_trade"}


def normalise_risk_categories(value):
    """Coerce a category list to valid RiskCategory values.

    Maps known aliases and drops anything not in the taxonomy, so a mis-typed
    category can never make an event invisible to a scoring pillar again.
    Non-list values pass through unchanged (defensive).
    """
    if not isinstance(value, (list, tuple)):
        return value
    valid = {c.value for c in RiskCategory}
    out: list[str] = []
    for item in value:
        s = _RISK_CATEGORY_ALIASES.get(str(item), str(item))
        if s in valid and s not in out:
            out.append(s)
    return out
