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

from app.models.regulatory import RiskEvent, RiskEventGeography, RiskEventHsMapping
from app.models.scoring import HsCodeGeographyRiskScore
from app.models.supply import HsCodeMaterialMapping, HsCodeProductionShare
from app.services.scoring.decay import compute_recency_multiplier
from app.constants import RiskCategory
from app.services.scoring.supplier_risk import SCORING_VERSION

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Composite score weights — must sum to 1.0
# ---------------------------------------------------------------------------

_HHI_WEIGHT = 0.50
_TARIFF_WEIGHT = 0.25
_EXPORT_WEIGHT = 0.25

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
    no production share data exists for the node (nothing to score).

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
    latest_year: Optional[int] = db.scalar(
        select(func.max(HsCodeProductionShare.reference_year)).where(
            HsCodeProductionShare.hs_mapping_id == hs_mapping_id,
            HsCodeProductionShare.market_scope == market_scope,
        )
    )
    if latest_year is None:
        log.debug(
            "hs_node_scorer.no_production_shares",
            hs_mapping_id=hs_mapping_id,
            country_code=country_code,
            market_scope=market_scope,
        )
        return None

    # ── 2. Load all production shares for this node + year ──────────────────
    all_shares: list[HsCodeProductionShare] = list(
        db.scalars(
            select(HsCodeProductionShare).where(
                HsCodeProductionShare.hs_mapping_id == hs_mapping_id,
                HsCodeProductionShare.reference_year == latest_year,
                HsCodeProductionShare.market_scope == market_scope,
            )
        ).all()
    )

    # ── 3. This country's production share (0.0 if not in dataset) ──────────
    country_row = next(
        (s for s in all_shares if s.country_code == country_code), None
    )
    production_share: float = country_row.production_share if country_row else 0.0

    # ── 4. HHI — computed from all countries in this node + year ───────────
    share_values = [s.production_share for s in all_shares if s.production_share > 0]
    hhi_at_stage = _compute_hhi(share_values)

    # ── 5. Tariff events scoped to this HS code AND this country ────────────
    # G11 (2026-05-06): tariff events are import-side interventions where the
    # implementing country is the importer; the producer being targeted is in
    # ``RiskEventGeography`` with ``geography_context="affected"``.  Join on
    # both the HS code and the affected-country tag so a US tariff on Chinese
    # lithium lifts CN's score on the lithium HS node, not US's.  Events with
    # no ``"affected"`` geography row are excluded entirely (strict
    # attribution; no global fallback).  Filter on typed ``event_subtype``
    # column (migration 040), not the ingester-specific ``event_type``.
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
            RiskEventHsMapping.hs_mapping_id == hs_mapping_id,
            RiskEvent.event_subtype.in_(_TARIFF_SUBTYPES),
            RiskEventGeography.country_code == country_code,
            RiskEventGeography.geography_context == _TARIFF_GEOGRAPHY_CONTEXT,
        )
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
            RiskEventHsMapping.hs_mapping_id == hs_mapping_id,
            RiskEvent.event_subtype.in_(_EXPORT_SUBTYPES),
            RiskEventGeography.country_code == country_code,
            RiskEventGeography.geography_context == _EXPORT_GEOGRAPHY_CONTEXT,
        )
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

    # ── 7. Composite node score (0–100) ─────────────────────────────────────
    # HHI contribution: scale HHI (0–1) to 0–100 then weight
    hhi_component = hhi_at_stage * 100 * _HHI_WEIGHT
    tariff_component = tariff_exposure * 100 * _TARIFF_WEIGHT
    export_component = export_restriction * 100 * _EXPORT_WEIGHT
    composite_node_score = hhi_component + tariff_component + export_component

    # ── 8. Upsert into hs_code_geography_risk_scores ────────────────────────
    # Pre-2026-05-06 this listed every event tied to the HS mapping, ignoring
    # country.  After the G11 country-scope filter that was misleading
    # rationale data (events listed but not actually consumed for this
    # country's score).  Use the actual filtered result rows instead.
    event_ids_consumed = sorted({
        row.id for row in (*tariff_rows, *export_rows)
    })

    metadata: dict = {
        "reference_year": latest_year,
        "share_country_count": len(all_shares),
        "hhi_raw": round(hhi_at_stage, 4),
        "tariff_event_count": len(tariff_events),
        "export_event_count": len(export_events),
        "event_ids_consumed": event_ids_consumed,
        "weight_breakdown": {
            "hhi":    _HHI_WEIGHT,
            "tariff": _TARIFF_WEIGHT,
            "export": _EXPORT_WEIGHT,
        },
        "scoring_version": SCORING_VERSION,
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
        production_share=round(production_share, 4),
        hhi_at_stage=round(hhi_at_stage, 4),
        tariff_exposure=round(tariff_exposure, 4),
        export_restriction=round(export_restriction, 4),
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

    # Enumerate all (hs_mapping_id × country_code) pairs with production shares
    pair_stmt = (
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
        pair_stmt = pair_stmt.where(
            HsCodeProductionShare.hs_mapping_id.in_(hs_mapping_ids)
        )

    pairs = db.execute(pair_stmt).all()
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
