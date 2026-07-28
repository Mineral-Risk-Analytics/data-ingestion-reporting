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
# Event subtypes that mean "supply-restricting export measure" (2026-07-27).
# Single source of truth for the HS-node scorer's exact-match gate AND the
# market aggregator's event classifier. The uppercase value is the GTA/FR
# convention; the lowercase ones are manual-walkthrough subtypes that were
# invisible to the HS-node path (it has no title-heuristic fallback).
# capital_control_export_proceeds is deliberately excluded — it constrains
# money flows, not material supply.
EXPORT_RESTRICTION_SUBTYPES: frozenset[str] = frozenset({
    "EXPORT_RESTRICTION",
    "national_export_quota",
    "national_export_ban",
    "quota_allocation_constraint",
    "export_ban_inventory_lockup",
    "export_license_expiration",
})

# Controlled operational subtype taxonomy → default severity (2026-07-27,
# Nicole — operational news triage). Applied at PROMOTION of
# operational_news_candidate events (operational_triage.promote_candidate):
# the partner picks the subtype, severity fills from this ladder unless
# explicitly overridden. Defaults are calibrated to the severity ranges the
# manual walkthroughs already used (e.g. export-ban/court stoppages 0.75,
# strategic suspensions 0.60, weather 0.35-0.50, guidance cuts 0.30).
#
# Rationale for type-driven defaults: mirrors the GTA intervention-type →
# severity mapping on the geopolitical pillar — reproducible, auditable,
# no per-triage drift. The override exists because operational events of
# the same type vary by scale (a strike at Escondida ≠ a strike at a 5kt
# mine); GTA types don't have that variance as acutely.
#
# Subtypes OUTSIDE this dict remain allowed at promotion (the historical
# walkthrough vocabulary is free-form) but then an explicit severity is
# REQUIRED — no default, no guess.
OPERATIONAL_SUBTYPE_DEFAULT_SEVERITY: dict[str, float] = {
    # Major — supply materially offline, indefinite horizon
    "force_majeure":              0.75,
    "facility_shutdown":          0.70,
    # Serious — deliberate or forced stoppage with a path back
    "care_and_maintenance":       0.65,
    "permit_or_court_stoppage":   0.60,
    "incident_major":             0.60,   # facility-wide fire/explosion/tailings WITH stoppage
    # Moderate — partial or temporary supply impact
    "production_curtailment":     0.55,
    "labor_strike":               0.55,
    "power_supply_disruption":    0.50,
    "operations_halt":            0.50,   # generic halt/suspension, scope unclear
    "weather_supply_disruption":  0.45,
    "incident":                   0.45,   # contained incident, partial impact
    # Minor — signal of future/limited impact, production largely continues
    "guidance_cut":               0.35,
    "project_schedule_slip":      0.30,
    "fatality_no_stoppage":       0.30,   # walkthrough precedent: fatality events at 0.30
}

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
