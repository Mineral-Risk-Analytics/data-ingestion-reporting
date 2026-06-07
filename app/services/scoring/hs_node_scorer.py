"""Level-0 HS node scorer — Phase 3, PR 18.

Computes and persists ``HsCodeGeographyRiskScore`` rows: one per
(hs_mapping_id × country_code × as_of_date × market_scope).

These are the most granular persisted scores in the stack.  They roll up into
``MaterialGeographyRiskScore`` (Level 1) via ``STAGE_ROLLUP_WEIGHTS`` in
``market_aggregator.py``.

Sub-scores
----------
- ``production_share``   — this country's share of global production for the
  HS stage node in the most recent reference year available.
- ``hhi_at_stage``       — Σ(production_share²) across all countries for the
  same node and reference year.  Ranges 0→1; HHI ≈ 0.25 is high concentration.
- ``tariff_exposure``    — 0–1 signal derived from tariff risk events scoped to
  this HS code via ``risk_event_hs_mappings``.  Average severity of active tariff
  events, weighted by recency and confidence.
- ``export_restriction`` — 0–1 signal from export restriction events tagged to
  this HS code.  Same formula as tariff_exposure.
- ``composite_node_score`` — 0–100 weighted combination:
    hhi_component    × 50  (concentration dominates)
    tariff_component × 25
    export_component × 25

Idempotency
-----------
Rows are upserted on the unique key (hs_mapping_id, country_code, as_of_date,
market_scope).  Re-running on the same date overwrites the previous values.

Caller
------
``rescore_hs_nodes()`` in ``scoring_jobs.py`` calls
``score_all_hs_nodes()`` which fans out over every (hs_mapping_id × country)
pair that has production share data.  Must run before ``score_all_active_materials()``.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.facility import Facility, FacilityMaterialLink
from app.models.regulatory import RiskEvent, RiskEventGeography, RiskEventHsMapping
from app.models.scoring import HsCodeGeographyRiskScore
from app.models.supply import HsCodeMaterialMapping, HsCodeProductionShare
from app.services.scoring.decay import compute_recency_multiplier
from app.constants import RiskCategory
from app.services.scoring.supplier_risk import SCORING_VERSION

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Composite score weights — must sum to 1.0 within each scoring path.
# ---------------------------------------------------------------------------
# Two profiles depending on whether facility-level operational data is
# available for the (material × stage × country) being scored.  G5 audit
# fix (2026-05-09): added the operational sub-score.  See
# ``docs/scoring-audit-2026-05.md`` § G5.

# Default 3-sub-score profile (no facility data — most materials today)
_HHI_WEIGHT = 0.50
_TARIFF_WEIGHT = 0.25
_EXPORT_WEIGHT = 0.25

# 4-sub-score profile (facility data present — partner-curated launch list)
_HHI_WEIGHT_WITH_OPERATIONAL = 0.40
_TARIFF_WEIGHT_WITH_OPERATIONAL = 0.20
_EXPORT_WEIGHT_WITH_OPERATIONAL = 0.20
_OPERATIONAL_WEIGHT = 0.20

# Statuses that mark capacity as "at risk" — a facility in any of these
# states represents disrupted or pending supply.  Mirrors the operational
# pillar's logic in ``market_aggregator._facility_structural_dependency``.
_AT_RISK_STATUSES = frozenset({"mothballed", "closed", "care_maintenance"})

# ---------------------------------------------------------------------------
# Sub-score helpers (pure functions)
# ---------------------------------------------------------------------------

# ── event_subtype classifications consumed by Tariff / Export sub-scores ──
# These match the canonical taxonomy emitted by ingesters (see migration 040
# docstring + ``RiskEvent.event_subtype`` column comment).  Pre-2026-05-05
# these constants were lowercase (``"tariff"``, ``"export_restriction"``…)
# matching against ``RiskEvent.event_type``, but no ingester ever wrote those
# values — see ``docs/scoring-audit-2026-05-addendum.md`` G3 for the
# diagnosis.  Migration 040 promoted ``event_subtype`` to a typed column
# and aligned both sides on the uppercase form ingesters had been using
# all along.
#
# 2026-05-06 (G11 Scope 2): added ``IMPORT_DISRUPTION`` to ``_TARIFF_SUBTYPES``.
# GTA emits ``IMPORT_DISRUPTION`` for "Import tariff/quota/ban" interventions;
# the previous ``{"TARIFF"}`` constant matched zero events in production,
# leaving ``tariff_exposure`` permanently 0.  See gta._INTERVENTION_SUBTYPE_MAP.
_TARIFF_SUBTYPES = frozenset({"TARIFF", "IMPORT_DISRUPTION"})
_EXPORT_SUBTYPES = frozenset({"EXPORT_RESTRICTION"})

# ── Geography contexts consumed by Tariff / Export sub-scores ─────────────
# Primary    = the implementing country.  For export-side interventions
#              this IS the producer whose supply just got constrained, so
#              the export sub-score uses ``geography_context="primary"``.
# Affected   = the targeted country.  For import-side interventions
#              (tariffs / quotas / bans on imports from country X), this
#              is the producer whose exports just got penalised, so the
#              tariff sub-score uses ``geography_context="affected"``.
#
# Events without a matching geography row are excluded entirely (strict
# attribution per partner direction — no "global" fallback).  See
# ``app/services/ingestion/gta.py`` ``ingest_gta`` for the writer side.
_EXPORT_GEOGRAPHY_CONTEXT = "primary"
_TARIFF_GEOGRAPHY_CONTEXT = "affected"


def _compute_hhi(shares: list[float]) -> float:
    """Herfindahl-Hirschman Index from a list of fractional production shares.

    Returns a value in [0, 1].  0 = perfectly dispersed, 1 = monopoly.
    Shares need not sum to 1.0 — each is used directly as a fraction.
    """
    return sum(s * s for s in shares)


def _compute_event_signal(
    events: list[tuple[float, float, date]],
    as_of_date: date,
    category: RiskCategory,
) -> float:
    """Convert a list of (severity, confidence, event_date) tuples to a 0–1 signal.

    Uses the standard ``severity × confidence × recency_multiplier`` formula
    and returns the average of the top-3 non-zero impacts.  Returns 0.0 if
    no events are present.

    Args:
        events:     List of (severity, confidence, event_date) tuples.
        as_of_date: Evaluation date for recency computation.
        category:   RiskCategory used to determine the decay function.
    """
    if not events:
        return 0.0

    impacts: list[float] = []
    for severity, confidence, event_date in events:
        eff_conf = max(confidence, 0.60) if severity >= 0.80 else confidence
        recency = compute_recency_multiplier(
            category=category,
            event_date=event_date if event_date is not None else as_of_date,
            as_of_date=as_of_date,
        )
        impacts.append(severity * eff_conf * recency)

    impacts.sort(reverse=True)
    top3 = impacts[:3]
    return sum(top3) / len(top3) if top3 else 0.0


def _compute_operational_signal(
    db: Session,
    *,
    material_id: int,
    country_code: str,
    supply_chain_stage: Optional[str],
) -> tuple[Optional[float], int, float, float]:
    """Compute a 0–1 operational disruption signal for (material × stage × country).

    Joins ``FacilityMaterialLink`` to ``Facility`` on facility_id and selects
    rows matching the (material × stage × country) tuple.  Computes:

        at_risk_capacity = SUM(annual_capacity_tpy) WHERE status ∈
                           {mothballed, closed, care_maintenance}
        total_capacity   = SUM(annual_capacity_tpy)  -- any status
        signal           = at_risk / total

    Returns ``(signal, facility_count, at_risk_capacity, total_capacity)``.
    ``signal`` is ``None`` when no usable capacity rows exist (e.g. all
    MRDS facilities have NULL ``annual_capacity_tpy`` until partner
    facility data is seeded).  Caller treats ``None`` as "no operational
    data" and falls back to the 3-sub-score profile.

    Why per-stage filtering: the operational pillar at Level 1
    (``market_aggregator._facility_structural_dependency``) already
    splits by stage to avoid attributing a closed mine's tonnage to a
    refining-stage HS node.  This helper applies the same logic at
    Level 0 so an HS code at the refined stage only sees refined-stage
    facility risk.

    Caveat: most MRDS-ingested rows have ``annual_capacity_tpy=NULL``
    (USGS does not publish capacity), so until partner facility data
    lands the signal returns ``None`` for those (material × stage ×
    country) tuples.  This is intentional — the operational sub-score
    is opt-in based on data availability rather than fabricated.
    """
    if supply_chain_stage is None:
        return None, 0, 0.0, 0.0

    # Pull capacity rows joined to facility status.  Skip rows with NULL
    # capacity at the SQL level so the SUM is over usable values only.
    rows = db.execute(
        select(
            FacilityMaterialLink.annual_capacity_tpy,
            Facility.status,
        )
        .join(Facility, Facility.id == FacilityMaterialLink.facility_id)
        .where(
            FacilityMaterialLink.material_id == material_id,
            FacilityMaterialLink.supply_chain_stage == supply_chain_stage,
            Facility.country == country_code,
            FacilityMaterialLink.annual_capacity_tpy.isnot(None),
        )
    ).all()

    if not rows:
        return None, 0, 0.0, 0.0

    total_capacity = 0.0
    at_risk_capacity = 0.0
    for capacity, status in rows:
        cap = float(capacity)
        total_capacity += cap
        if (status or "").lower() in _AT_RISK_STATUSES:
            at_risk_capacity += cap

    if total_capacity <= 0:
        return None, len(rows), 0.0, 0.0

    signal = min(1.0, at_risk_capacity / total_capacity)
    return signal, len(rows), at_risk_capacity, total_capacity


# ---------------------------------------------------------------------------
# 11.1.A (2026-06) — Parent/child HS mapping propagation
# ---------------------------------------------------------------------------
# Pre-11.1.A the scorer joined events on ``RiskEventHsMapping.hs_mapping_id ==
# hs_mapping_id`` (strict equality).  That meant a Federal Register tariff
# tagged to the 4-digit chapter mapping (e.g. ``2601`` for iron ore) would
# only lift ``tariff_exposure`` on the 4-digit node, NOT on its 6-digit
# children ``260111`` / ``260112``.  At Level-1 the rollup averages the
# children's zero-tariff scores with the parent's lifted score, diluting
# the signal.
#
# 11.1.A closes this by treating events tagged to any (parent ∪ child)
# mapping FOR THE SAME MATERIAL as in-scope for the current node.  Cross-
# material contamination (e.g. HS 2615 covers Nb/Ta/V/Zr) is prevented by
# the same-material filter.  Production shares + HHI + operational signal
# remain strictly per-node — those are structural measurements that don't
# logically propagate across granularities.


def _classify_hs_relations(
    our_prefix: str,
    siblings: list[tuple[int, str]],
) -> tuple[list[int], list[int]]:
    """Classify same-material HS mappings as parents / children of ``our_prefix``.

    Pure helper for testing.  Inputs are normalised (no dots).  Returns
    ``(parent_ids, child_ids)``.

    Parent: ``sibling_prefix`` is a strict prefix of ``our_prefix`` (e.g.
    ``2601`` is a parent of ``260111``).

    Child: ``our_prefix`` is a strict prefix of ``sibling_prefix`` (e.g.
    ``260111`` is a child of ``2601``).

    Equal prefixes are NEITHER parent nor child — they're the same node
    (or a data duplicate that should have been caught by the unique
    constraint on ``hs_code_material_mappings``).  Such rows are skipped
    silently.
    """
    parents: list[int] = []
    children: list[int] = []
    for other_id, other_prefix in siblings:
        if other_prefix == our_prefix:
            continue
        if our_prefix.startswith(other_prefix):
            parents.append(other_id)
        elif other_prefix.startswith(our_prefix):
            children.append(other_id)
        # Otherwise the prefixes are unrelated (e.g. both 6-digit
        # codes under the same material, like 260111 and 260112).
    return parents, children


def _get_related_hs_mapping_ids(
    db: Session,
    hs_mapping_id: int,
) -> tuple[set[int], dict]:
    """Return mapping IDs for parent + child HS codes of the same material.

    Returns ``(set_of_ids, propagation_metadata)``.  The current mapping
    ID is always included in the set; if no parent/child relations exist
    the set has exactly one element and the score is unchanged from
    pre-11.1.A behaviour.

    The same-material filter is intentional: HS 2615 ("Niobium /
    Tantalum / Vanadium / Zirconium ores") is shared by FOUR distinct
    mineral mappings.  Without the filter, a tariff tagged to the
    Vanadium-at-2615 mapping would propagate to Niobium-at-261590 etc.
    — cross-material contamination.
    """
    # Fetch our prefix + material_id in one query.
    our = db.execute(
        select(
            HsCodeMaterialMapping.hs_prefix,
            HsCodeMaterialMapping.material_id,
        ).where(HsCodeMaterialMapping.id == hs_mapping_id)
    ).one_or_none()
    if our is None or our.material_id is None:
        return {hs_mapping_id}, {
            "primary_mapping_id": hs_mapping_id,
            "parent_mapping_ids": [],
            "child_mapping_ids": [],
            "total_propagated": 0,
            "skipped_reason": "mapping_or_material_missing",
        }

    our_prefix = (our.hs_prefix or "").replace(".", "").strip()
    if not our_prefix:
        return {hs_mapping_id}, {
            "primary_mapping_id": hs_mapping_id,
            "parent_mapping_ids": [],
            "child_mapping_ids": [],
            "total_propagated": 0,
            "skipped_reason": "empty_prefix",
        }

    # Pull every other mapping for the same material.
    sibling_rows = db.execute(
        select(
            HsCodeMaterialMapping.id,
            HsCodeMaterialMapping.hs_prefix,
        ).where(
            HsCodeMaterialMapping.material_id == our.material_id,
            HsCodeMaterialMapping.id != hs_mapping_id,
        )
    ).all()
    siblings = [
        (row.id, (row.hs_prefix or "").replace(".", "").strip())
        for row in sibling_rows
        if (row.hs_prefix or "").strip()
    ]

    parents, children = _classify_hs_relations(our_prefix, siblings)

    related = {hs_mapping_id, *parents, *children}
    return related, {
        "primary_mapping_id": hs_mapping_id,
        "parent_mapping_ids": sorted(parents),
        "child_mapping_ids": sorted(children),
        "total_propagated": len(parents) + len(children),
    }


# ---------------------------------------------------------------------------
# Node scorer
# ---------------------------------------------------------------------------

def score_hs_node_geography(
    db: Session,
    hs_mapping_id: int,
    country_code: str,
    as_of_date: date,
    *,
    market_scope: str = "global",
) -> Optional[HsCodeGeographyRiskScore]:
    """Compute and persist a Level-0 score for one (HS node × country) pair.

    Returns the persisted ``HsCodeGeographyRiskScore`` row, or ``None`` if
    NEITHER production share data NOR country-scoped events exist for the
    node (truly nothing to score).

    Two scoring paths (Option 1 audit fix, 2026-05-06):

      * **HHI-anchored** — production shares present.  Composite is the
        canonical ``0.50 × HHI + 0.25 × tariff + 0.25 × export``.  This is
        the ore-stage path for materials where USGS MCS publishes per-
        country production figures.

      * **Event-only fallback** — production shares missing but tariff /
        export events exist for this (HS code × country) pair.  Composite
        becomes ``0.5 × tariff + 0.5 × export`` with the HHI weight
        redistributed across the event sub-scores.  Marked
        ``score_method="event_only_no_hhi"`` in metadata so consumers can
        distinguish anchored from event-only scores.  Pre-fix, refined-
        and intermediate-stage HS codes for materials like aluminum
        returned None even when 400+ events were tagged to the mapping —
        the gap was visible in event counts but invisible in scoring.

    The function is idempotent — calling it twice on the same
    (hs_mapping_id, country_code, as_of_date, market_scope) key overwrites
    the previous values.

    Args:
        db:           Active SQLAlchemy session (caller must commit).
        hs_mapping_id: PK of the ``hs_code_material_mappings`` row.
        country_code:  ISO 3166-1 alpha-2.
        as_of_date:    Evaluation date.
        market_scope:  ``"global"`` (default) or ``"us"``.
    """
    # ── 1. Find the most recent reference year with production share data ──
    # May be None — that triggers the event-only fallback path below.
    latest_year: Optional[int] = db.scalar(
        select(func.max(HsCodeProductionShare.reference_year)).where(
            HsCodeProductionShare.hs_mapping_id == hs_mapping_id,
            HsCodeProductionShare.market_scope == market_scope,
        )
    )

    # ── 2. Load production shares (only if a year exists) ───────────────────
    all_shares: list[HsCodeProductionShare] = []
    production_share: float = 0.0
    hhi_at_stage: Optional[float] = None
    if latest_year is not None:
        all_shares = list(
            db.scalars(
                select(HsCodeProductionShare).where(
                    HsCodeProductionShare.hs_mapping_id == hs_mapping_id,
                    HsCodeProductionShare.reference_year == latest_year,
                    HsCodeProductionShare.market_scope == market_scope,
                )
            ).all()
        )
        # ── 3. This country's production share (0.0 if not in dataset) ──
        country_row = next(
            (s for s in all_shares if s.country_code == country_code), None
        )
        production_share = country_row.production_share if country_row else 0.0
        # ── 4. HHI — computed from all countries in this node + year ────
        share_values = [
            s.production_share for s in all_shares if s.production_share > 0
        ]
        hhi_at_stage = _compute_hhi(share_values)

    # ── 11.1.A (2026-06) ── parent/child HS propagation
    # Events tagged to a parent (4-digit chapter) or child (6-digit
    # subheading) of THIS mapping — for the same material — are
    # in-scope for the current node's score.  Closes the vertical
    # asymmetry where a FR tariff on ``2601`` previously only lifted
    # the 4-digit node and not its 6-digit children.  Same-material
    # filter prevents cross-material contamination on shared chapters
    # (e.g. HS 2615 covers Nb/Ta/V/Zr).
    related_mapping_ids, hs_propagation_meta = _get_related_hs_mapping_ids(
        db, hs_mapping_id,
    )

    # ── 5. Tariff events scoped to this HS code AND this country ────────────
    # G11 (2026-05-06): tariff events are import-side interventions where the
    # implementing country is the importer; the producer being targeted is in
    # ``RiskEventGeography`` with ``geography_context="affected"``.  Join on
    # both the HS code and the affected-country tag so a US tariff on Chinese
    # lithium lifts CN's score on the lithium HS node, not US's.  Events with
    # no ``"affected"`` geography row are excluded entirely (strict
    # attribution; no global fallback).  Filter on typed ``event_subtype``
    # column (migration 040), not the ingester-specific ``event_type``.
    #
    # 11.1.A: ``hs_mapping_id IN (related_mapping_ids)`` instead of
    # strict equality so parent + child HS mappings contribute events
    # at the same material × country.
    tariff_rows = db.execute(
        select(
            RiskEvent.id,
            RiskEvent.severity_score,
            RiskEvent.confidence_score,
            RiskEvent.event_date,
        )
        .join(RiskEventHsMapping, RiskEventHsMapping.risk_event_id == RiskEvent.id)
        .join(RiskEventGeography, RiskEventGeography.risk_event_id == RiskEvent.id)
        .where(
            RiskEventHsMapping.hs_mapping_id.in_(related_mapping_ids),
            RiskEvent.event_subtype.in_(_TARIFF_SUBTYPES),
            RiskEventGeography.country_code == country_code,
            RiskEventGeography.geography_context == _TARIFF_GEOGRAPHY_CONTEXT,
        )
        .distinct()  # events tagged to multiple related mappings count once
    ).all()

    # Normalise event_date (DateTime column) to date for the decay function.
    tariff_events = [
        (
            row.severity_score,
            row.confidence_score,
            row.event_date.date() if hasattr(row.event_date, "date") else row.event_date,
        )
        for row in tariff_rows
    ]
    tariff_exposure = _compute_event_signal(
        tariff_events, as_of_date, RiskCategory.GEOPOLITICAL_TRADE
    )

    # ── 6. Export restriction events scoped to this HS code AND this country ─
    # G11 (2026-05-06): export-side interventions are filtered by the
    # implementing country (``geography_context="primary"``) — the country
    # whose own producers' supply is being constrained.  A China graphite
    # export ban lifts CN's score on the graphite HS node only, not every
    # country's.
    #
    # 11.1.A: same parent/child propagation as tariff query above.
    export_rows = db.execute(
        select(
            RiskEvent.id,
            RiskEvent.severity_score,
            RiskEvent.confidence_score,
            RiskEvent.event_date,
        )
        .join(RiskEventHsMapping, RiskEventHsMapping.risk_event_id == RiskEvent.id)
        .join(RiskEventGeography, RiskEventGeography.risk_event_id == RiskEvent.id)
        .where(
            RiskEventHsMapping.hs_mapping_id.in_(related_mapping_ids),
            RiskEvent.event_subtype.in_(_EXPORT_SUBTYPES),
            RiskEventGeography.country_code == country_code,
            RiskEventGeography.geography_context == _EXPORT_GEOGRAPHY_CONTEXT,
        )
        .distinct()  # events tagged to multiple related mappings count once
    ).all()

    export_events = [
        (
            row.severity_score,
            row.confidence_score,
            row.event_date.date() if hasattr(row.event_date, "date") else row.event_date,
        )
        for row in export_rows
    ]
    export_restriction = _compute_event_signal(
        export_events, as_of_date, RiskCategory.GEOPOLITICAL_TRADE
    )

    # ── 7. Operational sub-score (G5 audit fix, 2026-05-09) ─────────────────
    # Compute the per-(material × stage × country) operational signal from
    # facility data.  Returns None when no usable capacity rows exist,
    # which means the scorer falls back to the existing 3-sub-score
    # profile (HHI + tariff + export).  When facility data is present
    # (typically from partner-curated seed via G4c loader), the composite
    # gains a 4th sub-score with weights renormalised to 0.40/0.20/0.20/0.20.
    #
    # We need the mapping's material_id and supply_chain_stage to scope
    # the facility query — load them once here.  ``stage_for_op`` is None
    # when the mapping pre-dates the stage column or wasn't seeded with
    # one; in that case operational stays None too.
    mapping_row = db.execute(
        select(
            HsCodeMaterialMapping.material_id,
            HsCodeMaterialMapping.supply_chain_stage,
        ).where(HsCodeMaterialMapping.id == hs_mapping_id)
    ).one_or_none()
    material_id = mapping_row.material_id if mapping_row else None
    stage_for_op = mapping_row.supply_chain_stage if mapping_row else None
    operational_signal: Optional[float] = None
    op_facility_count: int = 0
    op_at_risk: float = 0.0
    op_total: float = 0.0
    if material_id is not None and stage_for_op is not None:
        operational_signal, op_facility_count, op_at_risk, op_total = (
            _compute_operational_signal(
                db,
                material_id=material_id,
                country_code=country_code,
                supply_chain_stage=stage_for_op,
            )
        )

    # ── 8. Composite node score (0–100) ─────────────────────────────────────
    # Five scoring paths (G5 + Option 1 + canonical):
    #
    #   hhi_anchored                  HHI + tariff + export
    #                                 weights: 0.50 / 0.25 / 0.25
    #
    #   hhi_anchored_with_operational HHI + tariff + export + operational
    #                                 weights: 0.40 / 0.20 / 0.20 / 0.20
    #
    #   event_only_no_hhi             tariff + export (HHI absent)
    #                                 weights:        0.50 / 0.50
    #
    #   event_only_with_operational   tariff + export + operational (no HHI)
    #                                 weights:        0.40 / 0.40 / 0.20
    #
    #   operational_only              operational only (no HHI, no events)
    #                                 weights:                       1.00
    #
    # The composite is always a weighted average of components in [0, 100],
    # so the final score is itself in [0, 100] regardless of which path
    # fires.  Each weight set sums to 1.0.
    has_events = bool(tariff_events) or bool(export_events)
    has_operational = operational_signal is not None
    if hhi_at_stage is None and not has_events and not has_operational:
        # Truly nothing to score.  Pre-fix this branch was the entire
        # short-circuit at top of function.
        log.debug(
            "hs_node_scorer.no_data_for_node",
            hs_mapping_id=hs_mapping_id,
            country_code=country_code,
            market_scope=market_scope,
            note="No production shares, no events, no facility capacity data",
        )
        return None

    # Build component & weight dicts based on which signals are available.
    components: dict[str, float] = {}
    weights_used: dict[str, Optional[float]] = {
        "hhi": None, "tariff": None, "export": None, "operational": None,
    }

    if hhi_at_stage is not None and has_operational:
        # HHI-anchored 4-component path
        components = {
            "hhi":         hhi_at_stage * 100,
            "tariff":      tariff_exposure * 100,
            "export":      export_restriction * 100,
            "operational": (operational_signal or 0.0) * 100,
        }
        weights_used = {
            "hhi":         _HHI_WEIGHT_WITH_OPERATIONAL,
            "tariff":      _TARIFF_WEIGHT_WITH_OPERATIONAL,
            "export":      _EXPORT_WEIGHT_WITH_OPERATIONAL,
            "operational": _OPERATIONAL_WEIGHT,
        }
        score_method = "hhi_anchored_with_operational"
    elif hhi_at_stage is not None:
        # HHI-anchored canonical (no operational data — most common today)
        components = {
            "hhi":    hhi_at_stage * 100,
            "tariff": tariff_exposure * 100,
            "export": export_restriction * 100,
        }
        weights_used = {
            "hhi":         _HHI_WEIGHT,
            "tariff":      _TARIFF_WEIGHT,
            "export":      _EXPORT_WEIGHT,
            "operational": None,
        }
        score_method = "hhi_anchored"
    elif has_events and has_operational:
        # Event-only with operational (HHI absent, both event signals + ops)
        components = {
            "tariff":      tariff_exposure * 100,
            "export":      export_restriction * 100,
            "operational": (operational_signal or 0.0) * 100,
        }
        weights_used = {
            "hhi":         None,
            "tariff":      0.40,
            "export":      0.40,
            "operational": 0.20,
        }
        score_method = "event_only_with_operational"
    elif has_events:
        # Event-only fallback (Option 1, 2026-05-06)
        components = {
            "tariff": tariff_exposure * 100,
            "export": export_restriction * 100,
        }
        weights_used = {
            "hhi":         None,
            "tariff":      0.5,
            "export":      0.5,
            "operational": None,
        }
        score_method = "event_only_no_hhi"
    else:
        # Operational-only — facility data exists but no HHI and no events.
        # Rare today (mostly partner-seeded refining-stage facilities for
        # materials that don't yet have country-scoped events).
        components = {"operational": (operational_signal or 0.0) * 100}
        weights_used = {
            "hhi":         None,
            "tariff":      None,
            "export":      None,
            "operational": 1.0,
        }
        score_method = "operational_only"

    # Weighted sum — weights in each path sum to 1.0, so this is a clean
    # weighted average without an explicit normalisation step.
    composite_node_score = sum(
        components[k] * weights_used[k]  # type: ignore[operator]
        for k in components
    )

    # ── 9. Upsert into hs_code_geography_risk_scores ────────────────────────
    # Pre-2026-05-06 this listed every event tied to the HS mapping, ignoring
    # country.  After the G11 country-scope filter that was misleading
    # rationale data (events listed but not actually consumed for this
    # country's score).  Use the actual filtered result rows instead.
    event_ids_consumed = sorted({
        row.id for row in (*tariff_rows, *export_rows)
    })

    metadata: dict = {
        "reference_year":      latest_year,    # None on event-only fallback
        "share_country_count": len(all_shares),
        "hhi_raw":             round(hhi_at_stage, 4) if hhi_at_stage is not None else None,
        "tariff_event_count":  len(tariff_events),
        "export_event_count":  len(export_events),
        "event_ids_consumed":  event_ids_consumed,
        # G5 (2026-05-09): operational sub-score from facility capacity data
        "operational_signal":  round(operational_signal, 4) if operational_signal is not None else None,
        "facility_count":      op_facility_count,
        "at_risk_capacity_tpy": round(op_at_risk, 2) if op_at_risk else 0.0,
        "total_capacity_tpy":   round(op_total, 2) if op_total else 0.0,
        "score_method":        score_method,    # extended taxonomy 2026-05-09
        "weight_breakdown":    weights_used,
        "scoring_version":     SCORING_VERSION,
        # 11.1.A (2026-06): which related mappings contributed events to
        # this node's tariff_exposure / export_restriction values.
        "hs_propagation":      hs_propagation_meta,
        # 11.1.D (2026-06): per-component data-presence diagnostic.
        # Mirrors the 11.6 data_completeness pattern at Level-0 so the
        # Level-1 rollup can see which Level-0 nodes were thin.  Each
        # field is True when real data fed the corresponding sub-score,
        # False when the sub-score defaulted to 0 / None.  ``score_method``
        # already encodes which scoring path fired; this block makes the
        # individual component coverage queryable.
        "data_stage_coverage": {
            "hhi_data_present":         hhi_at_stage is not None,
            "tariff_data_present":      bool(tariff_events),
            "export_data_present":      bool(export_events),
            "operational_data_present": operational_signal is not None,
        },
    }

    stmt = (
        pg_insert(HsCodeGeographyRiskScore)
        .values(
            hs_mapping_id=hs_mapping_id,
            country_code=country_code,
            as_of_date=as_of_date,
            market_scope=market_scope,
            production_share=production_share,
            hhi_at_stage=hhi_at_stage,
            tariff_exposure=tariff_exposure,
            export_restriction=export_restriction,
            composite_node_score=composite_node_score,
            methodology_version=SCORING_VERSION,
            metadata_json=metadata,
        )
        .on_conflict_do_update(
            constraint="uq_hs_geo_score",
            set_={
                "production_share":     production_share,
                "hhi_at_stage":         hhi_at_stage,
                "tariff_exposure":      tariff_exposure,
                "export_restriction":   export_restriction,
                "composite_node_score": composite_node_score,
                "methodology_version":  SCORING_VERSION,
                "metadata_json":        metadata,
            },
        )
        .returning(HsCodeGeographyRiskScore)
    )

    row: HsCodeGeographyRiskScore = db.scalars(stmt).one()
    log.info(
        "hs_node_scorer.scored",
        hs_mapping_id=hs_mapping_id,
        country_code=country_code,
        as_of_date=as_of_date.isoformat(),
        market_scope=market_scope,
        score_method=score_method,
        production_share=round(production_share, 4),
        hhi_at_stage=round(hhi_at_stage, 4) if hhi_at_stage is not None else None,
        tariff_exposure=round(tariff_exposure, 4),
        export_restriction=round(export_restriction, 4),
        operational_signal=round(operational_signal, 4) if operational_signal is not None else None,
        facility_count=op_facility_count,
        composite_node_score=round(composite_node_score, 2),
    )
    return row


# ---------------------------------------------------------------------------
# Batch scorer
# ---------------------------------------------------------------------------

def score_all_hs_nodes(
    db: Session,
    as_of_date: Optional[date] = None,
    *,
    market_scope: str = "global",
    hs_mapping_ids: Optional[list[int]] = None,
) -> dict[str, int]:
    """Score every (HS node × country) pair that has production share data.

    This is the function called by the ``rescore-hs-nodes`` Inngest job.  It
    must complete before ``score_all_active_materials()`` runs.

    Args:
        db:             Active SQLAlchemy session.
        as_of_date:     Evaluation date; defaults to today.
        market_scope:   ``"global"`` (default) or ``"us"``.
        hs_mapping_ids: Optional filter — score only these HS mapping IDs.
                        Useful for partial rescoring after new data is ingested.

    Returns:
        Dict with ``pairs_scored``, ``pairs_skipped`` (no share data),
        ``nodes_processed`` counts.
    """
    if as_of_date is None:
        as_of_date = date.today()

    # ── Enumerate (hs_mapping_id × country_code) pairs from THREE sources ─────
    # 1. ``hs_code_production_shares`` — every country with a non-zero share
    #    for any HS node (the canonical HHI-anchored path).
    # 2. ``risk_event_hs_mappings`` × ``risk_event_geographies`` — every
    #    (mapping × country) pair that has tariff or export events tagged
    #    with the right ``geography_context``.  Without this the event-only
    #    fallback path (Option 1, 2026-05-06) would never fire from the
    #    batch entrypoint: refined / intermediate stages with hundreds of
    #    events but no MCS production data would stay un-scored.
    # 3. ``facility_material_links`` × ``facilities`` — every (mapping ×
    #    country) pair that has facility capacity data at the mapping's
    #    stage.  Added for G5 (2026-05-09) — the new ``operational_only``
    #    scoring path needs to fire even when no shares or events exist
    #    (e.g. a partner-seeded refining facility for a material with no
    #    country-scoped events yet).
    share_pair_stmt = (
        select(
            HsCodeProductionShare.hs_mapping_id,
            HsCodeProductionShare.country_code,
        )
        .where(
            HsCodeProductionShare.market_scope == market_scope,
            HsCodeProductionShare.production_share > 0,
        )
        .distinct()
    )
    if hs_mapping_ids is not None:
        share_pair_stmt = share_pair_stmt.where(
            HsCodeProductionShare.hs_mapping_id.in_(hs_mapping_ids)
        )

    # Tariff side: implementing country is the importer; producer is in
    # ``geography_context="affected"``.  Export side: implementing country
    # IS the producer; ``geography_context="primary"``.  Same pattern as
    # ``score_hs_node_geography`` lines 235-298.
    event_pair_stmt = (
        select(
            RiskEventHsMapping.hs_mapping_id,
            RiskEventGeography.country_code,
        )
        .join(RiskEvent, RiskEvent.id == RiskEventHsMapping.risk_event_id)
        .join(RiskEventGeography, RiskEventGeography.risk_event_id == RiskEvent.id)
        .where(
            (
                (RiskEvent.event_subtype.in_(_TARIFF_SUBTYPES))
                & (RiskEventGeography.geography_context == _TARIFF_GEOGRAPHY_CONTEXT)
            )
            | (
                (RiskEvent.event_subtype.in_(_EXPORT_SUBTYPES))
                & (RiskEventGeography.geography_context == _EXPORT_GEOGRAPHY_CONTEXT)
            )
        )
        .distinct()
    )
    if hs_mapping_ids is not None:
        event_pair_stmt = event_pair_stmt.where(
            RiskEventHsMapping.hs_mapping_id.in_(hs_mapping_ids)
        )

    # Facility-pair source: every (mapping × country) where the mapping
    # has a stage AND a facility with non-NULL capacity exists at that
    # stage in that country.  Joins via ``HsCodeMaterialMapping.material_id``
    # because facility links are keyed by material, not HS prefix.
    facility_pair_stmt = (
        select(
            HsCodeMaterialMapping.id.label("hs_mapping_id"),
            Facility.country.label("country_code"),
        )
        .join(
            FacilityMaterialLink,
            (FacilityMaterialLink.material_id == HsCodeMaterialMapping.material_id)
            & (FacilityMaterialLink.supply_chain_stage == HsCodeMaterialMapping.supply_chain_stage),
        )
        .join(Facility, Facility.id == FacilityMaterialLink.facility_id)
        .where(
            HsCodeMaterialMapping.market_scope == market_scope,
            HsCodeMaterialMapping.supply_chain_stage.is_not(None),
            FacilityMaterialLink.annual_capacity_tpy.is_not(None),
        )
        .distinct()
    )
    if hs_mapping_ids is not None:
        facility_pair_stmt = facility_pair_stmt.where(
            HsCodeMaterialMapping.id.in_(hs_mapping_ids)
        )

    # Union and de-dupe (a pair may appear in multiple sources — that's
    # fine, ``score_hs_node_geography`` is idempotent on the unique key).
    share_pairs = db.execute(share_pair_stmt).all()
    event_pairs = db.execute(event_pair_stmt).all()
    facility_pairs = db.execute(facility_pair_stmt).all()
    pairs = list({
        (mid, cc) for mid, cc in (*share_pairs, *event_pairs, *facility_pairs)
    })
    pairs_scored = pairs_skipped = 0
    processed_nodes: set[int] = set()

    log.info(
        "hs_node_scorer.batch.start",
        pair_count=len(pairs),
        as_of_date=as_of_date.isoformat(),
        market_scope=market_scope,
    )

    for hs_mapping_id, country_code in pairs:
        try:
            result = score_hs_node_geography(
                db,
                hs_mapping_id=hs_mapping_id,
                country_code=country_code,
                as_of_date=as_of_date,
                market_scope=market_scope,
            )
            if result is None:
                pairs_skipped += 1
            else:
                pairs_scored += 1
                processed_nodes.add(hs_mapping_id)
            db.commit()
        except Exception:
            db.rollback()
            log.exception(
                "hs_node_scorer.batch.pair_failed",
                hs_mapping_id=hs_mapping_id,
                country_code=country_code,
            )
            pairs_skipped += 1

    log.info(
        "hs_node_scorer.batch.complete",
        pairs_scored=pairs_scored,
        pairs_skipped=pairs_skipped,
        nodes_processed=len(processed_nodes),
        as_of_date=as_of_date.isoformat(),
    )
    return {
        "pairs_scored": pairs_scored,
        "pairs_skipped": pairs_skipped,
        "nodes_processed": len(processed_nodes),
    }
