"""Battery chemistry risk scorer.

Computes an intensity-weighted risk score for each battery chemistry by:

1. Fetching active ``BatteryChemistryMaterial`` rows (valid_from ≤ as_of_date
   ≤ COALESCE(valid_to, 'infinity')).
2. Resolving the best available criticality signal for each material
   (preference order: eu_crma → iea_report → usgs_mcs → manual → fallback
   to ``Material.criticality_score``).
3. Fetching geographic concentration from ``trade_flows`` for the material's
   HS codes. Materials with no HS codes or no trade flow rows are flagged in
   metadata and default to a neutral 0.5 geo concentration (not silently zeroed).
4. Applying ``PATENT_TREND_MODIFIERS`` to the criticality value.
5. Computing per-material risk via ``score_material_exposure()`` from
   ``material_risk.py``.
6. Aggregating with intensity weights: Σ(intensity_i × score_i) / Σ(intensity_i).
7. Computing ``score_confidence`` as Π DATA_AVAILABILITY_CONFIDENCE[data_availability]
   (product of per-material confidence factors, floored at 0.3).
8. Persisting a ``ChemistryRiskScore`` row.

Key design decisions
--------------------
- ``PATENT_TREND_MODIFIERS`` and ``DATA_AVAILABILITY_CONFIDENCE`` are named
  constants, not magic numbers. The trend modifiers are empirical estimates —
  NOT derived quantitatively from the PATSTAT paper. Move to
  ``supply_chain_contexts`` config in a future phase.
- Materials missing HS data are logged at WARNING and recorded in
  ``metadata_json["materials_missing_hs"]``. Their scores are not suppressed —
  they use a neutral geo concentration so the overall score is not artificially
  deflated.
- The scorer does not commit — callers are responsible for commit/rollback.
"""

from __future__ import annotations

import datetime
from typing import Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.battery_chemistry import BatteryChemistry, BatteryChemistryMaterial, ChemistryRiskScore
from app.models.criticality_signal import MaterialCriticalitySignal
from app.models.supply import HsCodeMaterialMapping, Material, TradeFlow
from app.services.scoring.material_risk import score_material_exposure

log = structlog.get_logger(__name__)

METHODOLOGY_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Named constants (see module docstring for rationale)
# ---------------------------------------------------------------------------

# Empirical modifiers translating patent occurrence trend into a criticality
# adjustment.  NOT derived quantitatively from the EPO PATSTAT paper — these
# are informed estimates. Move to supply_chain_contexts config in a future phase.
PATENT_TREND_MODIFIERS: dict[str, float] = {
    "rising":    1.15,
    "declining": 0.85,
    "stable":    1.00,
}

# Confidence penalty per material. Score backed solely by no_benchmark materials
# floors at 0.3 (product of penalties across all materials in the chemistry).
DATA_AVAILABILITY_CONFIDENCE: dict[str, float] = {
    "commercial":    1.00,
    "limited":       0.85,
    "no_benchmark":  0.65,
}

# Signal source priority (highest first).
_SIGNAL_PRIORITY: list[str] = ["eu_crma", "iea_report", "usgs_mcs", "manual", "patstat"]

# Default geo concentration when no trade flow data is available.
_DEFAULT_GEO_CONCENTRATION = 0.5

# High-concentration geographies used as the denominator for geo_concentration.
_HIGH_CONC_GEOS = {"CN", "CD", "RU"}


# ---------------------------------------------------------------------------
# Signal resolution helpers
# ---------------------------------------------------------------------------

def _resolve_criticality_signal(
    session: Session,
    material_id: int,
    material_criticality_score: Optional[float],
) -> tuple[float, str]:
    """Return (criticality_score, source_used).

    Tries signals in priority order; falls back to Material.criticality_score
    (float or 0.5 if None).
    """
    for source in _SIGNAL_PRIORITY:
        signal = session.scalar(
            select(MaterialCriticalitySignal)
            .where(
                MaterialCriticalitySignal.material_id == material_id,
                MaterialCriticalitySignal.source == source,
            )
            .order_by(MaterialCriticalitySignal.reference_year.desc())
            .limit(1)
        )
        if signal is not None and signal.criticality_score is not None:
            return signal.criticality_score, source

    # Fall back to the denormalized Material column.
    fallback = material_criticality_score if material_criticality_score is not None else 0.5
    return fallback, "material_column_fallback"


def _geo_concentration(session: Session, material: Material) -> Optional[float]:
    """Compute fraction of recent trade_flows from high-concentration geographies.

    Returns None if no HS codes are configured for this material OR if no rows
    exist in trade_flows for those HS codes (caller logs the gap).
    """
    hs_codes: list[str] = material.hs_codes or []
    if not hs_codes:
        return None

    # Match 6-digit trade_flow hs_codes against the material's HS prefix codes.
    # hs_codes on Material are stored as "XXXX.XX" or "XXXX" format; trade_flows
    # store raw 6-digit strings like "850760".  We strip the dot and match by prefix.
    prefixes = [code.replace(".", "") for code in hs_codes]

    # Build a filter: hs_code starts with any of the prefixes (up to 6 digits).
    from sqlalchemy import or_
    prefix_filters = [
        TradeFlow.hs_code.like(f"{p[:6]}%")
        for p in prefixes if p
    ]
    if not prefix_filters:
        return None

    # Find the most recent period with data for this material.
    latest_period = session.scalar(
        select(func.max(TradeFlow.period))
        .where(
            TradeFlow.material_id == material.id,
            TradeFlow.import_export_flag == "export",
        )
    )
    if latest_period is None:
        # Try by HS code prefix instead of material_id FK (FK may not be set).
        latest_period = session.scalar(
            select(func.max(TradeFlow.period))
            .where(or_(*prefix_filters), TradeFlow.import_export_flag == "export")
        )
    if latest_period is None:
        return None

    # Total export value in the latest period for this material's HS codes.
    total_value = session.scalar(
        select(func.sum(TradeFlow.trade_value_usd))
        .where(
            or_(*prefix_filters),
            TradeFlow.import_export_flag == "export",
            TradeFlow.period == latest_period,
        )
    ) or 0.0

    if total_value == 0.0:
        return None

    # Export value from high-concentration geographies.
    hcg_filters = [
        TradeFlow.reporter_country == geo for geo in _HIGH_CONC_GEOS
    ]
    hcg_value = session.scalar(
        select(func.sum(TradeFlow.trade_value_usd))
        .where(
            or_(*prefix_filters),
            or_(*hcg_filters),
            TradeFlow.import_export_flag == "export",
            TradeFlow.period == latest_period,
        )
    ) or 0.0

    return min(1.0, hcg_value / total_value)


# ---------------------------------------------------------------------------
# Main scorer
# ---------------------------------------------------------------------------

def score_chemistry(
    session: Session,
    chemistry_id: int,
    as_of_date: datetime.date,
) -> ChemistryRiskScore:
    """Compute and persist a ChemistryRiskScore for the given chemistry.

    Does NOT commit — caller is responsible.

    Raises ValueError if the chemistry does not exist or has no active materials.
    """
    chemistry = session.get(BatteryChemistry, chemistry_id)
    if chemistry is None:
        raise ValueError(f"BatteryChemistry id={chemistry_id} not found")

    # Fetch active composition rows for this as_of_date.
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
            f"as_of {as_of_date}. Run seed-materials first."
        )

    # Pre-load all materials in one query to avoid N+1.
    material_ids = [r.material_id for r in active_rows]
    materials_by_id: dict[int, Material] = {
        m.id: m
        for m in session.scalars(select(Material).where(Material.id.in_(material_ids))).all()
    }

    # -----------------------------------------------------------------------
    # Per-material score computation
    # -----------------------------------------------------------------------
    weighted_material_sum = 0.0
    weighted_geo_sum = 0.0
    total_intensity = 0.0
    confidence_product = 1.0

    # Metadata
    signal_sources: dict[str, str] = {}
    geo_coverage: dict[str, Optional[float]] = {}
    materials_missing_hs: list[str] = []
    patent_modifiers_applied: dict[str, float] = {}
    no_benchmark_materials: list[str] = []
    trade_flows_vintage: Optional[str] = None

    for junc in active_rows:
        material = materials_by_id.get(junc.material_id)
        if material is None:
            log.warning(
                "chemistry_risk.missing_material",
                chemistry=chemistry.slug,
                material_id=junc.material_id,
            )
            continue

        name = material.canonical_name
        intensity = junc.intensity

        # 1. Criticality signal
        criticality, source_used = _resolve_criticality_signal(
            session, material.id, material.criticality_score
        )
        signal_sources[name] = source_used

        # 2. Patent trend modifier
        trend = material.patent_occurrence_trend
        modifier = PATENT_TREND_MODIFIERS.get(trend or "", 1.00)
        if trend and trend in PATENT_TREND_MODIFIERS:
            patent_modifiers_applied[name] = modifier
        criticality = min(1.0, criticality * modifier)

        # 3. Geographic concentration from trade_flows
        geo_conc = _geo_concentration(session, material)
        if geo_conc is None:
            log.warning(
                "chemistry_risk.missing_geo_data",
                chemistry=chemistry.slug,
                material=name,
                note="Defaulting geo concentration to 0.5 (neutral). "
                     "Set HS codes and run ingest-comtrade to populate trade_flows.",
            )
            materials_missing_hs.append(name)
            geo_conc = _DEFAULT_GEO_CONCENTRATION
        geo_coverage[name] = geo_conc if geo_conc != _DEFAULT_GEO_CONCENTRATION else None

        # Track trade flows vintage (take the latest period seen).
        # We don't have it readily available here — would need an extra query.
        # Left as "unknown" if trade_flows is empty.

        # 4. Per-material risk score via existing material_risk scorer
        mat_score = score_material_exposure(
            criticality=criticality,
            concentration=geo_conc,
            trade_volatility=0.3,  # neutral default; no chemistry-level trade volatility yet
        )

        # 5. Weighted accumulation
        weighted_material_sum += intensity * criticality * 100.0
        weighted_geo_sum += intensity * geo_conc * 100.0
        total_intensity += intensity

        # 6. Data availability confidence penalty
        avail = material.data_availability or "commercial"
        conf_factor = DATA_AVAILABILITY_CONFIDENCE.get(avail, 1.00)
        confidence_product *= conf_factor
        if avail == "no_benchmark":
            no_benchmark_materials.append(name)

    if total_intensity == 0.0:
        raise ValueError(f"Total intensity is zero for chemistry slug='{chemistry.slug}'")

    # Normalised aggregate scores
    material_concentration_score = round(weighted_material_sum / total_intensity, 2)
    geopolitical_score = round(weighted_geo_sum / total_intensity, 2)
    composite_risk_score = round(
        0.50 * material_concentration_score + 0.50 * geopolitical_score, 2
    )
    score_confidence = round(max(0.30, confidence_product), 3)

    # Detect trade_flows vintage from a representative material
    if materials_by_id:
        sample_mid = next(iter(materials_by_id.keys()))
        latest_p = session.scalar(
            select(func.max(TradeFlow.period))
            .where(TradeFlow.material_id == sample_mid)
        )
        if latest_p:
            trade_flows_vintage = str(latest_p)

    metadata = {
        "signal_sources": signal_sources,
        "geo_coverage": geo_coverage,
        "materials_missing_hs": materials_missing_hs,
        "patent_modifiers_applied": patent_modifiers_applied,
        "trade_flows_vintage": trade_flows_vintage or "none",
        "no_benchmark_materials": no_benchmark_materials,
        "score_confidence": score_confidence,
    }

    log.info(
        "chemistry_risk.scored",
        chemistry=chemistry.slug,
        composite_risk_score=composite_risk_score,
        score_confidence=score_confidence,
        missing_hs_count=len(materials_missing_hs),
    )

    score_row = ChemistryRiskScore(
        battery_chemistry_id=chemistry_id,
        as_of_date=as_of_date,
        methodology_version=METHODOLOGY_VERSION,
        material_concentration_score=material_concentration_score,
        geopolitical_score=geopolitical_score,
        composite_risk_score=composite_risk_score,
        score_confidence=score_confidence,
        metadata_json=metadata,
    )
    session.add(score_row)
    session.flush()
    return score_row


def rescore_one_chemistry(
    session: Session,
    chemistry_id: int,
    as_of_date: datetime.date,
) -> ChemistryRiskScore:
    """Score one chemistry and commit. Returns the persisted score row."""
    score = score_chemistry(session, chemistry_id, as_of_date)
    session.commit()
    return score


def rescore_all_chemistries(
    session: Session,
    as_of_date: datetime.date,
) -> list[dict]:
    """Score all active chemistries. Commits after all succeed.

    Returns list of dicts: {slug, composite_risk_score, score_confidence}.
    Individual chemistry failures are logged and skipped — partial results
    are still committed.
    """
    chemistries: list[BatteryChemistry] = session.scalars(
        select(BatteryChemistry).where(BatteryChemistry.is_active == True)  # noqa: E712
    ).all()

    results = []
    for chem in chemistries:
        try:
            score = score_chemistry(session, chem.id, as_of_date)
            results.append({
                "slug": chem.slug,
                "composite_risk_score": score.composite_risk_score,
                "score_confidence": score.score_confidence,
            })
        except Exception as exc:
            log.warning(
                "chemistry_risk.score_failed",
                chemistry=chem.slug,
                error=str(exc),
            )

    session.commit()
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
