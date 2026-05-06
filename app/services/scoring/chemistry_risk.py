"""Battery chemistry risk scorer (methodology v2.0 rollup path).

Computes an intensity-weighted risk score for each battery chemistry by
reading pre-computed ``MaterialGlobalRiskScore`` rows (one per material,
trade-flow-weighted across geographies) and aggregating with
``MARKET_PILLAR_WEIGHTS``.

Scoring hierarchy:
    material_geography_risk_scores  (per-geo, five pillars)
              ↓  trade-flow weighted avg
    material_global_risk_scores
              ↓  intensity-weighted avg across minerals
    chemistry_risk_scores           (this scorer)

The v1.0 direct-ingestion path (``score_chemistry``, ``rescore_one_chemistry``,
``rescore_all_chemistries``) was removed in the PR 10 cleanup. All callers
must use ``score_chemistry_from_rollup`` or ``score_all_chemistries_from_rollup``.
See docs/deprecation-audit.md §B1.
"""

from __future__ import annotations

import datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.battery_chemistry import BatteryChemistry, BatteryChemistryMaterial, ChemistryRiskScore
from app.models.criticality_signal import MaterialCriticalitySignal
from app.models.supply import Material

log = structlog.get_logger(__name__)

# Confidence penalty per material. Score backed solely by no_benchmark materials
# floors at 0.3 (product of penalties across all materials in the chemistry).
#
# "unknown" is the explicit sentinel for materials where data_availability
# has not been partner-reviewed yet.  Conservative penalty (0.70) so the
# resulting score_confidence visibly drops rather than silently inflating
# under the prior ``or "commercial"`` fallback.  When PATSTAT or partner
# review fills these in, the penalty disappears.
DATA_AVAILABILITY_CONFIDENCE: dict[str, float] = {
    "commercial":    1.00,
    "limited":       0.85,
    "no_benchmark":  0.65,
    "unknown":       0.70,
}

METHODOLOGY_VERSION_ROLLUP = "2.0"


def _upsert_chemistry_score(
    session: Session,
    values: dict,
) -> int:
    """Insert-or-update one chemistry_risk_scores row on the logical key.

    Key = (battery_chemistry_id, as_of_date, methodology_version)
    """
    key_cols = {"battery_chemistry_id", "as_of_date", "methodology_version"}
    update_cols = {k: v for k, v in values.items() if k not in key_cols}
    update_cols["computed_at"] = func.now()

    stmt = (
        pg_insert(ChemistryRiskScore)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_chemistry_risk_score_key",
            set_=update_cols,
        )
        .returning(ChemistryRiskScore.id)
    )
    return int(session.execute(stmt).scalar_one())


# ---------------------------------------------------------------------------
# Rollup-based chemistry scorer (methodology_version 2.0)
# ---------------------------------------------------------------------------

def score_chemistry_from_rollup(
    session: Session,
    chemistry_id: int,
    as_of_date: datetime.date,
) -> ChemistryRiskScore:
    """Compute and persist a ChemistryRiskScore from MaterialGlobalRiskScore rows.

    For each active constituent material, reads its most recent
    MaterialGlobalRiskScore ≤ as_of_date and intensity-weights all five pillars.
    Produces a ChemistryRiskScore with methodology_version=2.0 and all five
    pillar columns populated.

    Falls back per-material: if no MaterialGlobalRiskScore exists for a material
    (e.g. first run, or material has no geo scores yet), that material is logged
    at WARNING and excluded from the weighted average. A score computed with
    missing materials is still written — ``score_confidence`` is penalised by
    data_availability as usual, and the rationale records which materials were
    missing.

    Does NOT commit — caller is responsible.

    Raises ValueError if the chemistry doesn't exist, has no active materials,
    or ALL constituent materials are missing global scores.
    """
    from app.models.scoring import MaterialGlobalRiskScore
    from app.services.scoring.market_aggregator import MARKET_PILLAR_WEIGHTS

    chemistry = session.get(BatteryChemistry, chemistry_id)
    if chemistry is None:
        raise ValueError(f"BatteryChemistry id={chemistry_id} not found")

    active_rows = session.scalars(
        select(BatteryChemistryMaterial)
        .where(
            BatteryChemistryMaterial.battery_chemistry_id == chemistry_id,
            BatteryChemistryMaterial.valid_from <= as_of_date,
            (
                (BatteryChemistryMaterial.valid_to == None) |  # noqa: E711
                (BatteryChemistryMaterial.valid_to >= as_of_date)
            ),
        )
        .order_by(BatteryChemistryMaterial.material_id)
    ).all()

    if not active_rows:
        raise ValueError(
            f"No active battery_chemistry_materials for slug='{chemistry.slug}' "
            f"as_of {as_of_date}."
        )

    material_ids = [r.material_id for r in active_rows]
    materials_by_id: dict[int, Material] = {
        m.id: m
        for m in session.scalars(select(Material).where(Material.id.in_(material_ids))).all()
    }

    # Pre-load the most recent MaterialGlobalRiskScore per material
    from sqlalchemy import func as sqlfunc
    latest_global_subq = (
        select(
            MaterialGlobalRiskScore.material_id,
            sqlfunc.max(MaterialGlobalRiskScore.as_of_date).label("max_date"),
        )
        .where(
            MaterialGlobalRiskScore.material_id.in_(material_ids),
            MaterialGlobalRiskScore.as_of_date <= as_of_date,
        )
        .group_by(MaterialGlobalRiskScore.material_id)
        .subquery()
    )
    global_scores: dict[int, MaterialGlobalRiskScore] = {
        gs.material_id: gs
        for gs in session.scalars(
            select(MaterialGlobalRiskScore)
            .join(
                latest_global_subq,
                (MaterialGlobalRiskScore.material_id == latest_global_subq.c.material_id)
                & (MaterialGlobalRiskScore.as_of_date == latest_global_subq.c.max_date),
            )
        ).all()
    }

    # ── Weighted accumulation across constituent materials ──────────────────
    pillar_sums = {
        "material_concentration_score": 0.0,
        "geopolitical_trade_score": 0.0,
        "regulatory_compliance_score": 0.0,
        "operational_score": 0.0,
        "financial_pressure_score": 0.0,
    }
    total_intensity = 0.0
    confidence_product = 1.0

    # Metadata
    material_scores_used: dict[str, dict] = {}
    materials_missing_global: list[str] = []
    no_benchmark_materials: list[str] = []
    # Materials whose data_availability column is NULL — partner has
    # not yet classified them as commercial / limited / no_benchmark.
    # Currently treated with a conservative confidence penalty (0.70)
    # via the ``or "unknown"`` fallback below; surfaced in the rationale
    # so the partner can see exactly which rows need review.
    materials_unknown_availability: list[str] = []

    for junc in active_rows:
        material = materials_by_id.get(junc.material_id)
        if material is None:
            continue

        name = material.canonical_name
        intensity = junc.intensity
        gs = global_scores.get(junc.material_id)

        if gs is None:
            log.warning(
                "chemistry_risk.missing_global_score",
                chemistry=chemistry.slug,
                material=name,
                note="No MaterialGlobalRiskScore found — material excluded from rollup",
            )
            materials_missing_global.append(name)
            continue

        for col in pillar_sums:
            val = getattr(gs, col)
            if val is not None:
                pillar_sums[col] += intensity * float(val)

        total_intensity += intensity

        # Default-to-"unknown" rather than "commercial" — surfacing
        # missing data via the rationale instead of silently treating
        # NULL rows as fully reliable.  See May 2026 audit: 35 of 39
        # USGS-tracked materials had hardcoded data_availability values
        # with no provenance and have been stripped to NULL pending
        # partner review.
        avail = material.data_availability or "unknown"
        conf_factor = DATA_AVAILABILITY_CONFIDENCE.get(avail, 0.70)
        confidence_product *= conf_factor
        if avail == "no_benchmark":
            no_benchmark_materials.append(name)
        elif avail == "unknown":
            materials_unknown_availability.append(name)

        material_scores_used[name] = {
            "material_id": junc.material_id,
            "intensity": intensity,
            "global_score_date": gs.as_of_date.isoformat(),
            "geo_count": gs.trade_weighted_geo_count,
            "weight_source": (gs.rationale_json or {}).get("weight_source"),
            "pillars": {col: getattr(gs, col) for col in pillar_sums},
            "overall": gs.overall_risk_score,
        }

    if total_intensity == 0.0:
        raise ValueError(
            f"No materials with global scores for chemistry slug='{chemistry.slug}'. "
            f"Run score_all_material_global_rollups() first."
        )

    # Normalise by total intensity
    normalised = {col: round(v / total_intensity, 2) for col, v in pillar_sums.items()}

    # Composite = MARKET_PILLAR_WEIGHTS weighted average of all five pillars
    composite_risk_score = round(
        MARKET_PILLAR_WEIGHTS["material"]     * normalised["material_concentration_score"]
        + MARKET_PILLAR_WEIGHTS["geopolitical"] * normalised["geopolitical_trade_score"]
        + MARKET_PILLAR_WEIGHTS["regulatory"]   * normalised["regulatory_compliance_score"]
        + MARKET_PILLAR_WEIGHTS["operational"]  * normalised["operational_score"]
        + MARKET_PILLAR_WEIGHTS["financial"]    * normalised["financial_pressure_score"],
        2,
    )
    score_confidence = round(max(0.30, confidence_product), 3)

    metadata = {
        "methodology": "rollup_v2",
        "materials_scored": list(material_scores_used.keys()),
        "materials_missing_global_score": materials_missing_global,
        "no_benchmark_materials": no_benchmark_materials,
        # Materials with data_availability=NULL are penalised by the
        # "unknown" confidence factor (0.70) and listed here so the
        # rationale shows exactly which rows still need partner review.
        "materials_unknown_availability": materials_unknown_availability,
        "score_confidence": score_confidence,
        "pillar_weights_used": MARKET_PILLAR_WEIGHTS,
        "material_detail": material_scores_used,
    }

    log.info(
        "chemistry_risk.rollup_scored",
        chemistry=chemistry.slug,
        composite_risk_score=composite_risk_score,
        score_confidence=score_confidence,
        materials_scored=len(material_scores_used),
        materials_missing=len(materials_missing_global),
    )

    upsert_vals = {
        "battery_chemistry_id": chemistry_id,
        "as_of_date": as_of_date,
        "methodology_version": METHODOLOGY_VERSION_ROLLUP,
        "material_concentration_score": normalised["material_concentration_score"],
        "geopolitical_score": normalised["geopolitical_trade_score"],
        "regulatory_compliance_score": normalised["regulatory_compliance_score"],
        "operational_score": normalised["operational_score"],
        "financial_pressure_score": normalised["financial_pressure_score"],
        "composite_risk_score": composite_risk_score,
        "score_confidence": score_confidence,
        "metadata_json": metadata,
    }
    row_id = _upsert_chemistry_score(session, upsert_vals)
    score_row = session.get(ChemistryRiskScore, row_id)
    if score_row is None:
        raise ValueError("Failed to persist chemistry rollup row")
    return score_row


def score_all_chemistries_from_rollup(
    session: Session,
    as_of_date: datetime.date,
) -> list[dict]:
    """Score all active chemistries using MaterialGlobalRiskScore rollup data.

    Commits per-chemistry to avoid one failure rolling back the batch.
    Returns list of dicts: {slug, composite_risk_score, score_confidence,
    materials_scored, materials_missing}.
    """
    chemistries: list[BatteryChemistry] = session.scalars(
        select(BatteryChemistry).where(BatteryChemistry.is_active == True)  # noqa: E712
    ).all()

    results = []
    for chem in chemistries:
        try:
            score = score_chemistry_from_rollup(session, chem.id, as_of_date)
            session.commit()
            meta = score.metadata_json or {}
            results.append({
                "slug": chem.slug,
                "composite_risk_score": score.composite_risk_score,
                "score_confidence": score.score_confidence,
                "methodology_version": METHODOLOGY_VERSION_ROLLUP,
                "materials_scored": len(meta.get("materials_scored", [])),
                "materials_missing": len(meta.get("materials_missing_global_score", [])),
            })
        except Exception as exc:
            session.rollback()
            log.warning(
                "chemistry_risk.rollup_score_failed",
                chemistry=chem.slug,
                error=str(exc),
            )

    return results


def _sync_patent_trend(session: Session, material_id: int) -> None:
    """Update materials.patent_occurrence_trend from the latest signal.

    Called after any write to material_criticality_signals to keep the
    denormalized cache consistent.  Does not commit.
    """
    latest_signal = session.scalar(
        select(MaterialCriticalitySignal)
        .where(MaterialCriticalitySignal.material_id == material_id)
        .order_by(
            MaterialCriticalitySignal.reference_year.desc(),
        )
        .limit(1)
    )
    if latest_signal is None or latest_signal.trend_direction is None:
        return

    material = session.get(Material, material_id)
    if material is not None:
        material.patent_occurrence_trend = latest_signal.trend_direction
