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
from app.models.scoring import MaterialGeographyRiskScore
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
        severity=float(ew.event.severity_score or 0.5),
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
) -> tuple[float, float, float]:
    """
    Returns (criticality, concentration, trade_volatility) each on [0, 1.0].

    criticality:
        Blend of production HHI criticality_score (70%) and a reserve scarcity
        signal derived from reserve_life_index (30%). Default 0.5 when no signal
        exists (data gap — caller should log a warning; don't assume zero risk).

        Reserve scarcity thresholds:
          RLI <= 20 years  → scarcity_signal = 1.0  (near-term constraint)
          RLI >= 80 years  → scarcity_signal = 0.0  (abundant; not a near-term risk)
          Linear interpolation between 20 and 80.
        When RLI is absent the scarcity component defaults to 0.5 (unknown = neutral).

    concentration:
        Five-component composite, each [0, 1]:
          0.45 × production HHI (hhi_score)
          0.15 × reserve HHI    (reserve_hhi_score) — forward-looking concentration
          0.25 × producer signal (geography's actual MCS share of this material;
                                   falls back to facility-presence floor 0.02 if
                                   MRDS knows about a facility here, else 0.0)
          0.10 × capacity stress (capacity_utilization normalised; high util = tight market)
          0.05 × supply trend    (production YoY contraction only; growth = no extra risk)
        All weights sum to 1.0.  Any absent component falls back to a neutral 0.5.

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
    """
    sig = criticality_signal  # alias for brevity

    # ── criticality ──────────────────────────────────────────────────────────
    if sig and sig.criticality_score is not None:
        hhi_criticality = float(sig.criticality_score)
    else:
        log.warning(
            "market_aggregator.no_criticality_signal",
            geography_code=geography_code,
        )
        hhi_criticality = 0.5

    # Reserve scarcity signal: lower RLI = higher scarcity risk.
    _RLI_HIGH = 80.0  # years — effectively no near-term scarcity concern
    _RLI_LOW  = 20.0  # years — meaningful constraint horizon
    if sig and sig.reserve_life_index is not None:
        rli = float(sig.reserve_life_index)
        scarcity_signal = max(0.0, min(1.0, (_RLI_HIGH - rli) / (_RLI_HIGH - _RLI_LOW)))
    else:
        scarcity_signal = 0.5  # absent = neutral; don't penalise data-sparse materials

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
        producer_signal = (
            _FACILITY_PRESENCE_FLOOR if _has_facility is not None else 0.0
        )

    prod_hhi = float(sig.hhi_score) if sig and sig.hhi_score is not None else 0.5
    res_hhi  = float(sig.reserve_hhi_score) if sig and sig.reserve_hhi_score is not None else 0.5

    # Capacity utilization stress: >= 0.90 → 1.0 (very tight), <= 0.50 → 0.0 (slack).
    _CAP_HIGH = 0.90
    _CAP_LOW  = 0.50
    if sig and sig.capacity_utilization is not None:
        cap_util = float(sig.capacity_utilization)
        cap_stress = max(0.0, min(1.0, (cap_util - _CAP_LOW) / (_CAP_HIGH - _CAP_LOW)))
    else:
        cap_stress = 0.5  # absent = neutral

    # Supply trend: only contractions are a risk signal; growth eases pressure.
    # Max meaningful contraction for single-year shift: ~15%.
    _YOY_CONTRACTION_MAX = 0.15
    if sig and sig.production_yoy_pct is not None:
        yoy_contraction = max(0.0, -float(sig.production_yoy_pct))  # positive = contraction
        trend_stress = min(1.0, yoy_contraction / _YOY_CONTRACTION_MAX)
    else:
        trend_stress = 0.0  # absent = no extra stress (conservative; don't assume contraction)

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
    else:
        trade_volatility = _avg_impact_normalised(
            trade_events, RiskCategory.GEOPOLITICAL_TRADE, as_of_date
        )

    return criticality, concentration, trade_volatility


def _derive_market_geopolitical_inputs(
    db: Session,
    material_id: int,
    geography_code: str,
    geo_trade_events: list[EventWithRelevance],
    as_of_date: date,
    *,
    eligible_nodes: Optional[list["HsCodeGeographyRiskScore"]] = None,
) -> tuple[float, float, float, Optional[float], str]:
    """
    Returns (country_concentration, export_restriction_exposure, tariff_exposure,
             production_subsidy_distortion, geopolitical_method) where the
    floats are on [0, 1.0].  ``production_subsidy_distortion`` is
    ``None`` when no EXPORT_SUBSIDY events exist for this (material ×
    geography) pair (triggers the 3-component scoring profile in
    ``geopolitical_risk.score_geopolitical_trade``); any numeric value
    (including 0.0) triggers the 4-component profile.  Added 2026-05-09
    as part of the G-Cov-3 audit fix.

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
        else:
            country_concentration = 0.0
            fallback_label = "no_signal"
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
            return (
                country_concentration, export_exposure, tariff_exposure,
                subsidy_distortion, method,
            )

    return (
        country_concentration, event_export, event_tariff,
        subsidy_distortion, method,
    )


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
    """
    if not geo_weights:
        return 0.50
    return float(geo_weights.get(geography_code, geo_weights.get("DEFAULT", 0.50)))


def _derive_market_regulatory_inputs(
    db: Session,
    material_id: int,
    geography_code: str,
    as_of_date: date,
) -> tuple[list[float], list[tuple[str, float]], float]:
    """
    Returns (top_event_impacts, scope_obligations, policy_proximity_adjustment).

    scope_obligations:
        Regulations linked via RegulationMaterialScope to this material OR via
        RegulationGeographyScope to this geography, each paired with its resolved
        compliance risk weight for ``geography_code``.

        Weights come from ``Regulation.geography_compliance_weights`` (JSONB):
          - Exact ISO2 match → that weight
          - "DEFAULT" key → fallback weight
          - NULL column (not yet curated) → 0.50 universal default

        This replaces the previous hardcoded 0.50 for all obligations, allowing
        UFLPA to score CN at 1.0 while US scores 0.05 for the same regulation.

    top_event_impacts:
        Normalised event_impact values for regulatory events scoped to this
        material + geography. Passed to regulatory_risk.score_regulatory_profile().

    policy_proximity_adjustment:
        1.15 if any event has an effective_date within 90 days of as_of_date.
    """
    from app.models.regulatory import (
        Regulation,
        RegulationGeographyScope,
        RegulationMaterialScope,
        RiskEventRegulation,
    )
    from datetime import datetime as _dt

    # Scope-derived regulations (material + geography).
    # weights maps regulation_key → resolved compliance risk weight for this geography.
    weights: dict[str, float] = {}

    mat_stmt = (
        select(Regulation.regulation_key, Regulation.geography_compliance_weights)
        .join(RegulationMaterialScope, RegulationMaterialScope.regulation_id == Regulation.id)
        .where(RegulationMaterialScope.material_id == material_id)
    )
    for key, geo_weights in db.execute(mat_stmt).all():
        weights[key] = _resolve_compliance_weight(geo_weights, geography_code)

    geo_stmt = (
        select(Regulation.regulation_key, Regulation.geography_compliance_weights)
        .join(RegulationGeographyScope, RegulationGeographyScope.regulation_id == Regulation.id)
        .where(RegulationGeographyScope.country_code == geography_code)
    )
    for key, geo_weights in db.execute(geo_stmt).all():
        resolved = _resolve_compliance_weight(geo_weights, geography_code)
        # Take the higher weight if the regulation was already added via material scope
        if resolved > weights.get(key, 0.0):
            weights[key] = resolved

    scope_obligations = sorted(weights.items())

    # Events for scoped regulations
    reg_keys = set(weights.keys())
    reg_events = (
        get_events_for_regulations(db, reg_keys, as_of_date)
        if reg_keys
        else []
    )

    top_event_impacts: list[float] = []
    policy_proximity_adjustment = 1.0

    for ew in reg_events:
        effective_date: Optional[date] = None
        meta = ew.event.metadata_json or {}
        raw_eff = meta.get("effective_date")
        if raw_eff and isinstance(raw_eff, str):
            try:
                effective_date = _dt.fromisoformat(raw_eff).date()
            except ValueError:
                pass

        if effective_date is not None:
            days_to_effective = (effective_date - as_of_date).days
            if 0 <= days_to_effective <= 90:
                policy_proximity_adjustment = 1.15

        impact = _event_impact(ew, RiskCategory.REGULATORY_COMPLIANCE, as_of_date)
        top_event_impacts.append(impact)

    return top_event_impacts, scope_obligations, policy_proximity_adjustment


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
) -> tuple[Optional[float], list[float], str, Optional[dict]]:
    """
    Returns (structural_dependency, weighted_event_impacts, dep_source, stage_breakdown).

    ``structural_dependency`` is ``None`` when no facility data and no
    capacity-constraint events exist for the (material, geography) pair
    — added 2026-05-12 as a follow-up to the Step 2 audit so the
    operational pillar doesn't impute a silent 12-point ghost from the
    legacy 0.3 placeholder.  Callers (``_score_operational_market``)
    redistribute pillar weight to events when this is None.

    structural_dependency — three-tier resolution:

      1. MRDS geography-level: stage-weighted fraction of production-asset sites
         that are mothballed.  G4 Half 1 (2026-05-06) made this stage-aware —
         the per-stage dependency is rolled up using ``STAGE_ROLLUP_WEIGHTS``
         (ore=0.10, concentrate=0.15, intermediate=0.20, refined=0.25,
         battery_grade=0.30) over only the stages that have data.  No
         default-fill for missing stages — partner direction is to honestly
         reflect what's measurable.

      2. MRDS global fallback: when no MRDS sites exist for this specific
         geography, try the global material-level fraction (all geographies).
         Discounted by 0.5 to reflect that it's a broader, less specific signal.

      3. Event baseline: if no MRDS data exists for this material at all,
         fall back to SINGLE_SOURCE / CAPACITY_CONSTRAINT event severity, or
         the conservative 0.3 default.

    weighted_event_impacts:
        event_impact for each operational event, regardless of structural_dependency
        source. Events and capacity data are complementary, not redundant.

    dep_source:
        Provenance tag stored in rationale_json so post-run queries can identify
        which (material, geography) pairs are hitting the conservative default
        rather than real facility data.  One of:
            "mrds_geography_stage_weighted"           — stage-weighted fraction for this geo
            "mrds_global_discounted_stage_weighted"   — global stage-weighted × 0.5
            "event_derived"                            — capacity-constraint event severity avg
            "default_0.3"                              — no data; conservative placeholder

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
            struct_dep = sum(
                float(ew.event.severity_score or 0.5) for ew in struct_events
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

    return struct_dep, weighted_event_impacts, dep_source, stage_breakdown


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
) -> tuple[float, float, float, int, dict]:
    """
    Market-level financial pressure sub-inputs.

    Returns a 5-tuple: (base_filing_signal, leverage_warning_bonus,
    liquidity_stress_bonus, filing_count, company_signal_meta).

    ``company_signal_meta`` is a dict for rationale_json — it is NOT passed to
    ``fp_module.score_financial_pressure()``, which still takes the first four
    values unchanged.

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

    if filing_count >= 2:
        prices = [float(row.price_usd) for row in price_rows]
        mean_price = sum(prices) / len(prices)

        if mean_price > 0:
            import statistics
            std_price = statistics.stdev(prices)
            cv = std_price / mean_price
            base_filing_signal = min(1.0, cv / _PRICE_CV_MAX) * 40.0

        # Directional trend: compare last price vs. first price
        pct_change = (prices[-1] - prices[0]) / prices[0] if prices[0] > 0 else 0.0

        if pct_change > 0:
            # Price spike → buyer leverage stress
            leverage_warning_bonus = min(1.0, pct_change / _PRICE_SPIKE_PCT) * 30.0
        else:
            # Price crash → producer liquidity stress
            liquidity_stress_bonus = min(1.0, abs(pct_change) / _PRICE_CRASH_PCT) * 30.0

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
            fig10_meta["cagr_contribution_to_base_filing_signal"] = round(cagr_signal, 2)

        # YoY signed → spike (positive) or crash (negative).
        if f_yoy is not None and f_yoy > 0:
            spike_signal = min(1.0, f_yoy / _FIG10_YOY_SPIKE_PCT) * 30.0
            leverage_warning_bonus = max(leverage_warning_bonus, spike_signal)
            fig10_meta["yoy_contribution_to_leverage_warning"] = round(spike_signal, 2)
        elif f_yoy is not None and f_yoy < 0:
            crash_signal = min(1.0, abs(f_yoy) / _FIG10_YOY_CRASH_PCT) * 30.0
            liquidity_stress_bonus = max(liquidity_stress_bonus, crash_signal)
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
        severity = float(ew.event.severity_score or 0.5)

        if subtype in ("PRICE_SURGE", "MARKET_SQUEEZE") or (
            "price surge" in text or "market squeeze" in text
        ):
            leverage_warning_bonus = min(30.0, leverage_warning_bonus + severity * 10.0)
        elif subtype in ("PRODUCER_EXIT", "MINE_CLOSURE", "BANKRUPTCY") or (
            "producer exit" in text
            or "mine closure" in text
            or "mine shut" in text
            or "bankruptcy" in text
        ):
            liquidity_stress_bonus = min(30.0, liquidity_stress_bonus + severity * 10.0)
        else:
            # Generic financial pressure event — contributes to base signal
            base_filing_signal = min(40.0, base_filing_signal + severity * 5.0)

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
    base_filing_signal = min(40.0, base_filing_signal + company_contribution)

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
    return base_filing_signal, leverage_warning_bonus, liquidity_stress_bonus, filing_count, company_signal_meta


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
    top_reg_impacts, scope_obligations, prox_adj = _derive_market_regulatory_inputs(
        db, material_id, geography_code, as_of_date
    )

    # Financial pressure: commodity prices + producer events + SEC EDGAR weighted signal
    base_sig, lev_bon, liq_bon, fin_count, company_fin_meta = _derive_market_financial_inputs(
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
    crit, conc, trade_vol = _derive_market_material_inputs(
        db, material_id, criticality_signal, geography_code,
        geo_specific_trade_events, as_of_date,
    )
    (
        ctry_conc, exp_rest, tariff, subsidy_distortion, geo_method,
    ) = _derive_market_geopolitical_inputs(
        db, material_id, geography_code, geo_trade_events, as_of_date,
        eligible_nodes=eligible_nodes,
    )
    struct_dep, op_impacts, dep_source, stage_breakdown = _derive_market_operational_inputs(
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
            },
            "regulatory": {
                "top_event_count": len(top_reg_impacts),
                "scope_obligations": scope_obligations,
                "policy_proximity_adjustment": prox_adj,
            },
            "operational": {
                "structural_dependency": struct_dep,
                "structural_dependency_source": dep_source,
                "stage_breakdown": stage_breakdown,   # G4 Half 1 (2026-05-06)
                "event_impact_count": len(op_impacts),
            },
            "financial_pressure": {
                "note": (
                    "Three-tier market signal: "
                    "(1) commodity price volatility + directional trend, "
                    "(2) producer stress events, "
                    "(3) SEC EDGAR company scores weighted by production share."
                ),
                "base_filing_signal": base_sig,
                "leverage_warning_bonus": lev_bon,
                "liquidity_stress_bonus": liq_bon,
                "evidence_count": fin_count,
                "sec_edgar_company_signal": company_fin_meta,
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
]
