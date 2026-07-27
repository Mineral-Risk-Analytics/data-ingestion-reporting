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


# Build 1 (2026-07-24): precedence for assigning an event's PRIMARY scoring
# category from its (possibly multi-valued) display tags. Most-specific /
# most-physical wins: an operational disruption outranks the trade measure
# that caused it; sanctions (geo+reg tagged) land in geopolitical per spec
# §4. Used by the RiskEvent before_insert autofill and migration 062.
# Region aliases usable as keys in Regulation.geography_compliance_weights.
# Resolution order (see market_aggregator._resolve_compliance_weight):
# exact ISO2 > region alias containing the country > "DEFAULT" > 0.50.
# Lets curation write one "EU" row (e.g. CBAM exempting intra-EU sourcing)
# instead of 27 member rows. Extend with new regions as needed.
REGION_MEMBERS: dict[str, frozenset[str]] = {
    "EU": frozenset({
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
        "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
        "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    }),
}


PRIMARY_CATEGORY_PRECEDENCE: tuple[str, ...] = (
    RiskCategory.OPERATIONAL.value,
    RiskCategory.GEOPOLITICAL_TRADE.value,
    RiskCategory.REGULATORY_COMPLIANCE.value,
    RiskCategory.FINANCIAL_PRESSURE.value,
    RiskCategory.MATERIAL_CONCENTRATION.value,
)


def derive_primary_category(categories) -> "str | None":
    """First precedence match among the event's display categories.

    Returns None when no valid category is present — such events are
    display-only and never selected by pillar scoring queries.
    """
    cats = set(categories or [])
    for value in PRIMARY_CATEGORY_PRECEDENCE:
        if value in cats:
            return value
    return None


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
