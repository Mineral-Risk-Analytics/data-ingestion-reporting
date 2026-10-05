"""
Scoring orchestrator: single entry point for computing and persisting company scores.

Call rescore_company() after ingestion or from the manual rescore API endpoint.
Never call component scorers (material_risk, geopolitical_risk, etc.) directly from
outside this module — they must be invoked through this coordinator so that the
full evidence → aggregation → scoring → persistence pipeline runs as a unit.

Transaction contract:
  rescore_company() calls db.flush() to obtain the new row ID but does NOT commit.
  The caller owns the transaction and must call db.commit() (or db.rollback() on
  failure). This allows ingestion + scoring to share a single atomic commit.

Scoring v3.0 pipeline (vs. v2 below):
  - Pulls facilities + supplier chain + latest supplier scores.
  - Pulls scope-derived regulations (RegulationMaterialScope /
    RegulationGeographyScope) and folds them into active_obligations.
  - Pulls events tagged via RiskEventGeography / RiskEventMaterial /
    RiskEventRegulation and folds them into the appropriate aggregator.
  - Pulls the company's chemistry mix (via CompanyVehicleModel) and the
    chemistry material intensities, and uses both to re-weight the
    material pillar's CompanyMaterialExposure inputs.
  - Computes the sixth pillar (supply_chain_propagation) and persists the
    new ``supply_chain_propagation_score`` and ``propagation_depth_used``
    columns. The pillar is None when the company has no scoreable suppliers
    in the graph; supplier_risk.aggregate_supplier_risk renormalises the
    remaining five pillars.

Scoped views (UI hook):
  - ``rescore_company`` accepts a ``scope: ScoringScope`` kwarg threaded
    through every evidence_query helper. ``ScoringScope.ALL`` (default) is a
    true no-op.
  - When ``scope != ScoringScope.ALL``, the orchestrator forces
    ``persist=False`` — scoped runs never write to ``company_scores``.
    Propagation is also skipped under scope (set to None) and surfaced via
    ``rationale_json.signals_used.propagation_skipped_due_to_scope = True``;
    a follow-up PR will recursively rescore each supplier under the same
    scope so the pillar can return for scoped views too.
  - ``score_company_scoped`` is a thin wrapper that returns the in-memory
    ``CompanyScore`` (id stays ``None``).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import RiskCategory
from app.models.company import CompanyScore
from app.services.scoring import (
    financial_pressure as fp_module,
    geopolitical_risk,
    material_risk,
    propagation_risk,
    regulatory_risk,
    supplier_risk,
)
from app.services.scoring.evidence_aggregator import (
    derive_financial_inputs,
    derive_geopolitical_inputs,
    derive_material_inputs,
    derive_operational_inputs,
    derive_propagation_inputs,
    derive_regulatory_inputs,
)
from app.services.scoring.evidence_query import (
    EventWithRelevance,
    get_active_compliance_obligations,
    get_chemistry_material_intensities,
    get_chemistry_mix_for_company,
    get_chemistry_slugs,
    get_company_material_exposure,
    get_events_for_company,
    get_events_for_geographies,
    get_events_for_materials,
    get_events_for_regulations,
    get_facilities_for_company,
    get_filing_signals,
    get_latest_chemistry_risk_scores,
    get_latest_company_scores,
    get_regulations_scoping_company,
    get_supplier_chain,
    get_targeted_countries_for_events,
)
from app.services.scoring.types import (
    ChemistryMixContribution,
    ComponentScores,
    DecayParameters,
    PropagationContribution,
    ScoringInputs,
    ScoringScope,
    SupplierScoreRationale,
)

log = structlog.get_logger(__name__)


def rescore_company(
    db: Session,
    company_id: uuid.UUID,
    run_id: str,
    as_of_date: Optional[date] = None,
    *,
    propagation_max_depth: int = 2,
    propagation_max_suppliers: int = 50,
    propagation_depth_weights: tuple[float, ...] = propagation_risk.DEFAULT_DEPTH_WEIGHTS,
    scope: ScoringScope = ScoringScope.ALL,
    persist: bool = True,
) -> CompanyScore:
    """
    Compute and (by default) persist a new company_scores row for this company.

    Steps:
      1. Query evidence by category + facility + supplier-chain + chemistry
         (via evidence_query.py).
      2. Aggregate evidence into component inputs (via evidence_aggregator.py).
      3. Score each component (via the six pillar modules).
      4. Aggregate into an overall score (via supplier_risk.py).
      5. Build SupplierScoreRationale for rationale_json (incl. propagation
         chain + chemistry mix).
      6. Persist a new CompanyScore row (append-only — never overwrite).
         Skipped when ``persist=False`` (always the case for scoped runs).

    Returns the persisted CompanyScore ORM object (ID populated after flush).
    Raises on DB write failure — caller handles the transaction.
    """
    if as_of_date is None:
        as_of_date = date.today()

    # SCOPED RUNS NEVER PERSIST — single non-negotiable rule. The
    # company_scores time-series only ever contains comparable, full-scope
    # rows.
    if not scope.is_all() and persist:
        log.warning(
            "orchestrator.scoped_run_forced_to_non_persist",
            company_id=str(company_id),
            run_id=run_id,
        )
        persist = False

    log.info(
        "orchestrator.rescore_company.start",
        company_id=str(company_id),
        as_of_date=as_of_date.isoformat(),
        run_id=run_id,
        scoped=not scope.is_all(),
        persist=persist,
    )

    # --- STEP 1a: Existing company-tagged evidence ---
    material_exposures = get_company_material_exposure(db, company_id, scope=scope)
    trade_events = get_events_for_company(
        db, company_id, RiskCategory.GEOPOLITICAL_TRADE, as_of_date, scope=scope
    )
    regulatory_events = get_events_for_company(
        db, company_id, RiskCategory.REGULATORY_COMPLIANCE, as_of_date, scope=scope
    )
    operational_events = get_events_for_company(
        db, company_id, RiskCategory.OPERATIONAL, as_of_date, scope=scope
    )
    filing_events = get_filing_signals(db, company_id, scope=scope)
    active_obligations = get_active_compliance_obligations(db, company_id, scope=scope)

    # --- STEP 1b: New structural inputs (facilities, scope regs, propagated chain) ---
    facilities = get_facilities_for_company(db, company_id, scope=scope)
    material_ids: set[int] = {e.material_id for e in material_exposures}
    country_set: set[str] = {
        e.source_geography for e in material_exposures if e.source_geography
    } | {f.country for f in facilities if f.country}

    geo_events_trade = get_events_for_geographies(
        db, country_set, RiskCategory.GEOPOLITICAL_TRADE, as_of_date, scope=scope
    )
    geo_events_op = get_events_for_geographies(
        db, country_set, RiskCategory.OPERATIONAL, as_of_date, scope=scope
    )
    material_country_events = get_events_for_materials(
        db, material_ids, RiskCategory.GEOPOLITICAL_TRADE, as_of_date, scope=scope
    )
    scope_obligations = get_regulations_scoping_company(
        db, company_id, material_ids, country_set, scope=scope
    )
    regulation_keys: set[str] = {k for k, _ in scope_obligations} | {
        k for k, _ in active_obligations
    }
    regulation_events = get_events_for_regulations(
        db, regulation_keys, as_of_date, scope=scope
    )

    # --- STEP 1c: Chemistry mix + intensities ---
    chemistry_mix = get_chemistry_mix_for_company(
        db, company_id, as_of_date, scope=scope
    )
    chemistry_intensities: dict[int, dict[int, float]] = {}
    if chemistry_mix:
        chemistry_intensities = get_chemistry_material_intensities(
            db, set(chemistry_mix.keys()), as_of_date, scope=scope
        )

    # --- STEP 1d: Supplier chain + latest persisted scores (propagation) ---
    propagation_skipped_due_to_scope = not scope.is_all()
    supplier_chain = []
    latest_supplier_scores: dict[uuid.UUID, CompanyScore] = {}
    if not propagation_skipped_due_to_scope:
        supplier_chain = get_supplier_chain(
            db,
            company_id,
            max_depth=propagation_max_depth,
            max_visited=propagation_max_suppliers,
            scope=scope,
        )
        if supplier_chain:
            latest_supplier_scores = get_latest_company_scores(
                db, [edge.supplier_id for edge in supplier_chain], scope=scope
            )

    # --- STEP 2: Aggregate evidence into component inputs ---
    crit, conc, trade_vol = derive_material_inputs(
        material_exposures,
        trade_events,
        as_of_date,
        material_country_events=material_country_events,
        chemistry_mix=chemistry_mix,
        chemistry_intensities=chemistry_intensities or None,
    )
    ctry_conc, exp_rest, tariff = derive_geopolitical_inputs(
        trade_events,
        material_exposures,
        as_of_date,
        facilities=facilities,
        geo_events=geo_events_trade,
    )
    # Conditional scope intersection (Option C, EUR-Lex Section 5):
    # batch-load targeted_country geography scopes for any event in the
    # regulatory pool so derive_regulatory_inputs can downweight events
    # whose targeted producer countries the company has no exposure to.
    reg_event_ids: set[int] = {
        ew.event.id for ew in regulatory_events
    } | {ew.event.id for ew in regulation_events}
    event_targeted_countries = get_targeted_countries_for_events(
        db, reg_event_ids
    )
    # Track the count of "real" event impacts so we can report how many
    # synthetic single-country-cap violations were appended (Idea B).
    # ``derive_regulatory_inputs`` dedups the two event lists by event.id
    # internally, so we mirror that here for an accurate baseline.
    _real_event_impact_count = len({
        ew.event.id for ew in (list(regulatory_events) + list(regulation_events))
    })
    top_impacts, obligations, prox = derive_regulatory_inputs(
        regulatory_events,
        active_obligations,
        as_of_date,
        scope_obligations=scope_obligations,
        regulation_events=regulation_events,
        event_targeted_countries=event_targeted_countries,
        company_country_set=country_set,
        db=db,
        exposures=material_exposures,
    )
    threshold_violations_detected = max(
        0, len(top_impacts) - _real_event_impact_count
    )
    struct_dep, op_impacts = derive_operational_inputs(
        operational_events,
        as_of_date,
        facilities=facilities,
        facility_country_events=geo_events_op,
    )
    base_sig, lev_bon, liq_bon, fc = derive_financial_inputs(filing_events)

    # --- STEP 3: Score each component ---
    mat_score = material_risk.score_material_exposure(crit, conc, trade_vol)
    geo_score = geopolitical_risk.score_geopolitical_trade(ctry_conc, exp_rest, tariff)
    # Build 2 (2026-07-24): obligation base points come from the DB
    # (regulations.obligation_points, coalesced to 0) instead of the
    # retired COMPLIANCE_OBLIGATIONS dict.
    from app.models.regulatory import Regulation as _Regulation
    _ob_keys = {k for k, _ in obligations}
    _ob_points: dict[str, float] = {}
    if _ob_keys:
        for _k, _p in db.execute(
            select(_Regulation.regulation_key, _Regulation.obligation_points)
            .where(_Regulation.regulation_key.in_(_ob_keys))
        ):
            _ob_points[_k] = float(_p or 0)
    reg_score = regulatory_risk.score_regulatory_profile(
        top_impacts, obligations, prox, obligation_points=_ob_points
    )
    op_score = _score_operational(struct_dep, op_impacts)
    fin_score = fp_module.score_financial_pressure(base_sig, lev_bon, liq_bon, fc)

    # --- STEP 3b: Sixth pillar — supply-chain propagation ---
    propagation_score: Optional[float] = None
    propagation_depth_used: Optional[int] = None
    propagation_inputs: list[tuple[float, float, int]] = []
    if not propagation_skipped_due_to_scope and supplier_chain:
        propagation_inputs = derive_propagation_inputs(
            supplier_chain, latest_supplier_scores
        )
        if propagation_inputs:
            propagation_score = propagation_risk.score_propagation(
                propagation_inputs, propagation_depth_weights
            )
            propagation_depth_used = max(d for *_x, d in propagation_inputs)

    # --- STEP 4: Aggregate ---
    result = supplier_risk.aggregate_supplier_risk(
        mat_score,
        geo_score,
        reg_score,
        op_score,
        fin_score,
        propagation_score=propagation_score,
    )

    # --- STEP 5: Build rationale ---
    top_evidence = _collect_top_evidence(
        trade_events,
        regulatory_events,
        operational_events,
        filing_events,
        n=5,
    )
    propagation_chain_block = _build_propagation_chain_block(
        supplier_chain, latest_supplier_scores
    )
    chemistry_mix_block = _build_chemistry_mix_block(
        db, chemistry_mix
    )

    signals_used = {
        "facilities":               len(facilities),
        "scope_obligations":        len(scope_obligations),
        "regulation_events":        len(regulation_events),
        "geo_events_trade":         len(geo_events_trade),
        "geo_events_operational":   len(geo_events_op),
        "material_country_events":  len(material_country_events),
        "supplier_chain_size":      len(supplier_chain),
        "supplier_scores_used":     len(propagation_inputs),
        "chemistries_weighted":     len(chemistry_mix or {}),
        "threshold_violations_detected": threshold_violations_detected,
    }
    if propagation_skipped_due_to_scope:
        signals_used["propagation_skipped_due_to_scope"] = True

    rationale = SupplierScoreRationale(
        inputs=ScoringInputs(
            supplier_id=str(company_id),
            evidence_window_days=730,
            scoring_version=supplier_risk.SCORING_VERSION,
            run_id=run_id,
        ),
        components=ComponentScores(
            material=mat_score,
            geopolitical=geo_score,
            regulatory=reg_score,
            operational=op_score,
            financial=fin_score,
            overall=result["overall_risk_score"],
            supply_chain_propagation=propagation_score,
        ),
        top_evidence=top_evidence,
        decay=DecayParameters(
            eval_date=as_of_date.isoformat(),
            material_window=365,
            geopolitical_window=730,
            regulatory_window=365,
            operational_half_life=90,
        ),
        notes=_generate_notes(
            result,
            mat_score,
            geo_score,
            reg_score,
            op_score,
            fin_score,
            propagation_score,
        ),
        propagation_chain=propagation_chain_block,
        chemistry_mix=chemistry_mix_block,
        signals_used=signals_used,
    )

    score_row = CompanyScore(
        company_id=company_id,
        as_of_date=as_of_date,
        material_concentration_risk_score=mat_score,
        geopolitical_trade_risk_score=geo_score,
        regulatory_risk_score=reg_score,
        operational_risk_score=op_score,
        financial_pressure_score=fin_score,
        overall_risk_score=result["overall_risk_score"],
        supply_chain_propagation_score=propagation_score,
        propagation_depth_used=propagation_depth_used,
        scoring_version=result["scoring_version"],
        rationale_json=rationale.model_dump(),
    )

    if persist:
        db.add(score_row)
        db.flush()  # populates score_row.id — caller owns db.commit()

    log.info(
        "orchestrator.rescore_company.done",
        company_id=str(company_id),
        score_id=score_row.id,
        overall=round(result["overall_risk_score"], 2),
        scoring_version=result["scoring_version"],
        propagation=(
            round(propagation_score, 2) if propagation_score is not None else None
        ),
        persisted=persist,
    )
    return score_row


def score_company_scoped(
    db: Session,
    company_id: uuid.UUID,
    scope: ScoringScope,
    as_of_date: Optional[date] = None,
    run_id: Optional[str] = None,
) -> CompanyScore:
    """UI-facing entry point for scoped, never-persisted previews.

    Returns the in-memory ``CompanyScore`` (``id`` stays ``None``). All
    heavy lifting happens in ``rescore_company`` via the threaded ``scope``
    kwarg; the contract here is "scoped runs never write".
    """
    return rescore_company(
        db,
        company_id,
        run_id or f"scoped-{uuid.uuid4()}",
        as_of_date,
        scope=scope,
        persist=False,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _score_operational(struct_dep: float, op_impacts: list[float]) -> float:
    """
    Operational score: 40% structural dependency + 60% weighted event rollup.
    op_impacts are already recency- and relevance-weighted event_impact values.
    """
    event_component = sum(op_impacts) / len(op_impacts) if op_impacts else 0.0
    raw = (0.40 * struct_dep + 0.60 * event_component) * 100
    return min(100.0, raw)


def _collect_top_evidence(
    *event_lists: list[EventWithRelevance],
    n: int = 5,
) -> list[str]:
    """Collect IDs of the top N events by severity_score across all categories."""
    all_ew = [ew for lst in event_lists for ew in lst]
    sorted_ew = sorted(
        all_ew, key=lambda ew: ew.event.severity_score or 0.0, reverse=True
    )
    return [str(ew.event.id) for ew in sorted_ew[:n]]


def _build_propagation_chain_block(
    supplier_chain: list,
    latest_supplier_scores: dict[uuid.UUID, CompanyScore],
) -> list[PropagationContribution]:
    """One ``PropagationContribution`` per visited supplier (regardless of score)."""
    out: list[PropagationContribution] = []
    for edge in supplier_chain:
        score = latest_supplier_scores.get(edge.supplier_id)
        supplier_overall = (
            float(score.overall_risk_score)
            if score is not None and score.overall_risk_score is not None
            else 0.0
        )
        out.append(
            PropagationContribution(
                supplier_id=str(edge.supplier_id),
                depth=edge.depth,
                edge_volume_share=edge.cumulative_volume_share,
                supplier_overall_score=supplier_overall,
                supplier_score_as_of_date=(
                    score.as_of_date.isoformat()
                    if score is not None and score.as_of_date is not None
                    else None
                ),
            )
        )
    return out


def _build_chemistry_mix_block(
    db: Session,
    chemistry_mix: Optional[dict[int, float]],
) -> list[ChemistryMixContribution]:
    """One ``ChemistryMixContribution`` per chemistry in the mix."""
    if not chemistry_mix:
        return []

    chem_ids = set(chemistry_mix.keys())
    slugs = get_chemistry_slugs(db, chem_ids)
    risk_scores = get_latest_chemistry_risk_scores(db, chem_ids)

    out: list[ChemistryMixContribution] = []
    for chem_id, share in sorted(
        chemistry_mix.items(), key=lambda kv: kv[1], reverse=True
    ):
        rs = risk_scores.get(chem_id)
        out.append(
            ChemistryMixContribution(
                battery_chemistry_id=chem_id,
                chemistry_slug=slugs.get(chem_id),
                share_pct=share,
                composite_risk_score=(
                    float(rs.composite_risk_score)
                    if rs is not None and rs.composite_risk_score is not None
                    else None
                ),
                score_as_of_date=(
                    rs.as_of_date.isoformat()
                    if rs is not None and rs.as_of_date is not None
                    else None
                ),
            )
        )
    return out


def _generate_notes(
    result: dict,
    mat: float,
    geo: float,
    reg: float,
    op: float,
    fin: float,
    propagation: Optional[float],
) -> str:
    overall = result["overall_risk_score"]
    candidates = [
        ("material concentration", mat),
        ("geopolitical/trade", geo),
        ("regulatory/compliance", reg),
        ("operational", op),
        ("financial", fin),
    ]
    if propagation is not None:
        candidates.append(("supply-chain propagation", propagation))
    dominant = max(candidates, key=lambda x: x[1])
    band = (
        "Critical" if overall >= 75
        else "High" if overall >= 55
        else "Moderate" if overall >= 35
        else "Low"
    )
    propagation_note = (
        f" Propagation pillar {propagation:.1f}."
        if propagation is not None
        else " Propagation pillar skipped (no scoreable suppliers in graph)."
    )
    return (
        f"Overall score {overall:.1f} ({band}). "
        f"Dominant risk driver: {dominant[0]} ({dominant[1]:.1f})."
        f"{propagation_note} "
        f"Scoring version {result['scoring_version']}."
    )
