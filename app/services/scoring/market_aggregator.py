"""
Market-level (company-agnostic) evidence aggregation and scoring.

Computes risk scores at the material × geography intersection without requiring
any company data. This is the primary intelligence layer:

    material × geography  →  market_aggregator  →  MaterialGeographyRiskScore

Company overlay scoring (CompanyScore in orchestrator.py) builds ON TOP of this
layer by blending the customer's configured exposure profile with market scores.

Architecture relationship
--------------------------
evidence_query.py   — DB read layer (shared by both this module and orchestrator.py)
evidence_aggregator.py — company-anchored evidence → float inputs
market_aggregator.py   — market-anchored evidence → float inputs  ← THIS FILE
orchestrator.py        — company scoring entry point

Key differences from the company layer
---------------------------------------
* No CompanyMaterialExposure. Criticality comes from MaterialCriticalitySignal.
* Concentration is derived from the target geography's HCG status + the
  material's HHI score from the criticality signal.
* Financial pressure and supply-chain propagation are excluded — both require
  company-specific data. Pillar weights are renormalised across the four active
  pillars; see MARKET_PILLAR_WEIGHTS.
* ``score_material_geography()`` is the single public entry point for a
  specific (material, geography) pair.
* ``score_all_active_materials()`` batch-scores every active material against
  every geography with sufficient event coverage.

Transaction contract
---------------------
``score_material_geography()`` calls db.flush() but does NOT commit. The caller
owns the transaction, consistent with rescore_company() in orchestrator.py.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.constants import RiskCategory
from app.models.criticality_signal import MaterialCriticalitySignal
from app.models.scoring import HsCodeGeographyRiskScore, MaterialGeographyRiskScore
from app.models.supply import Material, MaterialProductionShare
from app.services.scoring import (
    financial_pressure as fp_module,
    geopolitical_risk,
    material_risk,
    regulatory_risk,
)
from app.services.scoring.decay import compute_recency_multiplier
from app.services.scoring.event_impact import (
    compute_event_impact,
    relevance_score_to_multiplier,
)
from app.services.scoring.evidence_query import (
    EventWithRelevance,
    HIGH_CONCENTRATION_GEOS,
    get_events_for_geographies,
    get_events_for_material,
    get_events_for_materials,
    get_events_for_regulations,
    get_hs_nodes_for_material,
)
from app.services.scoring.supplier_risk import SCORING_VERSION

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Market-level pillar weights
# Supply-chain propagation is excluded (requires a company graph). Financial
# pressure is KEPT but reframed with market-level inputs — commodity price
# volatility + producer stress events instead of company filing signals.
# Remaining five weights are renormalised from PILLAR_WEIGHTS:
#   material 0.25, geopolitical 0.20, regulatory 0.20, operational 0.10,
#   financial 0.10  →  sum = 0.85  →  renormalise each by /0.85
# ---------------------------------------------------------------------------
MARKET_PILLAR_WEIGHTS: dict[str, float] = {
    "material":      0.25 / 0.85,   # ≈ 0.294
    "geopolitical":  0.20 / 0.85,   # ≈ 0.235
    "regulatory":    0.20 / 0.85,   # ≈ 0.235
    "operational":   0.10 / 0.85,   # ≈ 0.118
    "financial":     0.10 / 0.85,   # ≈ 0.118
}

# ---------------------------------------------------------------------------
# Phase 3 — Stage-weighted rollup for Material Concentration pillar
# ---------------------------------------------------------------------------
# Weights map supply_chain_stage → contribution fraction.  Stages closer to
# battery-grade have higher weights because disruptions there propagate fastest
# into cell manufacturing.  Weights need not sum to 1 — the rollup normalises
# by the sum of weights of stages that actually have Level-0 scores.
# Source: docs/hs-code-redesign.md § "Rollup weighting at Level 0 → Level 1"
# ---------------------------------------------------------------------------
STAGE_ROLLUP_WEIGHTS: dict[str, float] = {
    "ore":           0.10,
    "concentrate":   0.15,
    "intermediate":  0.20,
    "refined":       0.25,
    "battery_grade": 0.30,
    # fabricated and scrap not used in Material Concentration rollup
}

# Minimum number of Level-0 stage nodes required to use the stage-weighted
# rollup path.  Below this threshold we fall back to the legacy
# material_risk.score_material_exposure() path.
#
# Threshold history:
#   2026-05-06: Set to 2 — initial conservative value to avoid single-node
#               noise feeding the rollup.
#   2026-05-09: Lowered to 1.  G6 audit measurement against the launch-10
#               materials showed 22 of 36 scored (material × country) pairs
#               (61%) fell to legacy fallback at threshold=2, because most
#               materials only have ore-stage scoring.  Single-node rollup
#               is mathematically clean — the formula
#               ``(node × stage_weight) / stage_weight`` collapses to
#               ``node`` exactly — and the Option-1 event-only fallback
#               (2026-05-06) handles materials with no production-share
#               data so single-node noise from empty materials is not a
#               concern.  See ``docs/scoring-audit-2026-05.md`` G6.
_STAGE_ROLLUP_MIN_NODES = 1

# ── Facility-presence floor for country_concentration fallback ──────────
# Set when no MaterialProductionShare row exists for the (material, country)
# pair but MRDS has at least one FacilityMaterialLink row for that material
# in that country.  See _derive_market_geopolitical_inputs for full
# rationale.  0.02 sits below the smallest USGS-tracked producer share
# (Cobalt AU = 0.01) so it's clearly a presence marker, not a real share.
# Contribution to pillar: 0.40 × 0.02 = 0.8 score points out of 100.
_FACILITY_PRESENCE_FLOOR = 0.02

# ── Pink Sheet (commodity_prices) thresholds — short-window ─────────────
# CV (coefficient of variation = std/mean) over the look-back window
# scaled into base_filing_signal (0-40).
_PRICE_CV_MAX = 0.50   # CV >= 0.50 → full signal (40)
# Directional price-change thresholds for stress signals.
_PRICE_SPIKE_PCT  = 0.20   # +20% in window → buyer leverage stress
_PRICE_CRASH_PCT  = 0.20   # -20% in window → producer liquidity stress
# Look-back window for the Pink Sheet trend signals (days).
_PRICE_WINDOW_DAYS = 180

# ── Fig 10 (USGS MCS price growth rates) thresholds — annual / 5-yr ────
# Fig 10 ships single-number annual % change + 5-yr CAGR per material.
# Both values are signed fractions (e.g. 1.44 = +144%).  Mapped to the
# same three sub-signals as Pink Sheet, then combined via max() so:
#   * materials with no Pink Sheet coverage (most of them) still get a
#     financial-pressure signal from Fig 10;
#   * materials with Pink Sheet coverage get whichever signal is
#     stronger (no false dampening from the lower of two estimates).
# Thresholds chosen to match observed MCS 2026 magnitudes (Antimony
# +144%, Bismuth +270%, Germanium +106%): a YoY swing of 50% or
# 5-yr CAGR of 30% is "extreme" and saturates the contribution.
_FIG10_YOY_SPIKE_PCT  = 0.50   # +50% YoY → max leverage_warning_bonus contribution
_FIG10_YOY_CRASH_PCT  = 0.50   # -50% YoY → max liquidity_stress_bonus contribution
_FIG10_CAGR_VOL_MAX   = 0.30   # |CAGR| >= 30% → full base_filing_signal contribution

_MAX_EVENT_IMPACT = 1.56

# Source priority for MaterialCriticalitySignal — higher index = lower priority.
# Removed 2026-05-06: ``iea_report`` (the iea_reports.py ingester was deleted —
# it overlapped with usgs_mcs for criticality signals + had a SourceDocument
# schema bug that crashed the script on first run).  Add back if a future,
# more rigorous IEA extractor lands.
_CRITICALITY_SOURCE_PRIORITY: list[str] = [
    "eu_crma",
    "usgs_mcs",
    "manual",
    "patstat",
]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_best_criticality_signal(
    db: Session,
    material_id: int,
    as_of_date: date,
) -> Optional[MaterialCriticalitySignal]:
    """Return the most authoritative MaterialCriticalitySignal for this material.

    Selects the latest reference_year row for the highest-priority source that
    has a non-null criticality_score. Falls back through the source hierarchy
    defined in ``_CRITICALITY_SOURCE_PRIORITY``.
    """
    stmt = (
        select(MaterialCriticalitySignal)
        .where(
            MaterialCriticalitySignal.material_id == material_id,
            MaterialCriticalitySignal.criticality_score.is_not(None),
            MaterialCriticalitySignal.reference_year <= as_of_date.year,
        )
        .order_by(MaterialCriticalitySignal.reference_year.desc())
    )
    rows = list(db.scalars(stmt).all())
    if not rows:
        return None

    # Select highest-priority source among rows
    for preferred_source in _CRITICALITY_SOURCE_PRIORITY:
        for row in rows:
            if row.source == preferred_source:
                return row

    # Fallback: return most recent row regardless of source
    return rows[0]


def _event_impact(
    ew: EventWithRelevance,
    category: RiskCategory,
    as_of_date: date,
) -> float:
    ev_date = ew.event.event_date.date() if ew.event.event_date else as_of_date
    recency = compute_recency_multiplier(category, ev_date, as_of_date)
    return compute_event_impact(
        # 11.4-Op (2026-06-06): changed `or 0.5` → `or 0.0` to remove the
        # silent midpoint inflation when an event lands with NULL
        # severity_score.  All current parsers explicitly set severity,
        # but the schema column is nullable so this is defensive.  No-
        # data-no-signal is consistent with the Material 11.4 fix.
        severity=float(ew.event.severity_score or 0.0),
        confidence=float(ew.event.confidence_score or 0.5),
        recency_multiplier=recency,
        # ew.relevance_score is on [0, 1]; map to the [0.70, 1.30]
        # multiplier domain compute_event_impact validates against.
        relevance_multiplier=relevance_score_to_multiplier(ew.relevance_score),
    )


def _avg_impact_normalised(
    events: list[EventWithRelevance],
    category: RiskCategory,
    as_of_date: date,
) -> float:
    if not events:
        return 0.0
    impacts = [_event_impact(ew, category, as_of_date) for ew in events]
    return min(1.0, sum(impacts) / len(impacts) / _MAX_EVENT_IMPACT)


def _classify_geo_events(
    events: list[EventWithRelevance],
) -> tuple[list[EventWithRelevance], list[EventWithRelevance], list[EventWithRelevance]]:
    """Split events into (export_restriction, tariff, subsidy) buckets.

    Subsidy bucket added 2026-05-09 (G-Cov-3).  Detection is event_subtype-only:
    title-text heuristics are unreliable here (a sentence like "U.S. EXIM
    Bank finances export contract for X" does not contain the word
    "subsidy" but is exactly the kind of event we want to capture).  GTA
    and IEA Policy Tracker ingesters set ``event_subtype="EXPORT_SUBSIDY"``
    on the relevant rows; pre-2026-05-09 events have NULL subtype and
    will need a re-ingest to flow into this bucket.
    """
    export_events: list[EventWithRelevance] = []
    tariff_events: list[EventWithRelevance] = []
    subsidy_events: list[EventWithRelevance] = []
    for ew in events:
        subtype = ew.event.event_subtype or ""  # typed col (migration 040)
        text = (ew.event.title or "").lower()
        if subtype == "EXPORT_SUBSIDY":
            subsidy_events.append(ew)
        elif subtype == "EXPORT_RESTRICTION" or (
            "export" in text and ("restrict" in text or "ban" in text or "control" in text)
        ):
            export_events.append(ew)
        elif subtype in ("TARIFF", "TRADE_POLICY") or (
            "tariff" in text or "trade policy" in text or "section 301" in text
        ):
            tariff_events.append(ew)
    return export_events, tariff_events, subsidy_events


def _dedup_events(*event_lists: list[EventWithRelevance]) -> list[EventWithRelevance]:
    by_id: dict[int, EventWithRelevance] = {}
    for lst in event_lists:
        for ew in lst:
            existing = by_id.get(ew.event.id)
            if existing is None or ew.relevance_score > existing.relevance_score:
                by_id[ew.event.id] = ew
    return list(by_id.values())


# ---------------------------------------------------------------------------
# Market-level input derivation functions
# ---------------------------------------------------------------------------

def _derive_market_material_inputs(
    db: Session,
    material_id: int,
    criticality_signal: Optional[MaterialCriticalitySignal],
    geography_code: str,
    trade_events: list[EventWithRelevance],
    as_of_date: date,
) -> tuple[float, float, float, dict]:
    """
    Returns (criticality, concentration, trade_volatility, sub_input_diagnostic)
    where the three numeric values are each on [0, 1.0] and
    ``sub_input_diagnostic`` is a dict reporting which components were data-
    backed vs defaulted.

    criticality:
        Blend of production HHI criticality_score (70%) and a reserve scarcity
        signal derived from reserve_life_index (30%).

        Reserve scarcity thresholds:
          RLI <= 20 years  → scarcity_signal = 1.0  (near-term constraint)
          RLI >= 80 years  → scarcity_signal = 0.0  (abundant; not a near-term risk)
          Linear interpolation between 20 and 80.

        Defaults (pre-11.4 vs post-11.4):
          criticality_score absent: pre 0.5 / post 0.0 (no data = no signal)
          reserve_life_index absent: pre 0.5 / post 0.0 (no data = no signal)

    concentration:
        Five-component composite, each [0, 1]:
          0.45 × production HHI (hhi_score)
          0.15 × reserve HHI    (reserve_hhi_score) — forward-looking concentration
          0.25 × producer signal (geography's actual MCS share of this material;
                                   falls back to facility-presence floor 0.02 if
                                   MRDS knows about a facility here, else 0.0)
          0.10 × capacity stress (capacity_utilization normalised; high util = tight market)
          0.05 × supply trend    (production YoY contraction only; growth = no extra risk)
        All weights sum to 1.0.

        11.4 (2026-06) fix
        ------------------
        Pre-11.4 every absent component defaulted to a neutral 0.5 midpoint,
        which silently inflated concentration scores for materials with thin
        MCS coverage.  Per the 11.0 coverage matrix, this affected Cobalt /
        Lithium / Manganese / Natural Graphite / Phosphate / REE the most.
        11.4 changes the defaults to 0.0 (no data = no signal), consistent
        with the supply_trend default that was already on this pattern.
        Affected components: prod_hhi, reserve_hhi, capacity_stress.

        The producer_signal component already used 0.0 as its terminal
        fallback (no MCS share + no facility) — that pattern is preserved.

        Producer-signal history: this sub-input was a hardcoded binary HCG flag
        (1.0 if geography ∈ {CN, CD, RU}, else 0.0) prior to 2026-05-12, which
        forced CD to "max concentration" for materials it doesn't produce
        (Aluminum, Iron Ore, Manganese, Graphite, Nickel, Phosphate, REE) and
        RU on Lithium/Manganese.  Same phantom-producer problem the Geopolitical
        pillar's country_concentration had — fixed in parallel with the same
        Option-B (MCS-share + facility-presence floor) approach.

    trade_volatility:
        Average normalised event_impact for GEOPOLITICAL_TRADE events for this
        (material, geography) pair. Default 0.3 when no events.

    sub_input_diagnostic (11.4.C — 2026-06):
        Dict mirroring the 11.6 data_completeness pattern at the sub-input
        level.  Reports which of the five concentration components plus the
        two criticality components and the trade_volatility were data-backed
        vs defaulted, so the Level-1 rationale_json can surface "this
        material's concentration was computed from 3 of 5 real components"
        without forcing the caller to re-derive the same checks.
    """
    sig = criticality_signal  # alias for brevity

    # ── criticality ──────────────────────────────────────────────────────────
    # 11.4 (2026-06): default changed 0.5 → 0.0 for both components.  See
    # function docstring for the bias-correction rationale.
    if sig and sig.criticality_score is not None:
        hhi_criticality = float(sig.criticality_score)
        crit_score_data_backed = True
    else:
        log.warning(
            "market_aggregator.no_criticality_signal",
            geography_code=geography_code,
        )
        hhi_criticality = 0.0
        crit_score_data_backed = False

    # Reserve scarcity signal: lower RLI = higher scarcity risk.
    _RLI_HIGH = 80.0  # years — effectively no near-term scarcity concern
    _RLI_LOW  = 20.0  # years — meaningful constraint horizon
    if sig and sig.reserve_life_index is not None:
        rli = float(sig.reserve_life_index)
        scarcity_signal = max(0.0, min(1.0, (_RLI_HIGH - rli) / (_RLI_HIGH - _RLI_LOW)))
        rli_data_backed = True
    else:
        scarcity_signal = 0.0
        rli_data_backed = False

    criticality = 0.70 * hhi_criticality + 0.30 * scarcity_signal

    # ── concentration ────────────────────────────────────────────────────────
    # Producer-signal component (renamed from "hcg_component" 2026-05-12).
    # Was: hardcoded {CN, CD, RU} binary. Now: actual MCS share for this
    # (material, country) pair, with the same facility-presence floor we
    # apply in the Geopolitical pillar's country_concentration fallback.
    # See the function docstring for the phantom-producer rationale.
    _producer_share_row = db.scalar(
        select(MaterialProductionShare)
        .where(
            MaterialProductionShare.material_id == material_id,
            MaterialProductionShare.country_code == geography_code,
            MaterialProductionShare.production_share > 0,
        )
        .order_by(MaterialProductionShare.reference_year.desc())
        .limit(1)
    )
    if _producer_share_row is not None:
        producer_signal = float(_producer_share_row.production_share)
        producer_source = "mcs_share"
    else:
        # No MCS row — check facility-presence floor.
        from app.models.facility import Facility, FacilityMaterialLink

        _has_facility = db.scalar(
            select(FacilityMaterialLink.id)
            .join(Facility, Facility.id == FacilityMaterialLink.facility_id)
            .where(
                FacilityMaterialLink.material_id == material_id,
                Facility.country == geography_code,
            )
            .limit(1)
        )
        if _has_facility is not None:
            producer_signal = _FACILITY_PRESENCE_FLOOR
            producer_source = "facility_floor"
        else:
            producer_signal = 0.0
            producer_source = "no_data"

    # 11.4.B (2026-06): defaults for prod_hhi / reserve_hhi changed 0.5 → 0.0
    # (no data = no signal) to remove the silent score-inflation for
    # materials where MCS doesn't compute HHI.
    if sig and sig.hhi_score is not None:
        prod_hhi = float(sig.hhi_score)
        prod_hhi_data_backed = True
    else:
        prod_hhi = 0.0
        prod_hhi_data_backed = False

    if sig and sig.reserve_hhi_score is not None:
        res_hhi = float(sig.reserve_hhi_score)
        res_hhi_data_backed = True
    else:
        res_hhi = 0.0
        res_hhi_data_backed = False

    # Capacity utilization stress: >= 0.90 → 1.0 (very tight), <= 0.50 → 0.0 (slack).
    # 11.4.A (2026-06): default changed 0.5 → 0.0 (no data = no signal),
    # consistent with supply_trend below.
    _CAP_HIGH = 0.90
    _CAP_LOW  = 0.50
    if sig and sig.capacity_utilization is not None:
        cap_util = float(sig.capacity_utilization)
        cap_stress = max(0.0, min(1.0, (cap_util - _CAP_LOW) / (_CAP_HIGH - _CAP_LOW)))
        cap_stress_data_backed = True
    else:
        cap_stress = 0.0
        cap_stress_data_backed = False

    # Supply trend: only contractions are a risk signal; growth eases pressure.
    # Max meaningful contraction for single-year shift: ~15%.
    # Default already 0.0 pre-11.4 (the pattern the other defaults moved to).
    _YOY_CONTRACTION_MAX = 0.15
    if sig and sig.production_yoy_pct is not None:
        yoy_contraction = max(0.0, -float(sig.production_yoy_pct))  # positive = contraction
        trend_stress = min(1.0, yoy_contraction / _YOY_CONTRACTION_MAX)
        trend_data_backed = True
    else:
        trend_stress = 0.0
        trend_data_backed = False

    concentration = (
        0.45 * prod_hhi
        + 0.15 * res_hhi
        + 0.25 * producer_signal
        + 0.10 * cap_stress
        + 0.05 * trend_stress
    )
    concentration = min(1.0, concentration)

    # ── trade_volatility ─────────────────────────────────────────────────────
    if not trade_events:
        trade_volatility = 0.3
        trade_data_backed = False
    else:
        trade_volatility = _avg_impact_normalised(
            trade_events, RiskCategory.GEOPOLITICAL_TRADE, as_of_date
        )
        trade_data_backed = True

    # ── 11.4.C (2026-06) — per-sub-input diagnostic ──────────────────────────
    # Mirrors the 11.6 data_completeness pattern at the sub-input level.
    # Caller (score_material_geography) folds this into rationale_json so
    # partner-facing UI can show "concentration was computed from N of 5
    # real components" rather than just reporting the final number.
    sub_input_diagnostic = {
        "criticality": {
            "criticality_score_data_backed": crit_score_data_backed,
            "reserve_life_index_data_backed": rli_data_backed,
        },
        "concentration": {
            "production_hhi_data_backed":  prod_hhi_data_backed,
            "reserve_hhi_data_backed":     res_hhi_data_backed,
            "producer_signal_source":      producer_source,  # mcs_share / facility_floor / no_data
            "capacity_stress_data_backed": cap_stress_data_backed,
            "supply_trend_data_backed":    trend_data_backed,
        },
        "trade_volatility": {
            "trade_event_data_backed": trade_data_backed,
            "trade_event_count": len(trade_events),
        },
    }

    return criticality, concentration, trade_volatility, sub_input_diagnostic


def _derive_market_geopolitical_inputs(
    db: Session,
    material_id: int,
    geography_code: str,
    geo_trade_events: list[EventWithRelevance],
    as_of_date: date,
    *,
    eligible_nodes: Optional[list["HsCodeGeographyRiskScore"]] = None,
) -> tuple[float, float, float, Optional[float], str, dict]:
    """
    Returns (country_concentration, export_restriction_exposure, tariff_exposure,
             production_subsidy_distortion, geopolitical_method,
             sub_input_diagnostic).

    All numeric values are on [0, 1.0].  ``production_subsidy_distortion`` is
    ``None`` when no EXPORT_SUBSIDY events exist for this (material ×
    geography) pair (triggers the 3-component scoring profile in
    ``geopolitical_risk.score_geopolitical_trade``); any numeric value
    (including 0.0) triggers the 4-component profile.  Added 2026-05-09
    as part of the G-Cov-3 audit fix.

    11.4-Geo (2026-06-06): the 6th return value, ``sub_input_diagnostic``,
    mirrors the Material pillar's 11.4-C dict.  Reports which sub-inputs
    were data-backed vs defaulted so the partner UI can show "this
    Geopolitical score is computed from real MCS + real tariff events,
    but no export-restriction events fired" instead of just the final
    number.

    Note on a known asymmetric default:  When ``subsidy_distortion is
    None`` (no subsidy events for this material × country), the scoring
    function drops the 10% subsidy weight and implicitly redistributes
    it across the other three sub-inputs.  A country with NO subsidy
    data therefore scores ~3.5 points higher than the same country with
    subsidy_distortion explicitly equal to 0.0.  This is functionally
    a "midpoint default" via weight redistribution — same family of
    bias as the 0.5-midpoint defaults the Material pillar audit
    removed in 11.4-A/B.  The 11.4-Geo audit chose to KEEP this math
    (per the "fill data gaps over re-math" preference) and surface the
    state via ``sub_input_diagnostic["subsidy_data_backed"]`` so the
    bias is visible rather than silent.

    ``geopolitical_method`` is one of:

        "event_classification"  — only the legacy geography-anchored event
                                  path fired (HS nodes absent or below the
                                  ``_STAGE_ROLLUP_MIN_NODES`` threshold).
        "max_with_hs_nodes"     — both paths fired; we kept the higher of
                                  (HS-aggregated, event-classified) for each
                                  sub-score to avoid losing signal from
                                  geography-only events that aren't
                                  HS-attributed.

    country_concentration:
        The geography's share of global production for this material from
        MaterialProductionShare (most recent reference year).  Uses the
        fraction directly — e.g. China ≈ 0.70 for Graphite, Chile ≈ 0.30 for
        Lithium — so scores differentiate meaningfully across geographies
        and materials.  Falls back to the legacy HCG binary flag for
        well-known concentrated geographies (CN, CD, RU) when no production
        share data exists.

    export_restriction_exposure / tariff_exposure:
        Two complementary signal paths combined via max():

        1. Event classification path: scans ``geo_trade_events`` (events
           tagged to this geography for the GEOPOLITICAL_TRADE category)
           and classifies via ``event_subtype`` (typed col, migration 040)
           or title keywords.  Geography-anchored — captures events that
           reach this country regardless of HS attribution.

        2. HS-node aggregate path (May 2026 — G2 fix): when
           ``eligible_nodes`` >= ``_STAGE_ROLLUP_MIN_NODES``, computes a
           stage-weighted average of ``node.tariff_exposure`` and
           ``node.export_restriction`` across HS stages using
           ``STAGE_ROLLUP_WEIGHTS`` (the same weights Material Concentration
           uses).  Material × stage scoped — more precise than the
           geography-anchored event path.

        Combination via ``max()`` is intentional: the HS-node path is more
        precise where it has signal, but events tagged only to the
        geography (no HS code attribution) appear in path 1 and not 2.
        Taking max() ensures neither contribution is silently dropped.
    """
    # Primary: production share from MaterialProductionShare (USGS MCS).
    share_row = db.scalar(
        select(MaterialProductionShare)
        .where(
            MaterialProductionShare.material_id == material_id,
            MaterialProductionShare.country_code == geography_code,
            MaterialProductionShare.production_share > 0,
        )
        .order_by(MaterialProductionShare.reference_year.desc())
        .limit(1)
    )
    if share_row is not None:
        country_concentration = float(share_row.production_share)
        country_concentration_source = "mcs_share"
    else:
        # Secondary: facility-presence floor (2026-05-12, audit fix).
        # USGS MCS only reports producers above ~1% of world output, so any
        # country with a smaller-but-real footprint (e.g. Sri Lanka graphite)
        # gets no MCS row at all.  Previously the code fell back to a
        # hardcoded HIGH_CONCENTRATION_GEOS = {CN, CD, RU} set that returned
        # country_concentration = 1.0 — a phantom-producer signal that
        # forced CD to "max concentration" for materials it doesn't produce
        # (Aluminum, Iron Ore, Manganese, Graphite, Nickel, Phosphate, REE)
        # and RU on Lithium / Manganese.  The HCG fallback inflated 85-94%
        # of scored (material, country) pairs across the launch list.
        #
        # New behavior: if MRDS knows about at least one facility for this
        # material in this country, return a small floor value (0.02) to
        # acknowledge "we know there's some production here, just below
        # USGS's reporting threshold."  Otherwise 0.0 (no signal).
        #
        # Floor calibration: 0.02 sits below the smallest USGS-tracked
        # share (Cobalt AU = 0.01) so it's identifiable as a presence
        # marker rather than a real share.  Combined with the pillar's
        # 0.40 weight on country_concentration that's worth 0.8 score
        # points out of 100 — appreciable but not dominating.
        from app.models.facility import Facility, FacilityMaterialLink

        _has_facility = db.scalar(
            select(FacilityMaterialLink.id)
            .join(Facility, Facility.id == FacilityMaterialLink.facility_id)
            .where(
                FacilityMaterialLink.material_id == material_id,
                Facility.country == geography_code,
            )
            .limit(1)
        )
        if _has_facility is not None:
            country_concentration = _FACILITY_PRESENCE_FLOOR
            fallback_label = "facility_presence_floor"
            country_concentration_source = "facility_floor"
        else:
            country_concentration = 0.0
            fallback_label = "no_signal"
            country_concentration_source = "no_data"
        log.debug(
            "market_aggregator.geo.production_share_fallback",
            material_id=material_id,
            geography_code=geography_code,
            fallback_label=fallback_label,
            fallback_value=country_concentration,
        )

    # ── Path 1: event classification (geography-anchored) ──────────────────
    export_events, tariff_events, subsidy_events = _classify_geo_events(
        geo_trade_events
    )
    event_export = _avg_impact_normalised(
        export_events, RiskCategory.GEOPOLITICAL_TRADE, as_of_date
    )
    event_tariff = _avg_impact_normalised(
        tariff_events, RiskCategory.GEOPOLITICAL_TRADE, as_of_date
    )

    # ── Production-subsidy distortion (G-Cov-3, 2026-05-09) ────────────────
    # EXPORT_SUBSIDY events flow into a separate Geopolitical sub-input
    # rather than tariff/export — subsidies don't restrict trade, they
    # distort downstream competition.  Score is None (not 0.0) when no
    # subsidy events exist so the Geopolitical pillar falls back to the
    # legacy 3-component profile; once any subsidy event lands, the
    # 4-component profile fires.  See docs/coverage-gap-plan-2026-05.md
    # § G-Cov-3.
    if subsidy_events:
        subsidy_distortion: Optional[float] = _avg_impact_normalised(
            subsidy_events, RiskCategory.GEOPOLITICAL_TRADE, as_of_date
        )
    else:
        subsidy_distortion = None

    # ── Path 2: HS-node aggregate (material × stage scoped) — G2 fix ───────
    # Skip when no nodes were passed (caller hasn't fetched them) or when
    # there are too few to be representative.  Falls through to event-only.
    method = "event_classification"
    hs_export = 0.0
    hs_tariff = 0.0
    hs_path_fired = False
    if eligible_nodes is not None and len(eligible_nodes) >= _STAGE_ROLLUP_MIN_NODES:
        # Stage-weighted average of HS-node sub-scores, normalised by the
        # sum of weights for stages actually present.  Same shape as
        # Material Concentration's stage rollup (lines 1278–1290).
        weight_total = sum(
            STAGE_ROLLUP_WEIGHTS[n.hs_mapping.supply_chain_stage]
            for n in eligible_nodes
        )
        if weight_total > 0:
            hs_tariff = sum(
                (n.tariff_exposure or 0.0)
                * STAGE_ROLLUP_WEIGHTS[n.hs_mapping.supply_chain_stage]
                for n in eligible_nodes
            ) / weight_total
            hs_export = sum(
                (n.export_restriction or 0.0)
                * STAGE_ROLLUP_WEIGHTS[n.hs_mapping.supply_chain_stage]
                for n in eligible_nodes
            ) / weight_total
            # Combine via max — see docstring rationale.
            export_exposure = max(event_export, hs_export)
            tariff_exposure = max(event_tariff, hs_tariff)
            method = "max_with_hs_nodes"
            hs_path_fired = True
            log.debug(
                "market_aggregator.geo.hs_node_aggregate",
                material_id=material_id,
                geography_code=geography_code,
                node_count=len(eligible_nodes),
                event_tariff=event_tariff,
                hs_tariff=hs_tariff,
                event_export=event_export,
                hs_export=hs_export,
            )
        else:
            export_exposure = event_export
            tariff_exposure = event_tariff
    else:
        export_exposure = event_export
        tariff_exposure = event_tariff

    # ── 11.4-Geo (2026-06) — per-sub-input diagnostic ──────────────────────
    # Mirrors the 11.4-Material `sub_input_diagnostic` shape so the partner
    # UI can render both pillars with the same template.
    sub_input_diagnostic = {
        "country_concentration": {
            # data_backed = True only when MCS share is present.  The
            # facility-presence floor (0.02) is a structural marker, not
            # a quantitative share — we expose it as a separate source
            # label rather than asserting it's "data-backed" in the same
            # sense as a real MCS row.
            "data_backed": country_concentration_source == "mcs_share",
            "source": country_concentration_source,  # mcs_share / facility_floor / no_data
        },
        "export_restriction": {
            # data_backed = True iff EITHER path produced a non-zero signal.
            # Zero from both paths after the max() means we genuinely have
            # no data, not a default-induced zero.
            "data_backed": (event_export > 0.0) or (hs_export > 0.0),
            "source": _classify_path_source(
                event_value=event_export, hs_value=hs_export,
                hs_path_fired=hs_path_fired,
            ),
        },
        "tariff": {
            "data_backed": (event_tariff > 0.0) or (hs_tariff > 0.0),
            "source": _classify_path_source(
                event_value=event_tariff, hs_value=hs_tariff,
                hs_path_fired=hs_path_fired,
            ),
        },
        "subsidy": {
            # The 3-comp / 4-comp scoring asymmetry is the flag here.
            # data_backed=False means scoring used the 3-component profile
            # and silently redistributed the 10% subsidy weight across
            # the other terms — see function docstring.
            "data_backed": subsidy_distortion is not None,
            "scoring_profile": (
                "4_component" if subsidy_distortion is not None
                else "3_component"
            ),
        },
    }

    return (
        country_concentration, export_exposure, tariff_exposure,
        subsidy_distortion, method, sub_input_diagnostic,
    )


def _classify_path_source(
    *, event_value: float, hs_value: float, hs_path_fired: bool,
) -> str:
    """Return the source label for export_restriction / tariff sub-inputs.

    The Geopolitical pillar combines an event-classification path with an
    HS-node aggregate path via ``max()``.  Partner-facing UI wants to know
    which path actually contributed the signal, which max() alone hides.

    Returns one of:

      ``"both"``        — both paths produced a positive signal; max() picked one
      ``"events_only"`` — events>0, HS path either didn't fire or was 0
      ``"hs_only"``     — HS path>0, no event signal
      ``"neither"``     — both zero (no data either way)
    """
    has_events = event_value > 0.0
    has_hs = hs_path_fired and hs_value > 0.0
    if has_events and has_hs:
        return "both"
    if has_events:
        return "events_only"
    if has_hs:
        return "hs_only"
    return "neither"


def _resolve_compliance_weight(
    geo_weights: Optional[dict],
    geography_code: str,
) -> float:
    """Resolve a per-geography compliance risk weight from the regulation's JSONB column.

    Lookup order:
      1. Exact ISO2 match in ``geo_weights``
      2. "DEFAULT" fallback within ``geo_weights``
      3. Universal 0.50 default when the column is NULL or empty

    Returns a value in [0.0, 1.0].

    11.4-Reg-A REVERT (2026-06-06): briefly flipped to 0.0 then reverted
    after discovering ALL 8 partner-tier regulations (UFLPA, EU Battery
    Reg, CRMA, IRA Domestic, EU CSDDD, EU REACH Cobalt, EU CBAM, EU
    Conflict Minerals) have NULL ``geography_compliance_weights`` today.
    Flipping the implicit fallback to 0.0 would have zeroed the
    obligation_score component everywhere — exactly counter to the
    "fill data gaps over re-math" policy.

    The diagnostic added in 11.4-Reg-B (see
    ``_derive_market_regulatory_inputs``) surfaces the gap via
    ``obligations.default_weight_count``.  Partner-side workflow:
    populate the JSONB tables (per-regulation per-country weights),
    then revisit the math change once the default fallback is
    actually rarely-fired rather than the dominant path.
    """
    if not geo_weights:
        return 0.50
    return float(geo_weights.get(geography_code, geo_weights.get("DEFAULT", 0.50)))


def _derive_market_regulatory_inputs(
    db: Session,
    material_id: int,
    geography_code: str,
    as_of_date: date,
) -> tuple[list[float], list[tuple[str, float]], float, dict]:
    """
    Returns (top_event_impacts, scope_obligations, policy_proximity_adjustment,
             sub_input_diagnostic).

    11.4-Reg (2026-06-06): added the 4th return value, ``sub_input_diagnostic``,
    mirroring the Material/Geopolitical/Operational 11.4 pattern.  Surfaces
    visibility for three silent score-shaping behaviours the pillar has
    today: uncurated regulation weights, top-3 event truncation, and the
    40-point obligation cap.

    scope_obligations:
        Regulations linked via RegulationMaterialScope to this material OR via
        RegulationGeographyScope to this geography, each paired with its resolved
        compliance risk weight for ``geography_code``.

        Weights come from ``Regulation.geography_compliance_weights`` (JSONB):
          - Exact ISO2 match → that weight
          - "DEFAULT" key → fallback weight
          - NULL column (not yet curated) → 0.50 universal default

        11.4-Reg-A REVERT (2026-06-06): briefly flipped the NULL fallback
        to 0.0 then reverted after discovering all 8 partner-tier
        regulations have NULL JSONB today.  The diagnostic continues to
        surface ``default_weight_count`` so the curation gap is visible.

    top_event_impacts:
        Normalised event_impact values for regulatory events scoped to this
        material + geography. Passed to regulatory_risk.score_regulatory_profile()
        which uses ONLY the top 3.  The diagnostic surfaces both the total
        count and the top-3 used so partner UI can show "scored from 3 of 12."

    policy_proximity_adjustment:
        1.15 if any event has an effective_date within 90 days of as_of_date,
        else 1.0.  Diagnostic exposes the count of events with effective_date
        metadata so partner can distinguish "no imminent regulations" from
        "the parser didn't populate effective_date for any of these events."

    sub_input_diagnostic:
        {
          "obligations": {
            "data_backed": bool,                   # True iff any curated_weight_count > 0
            "total_count": int,                    # all scoped regulations
            "curated_weight_count": int,           # regulations whose weight came from the JSONB
            "default_weight_count": int,           # regulations that hit the 0.0 fallback (11.4-Reg-A)
            "raw_obligation_score": float,         # uncapped sum (post-weight, pre-cap)
            "capped_at_40": bool,                  # whether the cap fired
          },
          "events": {
            "data_backed": bool,                   # True iff total_count > 0
            "total_count": int,                    # all regulatory events found
            "top_3_used_count": int,               # 0/1/2/3 — the scoring subset
          },
          "proximity": {
            "adjustment_active": bool,             # True iff 1.15 was applied
            "events_with_effective_date_count": int,
          },
        }
    """
    from app.models.regulatory import (
        Regulation,
        RegulationGeographyScope,
        RegulationMaterialScope,
        RiskEventRegulation,
    )
    from app.services.scoring.regulatory_risk import COMPLIANCE_OBLIGATIONS
    from datetime import datetime as _dt

    # Scope-derived regulations (material + geography).
    # weights maps regulation_key → resolved compliance risk weight for this geography.
    # weight_sources tracks where each weight came from: "curated" (from the JSONB)
    # or "default" (the 0.0 fallback that fires when JSONB is NULL/empty).
    weights: dict[str, float] = {}
    weight_sources: dict[str, str] = {}

    def _record_weight(key: str, geo_weights: Optional[dict]) -> None:
        """Resolve + record the weight along with its source label."""
        resolved = _resolve_compliance_weight(geo_weights, geography_code)
        prev = weights.get(key, -1.0)
        if resolved > prev:
            weights[key] = resolved
            # Source = "curated" iff the JSONB column was populated for ANY
            # path, regardless of whether the resolved value happened to be
            # 0.  An explicit ``{"US": 0.0}`` is partner-curated information
            # (we know US is out of scope) and should not be conflated with
            # the "no JSONB at all" 0.0 fallback.
            weight_sources[key] = "curated" if geo_weights else "default"

    mat_stmt = (
        select(Regulation.regulation_key, Regulation.geography_compliance_weights)
        .join(RegulationMaterialScope, RegulationMaterialScope.regulation_id == Regulation.id)
        .where(RegulationMaterialScope.material_id == material_id)
    )
    for key, geo_weights in db.execute(mat_stmt).all():
        _record_weight(key, geo_weights)

    geo_stmt = (
        select(Regulation.regulation_key, Regulation.geography_compliance_weights)
        .join(RegulationGeographyScope, RegulationGeographyScope.regulation_id == Regulation.id)
        .where(RegulationGeographyScope.country_code == geography_code)
    )
    for key, geo_weights in db.execute(geo_stmt).all():
        _record_weight(key, geo_weights)

    scope_obligations = sorted(weights.items())

    curated_weight_count = sum(
        1 for src in weight_sources.values() if src == "curated"
    )
    default_weight_count = sum(
        1 for src in weight_sources.values() if src == "default"
    )

    # Mirror the obligation-score math in score_regulatory_profile so we can
    # surface raw vs capped values.  The scorer applies COMPLIANCE_OBLIGATIONS
    # × weight, sums, then caps at 40.
    raw_obligation_score = sum(
        COMPLIANCE_OBLIGATIONS.get(ob_key, 0) * weight
        for ob_key, weight in scope_obligations
    )
    capped_at_40 = raw_obligation_score > 40.0

    # Events for scoped regulations
    reg_keys = set(weights.keys())
    reg_events = (
        get_events_for_regulations(db, reg_keys, as_of_date)
        if reg_keys
        else []
    )

    top_event_impacts: list[float] = []
    policy_proximity_adjustment = 1.0
    events_with_effective_date_count = 0

    for ew in reg_events:
        effective_date: Optional[date] = None
        meta = ew.event.metadata_json or {}
        raw_eff = meta.get("effective_date")
        if raw_eff and isinstance(raw_eff, str):
            try:
                effective_date = _dt.fromisoformat(raw_eff).date()
                events_with_effective_date_count += 1
            except ValueError:
                pass

        if effective_date is not None:
            days_to_effective = (effective_date - as_of_date).days
            if 0 <= days_to_effective <= 90:
                policy_proximity_adjustment = 1.15

        impact = _event_impact(ew, RiskCategory.REGULATORY_COMPLIANCE, as_of_date)
        top_event_impacts.append(impact)

    # ── 11.4-Reg (2026-06) — per-sub-input diagnostic ──────────────────────
    sub_input_diagnostic = {
        "obligations": {
            "data_backed": curated_weight_count > 0,
            "total_count": len(scope_obligations),
            "curated_weight_count": curated_weight_count,
            "default_weight_count": default_weight_count,
            "raw_obligation_score": round(raw_obligation_score, 2),
            "capped_at_40": capped_at_40,
        },
        "events": {
            "data_backed": len(top_event_impacts) > 0,
            "total_count": len(top_event_impacts),
            "top_3_used_count": min(3, len(top_event_impacts)),
        },
        "proximity": {
            "adjustment_active": policy_proximity_adjustment > 1.0,
            "events_with_effective_date_count": events_with_effective_date_count,
        },
    }

    return (
        top_event_impacts, scope_obligations, policy_proximity_adjustment,
        sub_input_diagnostic,
    )


# ---------------------------------------------------------------------------
# Stage-aware operational structural-dependency (G4 Half 1 audit fix, 2026-05-06)
# ---------------------------------------------------------------------------
# Statuses considered "at risk" (curtailed supply) and "production assets"
# (the denominator pool).  "closed" is intentionally excluded from both:
# MRDS closed/historical records frequently date back decades and represent
# permanently lost capacity, not curtailed supply — including them would
# inflate the denominator with irrelevant history and conflate "mine shut
# forever" with "mine temporarily idled."  The N4 audit fix (2026-05-06)
# also stops ingesting closed MRDS rows, so for new data this is moot;
# kept here as defense for any older rows still in the DB.
_AT_RISK_STATUSES = frozenset({"mothballed"})
_PRODUCTION_ASSET_STATUSES = frozenset({"operating", "mothballed"})


def _facility_structural_dependency(
    db: Session,
    material_id: int,
    geography_code: Optional[str],
) -> Optional[dict]:
    """Compute stage-aware structural dependency from facility data.

    Returns ``None`` when no facilities exist for the (material, geography)
    pair.  Otherwise returns a structured dict::

        {
            "weighted":      float,                # stage-weighted dep (0-1)
            "stages":        {                     # per-stage breakdown
                "ore":          {"dependency": 0.20, "method": "count_based",
                                 "n_at_risk": 2, "n_total": 10, "weight": 0.10},
                "refined":      {"dependency": 0.50, "method": "count_based",
                                 "n_at_risk": 1, "n_total": 2,  "weight": 0.25},
                ...
            },
            "stages_used":   ["ore", "refined"],   # keys present in stages
            "method":        "stage_weighted",     # vs legacy mixed-stage
        }

    The per-stage dependency uses the same two-tier logic as before
    (capacity-weighted when ``annual_capacity_tpy`` is populated, else
    count-based).  The cross-stage rollup uses ``STAGE_ROLLUP_WEIGHTS``
    (same weights the Material Concentration pillar's HS-node rollup
    uses), normalised by the sum of weights of stages that actually
    have data — so a material with only ore-stage facilities returns
    that ore-stage dependency, not a default-filled 0.3.

    G4 Half 1 design choice (2026-05-06, partner direction): no
    default-fill across missing stages.  If a material has no refining
    facilities in our data, refining contributes nothing to the rollup;
    the score reflects what's measurable, and the gap is visible via
    ``stages_used`` in rationale_json.

    Facilities with ``supply_chain_stage IS NULL`` are excluded.  They
    represent old MRDS rows ingested before the N4 fix added stage
    classification, or rows whose stage couldn't be inferred from
    ``oper_type`` or facility name.  Re-running ingest-mrds after the
    N4 fix backfills these.

    Geography filter: applied when ``geography_code`` is provided, skipped
    when ``None`` (enables a global material-level fallback at the
    caller).
    """
    from app.models.facility import Facility, FacilityMaterialLink
    from sqlalchemy import func as sqlfunc

    base_filter = [
        FacilityMaterialLink.material_id == material_id,
        FacilityMaterialLink.supply_chain_stage.is_not(None),
        # Restrict to stages we know how to weight; drops unrecognised
        # values rather than KeyError'ing on the rollup.
        FacilityMaterialLink.supply_chain_stage.in_(STAGE_ROLLUP_WEIGHTS.keys()),
    ]
    if geography_code:
        base_filter.append(Facility.country == geography_code)

    # ── Pull all facility-stage rows in a single query ────────────────────
    # Returns one row per (stage, status, capacity-bucket).  We aggregate in
    # Python rather than SQL because the per-stage tier-1/tier-2 logic is
    # cleaner expressed as Python dispatch.
    rows = db.execute(
        select(
            FacilityMaterialLink.supply_chain_stage.label("stage"),
            Facility.status.label("status"),
            FacilityMaterialLink.annual_capacity_tpy.label("capacity"),
        )
        .join(Facility, Facility.id == FacilityMaterialLink.facility_id)
        .where(*base_filter)
    ).all()

    if not rows:
        return None

    # ── Bucket by stage, then by capacity-known vs unknown, by status ─────
    # stage_buckets[stage] = {
    #   "tpy_total": float, "tpy_at_risk": float,    # capacity-weighted side
    #   "n_total":   int,   "n_at_risk":   int,      # count-based side
    # }
    from collections import defaultdict
    stage_buckets: dict[str, dict[str, float]] = defaultdict(
        lambda: {"tpy_total": 0.0, "tpy_at_risk": 0.0, "n_total": 0, "n_at_risk": 0}
    )
    for row in rows:
        stage = row.stage
        if row.status not in _PRODUCTION_ASSET_STATUSES:
            # Skip planned / under_construction / unknown — not in the live
            # supply pool we're measuring.
            continue
        bucket = stage_buckets[stage]
        bucket["n_total"] += 1
        if row.status in _AT_RISK_STATUSES:
            bucket["n_at_risk"] += 1
        if row.capacity is not None:
            bucket["tpy_total"] += float(row.capacity)
            if row.status in _AT_RISK_STATUSES:
                bucket["tpy_at_risk"] += float(row.capacity)

    if not stage_buckets:
        return None  # No facilities matched the production-asset statuses

    # ── Compute per-stage dependency using the two-tier logic ─────────────
    stages_detail: dict[str, dict] = {}
    for stage, b in stage_buckets.items():
        stage_weight = STAGE_ROLLUP_WEIGHTS[stage]
        if b["tpy_total"] > 0:
            # Tier 1: capacity-weighted.  Reserved for when capacity data
            # arrives via G4c (partner-curated) or G4d (paid sub).
            dep = min(1.0, b["tpy_at_risk"] / b["tpy_total"])
            method = "capacity_weighted"
        elif b["n_total"] > 0:
            # Tier 2: count-based (the active path for MRDS data today).
            dep = min(1.0, b["n_at_risk"] / b["n_total"])
            method = "count_based"
        else:
            continue   # Stage had only non-production-asset facilities; skip.
        stages_detail[stage] = {
            "dependency": round(dep, 4),
            "method":     method,
            "n_at_risk":  b["n_at_risk"],
            "n_total":    b["n_total"],
            "weight":     stage_weight,
        }

    if not stages_detail:
        return None

    # ── Cross-stage rollup: weighted average over stages with data ────────
    # No default-fill for missing stages (G4 Half 1 partner direction):
    # divide by the sum of weights of stages that actually contributed.
    weight_sum = sum(s["weight"] for s in stages_detail.values())
    weighted_dep = (
        sum(s["dependency"] * s["weight"] for s in stages_detail.values())
        / weight_sum
    )

    log.debug(
        "market_aggregator.facility_structural_dependency",
        material_id=material_id,
        geography_code=geography_code,
        method="stage_weighted",
        stages_used=list(stages_detail.keys()),
        weighted_dependency=round(weighted_dep, 4),
    )

    return {
        "weighted":     round(weighted_dep, 4),
        "stages":       stages_detail,
        "stages_used":  sorted(stages_detail.keys()),
        "method":       "stage_weighted",
    }


def _derive_market_operational_inputs(
    db: Session,
    material_id: int,
    geography_code: str,
    operational_events: list[EventWithRelevance],
    as_of_date: date,
) -> tuple[Optional[float], list[float], str, Optional[dict], dict]:
    """
    Returns (structural_dependency, weighted_event_impacts, dep_source,
    stage_breakdown, sub_input_diagnostic).

    ``structural_dependency`` is ``None`` when no facility data and no
    capacity-constraint events exist for the (material, geography) pair
    — added 2026-05-12 as a follow-up to the Step 2 audit so the
    operational pillar doesn't impute a silent 12-point ghost from the
    legacy 0.3 placeholder.  Callers (``_score_operational_market``)
    redistribute pillar weight to events when this is None.

    11.4-Op (2026-06-06): the 5th return value, ``sub_input_diagnostic``,
    mirrors the Material/Geopolitical 11.4 dict pattern.  Reports which
    sub-inputs are data-backed plus a null-severity event count surfacing
    the `severity_score or 0.0` fallback (changed from `or 0.5` in 11.4
    to remove a silent midpoint inflation when severity is NULL).

    structural_dependency — three-tier resolution:

      1. MRDS geography-level: stage-weighted fraction of production-asset sites
         that are mothballed.  G4 Half 1 (2026-05-06) made this stage-aware —
         the per-stage dependency is rolled up using ``STAGE_ROLLUP_WEIGHTS``
         (ore=0.10, concentrate=0.15, intermediate=0.20, refined=0.25,
         battery_grade=0.30) over only the stages that have data.  No
         default-fill for missing stages — partner direction is to honestly
         reflect what's measurable.

      2. MRDS global fallback (DISABLED 2026-05-12): when no MRDS sites exist
         for this specific geography, used to try the global material-level
         fraction discounted by 0.5.  Now disabled to prevent cross-country
         contamination; the path falls straight through to Tier 3.

      3. Event baseline: if no MRDS data exists for this material at all,
         fall back to SINGLE_SOURCE / CAPACITY_CONSTRAINT event severity.
         When that's also empty, struct_dep is set to ``None`` (no signal).

    weighted_event_impacts:
        event_impact for each operational event, regardless of structural_dependency
        source. Events and capacity data are complementary, not redundant.

    dep_source:
        Provenance tag stored in rationale_json so post-run queries can identify
        which (material, geography) pairs are hitting the conservative default
        rather than real facility data.  One of:
            "mrds_geography_stage_weighted"           — stage-weighted fraction for this geo
            "event_derived"                            — capacity-constraint event severity avg
            "no_signal"                                — no MRDS, no events; struct_dep=None

        (The legacy "mrds_global_discounted_stage_weighted" and "default_0.3"
        labels are dead code post-2026-05-12 and have been removed from this
        list.)

    stage_breakdown:
        The structured dict from ``_facility_structural_dependency`` describing
        the per-stage dependency contributions.  ``None`` when struct_dep was
        derived from events or default (Tier 3).  Surfaced into rationale_json
        so consumers can see which stages had data and where the gaps are.
    """
    dep_source: str
    stage_breakdown: Optional[dict] = None
    struct_dep: Optional[float] = None

    # Tier 1: geography-specific MRDS stage-weighted fraction
    geo_result = _facility_structural_dependency(db, material_id, geography_code)
    if geo_result is not None:
        struct_dep = geo_result["weighted"]
        stage_breakdown = geo_result
        dep_source = "mrds_geography_stage_weighted"

    # Tier 2 (DISABLED 2026-05-12, Step 2 audit Fix B): global MRDS
    # stage-weighted fraction × 0.5.  This path imputed a global structural-
    # dependency value to countries with no per-material facility data,
    # which mathematically produces JP-style inversions (a country with no
    # graphite mines inheriting the global graphite mothballed-fraction).
    # Currently the fallback returns 0 because no facility in the DB carries
    # a mothballed status — Tier 2 is therefore moot in practice — but the
    # principled fix is to skip it: countries with no facility data should
    # surface as a coverage gap (Tier 3 / default), not be imputed.  When
    # the MRDS parser fix (ticket #61) lands status-diverse data, this gate
    # prevents accidental cross-country contamination on day one.
    #
    # If a future analysis wants a global comparison baseline, prefer
    # rendering it as a UI annotation rather than folding it into the
    # per-country score.

    # Tier 3: event-derived baseline — when no MRDS data exists for this material
    if struct_dep is None:
        struct_events = [
            ew for ew in operational_events
            if (ew.event.event_subtype or "") in (  # typed col (migration 040)
                "SINGLE_SOURCE", "CAPACITY_CONSTRAINT"
            ) or any(
                kw in (ew.event.title or "").lower()
                for kw in ("single source", "single-source", "capacity constraint")
            )
        ]
        if struct_events:
            # 11.4-Op (2026-06-06): same severity-default fix as line 226.
            struct_dep = sum(
                float(ew.event.severity_score or 0.0) for ew in struct_events
            ) / len(struct_events)
            dep_source = "event_derived"
        else:
            # Final tier: no MRDS data AND no capacity-constraint events.
            # 2026-05-12 (Step 2 audit follow-up): switched from default
            # 0.3 placeholder to None.  The placeholder was contributing a
            # silent 12-point ghost (0.40 weight × 0.3 default × 100) to
            # every (material, country) pair lacking facility data, which
            # for the current launch list means every pair.  None signals
            # honest "we don't have the data to score this," and
            # _score_operational_market redistributes the operational
            # pillar to 100% event-component when struct_dep is None.
            # Revisit once curated facility data (G4c track) populates
            # status-diverse MRDS rows.
            struct_dep = None
            dep_source = "no_signal"
            log.debug(
                "market_aggregator.structural_dependency_missing",
                material_id=material_id,
                geography_code=geography_code,
                note=(
                    "No MRDS facility data and no capacity-constraint events; "
                    "structural_dependency = None.  Operational pillar will "
                    "score from events only."
                ),
            )

    weighted_event_impacts = [
        _event_impact(ew, RiskCategory.OPERATIONAL, as_of_date)
        for ew in operational_events
    ]

    # 11.4-Op (2026-06-06): count NULL-severity operational events so the
    # partner UI can flag when the `severity_score or 0.0` fallback fires.
    # When the count is >0, those events effectively contributed 0 impact
    # — surface this so it's visible rather than silent.
    null_severity_event_count = sum(
        1 for ew in operational_events if ew.event.severity_score is None
    )

    # G-Cov-2 (2026-05-06): EXPORT_RESTRICTION events curtail supply, which
    # is operationally meaningful — but they're already tagged on the
    # Geopolitical category and feed that pillar via the HS-node scorer.
    # Fold them into the operational event component at HALF WEIGHT so the
    # operational pillar reflects the supply-curtailment signal without
    # double-counting the same event across two pillars at full weight.
    # Strict country attribution: requires a `RiskEventGeography` row with
    # `geography_context="primary"` matching the target geography (the G11
    # convention where the implementing country IS the producer for export-
    # side interventions).  Events without geography rows are excluded —
    # consistent with the G11 strict-attribution philosophy.
    export_restriction_impacts = _export_restriction_operational_impacts(
        db, material_id, geography_code, as_of_date
    )
    weighted_event_impacts.extend(export_restriction_impacts)

    # ── 11.4-Op (2026-06) — per-sub-input diagnostic ──────────────────────
    # Same shape as Material/Geopolitical 11.4 diagnostics.  Partner UI
    # can render any pillar with the same template.
    #
    # data_backed semantics:
    #   structural_dependency:
    #     True  iff source == "mrds_geography_stage_weighted"
    #     False for event_derived and no_signal (events shouldn't claim
    #     they're "structural" data — they're event-derived proxies).
    #   event_impacts:
    #     True iff at least one operational event of any flavor exists.
    sub_input_diagnostic = {
        "structural_dependency": {
            "data_backed": dep_source == "mrds_geography_stage_weighted",
            "source": dep_source,  # mrds_geography_stage_weighted / event_derived / no_signal
        },
        "event_impacts": {
            "data_backed": len(weighted_event_impacts) > 0,
            "operational_event_count": len(operational_events),
            # G-Cov-2 fold-in visibility: how many of the impacts in this
            # pillar are "shadow" half-weighted EXPORT_RESTRICTION events
            # vs primary operational events.
            "export_restriction_event_count": len(export_restriction_impacts),
            # 11.4-Op visibility: how many operational events had NULL
            # severity (now resolved to 0.0 instead of the old 0.5
            # midpoint).  A non-zero count means those events were
            # effectively dropped from the score; partner should consider
            # whether they're parser bugs worth backfilling.
            "null_severity_event_count": null_severity_event_count,
        },
        # The scoring math redistributes 100% to events when struct_dep is
        # None.  Surfacing the active scoring profile makes it explicit:
        # is this score a 40/60 blend, or events-only?
        "scoring_profile": (
            "events_only" if struct_dep is None
            else "structural_plus_events"
        ),
    }

    return (
        struct_dep, weighted_event_impacts, dep_source, stage_breakdown,
        sub_input_diagnostic,
    )


# G-Cov-2 (2026-05-06): half-weight applied to EXPORT_RESTRICTION events
# that feed the Operational pillar.  The same event already contributes to
# the Geopolitical pillar at full weight via the HS-node scorer; half-
# weighting here prevents double-counting while still letting supply-
# curtailment signals raise operational scores on materials where MRDS
# coverage is thin.
_EXPORT_RESTRICTION_OPERATIONAL_WEIGHT = 0.5


def _export_restriction_operational_impacts(
    db: Session,
    material_id: int,
    geography_code: str,
    as_of_date: date,
) -> list[float]:
    """Return half-weighted operational-event impacts for EXPORT_RESTRICTION
    events scoped to (material, geography).

    Country attribution mirrors the G11 strict path in hs_node_scorer:
    require a ``RiskEventGeography`` row with
    ``geography_context="primary"`` matching ``geography_code``.  The
    implementing country IS the producer for export-side interventions
    (China imposes graphite export controls → CN is the producer being
    constrained); ``"primary"`` is exactly that tagging convention.

    Returns an empty list when no qualifying events exist.
    """
    from app.models.regulatory import (
        RiskEvent,
        RiskEventGeography,
        RiskEventMaterial,
    )

    rows = db.execute(
        select(RiskEvent)
        .join(RiskEventMaterial, RiskEventMaterial.risk_event_id == RiskEvent.id)
        .join(RiskEventGeography, RiskEventGeography.risk_event_id == RiskEvent.id)
        .where(
            RiskEventMaterial.material_id == material_id,
            RiskEvent.event_subtype == "EXPORT_RESTRICTION",
            RiskEventGeography.country_code == geography_code,
            RiskEventGeography.geography_context == "primary",
        )
        .distinct()
    ).scalars().all()

    if not rows:
        return []

    impacts: list[float] = []
    for ev in rows:
        # Wrap each RiskEvent in an EventWithRelevance shim so we reuse the
        # existing _event_impact helper.  relevance_score=1.0 because the
        # half-weight is applied below, not in the relevance dimension.
        ew = EventWithRelevance(event=ev, relevance_score=1.0)
        full_impact = _event_impact(ew, RiskCategory.OPERATIONAL, as_of_date)
        impacts.append(full_impact * _EXPORT_RESTRICTION_OPERATIONAL_WEIGHT)
    return impacts


def _score_operational_market(
    struct_dep: Optional[float],
    op_impacts: list[float],
) -> float:
    """Operational pillar: structural dependency + weighted event rollup.

    Default weights: 40% structural_dependency, 60% events.  When
    ``struct_dep`` is None (no facility data AND no capacity-constraint
    events for this material × geography — see
    ``_derive_market_operational_inputs`` Tier-3 note), the pillar
    redistributes to 100% event-component rather than imputing a
    placeholder.  This makes "we don't know" visibly score as the
    event signal alone — countries with no events under this regime
    score 0, which is honest given the data state.

    Result capped at 100.
    """
    event_component = sum(op_impacts) / len(op_impacts) if op_impacts else 0.0
    if struct_dep is None:
        # No facility signal: pillar is 100% event-driven.
        raw = event_component * 100
    else:
        raw = (0.40 * struct_dep + 0.60 * event_component) * 100
    return min(100.0, raw)


def _derive_company_financial_signal(
    db: Session,
    material_id: int,
    as_of_date: date,
    *,
    geography_code: Optional[str] = None,
) -> tuple[float, float, list[dict]]:
    """
    Compute a production-share-weighted average of SEC EDGAR company financial
    pressure scores for producers of this material.

    Returns (weighted_avg_fp, coverage_weight, detail_records).

    weighted_avg_fp (0-100):
        Σ(company.financial_pressure_score × production_share) / Σ(production_share)
        for companies whose source_geography matches the target geography (or all
        producing countries when geography_code is None).
        Returns 0.0 when no qualifying company scores exist.

    coverage_weight (0.0–1.0):
        Sum of production shares represented by companies with SEC filings,
        capped at 1.0.  When geography_code is provided this reflects what
        fraction of that geography's production is backed by SEC filings.
        Materials/geographies where no producers file with the SEC contribute
        nothing (Gallium, Germanium, most CN-only producers).

    geography_code:
        When provided, restricts the signal to companies whose source_geography
        matches this code.  This ensures that Chilean Lithium producers' financial
        health only influences the CL geography score, not the AU or CN scores.
        When None, aggregates across all producing countries (backward-compatible).

    detail_records:
        List of per-company dicts for rationale_json. Empty when no data.

    Coverage notes (as of 2025):
        Strong:  Li (Albemarle, SQM), Co/Cu (Freeport, Vale), Ni (BHP, Rio Tinto)
        Partial: REE (MP Materials), Mn, Ti
        Thin:    Natural Graphite (CATL not SEC-registered), PGMs (Sibanye is JSE)
        Zero:    Gallium, Germanium, Tellurium, Indium — no major SEC filers
    """
    from app.models.company import Company, CompanyMaterialExposure, CompanyScore
    from sqlalchemy import func as sqlfunc

    # Step 1: Production shares for most recent reference year, optionally
    # filtered to a single geography.
    latest_year_subq = (
        select(sqlfunc.max(MaterialProductionShare.reference_year))
        .where(MaterialProductionShare.material_id == material_id)
        .scalar_subquery()
    )
    share_filters = [
        MaterialProductionShare.material_id == material_id,
        MaterialProductionShare.reference_year == latest_year_subq,
        MaterialProductionShare.production_share > 0,
    ]
    if geography_code:
        share_filters.append(MaterialProductionShare.country_code == geography_code)

    share_stmt = select(
        MaterialProductionShare.country_code,
        MaterialProductionShare.production_share,
    ).where(*share_filters)
    shares_by_country: dict[str, float] = {
        row.country_code: float(row.production_share)
        for row in db.execute(share_stmt).all()
    }

    if not shares_by_country:
        return 0.0, 0.0, []

    producing_countries = list(shares_by_country.keys())

    # Step 2: Companies with exposure to this material whose source_geography
    # matches a producing country
    exposure_stmt = select(
        Company.id,
        Company.canonical_name,
        CompanyMaterialExposure.source_geography,
        CompanyMaterialExposure.exposure_score,
    ).join(
        CompanyMaterialExposure, CompanyMaterialExposure.company_id == Company.id
    ).where(
        CompanyMaterialExposure.material_id == material_id,
        CompanyMaterialExposure.source_geography.in_(producing_countries),
    )
    exposure_rows = db.execute(exposure_stmt).all()

    if not exposure_rows:
        return 0.0, 0.0, []

    # Step 3: Latest financial_pressure_score for each company
    company_ids = list({row.id for row in exposure_rows})
    latest_score_subq = (
        select(
            CompanyScore.company_id,
            sqlfunc.max(CompanyScore.as_of_date).label("max_date"),
        )
        .where(
            CompanyScore.company_id.in_(company_ids),
            CompanyScore.financial_pressure_score.is_not(None),
            CompanyScore.as_of_date <= as_of_date,
        )
        .group_by(CompanyScore.company_id)
        .subquery()
    )
    score_stmt = select(
        CompanyScore.company_id,
        CompanyScore.financial_pressure_score,
    ).join(
        latest_score_subq,
        (CompanyScore.company_id == latest_score_subq.c.company_id)
        & (CompanyScore.as_of_date == latest_score_subq.c.max_date),
    )
    scores_by_company: dict = {
        row.company_id: float(row.financial_pressure_score)
        for row in db.execute(score_stmt).all()
    }

    # Step 4: Per-company, keep the source_geography with the highest production
    # share so each company counts exactly once in the weighted sum.
    best_by_company: dict = {}  # company_id → (name, geo, prod_share, fp_score)
    for exp_row in exposure_rows:
        company_id = exp_row.id
        geo = exp_row.source_geography
        fp_score = scores_by_company.get(company_id)
        if fp_score is None:
            continue  # No CompanyScore — SEC coverage gap
        prod_share = shares_by_country.get(geo, 0.0)
        if prod_share <= 0.0:
            continue
        existing = best_by_company.get(company_id)
        if existing is None or prod_share > existing[2]:
            best_by_company[company_id] = (exp_row.canonical_name, geo, prod_share, fp_score)

    if not best_by_company:
        return 0.0, 0.0, []

    # Step 5: Weighted average
    weighted_sum = 0.0
    weight_sum = 0.0
    details: list[dict] = []
    for company_id, (name, geo, prod_share, fp_score) in best_by_company.items():
        weighted_sum += fp_score * prod_share
        weight_sum += prod_share
        details.append({
            "company": name,
            "geography": geo,
            "production_share": round(prod_share, 4),
            "financial_pressure_score": round(fp_score, 2),
            "weighted_contribution": round(fp_score * prod_share, 4),
        })

    weighted_avg = weighted_sum / weight_sum
    coverage_weight = min(1.0, weight_sum)

    log.debug(
        "market_aggregator.company_financial_signal",
        material_id=material_id,
        geography_code=geography_code,
        companies_used=len(details),
        weighted_avg_fp=round(weighted_avg, 2),
        coverage_weight=round(coverage_weight, 4),
    )
    return weighted_avg, coverage_weight, details


# Maximum contribution (in base_filing_signal points) from the company-weighted
# SEC signal. Scaled by coverage_weight so materials with thin SEC coverage
# contribute proportionally less. 15 pts out of a 40-pt max = 37.5% ceiling
# when coverage is perfect — leaves room for price volatility to dominate.
_COMPANY_SIGNAL_MAX_CONTRIBUTION = 15.0


def _derive_market_financial_inputs(
    db: Session,
    material_id: int,
    geography_code: str,
    as_of_date: date,
) -> tuple[float, float, float, int, dict, dict]:
    """
    Market-level financial pressure sub-inputs.

    Returns a 6-tuple: (base_filing_signal, leverage_warning_bonus,
    liquidity_stress_bonus, evidence_count, company_signal_meta,
    sub_input_diagnostic).

    ``company_signal_meta`` is a dict for rationale_json — it is NOT passed to
    ``fp_module.score_financial_pressure()``, which still takes the first four
    values unchanged.

    11.4-Fin-A (2026-06-06): the 6th return value, ``sub_input_diagnostic``,
    mirrors the Material/Geopolitical/Operational/Regulatory 11.4 pattern.
    Surfaces visibility for the four silent score-shaping behaviours this
    pillar has:

      * Tier-source attribution for each of the three numeric sub-components
        (Tier 1 Pink Sheet vs Tier 1.5 Fig 10 vs Tier 2 events vs Tier 3
        SEC EDGAR) so partner can see which signal source dominated.
      * Sparse-evidence cap state — ``filing_count < 2`` halves the score
        in ``score_financial_pressure``; the diagnostic exposes both the
        boolean and the cap factor (0 / 0.5 / 1.0).
      * Tier 3 SEC EDGAR 15-pt cap firing — when the company signal would
        have moved ``base_filing_signal`` more than 15 points but was capped.
      * Coverage counts (Pink Sheet points / Fig 10 present / event count /
        SEC EDGAR coverage_weight) so the partner UI can render a
        "this score was computed from N price points, M events, and SEC
        EDGAR coverage of X% of producers" sentence.

    11.4-Fin-B (2026-06-06): ``filing_count`` was renamed ``evidence_count``
    in the public scorer signature.  The local variable here is still
    ``filing_count`` to minimise churn in the existing code paths;
    semantically it's the same value.

    Signal sources (four tiers — Tier 1.5 added May 2026):
    ─────────────────────────────────────────────────────────────────────
    Tier 1 — Commodity price series (Pink Sheet, daily):

    base_filing_signal (0-40)   ← price VOLATILITY
        CV = std(prices) / mean(prices) over _PRICE_WINDOW_DAYS.
        Scaled: min(CV / _PRICE_CV_MAX, 1.0) × 40.

    leverage_warning_bonus (0-30) ← price SPIKE
        If price rose > _PRICE_SPIKE_PCT over the window, buyers face
        increased procurement costs.

    liquidity_stress_bonus (0-30) ← price CRASH
        If price fell > _PRICE_CRASH_PCT, producers face margin pressure.

    Tier 1.5 — USGS MCS Fig 10 price growth rates (annual + 5-yr CAGR):
        Reads ``material_criticality_signals.{price_yoy_pct, price_cagr_5yr_pct}``
        for the material.  Combined with Tier 1 via ``max()`` so:
          * materials without daily Pink Sheet coverage (most of them)
            still produce a non-zero financial pressure score, and
          * materials with Pink Sheet coverage take the stronger signal.
        |CAGR| augments base_filing_signal (volatility proxy);
        signed YoY augments leverage_warning_bonus (positive) or
        liquidity_stress_bonus (negative).  Detail in ``fig10_signal``
        block of the rationale meta dict.

    Tier 2 — FINANCIAL_PRESSURE events tagged to this material:
        "price_surge" / "market_squeeze" → adds to leverage_warning_bonus
        "producer_exit" / "mine_closure" / "bankruptcy" → liquidity_stress_bonus
        generic → base_filing_signal

    Tier 3 — SEC EDGAR producer scores weighted by production share:
        Σ(company.financial_pressure_score × production_share) / Σ(production_share)
        Scaled to at most _COMPANY_SIGNAL_MAX_CONTRIBUTION (15 pts) on
        base_filing_signal, further multiplied by coverage_weight (fraction
        of world production represented by companies with SEC filings).
        Materials with no SEC filers (Gallium, Germanium, …) contribute 0.

    filing_count:
        Price data points + event count. Drives sparse-evidence cap in scorer.
    ─────────────────────────────────────────────────────────────────────
    Falls back gracefully when no price or company data exists — returns
    (0, 0, 0, 0, {}) which scores to 0.0 (data gap, not a false signal).
    """
    from datetime import timedelta

    from sqlalchemy import and_

    from app.models.supply import CommodityPrice

    window_start = as_of_date - timedelta(days=_PRICE_WINDOW_DAYS)

    price_stmt = (
        select(CommodityPrice.price_date, CommodityPrice.price_usd)
        .where(
            and_(
                CommodityPrice.material_id == material_id,
                CommodityPrice.price_date >= window_start,
                CommodityPrice.price_date <= as_of_date,
            )
        )
        .order_by(CommodityPrice.price_date.asc())
    )
    price_rows = db.execute(price_stmt).all()

    base_filing_signal: float = 0.0
    leverage_warning_bonus: float = 0.0
    liquidity_stress_bonus: float = 0.0
    filing_count = len(price_rows)

    # 11.4-Fin-A tier-source tracking.  Each flag records whether the
    # corresponding tier contributed a non-zero signal to that sub-input.
    # The final diagnostic translates the flag combinations into a single
    # source label (pink_sheet / fig10 / events / company / multiple / none).
    base_pink_sheet_contrib = 0.0
    base_fig10_contrib = 0.0
    base_events_contrib = 0.0
    base_company_contrib_uncapped = 0.0   # for the 15-pt cap visibility
    base_company_contrib = 0.0            # post-cap

    lev_pink_sheet_contrib = 0.0
    lev_fig10_contrib = 0.0
    lev_events_contrib = 0.0

    liq_pink_sheet_contrib = 0.0
    liq_fig10_contrib = 0.0
    liq_events_contrib = 0.0

    if filing_count >= 2:
        prices = [float(row.price_usd) for row in price_rows]
        mean_price = sum(prices) / len(prices)

        if mean_price > 0:
            import statistics
            std_price = statistics.stdev(prices)
            cv = std_price / mean_price
            base_filing_signal = min(1.0, cv / _PRICE_CV_MAX) * 40.0
            base_pink_sheet_contrib = base_filing_signal

        # Directional trend: compare last price vs. first price
        pct_change = (prices[-1] - prices[0]) / prices[0] if prices[0] > 0 else 0.0

        if pct_change > 0:
            # Price spike → buyer leverage stress
            leverage_warning_bonus = min(1.0, pct_change / _PRICE_SPIKE_PCT) * 30.0
            lev_pink_sheet_contrib = leverage_warning_bonus
        else:
            # Price crash → producer liquidity stress
            liquidity_stress_bonus = min(1.0, abs(pct_change) / _PRICE_CRASH_PCT) * 30.0
            liq_pink_sheet_contrib = liquidity_stress_bonus

    # ── Tier 1.5 — USGS MCS Fig 10 price growth rates (annual + 5-yr) ──
    # Materials with no Pink Sheet coverage still get a financial-pressure
    # signal here; materials with Pink Sheet coverage take the max of the
    # two so the stronger source wins.  Reads the latest usgs_mcs signal
    # row (the row ingest-mcs-prices wrote price_yoy_pct / price_cagr
    # onto).  Returns None gracefully when the signal row doesn't exist
    # for this material — the contribution is then 0.
    fig10_signal_row = db.execute(
        select(
            MaterialCriticalitySignal.price_yoy_pct,
            MaterialCriticalitySignal.price_cagr_5yr_pct,
            MaterialCriticalitySignal.reference_year,
        ).where(
            MaterialCriticalitySignal.material_id == material_id,
            MaterialCriticalitySignal.source == "usgs_mcs",
            MaterialCriticalitySignal.price_yoy_pct.is_not(None),
        )
        .order_by(MaterialCriticalitySignal.reference_year.desc())
        .limit(1)
    ).first()

    fig10_meta: dict = {}
    if fig10_signal_row is not None:
        f_yoy = (
            float(fig10_signal_row.price_yoy_pct)
            if fig10_signal_row.price_yoy_pct is not None
            else None
        )
        f_cagr = (
            float(fig10_signal_row.price_cagr_5yr_pct)
            if fig10_signal_row.price_cagr_5yr_pct is not None
            else None
        )

        # CAGR magnitude → base_filing_signal proxy.  Long-run CAGR is not
        # the same as short-window CV (Pink Sheet's volatility metric)
        # but high |CAGR| reliably indicates the material's price has
        # been moving — directionally if not chaotically — over the
        # 5-yr window.  Cap at _FIG10_CAGR_VOL_MAX = 30%.
        if f_cagr is not None:
            cagr_signal = min(1.0, abs(f_cagr) / _FIG10_CAGR_VOL_MAX) * 40.0
            base_filing_signal = max(base_filing_signal, cagr_signal)
            base_fig10_contrib = cagr_signal
            fig10_meta["cagr_contribution_to_base_filing_signal"] = round(cagr_signal, 2)

        # YoY signed → spike (positive) or crash (negative).
        if f_yoy is not None and f_yoy > 0:
            spike_signal = min(1.0, f_yoy / _FIG10_YOY_SPIKE_PCT) * 30.0
            leverage_warning_bonus = max(leverage_warning_bonus, spike_signal)
            lev_fig10_contrib = spike_signal
            fig10_meta["yoy_contribution_to_leverage_warning"] = round(spike_signal, 2)
        elif f_yoy is not None and f_yoy < 0:
            crash_signal = min(1.0, abs(f_yoy) / _FIG10_YOY_CRASH_PCT) * 30.0
            liquidity_stress_bonus = max(liquidity_stress_bonus, crash_signal)
            liq_fig10_contrib = crash_signal
            fig10_meta["yoy_contribution_to_liquidity_stress"] = round(crash_signal, 2)

        fig10_meta.update({
            "yoy_pct": round(f_yoy, 4) if f_yoy is not None else None,
            "cagr_5yr_pct": round(f_cagr, 4) if f_cagr is not None else None,
            "reference_year": fig10_signal_row.reference_year,
        })
        # Fig 10 contributes evidence regardless of whether Pink Sheet
        # had data — count it toward the sparse-evidence threshold so the
        # filing-count cap reflects the augmented signal.
        if filing_count == 0:
            filing_count = 1

    # Supplement with FINANCIAL_PRESSURE events tagged to this material
    fin_events = get_events_for_material(
        db, material_id, RiskCategory.FINANCIAL_PRESSURE, as_of_date
    )
    for ew in fin_events:
        subtype = ew.event.event_subtype or ""  # typed col (migration 040)
        text = (ew.event.title or "").lower()
        # 11.4-Op (2026-06-06): same severity-default fix as line 226.
        severity = float(ew.event.severity_score or 0.0)

        if subtype in ("PRICE_SURGE", "MARKET_SQUEEZE") or (
            "price surge" in text or "market squeeze" in text
        ):
            delta = min(30.0 - leverage_warning_bonus, severity * 10.0)
            leverage_warning_bonus += delta
            lev_events_contrib += delta
        elif subtype in ("PRODUCER_EXIT", "MINE_CLOSURE", "BANKRUPTCY") or (
            "producer exit" in text
            or "mine closure" in text
            or "mine shut" in text
            or "bankruptcy" in text
        ):
            delta = min(30.0 - liquidity_stress_bonus, severity * 10.0)
            liquidity_stress_bonus += delta
            liq_events_contrib += delta
        else:
            # Generic financial pressure event — contributes to base signal
            delta = min(40.0 - base_filing_signal, severity * 5.0)
            base_filing_signal += delta
            base_events_contrib += delta

    # Bump filing_count to include events so the sparse-evidence cap reflects
    # the full evidence pool (prices + events).
    filing_count = filing_count + len(fin_events)

    # Tier 3: SEC EDGAR producer scores weighted by production share,
    # filtered to companies whose source_geography matches this geography.
    company_weighted_fp, coverage_weight, company_details = _derive_company_financial_signal(
        db, material_id, as_of_date, geography_code=geography_code
    )
    company_contribution = (
        (company_weighted_fp / 100.0) * _COMPANY_SIGNAL_MAX_CONTRIBUTION * coverage_weight
    )
    base_company_contrib_uncapped = company_contribution
    pre_company_base = base_filing_signal
    base_filing_signal = min(40.0, base_filing_signal + company_contribution)
    base_company_contrib = base_filing_signal - pre_company_base

    company_signal_meta: dict = {
        "weighted_avg_fp": round(company_weighted_fp, 2),
        "coverage_weight": round(coverage_weight, 4),
        "contribution_to_base_signal": round(company_contribution, 2),
        "max_possible_contribution": _COMPANY_SIGNAL_MAX_CONTRIBUTION,
        "companies": company_details,
        "fig10_signal": fig10_meta or None,
        "note": (
            "SEC EDGAR company financial pressure scores weighted by production share. "
            "Coverage is strong for Li, Co, Cu, Ni; partial for REE, Mn; "
            "effectively zero for Ga, Ge, Te, In — coverage_weight reflects this. "
            "Fig 10 (USGS MCS price growth) signals are merged via max() against "
            "Pink Sheet so materials without daily-price coverage still produce a "
            "non-zero financial pressure score — see fig10_signal block."
        ),
    }

    log.debug(
        "market_aggregator.financial_inputs",
        material_id=material_id,
        price_points=len(price_rows),
        fin_events=len(fin_events),
        company_fp_weighted_avg=round(company_weighted_fp, 2),
        company_coverage=round(coverage_weight, 4),
        company_contribution=round(company_contribution, 2),
        base_signal=round(base_filing_signal, 2),
        leverage_bonus=round(leverage_warning_bonus, 2),
        liquidity_bonus=round(liquidity_stress_bonus, 2),
    )

    # ── 11.4-Fin-A (2026-06) — per-sub-input diagnostic ────────────────────
    # Compute the sparse-evidence cap state up-front so the diagnostic
    # mirrors what score_financial_pressure will apply.
    sparse_cap_applied = filing_count < 2
    sparse_cap_factor = (filing_count / 2.0) if sparse_cap_applied else 1.0
    # Tier 3 SEC EDGAR 15-pt cap.  ``base_company_contrib_uncapped`` is the
    # raw (company_weighted_fp/100) × 15 × coverage_weight value before the
    # 40-pt sub-input cap was applied.  But the 15-pt cap fires on the
    # contribution itself, NOT on the post-cap sub-input — so we compare
    # against _COMPANY_SIGNAL_MAX_CONTRIBUTION × coverage_weight.  In
    # practice the uncapped value is bounded by that same product (since
    # we already multiplied by coverage_weight), so the cap fires only
    # via the 40-pt outer min().
    tier_3_capped_at_15 = base_company_contrib_uncapped >= _COMPANY_SIGNAL_MAX_CONTRIBUTION

    sub_input_diagnostic = {
        "base_filing_signal": {
            "data_backed": base_filing_signal > 0.0,
            "source": _classify_fin_source(
                pink_sheet=base_pink_sheet_contrib,
                fig10=base_fig10_contrib,
                events=base_events_contrib,
                company=base_company_contrib,
            ),
            "pink_sheet_contribution":  round(base_pink_sheet_contrib, 2),
            "fig10_contribution":       round(base_fig10_contrib, 2),
            "events_contribution":      round(base_events_contrib, 2),
            "company_contribution":     round(base_company_contrib, 2),
            "tier_3_capped_at_15":      tier_3_capped_at_15,
        },
        "leverage_warning_bonus": {
            "data_backed": leverage_warning_bonus > 0.0,
            "source": _classify_fin_source(
                pink_sheet=lev_pink_sheet_contrib,
                fig10=lev_fig10_contrib,
                events=lev_events_contrib,
            ),
            "pink_sheet_contribution": round(lev_pink_sheet_contrib, 2),
            "fig10_contribution":      round(lev_fig10_contrib, 2),
            "events_contribution":     round(lev_events_contrib, 2),
        },
        "liquidity_stress_bonus": {
            "data_backed": liquidity_stress_bonus > 0.0,
            "source": _classify_fin_source(
                pink_sheet=liq_pink_sheet_contrib,
                fig10=liq_fig10_contrib,
                events=liq_events_contrib,
            ),
            "pink_sheet_contribution": round(liq_pink_sheet_contrib, 2),
            "fig10_contribution":      round(liq_fig10_contrib, 2),
            "events_contribution":     round(liq_events_contrib, 2),
        },
        "sparse_evidence_cap": {
            # The cap halves (or zeros) the final score when evidence_count
            # < 2.  Same family of silent score-shaping as the Regulatory
            # 40-pt cap + top-3 truncation; surface so partner can tell
            # "score is low because evidence is thin" from "score is low
            # because signal is weak."
            "applied": sparse_cap_applied,
            "factor": round(sparse_cap_factor, 2),
            "evidence_count": filing_count,
        },
        "coverage": {
            # Raw counts of what fed each tier.  Lets partner answer
            # "how many price points / events / SEC filers contributed?"
            # without parsing the full company_signal_meta block.
            "pink_sheet_price_points": len(price_rows),
            "fig10_signal_present": bool(fig10_meta),
            "fin_event_count": len(fin_events),
            "sec_edgar_coverage_weight": round(coverage_weight, 4),
            "sec_edgar_company_count": len(company_details) if company_details else 0,
        },
    }

    return (
        base_filing_signal, leverage_warning_bonus, liquidity_stress_bonus,
        filing_count, company_signal_meta, sub_input_diagnostic,
    )


def _classify_fin_source(
    *,
    pink_sheet: float = 0.0,
    fig10: float = 0.0,
    events: float = 0.0,
    company: float = 0.0,
) -> str:
    """Return a single-string source label for a Financial Pressure sub-input.

    Used by ``_derive_market_financial_inputs`` to give partner UI a
    concise summary of which tiers contributed.  Combines arbitrary tier
    contributions into one of six labels:

      ``"none"``         — every tier was 0.0
      ``"pink_sheet"``   — only Pink Sheet (Tier 1) contributed
      ``"fig10"``        — only Fig 10 (Tier 1.5) contributed
      ``"events"``       — only events (Tier 2) contributed
      ``"company"``      — only SEC EDGAR (Tier 3) contributed
      ``"multiple"``     — 2+ tiers contributed

    The "multiple" label is intentionally coarse — for forensic detail
    the per-tier numeric contributions are exposed alongside the label.
    """
    contribs = {
        "pink_sheet": pink_sheet,
        "fig10": fig10,
        "events": events,
        "company": company,
    }
    active = [name for name, v in contribs.items() if v > 0.0]
    if not active:
        return "none"
    if len(active) == 1:
        return active[0]
    return "multiple"


def _aggregate_market_score(
    mat_score: float,
    geo_score: float,
    reg_score: float,
    op_score: float,
    fin_score: float,
) -> float:
    """Weighted average of five active pillars using MARKET_PILLAR_WEIGHTS."""
    return (
        MARKET_PILLAR_WEIGHTS["material"]     * mat_score
        + MARKET_PILLAR_WEIGHTS["geopolitical"] * geo_score
        + MARKET_PILLAR_WEIGHTS["regulatory"]   * reg_score
        + MARKET_PILLAR_WEIGHTS["operational"]  * op_score
        + MARKET_PILLAR_WEIGHTS["financial"]    * fin_score
    )


# ---------------------------------------------------------------------------
# 11.6 (2026-06) — Per-pillar data completeness
# ---------------------------------------------------------------------------
# Background
# ----------
# ``_aggregate_market_score`` is a fixed-weight sum that uses each pillar's
# score as-is.  Each pillar function returns a score even when its inputs
# are missing — falling back to either neutral midpoints (Material
# Concentration components default around 0.3-0.5) or zero (Financial
# Pressure, Regulatory).  The asymmetric defaults silently push scores
# in different directions depending on which pillar is thin:
#
#   * Thin Material Concentration data -> neutral midpoint defaults pull
#     the score UP relative to a material with rich-and-low-actual-risk
#     data.
#   * Thin Financial Pressure / Regulatory data -> zero defaults pull
#     the score DOWN.
#
# The 2026-06 audit decided (Option E) to leave the scoring math alone
# for Phase 1 but emit a per-pillar data-completeness diagnostic so:
#
#   * Partner-facing UI can surface "data confidence" alongside the
#     score, letting customers see whether the number is backed by 5/5
#     real pillars or, say, 3/5 with two defaulting.
#   * Phase 1.5 has empirical data to validate whether the current
#     scoring math should switch to score-time renormalisation
#     (Option A/D) or per-pillar non-zero defaults (Option B).
#
# Definition
# ----------
# Per-pillar completeness is on [0.0, 1.0]: a fraction of the pillar's
# inputs that came from real measured data vs default fallbacks.  The
# value is informational only; it does NOT participate in the scoring
# math at this layer (11.6).
#
# Overall completeness is the MARKET_PILLAR_WEIGHTS-weighted sum of
# per-pillar values, so a material with all 5 pillars 100% real returns
# 1.0; a material with Financial Pressure 0% real returns 0.882
# (1.0 - 0.118 financial weight).


def _compute_pillar_data_completeness(
    criticality_signal,
    *,
    # Geopolitical inputs
    geo_country_concentration: float,
    geo_export_restriction: float,
    geo_tariff: float,
    # Regulatory inputs
    reg_top_event_count: int,
    reg_scope_obligation_count: int,
    # Operational inputs
    op_structural_dependency: float,
    op_event_count: int,
    # Financial Pressure inputs
    fin_evidence_count: int,
    fin_company_coverage: float,
) -> dict[str, float]:
    """Return a per-pillar + overall data-completeness diagnostic.

    Each per-pillar value is on ``[0.0, 1.0]`` representing the fraction
    of the pillar's input slots that came from real data rather than
    default fallbacks.  Overall is the MARKET_PILLAR_WEIGHTS-weighted
    mean of the five per-pillar values.

    11.6 (2026-06): does NOT change scoring math.  Result is recorded in
    the score's rationale_json for UI surfacing and Phase 1.5
    methodology review.
    """
    # ── Material Concentration ──
    # The Material Concentration pillar reads up to five fields from
    # MaterialCriticalitySignal: criticality_score, hhi_score,
    # reserve_hhi_score, capacity_utilization, production_yoy_pct.
    # Plus the reserve_life_index for the scarcity component.  Treat
    # each non-None field as one unit of completeness.
    if criticality_signal is None:
        material_completeness = 0.0
    else:
        slots = [
            criticality_signal.criticality_score is not None,
            criticality_signal.hhi_score is not None,
            criticality_signal.reserve_hhi_score is not None,
            criticality_signal.reserve_life_index is not None,
            criticality_signal.capacity_utilization is not None,
            criticality_signal.production_yoy_pct is not None,
        ]
        material_completeness = sum(slots) / len(slots)

    # ── Geopolitical / Trade ──
    # Three sub-inputs: country_concentration, export_restriction_exposure,
    # tariff_exposure.  country_concentration is "real" when it exceeds the
    # facility-presence floor (the engine returns _FACILITY_PRESENCE_FLOOR
    # = 0.02 when no MCS production share exists but a facility is known;
    # anything above that came from a real MCS share).  exp_rest / tariff
    # are real when non-zero (zero means no qualifying events).
    geo_slots = [
        geo_country_concentration > _FACILITY_PRESENCE_FLOOR,
        geo_export_restriction > 0.0,
        geo_tariff > 0.0,
    ]
    geopolitical_completeness = sum(geo_slots) / len(geo_slots)

    # ── Regulatory & Compliance ──
    # Real when at least one regulation is in scope for this
    # (material, geography) pair OR at least one scope obligation
    # applies.  Binary because the pillar function does not blend in
    # neutral defaults — it returns 0 when both inputs are absent.
    regulatory_completeness = 1.0 if (
        reg_top_event_count > 0 or reg_scope_obligation_count > 0
    ) else 0.0

    # ── Operational ──
    # Two inputs: structural_dependency (defaults to 0.3 floor when no
    # real signal) + operational event impacts (count == 0 when none).
    op_slots = [
        # struct_dep differs from 0.3 floor → real signal
        abs(op_structural_dependency - 0.3) > 0.001,
        op_event_count > 0,
    ]
    operational_completeness = sum(op_slots) / len(op_slots)

    # ── Financial Pressure ──
    # Three independent signal sources blend in fin_score via max():
    # Pink Sheet (fin_evidence_count > 0 implies price observations
    # were available), USGS MCS Fig 10 (folded into fin_evidence_count
    # too — see market_aggregator price-volatility section), and SEC
    # EDGAR coverage (fin_company_coverage > 0).  Treat each as a slot.
    fin_slots = [
        fin_evidence_count > 0,
        fin_company_coverage > 0.0,
    ]
    financial_completeness = sum(fin_slots) / len(fin_slots)

    # ── Overall: MARKET_PILLAR_WEIGHTS-weighted mean ──
    overall = (
        MARKET_PILLAR_WEIGHTS["material"]      * material_completeness
        + MARKET_PILLAR_WEIGHTS["geopolitical"] * geopolitical_completeness
        + MARKET_PILLAR_WEIGHTS["regulatory"]   * regulatory_completeness
        + MARKET_PILLAR_WEIGHTS["operational"]  * operational_completeness
        + MARKET_PILLAR_WEIGHTS["financial"]    * financial_completeness
    )

    return {
        "material":      round(material_completeness, 3),
        "geopolitical":  round(geopolitical_completeness, 3),
        "regulatory":    round(regulatory_completeness, 3),
        "operational":   round(operational_completeness, 3),
        "financial":     round(financial_completeness, 3),
        "overall":       round(overall, 3),
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def score_material_geography(
    db: Session,
    material_id: int,
    geography_code: str,
    as_of_date: Optional[date] = None,
    *,
    run_id: Optional[str] = None,
    persist: bool = True,
) -> MaterialGeographyRiskScore:
    """
    Compute (and optionally persist) a market risk score for a material × geography pair.

    Steps:
      1. Read MaterialCriticalitySignal for the material (best available source).
      2. Pull risk events: trade, operational, and regulatory, tagged to this
         material and/or geography via the junction tables.
      3. Derive four sets of pillar sub-inputs.
      4. Score each pillar using the existing scorer modules.
      5. Aggregate with MARKET_PILLAR_WEIGHTS (financial + propagation excluded).
      6. Persist to material_geography_risk_scores (upsert-safe via unique constraint).
         Skipped when ``persist=False``.

    Returns the MaterialGeographyRiskScore ORM object. ``id`` is populated
    after db.flush() when ``persist=True``; caller owns db.commit().

    This function is safe to call in batch (score_all_active_materials) and
    from individual API endpoints for on-demand scoring.
    """
    if as_of_date is None:
        as_of_date = date.today()
    if run_id is None:
        run_id = f"market-{uuid.uuid4()}"

    geography_code = geography_code.upper()

    log.info(
        "market_aggregator.score.start",
        material_id=material_id,
        geography_code=geography_code,
        as_of_date=as_of_date.isoformat(),
        run_id=run_id,
    )

    # --- STEP 1: Criticality signal ---
    criticality_signal = _get_best_criticality_signal(db, material_id, as_of_date)

    # --- STEP 2: Evidence ---
    # Trade events: tagged to this material OR this geography
    material_trade_events = get_events_for_material(
        db, material_id, RiskCategory.GEOPOLITICAL_TRADE, as_of_date
    )
    geo_trade_events = get_events_for_geographies(
        db, {geography_code}, RiskCategory.GEOPOLITICAL_TRADE, as_of_date
    )
    all_trade_events = _dedup_events(material_trade_events, geo_trade_events)

    # Operational events
    material_op_events = get_events_for_material(
        db, material_id, RiskCategory.OPERATIONAL, as_of_date
    )
    geo_op_events = get_events_for_geographies(
        db, {geography_code}, RiskCategory.OPERATIONAL, as_of_date
    )
    all_op_events = _dedup_events(material_op_events, geo_op_events)

    # Regulatory inputs derived inline (includes regulation query)
    # 11.4-Reg (2026-06-06): _derive_market_regulatory_inputs now returns
    # a 4-tuple — the 4th element is sub_input_diagnostic with
    # obligations.curated_weight_count / default_weight_count + the
    # raw/capped obligation score + top-3 event truncation visibility +
    # effective_date metadata coverage.  See function docstring for the
    # full dict shape.  Also note the 11.4-Reg-A math change: NULL
    # geography_compliance_weights now resolves to 0.0 instead of 0.50;
    # uncurated regulations contribute 0 to obligation_score.
    (
        top_reg_impacts, scope_obligations, prox_adj, reg_sub_input_diag,
    ) = _derive_market_regulatory_inputs(
        db, material_id, geography_code, as_of_date
    )

    # Financial pressure: commodity prices + producer events + SEC EDGAR weighted signal
    # 11.4-Fin-A (2026-06-06): _derive_market_financial_inputs now returns
    # a 6-tuple — the 6th element is sub_input_diagnostic with per-tier
    # contribution attribution + sparse-evidence cap visibility + Tier 3
    # 15-pt cap firing flag.  See function docstring for the dict shape.
    (
        base_sig, lev_bon, liq_bon, fin_count, company_fin_meta,
        fin_sub_input_diag,
    ) = _derive_market_financial_inputs(
        db, material_id, geography_code, as_of_date
    )

    total_event_count = len({
        ew.event.id
        for lst in [all_trade_events, all_op_events]
        for ew in lst
    })

    # Intersection event count (migration 042): events tagged to BOTH this
    # material AND this geography, within each category's lookback window.
    # Distinct from ``total_event_count`` (the UNION).  Stored separately so
    # the UI can display "events about this material in this country" — a
    # number an analyst can read at face value — without losing the audit
    # trail of what was fed into the score.
    #
    # 2026-05-12 (Step 2 audit, Fix A / B): the intersection is now ALSO
    # the input the Material and Operational pillar sub-input derivations
    # consume.  Previously they consumed the union, which dragged
    # trade_volatility and weighted_event_impacts toward the material-wide
    # event floor regardless of geography — graphite × Brazil's
    # trade_volatility was almost identical to graphite × China's.  Using
    # the intersection means each country's sub-input reflects only events
    # genuinely tagged to that (material, country) pair; countries with
    # no intersection events fall back to the derivation's default (0.3
    # for trade_volatility, empty list for op_impacts).
    #
    # An event is in the intersection iff it appears in BOTH the material-
    # anchored half AND the geography-anchored half of the same category's
    # event union.  We already have those two lists, so a set intersection
    # avoids a second round-trip to the DB.
    _mat_trade_ids = {ew.event.id for ew in material_trade_events}
    _geo_trade_ids = {ew.event.id for ew in geo_trade_events}
    _mat_op_ids = {ew.event.id for ew in material_op_events}
    _geo_op_ids = {ew.event.id for ew in geo_op_events}
    _trade_intersect_ids = _mat_trade_ids & _geo_trade_ids
    _op_intersect_ids = _mat_op_ids & _geo_op_ids
    geo_specific_event_count = len(_trade_intersect_ids | _op_intersect_ids)

    # Build the actual intersection event lists for Material / Operational
    # pillar derivation.  Source from material_trade_events / material_op_events
    # because they carry the HS-confidence-adjusted relevance_score
    # (_apply_hs_confidence_multiplier was applied to those — the geo-anchored
    # lists were not adjusted for HS confidence, so deferring to the material
    # side preserves the score-confidence semantics established in Tier 1.4).
    geo_specific_trade_events = [
        ew for ew in material_trade_events if ew.event.id in _trade_intersect_ids
    ]
    geo_specific_op_events = [
        ew for ew in material_op_events if ew.event.id in _op_intersect_ids
    ]

    # --- STEP 3: Fetch HS nodes early so both the Material Concentration
    # pillar (stage-weighted composite_node_score rollup) and the
    # Geopolitical pillar (G2 fix — HS-node tariff/export aggregates) can
    # share a single query.
    hs_nodes = get_hs_nodes_for_material(
        db, material_id, geography_code, as_of_date
    )
    eligible_nodes = [
        n for n in hs_nodes
        if n.composite_node_score is not None
        and n.hs_mapping.supply_chain_stage in STAGE_ROLLUP_WEIGHTS
    ]

    # --- STEP 4: Derive sub-inputs ---
    # Event-list-choice contract (2026-05-12, Step 2 audit Fix A/B):
    #   Material pillar       — geo_specific_trade_events (intersection)
    #       Why: trade_volatility was leaky against the union.  Falls back to
    #       0.3 default when intersection is empty, which is intentional —
    #       a country with no graphite-specific trade events shouldn't
    #       inherit China's graphite trade-event impact.
    #   Geopolitical pillar   — geo_trade_events (geo-anchored only)
    #       Why: sub-inputs are designed around "events tagged to this
    #       geography for this category" — country_concentration uses the
    #       MaterialProductionShare table separately, so the event list
    #       only needs the geo-anchored half.
    #   Operational pillar    — geo_specific_op_events (intersection)
    #       Why: same logic as Material — weighted_event_impacts was leaky
    #       against the union.  Empty intersection → empty event_component
    #       (operational score becomes 100% structural_dependency).
    # 11.4 (2026-06-04): _derive_market_material_inputs now returns a
    # 4-tuple — the 4th element is a per-sub-input data-backed diagnostic
    # dict, surfaced via rationale_json.sub_inputs.material.data_backed.
    # Production HHI / reserve HHI / capacity stress defaults flipped from
    # neutral-midpoint (0.5) to no-data-no-signal (0.0); partner-side this
    # means thin-data minerals no longer silently inflate Material
    # Concentration scores.
    crit, conc, trade_vol, mat_sub_input_diag = _derive_market_material_inputs(
        db, material_id, criticality_signal, geography_code,
        geo_specific_trade_events, as_of_date,
    )
    # 11.4-Geo (2026-06-06): _derive_market_geopolitical_inputs now
    # returns a 6-tuple — the 6th element is sub_input_diagnostic,
    # mirroring the 11.4-Material pattern.  Surfaces source attribution
    # for country_concentration / export_restriction / tariff / subsidy
    # so the partner UI can show "this score used MCS share + tariff
    # events but no export events".
    (
        ctry_conc, exp_rest, tariff, subsidy_distortion, geo_method,
        geo_sub_input_diag,
    ) = _derive_market_geopolitical_inputs(
        db, material_id, geography_code, geo_trade_events, as_of_date,
        eligible_nodes=eligible_nodes,
    )
    # 11.4-Op (2026-06-06): _derive_market_operational_inputs now returns
    # a 5-tuple — the 5th element is sub_input_diagnostic, mirroring the
    # Material/Geopolitical 11.4 pattern.
    (
        struct_dep, op_impacts, dep_source, stage_breakdown,
        op_sub_input_diag,
    ) = _derive_market_operational_inputs(
        db, material_id, geography_code, geo_specific_op_events, as_of_date
    )

    # --- STEP 5: Score each pillar ---

    # Material Concentration — use stage-weighted Level-0 rollup when ≥2
    # HsCodeGeographyRiskScore nodes exist.  Falls back to the legacy
    # material_risk path when Level-0 data is absent (eligible_nodes already
    # computed above so Geopolitical and Material pillars share the query).
    if len(eligible_nodes) >= _STAGE_ROLLUP_MIN_NODES:
        # Normalised weighted average of composite_node_scores across stages
        weighted_sum = sum(
            n.composite_node_score * STAGE_ROLLUP_WEIGHTS[n.hs_mapping.supply_chain_stage]
            for n in eligible_nodes
        )
        weight_total = sum(
            STAGE_ROLLUP_WEIGHTS[n.hs_mapping.supply_chain_stage]
            for n in eligible_nodes
        )
        mat_score = weighted_sum / weight_total if weight_total > 0 else 0.0
        stage_rollup_method = "stage_weighted"
        stage_rollup_count = len(eligible_nodes)
    else:
        mat_score = material_risk.score_material_exposure(crit, conc, trade_vol)
        stage_rollup_method = "material_fallback"
        stage_rollup_count = len(eligible_nodes)  # 0 or 1

    geo_score = geopolitical_risk.score_geopolitical_trade(
        ctry_conc, exp_rest, tariff,
        production_subsidy_distortion=subsidy_distortion,
    )
    reg_score = regulatory_risk.score_regulatory_profile(
        top_reg_impacts, scope_obligations, prox_adj
    )
    op_score = _score_operational_market(struct_dep, op_impacts)
    fin_score = fp_module.score_financial_pressure(base_sig, lev_bon, liq_bon, fin_count)

    # --- STEP 6: Aggregate ---
    overall = _aggregate_market_score(mat_score, geo_score, reg_score, op_score, fin_score)

    # --- Build rationale ---
    rationale = {
        "run_id": run_id,
        "scoring_version": SCORING_VERSION,
        "as_of_date": as_of_date.isoformat(),
        "criticality_signal": {
            "source": criticality_signal.source if criticality_signal else None,
            "reference_year": criticality_signal.reference_year if criticality_signal else None,
            "criticality_score": (
                float(criticality_signal.criticality_score)
                if criticality_signal and criticality_signal.criticality_score is not None
                else None
            ),
            "hhi_score": (
                float(criticality_signal.hhi_score)
                if criticality_signal and criticality_signal.hhi_score is not None
                else None
            ),
        },
        "sub_inputs": {
            "material": {
                "criticality": crit,
                "concentration": conc,
                "trade_volatility": trade_vol,
                "stage_rollup_method": stage_rollup_method,
                "stage_rollup_count": stage_rollup_count,
                # 11.4 (2026-06-04): per-sub-input data-backed flags so the
                # partner UI can show *which* component of the Material
                # Concentration pillar lacks real data — not just that the
                # pillar's overall completeness is low.  See
                # _derive_market_material_inputs for the dict shape.
                "data_backed": mat_sub_input_diag,
            },
            "geopolitical": {
                "country_concentration": ctry_conc,
                "export_restriction_exposure": exp_rest,
                "tariff_exposure": tariff,
                # G-Cov-3 (2026-05-09): EXPORT_SUBSIDY event signal.  None
                # when no subsidy events exist for this (material × geo) —
                # triggers the 3-component Geopolitical scoring profile.
                # Any numeric value (incl. 0.0) triggers the 4-component
                # profile.  See geopolitical_risk.score_geopolitical_trade.
                "production_subsidy_distortion": subsidy_distortion,
                # G2 fix (May 2026): records whether export/tariff sub-inputs
                # came from the legacy event-classification path only or
                # were combined via max() with the HS-node stage-weighted
                # aggregate.  See _derive_market_geopolitical_inputs docstring.
                "method": geo_method,
                "hs_node_count": len(eligible_nodes),
                # 11.4-Geo (2026-06): per-sub-input data-backed flags +
                # source attribution.  See _derive_market_geopolitical_inputs
                # docstring for the dict shape and the note on the 3-comp
                # subsidy asymmetric default — subsidy.data_backed=False
                # is the partner-visible flag for that bias.
                "data_backed": geo_sub_input_diag,
            },
            "regulatory": {
                "top_event_count": len(top_reg_impacts),
                "scope_obligations": scope_obligations,
                "policy_proximity_adjustment": prox_adj,
                # 11.4-Reg (2026-06): per-sub-input diagnostic with the
                # uncurated-weights counter, top-3 truncation visibility,
                # and 40-cap raw-vs-capped values.  See
                # _derive_market_regulatory_inputs docstring.  The
                # obligations.default_weight_count field is the partner-
                # visible flag for the 11.4-Reg-A math change
                # (0.50 → 0.0 fallback).
                "data_backed": reg_sub_input_diag,
            },
            "operational": {
                "structural_dependency": struct_dep,
                "structural_dependency_source": dep_source,
                "stage_breakdown": stage_breakdown,   # G4 Half 1 (2026-05-06)
                "event_impact_count": len(op_impacts),
                # 11.4-Op (2026-06): per-sub-input data-backed dict +
                # null-severity event counter + scoring profile.  See
                # _derive_market_operational_inputs docstring for the
                # dict shape and field semantics.
                "data_backed": op_sub_input_diag,
            },
            "financial_pressure": {
                "note": (
                    "Four-tier market signal: "
                    "(1) Pink Sheet commodity price volatility + directional trend, "
                    "(1.5) Fig 10 annual + 5-yr CAGR price growth rates, "
                    "(2) producer stress events, "
                    "(3) SEC EDGAR company scores weighted by production share."
                ),
                "base_filing_signal": base_sig,
                "leverage_warning_bonus": lev_bon,
                "liquidity_stress_bonus": liq_bon,
                "evidence_count": fin_count,
                "sec_edgar_company_signal": company_fin_meta,
                # 11.4-Fin-A (2026-06): per-sub-input diagnostic with
                # tier-source attribution + sparse-evidence cap state +
                # Tier 3 15-pt cap firing flag + coverage counts.
                # See _derive_market_financial_inputs docstring.
                "data_backed": fin_sub_input_diag,
            },
        },
        "pillar_scores": {
            "material_concentration": mat_score,
            "geopolitical_trade": geo_score,
            "regulatory_compliance": reg_score,
            "operational": op_score,
            "financial_pressure": fin_score,
        },
        "weights_used": MARKET_PILLAR_WEIGHTS,
        # 11.6 (2026-06): per-pillar data completeness diagnostic.
        # Informational; does NOT participate in the scoring math.  See
        # _compute_pillar_data_completeness for the per-pillar slot
        # definitions.  Each value is on [0.0, 1.0]; overall is the
        # MARKET_PILLAR_WEIGHTS-weighted mean of the five per-pillar
        # values.  A material with rich data across all pillars scores
        # overall=1.0; a material with Financial Pressure fully missing
        # scores overall=0.882; etc.  Surface in the UI to give
        # customers visibility into when defaults are doing the work
        # rather than real measurements.
        "data_completeness": _compute_pillar_data_completeness(
            criticality_signal,
            geo_country_concentration=ctry_conc,
            geo_export_restriction=exp_rest,
            geo_tariff=tariff,
            reg_top_event_count=len(top_reg_impacts),
            reg_scope_obligation_count=len(scope_obligations),
            op_structural_dependency=struct_dep,
            op_event_count=len(op_impacts),
            fin_evidence_count=fin_count,
            fin_company_coverage=float(company_fin_meta.get("coverage_weight", 0.0)),
        ),
        "event_counts": {
            "trade_events": len(all_trade_events),
            "operational_events": len(all_op_events),
            # Migration 042: surface the intersection (material ∩ geography)
            # alongside the union counts so analysts inspecting the rationale
            # can see how much of the consumed signal is genuinely
            # geo-specific vs. inherited from the global material pool.
            "trade_events_geo_specific": len(_mat_trade_ids & _geo_trade_ids),
            "operational_events_geo_specific": len(_mat_op_ids & _geo_op_ids),
        },
        "notes": (
            f"Market score for {geography_code} (material_id={material_id}). "
            f"Overall {overall:.1f}. "
            f"Dominant: {max(('material', mat_score), ('geopolitical', geo_score), ('regulatory', reg_score), ('operational', op_score), ('financial', fin_score), key=lambda x: x[1])[0]} "
            f"({max(mat_score, geo_score, reg_score, op_score, fin_score):.1f}). "
            f"Financial pressure: three-tier (price volatility + producer events + SEC EDGAR weighted by production share, coverage={company_fin_meta.get('coverage_weight', 0.0):.2f}). "
            f"Supply-chain propagation excluded (requires company graph). "
            f"Scoring version {SCORING_VERSION}."
        ),
    }

    score_row = MaterialGeographyRiskScore(
        material_id=material_id,
        geography_code=geography_code,
        as_of_date=as_of_date,
        material_concentration_score=mat_score,
        geopolitical_trade_score=geo_score,
        regulatory_compliance_score=reg_score,
        operational_score=op_score,
        financial_pressure_score=fin_score,
        overall_risk_score=overall,
        event_count=total_event_count,
        event_count_geo_specific=geo_specific_event_count,
        rationale_json=rationale,
        scoring_version=SCORING_VERSION,
        stage_rollup_count=stage_rollup_count,
        stage_rollup_method=stage_rollup_method,
    )

    if persist:
        # Upsert: re-running rescore-market on the same date refreshes scores
        # rather than crashing on the uq_mat_geo_risk_score unique constraint
        # (material_id, geography_code, as_of_date).
        upsert_vals = {
            "material_id":                    material_id,
            "geography_code":                 geography_code,
            "as_of_date":                     as_of_date,
            "material_concentration_score":   mat_score,
            "geopolitical_trade_score":       geo_score,
            "regulatory_compliance_score":    reg_score,
            "operational_score":              op_score,
            "financial_pressure_score":       fin_score,
            "overall_risk_score":             overall,
            "event_count":                    total_event_count,
            "event_count_geo_specific":       geo_specific_event_count,
            "rationale_json":                 rationale,
            "scoring_version":                SCORING_VERSION,
            "stage_rollup_count":             stage_rollup_count,
            "stage_rollup_method":            stage_rollup_method,
        }
        stmt = (
            pg_insert(MaterialGeographyRiskScore)
            .values(**upsert_vals)
            .on_conflict_do_update(
                constraint="uq_mat_geo_risk_score",
                set_={
                    "material_concentration_score": mat_score,
                    "geopolitical_trade_score":     geo_score,
                    "regulatory_compliance_score":  reg_score,
                    "operational_score":            op_score,
                    "financial_pressure_score":     fin_score,
                    "overall_risk_score":           overall,
                    "event_count":                  total_event_count,
                    "event_count_geo_specific":     geo_specific_event_count,
                    "rationale_json":               rationale,
                    "scoring_version":              SCORING_VERSION,
                    "stage_rollup_count":           stage_rollup_count,
                    "stage_rollup_method":          stage_rollup_method,
                },
            )
            .returning(MaterialGeographyRiskScore.id)
        )
        row_id = db.execute(stmt).scalar_one()
        score_row.id = row_id

    log.info(
        "market_aggregator.score.done",
        material_id=material_id,
        geography_code=geography_code,
        overall=round(overall, 2),
        scoring_version=SCORING_VERSION,
        persisted=persist,
    )
    return score_row


def score_all_active_materials(
    db: Session,
    as_of_date: Optional[date] = None,
    *,
    geography_codes: Optional[list[str]] = None,
    min_event_count: int = 0,
    run_id: Optional[str] = None,
) -> list[MaterialGeographyRiskScore]:
    """
    Batch-score every active material against each supplied geography.

    If ``geography_codes`` is None, derives the geography list from the union of:
      - country codes in material_production_shares for this material
      - country codes in risk_event_geographies for recent events

    This is the function a scheduled job (Inngest / Modal cron) should call.
    Each scored pair is committed individually to avoid one bad pair rolling back
    the entire batch — callers are expected to commit inside a loop or use
    ``db.commit()`` per row.

    ``min_event_count`` filters out (material, geography) pairs with fewer than
    N events — useful to avoid noise scores for very sparse pairs.

    ``run_id`` lets callers (e.g. the ``POST /market/rescore`` endpoint) supply
    a stable identifier so the response can be cross-referenced with the
    per-pair run ids stored in ``rationale_json["run_id"]`` (formatted as
    ``"{run_id}-{material.id}-{geo}"``). Defaults to a fresh ``batch-<uuid>``.

    Returns list of MaterialGeographyRiskScore objects (persisted, IDs populated).
    """
    if as_of_date is None:
        as_of_date = date.today()

    if run_id is None:
        run_id = f"batch-{uuid.uuid4()}"

    # Load active materials
    materials = list(db.scalars(select(Material)).all())
    log.info(
        "market_aggregator.batch.start",
        material_count=len(materials),
        as_of_date=as_of_date.isoformat(),
        run_id=run_id,
        geography_filter=geography_codes,
    )

    results: list[MaterialGeographyRiskScore] = []

    for material in materials:
        # Determine geographies for this material
        if geography_codes is not None:
            geos = [g.upper() for g in geography_codes]
        else:
            # Derive from material_production_shares (replaces removed
            # primary_producing_countries column dropped in migration 023)
            prod_share_geos = list(db.scalars(
                select(MaterialProductionShare.country_code)
                .where(
                    MaterialProductionShare.material_id == material.id,
                    MaterialProductionShare.production_share > 0,
                )
                .distinct()
            ).all())
            geos: list[str] = [g.upper() for g in prod_share_geos]

            # Supplement with any geography that has events for this material
            from app.models.regulatory import RiskEventGeography, RiskEventMaterial
            event_geo_stmt = (
                select(RiskEventGeography.country_code)
                .join(
                    RiskEventMaterial,
                    RiskEventMaterial.risk_event_id == RiskEventGeography.risk_event_id,
                )
                .where(RiskEventMaterial.material_id == material.id)
                .distinct()
            )
            event_geos = [row[0] for row in db.execute(event_geo_stmt).all()]
            geos = list({*geos, *event_geos})

        if not geos:
            log.debug(
                "market_aggregator.batch.skip_no_geographies",
                material_id=material.id,
                material_name=material.canonical_name,
            )
            continue

        for geo in geos:
            try:
                score_row = score_material_geography(
                    db,
                    material.id,
                    geo,
                    as_of_date,
                    run_id=f"{run_id}-{material.id}-{geo}",
                    persist=True,
                )
                results.append(score_row)
                db.commit()
            except Exception:
                db.rollback()
                log.exception(
                    "market_aggregator.batch.error",
                    material_id=material.id,
                    geography_code=geo,
                )

    log.info(
        "market_aggregator.batch.done",
        scored=len(results),
        run_id=run_id,
    )
    return results


__all__ = [
    "MARKET_PILLAR_WEIGHTS",
    "STAGE_ROLLUP_WEIGHTS",
    "score_material_geography",
    "score_all_active_materials",
    # 11.6: data-completeness diagnostic; informational only.
    "_compute_pillar_data_completeness",
]
