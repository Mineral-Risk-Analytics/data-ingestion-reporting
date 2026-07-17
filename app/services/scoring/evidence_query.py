"""
DB read layer for the scoring pipeline.

All functions return ORM objects or lightweight wrappers. No scoring math lives
here — only queries.

COMPANY LINKAGE:
  RiskEvent rows are linked to companies via the risk_event_companies junction table.
  All company-scoped event queries join through that table. The relevance_score from
  the junction row is surfaced as EventWithRelevance.relevance_score so the evidence
  aggregator can pass it as relevance_multiplier into compute_event_impact().

MATERIAL LINKAGE:
  For material-anchored scoring (no company required), use get_events_for_material()
  which joins through risk_event_materials instead.

COMPLIANCE OBLIGATIONS:
  get_active_compliance_obligations() reads from company_regulation_exposure.
  Falls back to text-derived approach while that table is empty.

SCOPE FILTERING:
  Every helper accepts a ``scope: ScoringScope = ScoringScope.ALL`` kwarg. Scope
  filters compose as additional WHERE clauses; ``ScoringScope.ALL`` is a true
  no-op (regression-tested). Scoped runs are only ever invoked via
  :func:`app.services.scoring.orchestrator.score_company_scoped`, which forces
  ``persist=False`` so scoped views never pollute the ``company_scores``
  time-series.

SUPPLY-CHAIN ROLLUP:
  ``get_supplier_chain`` does a BFS over ``CompanySupplyRelationship`` with
  cycle detection and a hard ``max_visited`` cap. ``get_latest_company_scores``
  fetches the most recent persisted ``CompanyScore`` per company so the
  propagation pillar consumes already-computed scores rather than recursing
  into rescore.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, defer, selectinload

from app.constants import RiskCategory
from app.models.battery_chemistry import (
    BatteryChemistry,
    BatteryChemistryMaterial,
    ChemistryRiskScore,
)
from app.models.company import (
    CompanyMaterialExposure,
    CompanyScore,
    CompanySupplyRelationship,
)
from app.models.documents import SourceDocument
from app.models.facility import CompanyFacility, Facility, FacilityMaterialLink
from app.models.regulatory import (
    CompanyRegulationExposure,
    Regulation,
    RegulationGeographyScope,
    RegulationMaterialScope,
    RiskEvent,
    RiskEventCompany,
    RiskEventGeography,
    RiskEventHsMapping,
    RiskEventMaterial,
    RiskEventRegulation,
)
from app.models.scoring import HsCodeGeographyRiskScore
from app.models.supply import HsCodeMaterialMapping
from app.models.vehicle import CompanyVehicleModel, VehicleModelChemistry
from app.services.scoring.decay import EVIDENCE_WINDOWS
from app.services.scoring.types import ScoringScope

log = structlog.get_logger(__name__)

HIGH_CONCENTRATION_GEOS = frozenset({"CN", "CD", "RU"})

# Weight applied to scope-only regulation hits (no CompanyRegulationExposure row)
# keyed by RegulationMaterialScope.scope_type.
#
# Rationale for each tier (descending severity):
#   banned                  0.70  Outright import / use prohibition — the strongest scope_type a
#                                 material can carry.  Added 2026-05-06 to close a latent gap where
#                                 ``banned`` rows fell through to the 0.50 default and scored LOWER
#                                 than ``restricted`` (0.60).  No current seed entry uses this value;
#                                 the weight is here so a future banned-material seed scores correctly.
#   strategic_raw_material  0.65  CRMA Annex II: binding 2030 extraction/processing/recycling
#                                 benchmarks; stronger enforcement obligation than disclosure alone.
#   restricted              0.60  Active restrictions (e.g. REACH SVHC authorisation); enforcement
#                                 is live but narrower in scope than a strategic designation.
#   disclosure_required     0.50  Standard disclosure / due-diligence mandate; default assumption
#                                 when no CompanyRegulationExposure status is recorded.
#   covered                 0.40  Indirect coverage (e.g. CBAM carbon certificate); less direct
#                                 enforcement exposure than a disclosure or restriction obligation.
#   compliant               0.20  A material the regulation explicitly lists as out-of-concern
#                                 or exempt.  Added 2026-06-07 to mirror the same entry in
#                                 ``event_impact.SCOPE_SEVERITY_MULTIPLIER`` (which carries 0.25
#                                 on the severity side).  Without this entry the relevance
#                                 multiplier fell through to the 0.50 default — a material
#                                 listed as 'compliant' was getting the same regulation-scope
#                                 weight as one listed as 'disclosure_required', which
#                                 contradicts the partner-curated semantics.
#   targeted_country        0.50  Used only on geography scopes, not material scopes; included for
#                                 completeness in case scope_type is ever added there.
#
# Unknown scope_type values fall back to 0.50 (same as disclosure_required).
_SCOPE_TYPE_WEIGHT: dict[str, float] = {
    "banned":                 0.70,
    "strategic_raw_material": 0.65,
    "restricted":             0.60,
    "disclosure_required":    0.50,
    "covered":                0.40,
    "compliant":              0.20,
}


@dataclass
class EventWithRelevance:
    """
    Pairs a RiskEvent with its per-company (or per-material) relevance score
    from the relevant junction table.
    relevance_score maps to the relevance_multiplier input of compute_event_impact().

    ``scope_type`` carries the regulation designation
    (``banned`` / ``restricted`` / ``strategic_raw_material`` /
    ``covered`` / ``disclosure_required``) when the row originated from a
    ``RiskEventMaterial`` populated by the EUR-Lex ingester.  Consumed by
    :func:`app.services.scoring.evidence_aggregator._impact` to widen
    per-material severity spread inside a single regulation.  ``None`` for
    company-anchored / geography-anchored events and for pre-migration
    junction rows — the multiplier is a passthrough in that case so
    backwards compatibility holds.
    """

    event: RiskEvent
    relevance_score: float  # 0.70–1.00
    scope_type: Optional[str] = None


@dataclass
class SupplierEdge:
    """One node visited during the supplier-chain BFS.

    ``cumulative_volume_share`` is the product of edge ``volume_share_pct``
    along the path from the root buyer to this supplier. ``None`` when any
    edge in the path lacks a volume share (we deliberately do NOT substitute
    a default mid-walk — uncertainty propagates).
    """

    supplier_id: uuid.UUID
    depth: int                                  # 1, 2, ...
    cumulative_volume_share: Optional[float]
    path: list[uuid.UUID]                       # buyer-to-this, exclusive of root


def _category_window_cutoff(category: RiskCategory, as_of_date: date) -> Optional[datetime]:
    window_days = EVIDENCE_WINDOWS[category]
    if window_days is None:
        return None
    cutoff_date = as_of_date - timedelta(days=window_days)
    return datetime(cutoff_date.year, cutoff_date.month, cutoff_date.day, tzinfo=timezone.utc)


def _dedup_event_rows(rows) -> list[EventWithRelevance]:
    """Collapse multiple junction rows for the same event into one EventWithRelevance.

    Keeps the maximum ``relevance_score`` per event (e.g. an event tagged to
    both ``CN`` and ``CD`` should not contribute twice when only one of those
    countries is the company's exposure focus).
    """
    by_id: dict[int, EventWithRelevance] = {}
    for ev, rel in rows:
        existing = by_id.get(ev.id)
        if existing is None or float(rel) > existing.relevance_score:
            by_id[ev.id] = EventWithRelevance(event=ev, relevance_score=float(rel))
    return list(by_id.values())


def _dedup_material_event_rows(rows) -> list[EventWithRelevance]:
    """3-tuple variant of :func:`_dedup_event_rows` for material queries.

    Rows are ``(event, relevance_score, scope_type)``.  When the same event
    is tagged to multiple materials in the query set, the row with the
    highest ``relevance_score`` wins; its ``scope_type`` is the one carried
    onto the resulting :class:`EventWithRelevance`.  This is the correct
    semantics because the scope-severity multiplier is then derived from
    the dominant material's designation.
    """
    by_id: dict[int, EventWithRelevance] = {}
    for ev, rel, scope_type in rows:
        rel_f = float(rel)
        existing = by_id.get(ev.id)
        if existing is None or rel_f > existing.relevance_score:
            by_id[ev.id] = EventWithRelevance(
                event=ev, relevance_score=rel_f, scope_type=scope_type
            )
    return list(by_id.values())


# ---------------------------------------------------------------------------
# Company-scoped queries
# ---------------------------------------------------------------------------

def get_events_for_company(
    db: Session,
    company_id: uuid.UUID,
    category: RiskCategory,
    as_of_date: date,
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> list[EventWithRelevance]:
    """
    Query risk_events for a company within the category-specific evidence window,
    joined through risk_event_companies.

    For FINANCIAL_PRESSURE delegates to get_filing_signals() which applies a
    most-recent-N filter instead of a time window.
    """
    if category == RiskCategory.FINANCIAL_PRESSURE:
        return get_filing_signals(db, company_id, scope=scope)

    cutoff = _category_window_cutoff(category, as_of_date)

    stmt = (
        select(RiskEvent, RiskEventCompany.relevance_score)
        .join(RiskEventCompany, RiskEventCompany.risk_event_id == RiskEvent.id)
        .where(
            RiskEventCompany.company_id == company_id,
            RiskEventCompany.review_status != "excluded",
            RiskEvent.duplicate_of_id.is_(None),  # 055: skip confirmed dupes
            RiskEvent.risk_categories_json.contains([category.value]),
        )
        # 2026-07-15 EGRESS: defer Text columns that scoring never reads —
        # traced callers (orchestrator → evidence_aggregator) touch title,
        # metadata_json, event_date, severity/confidence_score, event_type,
        # event_subtype, id — never summary or review_note. See
        # feedback_scoring_egress memory.
        .options(defer(RiskEvent.summary))
        .order_by(RiskEvent.event_date.desc())
    )
    if cutoff is not None:
        stmt = stmt.where(
            (RiskEvent.event_date >= cutoff) | (RiskEvent.event_date.is_(None))
        )

    # Scope: country / material filters apply via secondary joins so we still
    # only return events tagged to this company AND matching the scope facets.
    if scope.country_codes is not None:
        stmt = stmt.join(
            RiskEventGeography,
            RiskEventGeography.risk_event_id == RiskEvent.id,
        ).where(RiskEventGeography.country_code.in_(scope.country_codes))
    if scope.material_ids is not None:
        stmt = stmt.join(
            RiskEventMaterial,
            RiskEventMaterial.risk_event_id == RiskEvent.id,
        ).where(RiskEventMaterial.material_id.in_(scope.material_ids))

    rows = db.execute(stmt).all()
    results = _dedup_event_rows(rows)

    log.debug(
        "evidence_query.get_events_for_company",
        company_id=str(company_id),
        category=category.value,
        count=len(results),
        cutoff=cutoff.isoformat() if cutoff else None,
        scoped=not scope.is_all(),
    )
    return results


def get_company_material_exposure(
    db: Session,
    company_id: uuid.UUID,
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> list[CompanyMaterialExposure]:
    """
    Return all material exposure records for this company.
    Returns empty list if none — callers must supply conservative defaults (0.5)
    rather than treating empty as zero risk.
    """
    stmt = select(CompanyMaterialExposure).where(
        CompanyMaterialExposure.company_id == company_id
    )
    if scope.material_ids is not None:
        stmt = stmt.where(
            CompanyMaterialExposure.material_id.in_(scope.material_ids)
        )
    if scope.country_codes is not None:
        stmt = stmt.where(
            CompanyMaterialExposure.source_geography.in_(scope.country_codes)
        )
    return list(db.scalars(stmt).all())


def get_active_compliance_obligations(
    db: Session,
    company_id: uuid.UUID,
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> list[tuple[str, float]]:
    """
    Return active compliance obligations as (regulation_key, weight_multiplier) tuples.

    The weight_multiplier reflects compliance severity.  Two tiers of
    status weights, selected per regulation based on whether the
    regulation has a rebuttable-presumption structure:

      Standard (most regulations):
        non_compliant → 1.00  (confirmed violation; full uplift)
        unknown       → 0.50  (unassessed; conservative mid-weight)
        partial       → 0.40  (in transition; partial uplift)

      Rebuttable-presumption regulations (e.g., UFLPA — Idea A,
      2026-05-17, gated by regulation.metadata_json["rebuttable_presumption"]
      = True):
        non_compliant → 1.00  (unchanged — confirmed violation)
        unknown       → 0.85  (the regulation puts burden of proof
                                on importer; absence of evidence is
                                much closer to presumption of violation)
        partial       → 0.70  (good-faith due-diligence partial
                                effort still doesn't satisfy the
                                rebuttable presumption)

    Primary source: company_regulation_exposure joined to regulations.
    Fallback: text-scan of recent regulatory events (worst-case weight assumed).

    Returns e.g. [("IRA_DOMESTIC", 1.0), ("UFLPA", 0.85)]
    """
    _STATUS_WEIGHT: dict[str, float] = {
        "non_compliant": 1.00,
        "unknown":       0.50,
        "partial":       0.40,
    }
    _STATUS_WEIGHT_REBUTTABLE: dict[str, float] = {
        "non_compliant": 1.00,
        "unknown":       0.85,
        "partial":       0.70,
    }

    # ``verified=True`` filter: only seeded regulations contribute uplift.
    # See ``get_events_for_regulations`` below for the same rule.
    # Also pulls metadata_json so we can detect rebuttable_presumption
    # without a second query.
    stmt = (
        select(
            Regulation.regulation_key,
            CompanyRegulationExposure.compliance_status,
            Regulation.metadata_json,
        )
        .join(
            CompanyRegulationExposure,
            CompanyRegulationExposure.regulation_id == Regulation.id,
        )
        .where(
            CompanyRegulationExposure.company_id == company_id,
            CompanyRegulationExposure.compliance_status.in_(
                ["non_compliant", "partial", "unknown"]
            ),
            Regulation.verified.is_(True),
        )
    )
    if scope.regulation_keys is not None:
        stmt = stmt.where(Regulation.regulation_key.in_(scope.regulation_keys))

    rows = db.execute(stmt).all()
    if rows:
        result: list[tuple[str, float]] = []
        for key, status, metadata in rows:
            # Per-regulation rebuttable_presumption override (Idea A).
            # When the regulation's metadata_json carries
            # rebuttable_presumption=True, unknown/partial status
            # weights are elevated to reflect the burden-of-proof
            # structure.  See _STATUS_WEIGHT_REBUTTABLE above for the
            # rationale.
            rebuttable = (
                isinstance(metadata, dict)
                and bool(metadata.get("rebuttable_presumption", False))
            )
            weight_table = _STATUS_WEIGHT_REBUTTABLE if rebuttable else _STATUS_WEIGHT
            result.append((key, weight_table.get(status, 0.40)))
        return sorted(result, key=lambda t: t[0])

    # Hardcoded fallback for companies with zero company_regulation_exposure
    # rows — text-scans event titles for known regulation keywords.  Mapped
    # values must match canonical regulation_key values in seed_regulations.py
    # (post 2026-05-05: EU_BATTERY_REG → EU_BATTERY_REG_2023).
    known_obligations: dict[str, str] = {
        "uflpa":          "UFLPA",
        "eu battery":     "EU_BATTERY_REG_2023",
        "eu_battery":     "EU_BATTERY_REG_2023",
        "ira domestic":   "IRA_DOMESTIC",
        "ira_domestic":   "IRA_DOMESTIC",
    }
    stmt2 = (
        select(RiskEvent)
        .join(RiskEventCompany, RiskEventCompany.risk_event_id == RiskEvent.id)
        .where(
            RiskEventCompany.company_id == company_id,
            RiskEvent.duplicate_of_id.is_(None),  # 055: skip confirmed dupes
            RiskEvent.risk_categories_json.contains(
                [RiskCategory.REGULATORY_COMPLIANCE.value]
            ),
        )
        # 2026-07-15 EGRESS: this loop only reads ev.title — defer Text cols.
        .options(defer(RiskEvent.summary))
        .order_by(RiskEvent.event_date.desc())
        .limit(100)
    )
    events = db.scalars(stmt2).all()
    found: set[str] = set()
    for ev in events:
        text = (ev.title or "").lower()
        for keyword, obligation_key in known_obligations.items():
            if keyword in text:
                found.add(obligation_key)
    if scope.regulation_keys is not None:
        found = {k for k in found if k in scope.regulation_keys}
    return [(k, 1.0) for k in sorted(found)]


def get_filing_signals(
    db: Session,
    company_id: uuid.UUID,
    max_quarters: int = 4,
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> list[EventWithRelevance]:
    """Return the most recent N FINANCIAL_PRESSURE events linked to this company.

    ``scope`` is accepted for API uniformity but FINANCIAL_PRESSURE filings are
    company-wide and have no material/country/chemistry dimension to filter on
    today, so the kwarg is currently a no-op.
    """
    _ = scope  # explicit no-op; kept for signature parity
    stmt = (
        select(RiskEvent, RiskEventCompany.relevance_score)
        .join(RiskEventCompany, RiskEventCompany.risk_event_id == RiskEvent.id)
        .where(
            RiskEventCompany.company_id == company_id,
            RiskEventCompany.review_status != "excluded",
            RiskEvent.duplicate_of_id.is_(None),  # 055: skip confirmed dupes
            RiskEvent.risk_categories_json.contains(
                [RiskCategory.FINANCIAL_PRESSURE.value]
            ),
        )
        # 2026-07-15 EGRESS: defer Text cols (title/metadata_json used, summary/review_note not).
        .options(defer(RiskEvent.summary))
        .order_by(RiskEvent.event_date.desc().nullslast())
        .limit(max_quarters)
    )
    rows = db.execute(stmt).all()
    return [EventWithRelevance(event=row[0], relevance_score=row[1]) for row in rows]


# ---------------------------------------------------------------------------
# Material-scoped queries (no company required)
# ---------------------------------------------------------------------------

# Discount applied to events without HS stage attribution.
# Events ingested before Phase 1.5 (or keyword-only matches that lack a
# specific HS code) have no risk_event_hs_mappings row.  They are still
# included in scoring but down-weighted to reflect the reduced specificity.
_NO_STAGE_ATTRIBUTION_DISCOUNT = 0.85


def _apply_hs_confidence_multiplier(
    db: Session,
    material_id: int,
    results: list[EventWithRelevance],
) -> list[EventWithRelevance]:
    """
    Adjust relevance_score using hs_code_material_mappings.confidence.

    For each event:
      - If it has at least one risk_event_hs_mappings row whose hs_mapping
        belongs to ``material_id``, the relevance is multiplied by the
        *maximum* confidence across those mappings.  This means high-quality
        curated mappings (confidence=0.95) amplify the relevance, while
        lower-confidence inferred mappings (confidence=0.70) reduce it.
      - If the event has no HS stage attribution, the relevance is multiplied
        by ``_NO_STAGE_ATTRIBUTION_DISCOUNT`` (0.85).  Pre-Phase-1.5 events
        and pure keyword matches fall into this bucket.

    Called immediately after ``_dedup_event_rows()`` in
    ``get_events_for_material()`` so dedup has already resolved the best
    relevance per event before the multiplier is applied.
    """
    if not results:
        return results

    event_ids = [r.event.id for r in results]

    # One row per event: the max confidence across all HS mappings linked to
    # this event that belong to the requested material_id.
    confidence_rows = db.execute(
        select(
            RiskEventHsMapping.risk_event_id,
            func.max(HsCodeMaterialMapping.confidence).label("max_confidence"),
        )
        .join(
            HsCodeMaterialMapping,
            HsCodeMaterialMapping.id == RiskEventHsMapping.hs_mapping_id,
        )
        .where(
            RiskEventHsMapping.risk_event_id.in_(event_ids),
            HsCodeMaterialMapping.material_id == material_id,
            HsCodeMaterialMapping.market_scope == "global",
        )
        .group_by(RiskEventHsMapping.risk_event_id)
    ).all()

    confidence_map: dict[int, float] = {
        row.risk_event_id: row.max_confidence for row in confidence_rows
    }

    adjusted: list[EventWithRelevance] = []
    for r in results:
        multiplier = confidence_map.get(r.event.id, _NO_STAGE_ATTRIBUTION_DISCOUNT)
        adjusted.append(
            EventWithRelevance(
                event=r.event,
                relevance_score=round(r.relevance_score * multiplier, 4),
                scope_type=r.scope_type,
            )
        )
    return adjusted


def get_events_for_material(
    db: Session,
    material_id: int,
    category: RiskCategory,
    as_of_date: date,
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> list[EventWithRelevance]:
    """
    Query risk_events tagged to a specific material via risk_event_materials.
    Enables material-anchored scoring without any company data.

    Phase 1.5: relevance scores are adjusted by ``_apply_hs_confidence_multiplier``
    after deduplication.  Events with HS stage attribution are multiplied by the
    mapping confidence; events without are discounted by
    ``_NO_STAGE_ATTRIBUTION_DISCOUNT`` (0.85).
    """
    if scope.material_ids is not None and material_id not in scope.material_ids:
        return []

    cutoff = _category_window_cutoff(category, as_of_date)

    stmt = (
        select(
            RiskEvent,
            RiskEventMaterial.relevance_score,
            RiskEventMaterial.scope_type,
        )
        .join(RiskEventMaterial, RiskEventMaterial.risk_event_id == RiskEvent.id)
        .where(
            RiskEventMaterial.material_id == material_id,
            RiskEvent.duplicate_of_id.is_(None),  # 055: skip confirmed dupes
            RiskEvent.risk_categories_json.contains([category.value]),
        )
        .order_by(RiskEvent.event_date.desc())
    )
    if cutoff is not None:
        stmt = stmt.where(
            (RiskEvent.event_date >= cutoff) | (RiskEvent.event_date.is_(None))
        )
    if scope.country_codes is not None:
        stmt = stmt.join(
            RiskEventGeography,
            RiskEventGeography.risk_event_id == RiskEvent.id,
        ).where(RiskEventGeography.country_code.in_(scope.country_codes))

    rows = db.execute(stmt).all()
    results = _dedup_material_event_rows(rows)
    results = _apply_hs_confidence_multiplier(db, material_id, results)

    log.debug(
        "evidence_query.get_events_for_material",
        material_id=material_id,
        category=category.value,
        count=len(results),
    )
    return results


# ---------------------------------------------------------------------------
# Facility / geography / scoped-regulation helpers (Step 2 — supply-chain rollup)
# ---------------------------------------------------------------------------

def get_facilities_for_company(
    db: Session,
    company_id: uuid.UUID,
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> list[Facility]:
    """All ``Facility`` rows linked to this company via ``company_facilities``.

    Used by both the geopolitical aggregator (facility countries widen
    ``country_concentration``) and the operational aggregator (planned /
    under_construction facilities lift ``structural_dependency``).

    Joins through the junction table so that JV/co-owned facilities are
    returned for every company that holds a link row, without duplicating
    the underlying facility record.
    """
    stmt = (
        select(Facility)
        .join(CompanyFacility, CompanyFacility.facility_id == Facility.id)
        .where(CompanyFacility.company_id == company_id)
    )
    if scope.facility_ids is not None:
        stmt = stmt.where(Facility.id.in_(scope.facility_ids))
    if scope.country_codes is not None:
        stmt = stmt.where(Facility.country.in_(scope.country_codes))
    return list(db.scalars(stmt).all())


def get_events_for_geographies(
    db: Session,
    country_codes: set[str],
    category: RiskCategory,
    as_of_date: date,
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> list[EventWithRelevance]:
    """Events tagged via ``risk_event_geographies`` to any of ``country_codes``.

    Deduped by event id with max relevance kept. ``scope.country_codes``
    intersects the requested set first so a scoped run honours the filter.
    """
    if scope.country_codes is not None:
        country_codes = set(country_codes) & set(scope.country_codes)
    if not country_codes:
        return []

    cutoff = _category_window_cutoff(category, as_of_date)
    stmt = (
        select(RiskEvent, RiskEventGeography.relevance_score)
        .join(
            RiskEventGeography,
            RiskEventGeography.risk_event_id == RiskEvent.id,
        )
        .where(
            RiskEventGeography.country_code.in_(country_codes),
            RiskEvent.duplicate_of_id.is_(None),  # 055: skip confirmed dupes
            RiskEvent.risk_categories_json.contains([category.value]),
        )
        # 2026-07-15 EGRESS: defer Text cols summary + review_note.
        .options(defer(RiskEvent.summary))
        .order_by(RiskEvent.event_date.desc())
    )
    if cutoff is not None:
        stmt = stmt.where(
            (RiskEvent.event_date >= cutoff) | (RiskEvent.event_date.is_(None))
        )
    rows = db.execute(stmt).all()
    return _dedup_event_rows(rows)


def _apply_hs_confidence_multiplier_batch(
    db: Session,
    material_ids: set[int],
    results: list[EventWithRelevance],
) -> list[EventWithRelevance]:
    """
    Batch variant of ``_apply_hs_confidence_multiplier`` for multi-material queries.

    Identical logic to the single-material version: events that have at least one
    ``risk_event_hs_mappings`` row pointing to a mapping that belongs to *any* of
    ``material_ids`` are multiplied by the max confidence across those mappings;
    events with no HS stage attribution are discounted by
    ``_NO_STAGE_ATTRIBUTION_DISCOUNT`` (0.85).

    Called by ``get_events_for_materials()`` so that the batch path is consistent
    with the per-material path in ``get_events_for_material()``.
    """
    if not results:
        return results

    event_ids = [r.event.id for r in results]

    confidence_rows = db.execute(
        select(
            RiskEventHsMapping.risk_event_id,
            func.max(HsCodeMaterialMapping.confidence).label("max_confidence"),
        )
        .join(
            HsCodeMaterialMapping,
            HsCodeMaterialMapping.id == RiskEventHsMapping.hs_mapping_id,
        )
        .where(
            RiskEventHsMapping.risk_event_id.in_(event_ids),
            HsCodeMaterialMapping.material_id.in_(material_ids),
            HsCodeMaterialMapping.market_scope == "global",
        )
        .group_by(RiskEventHsMapping.risk_event_id)
    ).all()

    confidence_map: dict[int, float] = {
        row.risk_event_id: row.max_confidence for row in confidence_rows
    }

    adjusted: list[EventWithRelevance] = []
    for r in results:
        multiplier = confidence_map.get(r.event.id, _NO_STAGE_ATTRIBUTION_DISCOUNT)
        adjusted.append(
            EventWithRelevance(
                event=r.event,
                relevance_score=round(r.relevance_score * multiplier, 4),
                scope_type=r.scope_type,
            )
        )
    return adjusted


def get_events_for_materials(
    db: Session,
    material_ids: set[int],
    category: RiskCategory,
    as_of_date: date,
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> list[EventWithRelevance]:
    """Events tagged via ``risk_event_materials`` to any of ``material_ids``.

    Phase 1.5: relevance scores are adjusted by
    ``_apply_hs_confidence_multiplier_batch`` after deduplication, matching
    the behaviour of the singular ``get_events_for_material()``.  Events with
    HS stage attribution for any of the requested materials are multiplied by
    the mapping confidence; events without are discounted by
    ``_NO_STAGE_ATTRIBUTION_DISCOUNT`` (0.85).
    """
    if scope.material_ids is not None:
        material_ids = set(material_ids) & set(scope.material_ids)
    if not material_ids:
        return []

    cutoff = _category_window_cutoff(category, as_of_date)
    stmt = (
        select(
            RiskEvent,
            RiskEventMaterial.relevance_score,
            RiskEventMaterial.scope_type,
        )
        .join(
            RiskEventMaterial,
            RiskEventMaterial.risk_event_id == RiskEvent.id,
        )
        .where(
            RiskEventMaterial.material_id.in_(material_ids),
            RiskEvent.duplicate_of_id.is_(None),  # 055: skip confirmed dupes
            RiskEvent.risk_categories_json.contains([category.value]),
        )
        .order_by(RiskEvent.event_date.desc())
    )
    if cutoff is not None:
        stmt = stmt.where(
            (RiskEvent.event_date >= cutoff) | (RiskEvent.event_date.is_(None))
        )
    rows = db.execute(stmt).all()
    results = _dedup_material_event_rows(rows)
    results = _apply_hs_confidence_multiplier_batch(db, material_ids, results)

    log.debug(
        "evidence_query.get_events_for_materials",
        material_ids=sorted(material_ids),
        category=category.value,
        count=len(results),
    )
    return results


def get_events_for_hs_mapping(
    db: Session,
    hs_mapping_id: int,
    category: RiskCategory,
    as_of_date: date,
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> list[EventWithRelevance]:
    """
    Query risk_events scoped to a specific HS mapping node via
    ``risk_event_hs_mappings``.

    Intended for use by Phase 3 ``hs_node_scorer.py``, which scores at the
    stage level (e.g., "export ban on cobalt hydroxide 282200 from China").
    Unlike ``get_events_for_material()``, this function queries the HS junction
    table directly, so every row returned already has confirmed stage attribution.
    No additional confidence discount is applied.

    The ``relevance_score`` returned is::

        RiskEventHsMapping.relevance_score × HsCodeMaterialMapping.confidence

    where ``confidence`` reflects the quality of the HS mapping itself (curated
    rows carry 0.90–0.95; inferred rows carry 0.70–0.85).  This means a
    keyword-matched event (relevance 0.85) on a high-confidence curated mapping
    (confidence 0.95) scores 0.8075, while a direct HS-code-matched event
    (relevance 0.90) on the same mapping scores 0.8550.

    Deduplication is by ``event.id``, keeping the highest adjusted score.

    ``scope.material_ids``:  filters to mappings whose ``material_id`` is in the
        set — useful when the caller wants only events attributable to a specific
        material even when querying a shared 4-digit HS chapter node.
    ``scope.country_codes``: filters via ``risk_event_geographies`` as usual.
    """
    cutoff = _category_window_cutoff(category, as_of_date)

    stmt = (
        select(
            RiskEvent,
            RiskEventHsMapping.relevance_score,
            HsCodeMaterialMapping.confidence,
        )
        .join(RiskEventHsMapping, RiskEventHsMapping.risk_event_id == RiskEvent.id)
        .join(
            HsCodeMaterialMapping,
            HsCodeMaterialMapping.id == RiskEventHsMapping.hs_mapping_id,
        )
        .where(
            RiskEventHsMapping.hs_mapping_id == hs_mapping_id,
            RiskEvent.duplicate_of_id.is_(None),  # 055: skip confirmed dupes
            RiskEvent.risk_categories_json.contains([category.value]),
        )
        .order_by(RiskEvent.event_date.desc())
    )
    if cutoff is not None:
        stmt = stmt.where(
            (RiskEvent.event_date >= cutoff) | (RiskEvent.event_date.is_(None))
        )
    if scope.country_codes is not None:
        stmt = stmt.join(
            RiskEventGeography,
            RiskEventGeography.risk_event_id == RiskEvent.id,
        ).where(RiskEventGeography.country_code.in_(scope.country_codes))
    if scope.material_ids is not None:
        stmt = stmt.where(
            HsCodeMaterialMapping.material_id.in_(scope.material_ids)
        )

    rows = db.execute(stmt).all()

    # Dedup by event id; keep max adjusted score across any duplicate rows.
    by_id: dict[int, EventWithRelevance] = {}
    for ev, relevance, confidence in rows:
        adjusted = round(float(relevance) * float(confidence), 4)
        existing = by_id.get(ev.id)
        if existing is None or adjusted > existing.relevance_score:
            by_id[ev.id] = EventWithRelevance(event=ev, relevance_score=adjusted)
    results = list(by_id.values())

    log.debug(
        "evidence_query.get_events_for_hs_mapping",
        hs_mapping_id=hs_mapping_id,
        category=category.value,
        count=len(results),
    )
    return results


def get_regulations_scoping_company(
    db: Session,
    company_id: uuid.UUID,
    material_ids: set[int],
    country_codes: set[str],
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> list[tuple[str, float]]:
    """UNION of regulations that apply to this company by exposure or scope.

    Three sources:
      1. Existing ``CompanyRegulationExposure`` rows (status-weighted, highest authority).
      2. ``Regulation`` rows linked via ``RegulationMaterialScope`` to one of
         this company's exposed materials. Weight is derived from ``scope_type``
         via ``_SCOPE_TYPE_WEIGHT`` — ``strategic_raw_material`` (0.65) ranks
         above ``disclosure_required`` (0.50) which ranks above ``covered`` (0.40).
      3. ``Regulation`` rows linked via ``RegulationGeographyScope`` to one of
         this company's source-geographies / facility countries (flat 0.50).

    Dedup keeps the max weight per ``regulation_key`` across all three sources,
    so a structured exposure row always wins over a scope-derived hit.
    """
    weights: dict[str, float] = {}

    # Source 1: structured exposures (status-weighted — highest authority)
    for key, weight in get_active_compliance_obligations(db, company_id, scope=scope):
        weights[key] = max(weights.get(key, 0.0), weight)

    # Source 2: material-scoped regulations — weight varies by scope_type
    if material_ids:
        stmt = (
            select(Regulation.regulation_key, RegulationMaterialScope.scope_type)
            .join(
                RegulationMaterialScope,
                RegulationMaterialScope.regulation_id == Regulation.id,
            )
            .where(
                RegulationMaterialScope.material_id.in_(material_ids),
                Regulation.verified.is_(True),
            )
        )
        if scope.regulation_keys is not None:
            stmt = stmt.where(Regulation.regulation_key.in_(scope.regulation_keys))
        for key, scope_type in db.execute(stmt).all():
            w = _SCOPE_TYPE_WEIGHT.get(scope_type or "", 0.50)
            weights[key] = max(weights.get(key, 0.0), w)

    # Source 3: geography-scoped regulations (flat 0.50 — no scope_type on geo rows)
    if country_codes:
        stmt = (
            select(Regulation.regulation_key)
            .join(
                RegulationGeographyScope,
                RegulationGeographyScope.regulation_id == Regulation.id,
            )
            .where(
                RegulationGeographyScope.country_code.in_(country_codes),
                Regulation.verified.is_(True),
            )
        )
        if scope.regulation_keys is not None:
            stmt = stmt.where(Regulation.regulation_key.in_(scope.regulation_keys))
        for (key,) in db.execute(stmt).all():
            weights[key] = max(weights.get(key, 0.0), 0.50)

    return sorted(weights.items())


def get_events_for_regulations(
    db: Session,
    regulation_keys: set[str],
    as_of_date: date,
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> list[EventWithRelevance]:
    """Events tagged via ``risk_event_regulations`` to any of ``regulation_keys``."""
    if scope.regulation_keys is not None:
        regulation_keys = set(regulation_keys) & set(scope.regulation_keys)
    if not regulation_keys:
        return []

    cutoff = _category_window_cutoff(
        RiskCategory.REGULATORY_COMPLIANCE, as_of_date
    )
    # Filter to verified regulations only.  ``verified=True`` is set
    # exclusively by ``seed_regulations.py`` (the curated source of truth);
    # rows that other ingesters might create with ``verified=False``
    # (legacy / partial) are excluded from scoring evidence to keep the
    # regulatory pillar grounded in partner-reviewed instruments.
    stmt = (
        select(RiskEvent, RiskEventRegulation.relevance_score)
        .join(
            RiskEventRegulation,
            RiskEventRegulation.risk_event_id == RiskEvent.id,
        )
        .join(
            Regulation,
            Regulation.id == RiskEventRegulation.regulation_id,
        )
        .where(
            Regulation.regulation_key.in_(regulation_keys),
            Regulation.verified.is_(True),
            RiskEvent.duplicate_of_id.is_(None),  # 055: skip confirmed dupes
            RiskEvent.risk_categories_json.contains(
                [RiskCategory.REGULATORY_COMPLIANCE.value]
            ),
        )
        # 2026-07-15 EGRESS: defer Text cols summary + review_note.
        .options(defer(RiskEvent.summary))
        .order_by(RiskEvent.event_date.desc())
    )
    if cutoff is not None:
        stmt = stmt.where(
            (RiskEvent.event_date >= cutoff) | (RiskEvent.event_date.is_(None))
        )
    rows = db.execute(stmt).all()
    return _dedup_event_rows(rows)


def get_targeted_countries_for_events(
    db: Session,
    event_ids: set[int],
) -> dict[int, set[str]]:
    """Map ``event_id -> set of ISO2 country codes`` for regulation
    ``targeted_country`` scopes reachable via the event's regulation links.

    Used by :func:`derive_regulatory_inputs` to apply the conditional scope
    intersection downweight (Option C from EUR-Lex Section 5). An event whose
    linked regulation declares one or more ``targeted_country`` geography
    scopes (e.g. UFLPA → CN for Xinjiang, OFAC → CN/RU/KP/IR) returns a
    non-empty set; all other events are absent from the result (treated as
    "no targeted scope — no downweight").

    Two SQL statements at most: one join over the junction tables for the
    given event ids; one collection-build step in Python. No N+1.
    """
    if not event_ids:
        return {}

    stmt = (
        select(
            RiskEventRegulation.risk_event_id,
            RegulationGeographyScope.country_code,
        )
        .join(
            RegulationGeographyScope,
            RegulationGeographyScope.regulation_id
            == RiskEventRegulation.regulation_id,
        )
        .where(
            RiskEventRegulation.risk_event_id.in_(event_ids),
            RegulationGeographyScope.scope_type == "targeted_country",
        )
    )
    out: dict[int, set[str]] = {}
    for event_id, country_code in db.execute(stmt).all():
        if not country_code:
            continue
        out.setdefault(event_id, set()).add(country_code.upper())
    return out


# ---------------------------------------------------------------------------
# Supplier-chain BFS + latest-score lookup
# ---------------------------------------------------------------------------

def get_supplier_chain(
    db: Session,
    root_company_id: uuid.UUID,
    max_depth: int = 2,
    max_visited: int = 50,
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> list[SupplierEdge]:
    """BFS over ``CompanySupplyRelationship`` edges starting at ``root_company_id``.

    Returns one :class:`SupplierEdge` per *unique supplier reached* (the path
    that produced the highest cumulative volume share is kept; ties broken by
    shallower depth). Cycles are skipped via ``path`` membership; a hard
    ``max_visited`` cap bounds runtime.

    ``scope.supplier_depth_max`` clamps ``max_depth`` from above so a scoped
    run can ask for a tier-1-only view without callers needing to thread two
    knobs separately.
    """
    if scope.supplier_depth_max is not None:
        max_depth = min(max_depth, scope.supplier_depth_max)
    if max_depth <= 0:
        return []

    edges_by_buyer: dict[uuid.UUID, list[CompanySupplyRelationship]] = {}

    def _load_edges(buyer_id: uuid.UUID) -> list[CompanySupplyRelationship]:
        if buyer_id in edges_by_buyer:
            return edges_by_buyer[buyer_id]
        stmt = select(CompanySupplyRelationship).where(
            CompanySupplyRelationship.buyer_id == buyer_id
        )
        loaded = list(db.scalars(stmt).all())
        edges_by_buyer[buyer_id] = loaded
        return loaded

    best: dict[uuid.UUID, SupplierEdge] = {}
    queue: deque[tuple[uuid.UUID, int, Optional[float], list[uuid.UUID]]] = deque()
    queue.append((root_company_id, 0, 1.0, []))
    visited_count = 0

    while queue and visited_count < max_visited:
        buyer_id, depth, cum_share, path = queue.popleft()
        if depth >= max_depth:
            continue

        for edge in _load_edges(buyer_id):
            sup_id = edge.supplier_id
            if sup_id == root_company_id or sup_id in path:
                continue  # cycle / self-loop guard

            new_share: Optional[float]
            if cum_share is None or edge.volume_share_pct is None:
                new_share = None
            else:
                new_share = cum_share * float(edge.volume_share_pct)

            new_path = path + [sup_id]
            new_depth = depth + 1
            visited_count += 1

            candidate = SupplierEdge(
                supplier_id=sup_id,
                depth=new_depth,
                cumulative_volume_share=new_share,
                path=new_path,
            )

            existing = best.get(sup_id)
            if existing is None or _is_better_edge(candidate, existing):
                best[sup_id] = candidate

            if new_depth < max_depth:
                queue.append((sup_id, new_depth, new_share, new_path))

            if visited_count >= max_visited:
                log.warning(
                    "evidence_query.get_supplier_chain.max_visited",
                    root=str(root_company_id),
                    cap=max_visited,
                )
                break

    return sorted(best.values(), key=lambda e: (e.depth, str(e.supplier_id)))


def _is_better_edge(candidate: SupplierEdge, existing: SupplierEdge) -> bool:
    """Prefer shallower depth; within same depth prefer larger known share."""
    if candidate.depth < existing.depth:
        return True
    if candidate.depth > existing.depth:
        return False
    cand_share = candidate.cumulative_volume_share or 0.0
    exist_share = existing.cumulative_volume_share or 0.0
    return cand_share > exist_share


def get_latest_company_scores(
    db: Session,
    company_ids: list[uuid.UUID],
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> dict[uuid.UUID, CompanyScore]:
    """Most recent persisted ``CompanyScore`` per company id, as a dict.

    Only persisted (full-scope) scores are returned; the propagation pillar
    deliberately consumes already-stored rollups rather than recursing into
    rescore. ``scope`` is accepted for signature parity but does not filter —
    persisted scores are full-scope by construction.
    """
    _ = scope  # signature parity; persisted scores are full-scope
    if not company_ids:
        return {}

    stmt = (
        select(CompanyScore)
        .where(CompanyScore.company_id.in_(company_ids))
        .order_by(
            CompanyScore.company_id, CompanyScore.as_of_date.desc(), CompanyScore.id.desc()
        )
    )
    out: dict[uuid.UUID, CompanyScore] = {}
    for row in db.scalars(stmt):
        if row.company_id not in out:
            out[row.company_id] = row
    return out


# ---------------------------------------------------------------------------
# Chemistry-mix helpers (Step 2 — chemistry refinement)
# ---------------------------------------------------------------------------

def get_vehicle_models_for_company(
    db: Session,
    company_id: uuid.UUID,
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> list[CompanyVehicleModel]:
    """Active vehicle models for a company with their chemistries eager-loaded."""
    stmt = (
        select(CompanyVehicleModel)
        .where(
            CompanyVehicleModel.company_id == company_id,
            CompanyVehicleModel.is_active.is_(True),
        )
        .options(selectinload(CompanyVehicleModel.chemistries))
    )
    models = list(db.scalars(stmt).all())
    if scope.chemistry_ids is not None:
        models = [
            m
            for m in models
            if any(
                c.battery_chemistry_id in scope.chemistry_ids
                for c in m.chemistries
            )
        ]
    return models


def get_chemistry_mix_for_company(
    db: Session,
    company_id: uuid.UUID,
    as_of_date: Optional[date] = None,
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> Optional[dict[int, float]]:
    """Volume-weighted chemistry-share map keyed by ``battery_chemistry_id``.

    Algorithm:
      1. For each active model, sum ``share_pct`` per chemistry across rows
         whose validity window contains ``as_of_date``.
      2. Weight per-chemistry shares by ``production_volume_units`` (default
         ``1`` when NULL — uniform contribution).
      3. Normalise the company-level totals to sum to 1.0.
      4. Return ``None`` when the company has no vehicle models (signals
         "fall back to legacy uniform behaviour" to the aggregator). Never
         substitute industry-average market shares.

    When ``scope.chemistry_ids`` is provided, restrict the per-model
    aggregation to those chemistries and re-normalise — this is the
    "what-if 100% LFP" view.
    """
    today = as_of_date or date.today()
    models = get_vehicle_models_for_company(db, company_id, scope=scope)
    if not models:
        return None

    totals: dict[int, float] = {}
    for model in models:
        weight = model.production_volume_units or 1
        # Per-model: shares within the active window (model can have multiple
        # chemistries by trim; shares within a model must sum to 1.0)
        for chem in model.chemistries:
            if chem.valid_from > today:
                continue
            if chem.valid_to is not None and chem.valid_to <= today:
                continue
            if (
                scope.chemistry_ids is not None
                and chem.battery_chemistry_id not in scope.chemistry_ids
            ):
                continue
            totals[chem.battery_chemistry_id] = (
                totals.get(chem.battery_chemistry_id, 0.0)
                + float(chem.share_pct) * float(weight)
            )

    total = sum(totals.values())
    if total <= 0:
        return None

    return {chem_id: share / total for chem_id, share in totals.items()}


def get_chemistry_material_intensities(
    db: Session,
    chemistry_ids: set[int],
    as_of_date: Optional[date] = None,
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> dict[int, dict[int, float]]:
    """Per-chemistry ``{material_id -> intensity}`` map for ``as_of_date``.

    Filters ``BatteryChemistryMaterial`` rows so only those whose validity
    window contains ``as_of_date`` contribute. This is what makes the
    weighting time-aware (chemistry recipes shift as the industry moves to
    high-Ni or LMFP variants).
    """
    if scope.chemistry_ids is not None:
        chemistry_ids = set(chemistry_ids) & set(scope.chemistry_ids)
    if not chemistry_ids:
        return {}

    today = as_of_date or date.today()
    stmt = select(BatteryChemistryMaterial).where(
        BatteryChemistryMaterial.battery_chemistry_id.in_(chemistry_ids),
        BatteryChemistryMaterial.valid_from <= today,
        or_(
            BatteryChemistryMaterial.valid_to.is_(None),
            BatteryChemistryMaterial.valid_to > today,
        ),
    )
    if scope.material_ids is not None:
        stmt = stmt.where(
            BatteryChemistryMaterial.material_id.in_(scope.material_ids)
        )
    out: dict[int, dict[int, float]] = {}
    for row in db.scalars(stmt):
        out.setdefault(row.battery_chemistry_id, {})[row.material_id] = float(
            row.intensity
        )
    return out


def get_latest_chemistry_risk_scores(
    db: Session,
    chemistry_ids: set[int],
    *,
    scope: ScoringScope = ScoringScope.ALL,
) -> dict[int, ChemistryRiskScore]:
    """Most recent ``ChemistryRiskScore`` per chemistry — for rationale display only.

    Does NOT feed pillar math; chemistry refines the material pillar via
    intensities, not via its own composite score. The composite is exposed in
    ``rationale_json.chemistry_mix`` so the UI can show "NMC's persisted
    composite is 58, LFP's is 41" alongside each chemistry's share.
    """
    if scope.chemistry_ids is not None:
        chemistry_ids = set(chemistry_ids) & set(scope.chemistry_ids)
    if not chemistry_ids:
        return {}

    stmt = (
        select(ChemistryRiskScore)
        .where(ChemistryRiskScore.battery_chemistry_id.in_(chemistry_ids))
        .order_by(
            ChemistryRiskScore.battery_chemistry_id,
            ChemistryRiskScore.as_of_date.desc(),
            ChemistryRiskScore.id.desc(),
        )
    )
    out: dict[int, ChemistryRiskScore] = {}
    for row in db.scalars(stmt):
        if row.battery_chemistry_id not in out:
            out[row.battery_chemistry_id] = row
    return out


def get_chemistry_slugs(
    db: Session,
    chemistry_ids: set[int],
) -> dict[int, str]:
    """Lookup helper: ``id -> slug`` for the rationale chemistry_mix block."""
    if not chemistry_ids:
        return {}
    stmt = select(BatteryChemistry.id, BatteryChemistry.slug).where(
        BatteryChemistry.id.in_(chemistry_ids)
    )
    return {cid: slug for cid, slug in db.execute(stmt).all()}


def get_hs_nodes_for_material(
    db: Session,
    material_id: int,
    country_code: str,
    as_of_date: date,
    *,
    market_scope: str = "global",
) -> list[HsCodeGeographyRiskScore]:
    """Return all Level-0 HS stage node scores for a (material × country) pair.

    Used by ``market_aggregator.score_material_geography()`` to determine
    whether the stage-weighted rollup path is available (≥2 nodes present)
    or whether to fall back to the legacy material-level path.

    Rows are ordered by ``HsCodeMaterialMapping.stage_sequence`` so the
    caller can iterate stages in supply-chain order without sorting.

    Args:
        db:           Active SQLAlchemy session.
        material_id:  Primary key of the material in ``materials``.
        country_code: ISO 3166-1 alpha-2 country code.
        as_of_date:   Evaluation date — the most recent score row on or before
                      this date is returned for each HS mapping node.
        market_scope: ``"global"`` (default) or ``"us"``.  Must match the
                      ``market_scope`` value written by ``hs_node_scorer``.

    Returns:
        List of ``HsCodeGeographyRiskScore`` instances, possibly empty if
        Level-0 scoring has not yet run for this material.
    """
    # Subquery: for each hs_mapping_id, find the most recent as_of_date
    # on or before the requested date — avoids returning stale or future rows.
    latest_subq = (
        select(
            HsCodeGeographyRiskScore.hs_mapping_id,
            func.max(HsCodeGeographyRiskScore.as_of_date).label("latest_date"),
        )
        .where(
            HsCodeGeographyRiskScore.country_code == country_code,
            HsCodeGeographyRiskScore.as_of_date <= as_of_date,
            HsCodeGeographyRiskScore.market_scope == market_scope,
        )
        .group_by(HsCodeGeographyRiskScore.hs_mapping_id)
        .subquery()
    )

    stmt = (
        select(HsCodeGeographyRiskScore)
        .join(
            HsCodeMaterialMapping,
            HsCodeMaterialMapping.id == HsCodeGeographyRiskScore.hs_mapping_id,
        )
        .join(
            latest_subq,
            (latest_subq.c.hs_mapping_id == HsCodeGeographyRiskScore.hs_mapping_id)
            & (latest_subq.c.latest_date == HsCodeGeographyRiskScore.as_of_date),
        )
        .where(
            HsCodeMaterialMapping.material_id == material_id,
            HsCodeMaterialMapping.market_scope == market_scope,
            HsCodeGeographyRiskScore.country_code == country_code,
        )
        .order_by(HsCodeMaterialMapping.stage_sequence.asc().nulls_last())
    )

    return list(db.scalars(stmt).all())


# ---------------------------------------------------------------------------
# Market-score drill-down evidence
# ---------------------------------------------------------------------------
#
# Backs ``GET /materials/{material_id}/market-scores/{geography_code}/evidence``
# which replaces the raw-pillar-sub-input dump in the country-scores dropdown
# with the actual regulations / facilities / risk events the scoring engine
# saw for this (material × country) pair.
#
# Membership rule across all three lists: strict INTERSECTION.  We only
# return rows tagged to BOTH this material AND this country.  A regulation
# whose ``material_scopes`` covers Phosphate but whose ``geography_scopes``
# never mentions Morocco is excluded.  Same logic for facilities and events.
# This mirrors ``MaterialGeographyRiskScore.event_count_geo_specific`` (the
# intersection count surfaced in the table) — NOT ``event_count`` (the
# union the scoring engine consumes).  We're optimising for analyst trust:
# "the table says 9 events for MA × Phosphate; the dropdown shows those 9
# events specifically" rather than "the dropdown also shows global Phosphate
# events that contributed to the score but aren't Morocco-specific".

# 730-day window mirrors the longest finite EVIDENCE_WINDOW
# (GEOPOLITICAL_TRADE).  Events beyond that horizon have been fully decayed
# out of the scoring engine's view, so showing them in the drill-down would
# mislead about what the score "saw".  Same value used for OPERATIONAL +
# FINANCIAL_PRESSURE (which have ``None`` windows in EVIDENCE_WINDOWS — they
# rely on per-event half-life decay rather than a hard cutoff — but for a
# UI surface we still need a finite cutoff so the analyst sees recent
# events rather than the full event history).
RISK_EVENT_EVIDENCE_WINDOW_DAYS: int = 730


@dataclass
class MarketScoreEvidenceBundle:
    """Aggregated raw evidence rows for (material × country).

    Wraps three ORM lists + their pre-truncation counts.  Caller is
    responsible for serialising to Pydantic — this layer just queries.

    Lists are ordered from most-impactful first:
      - regulations: status='effective' first, then by severity-suggestive
        scope_type (banned > restricted > covered), then by effective_date desc.
      - facilities: operating > planned > under_construction > mothballed >
        closed, then by relevance_score desc, then by name.
      - risk_events: by event_date desc (so the analyst always sees newest
        first; severity ordering would hide stale-but-major events).
    """

    regulations: list[Regulation]
    """Each row carries ``material_scope_type`` and ``geography_scope_type``
    derived attributes attached at query time."""
    facilities: list[Facility]
    """Each row carries attached ``link_is_primary_product``,
    ``link_annual_capacity_tpy``, and ``link_supply_chain_stage`` attributes
    pulled from the FacilityMaterialLink junction row."""
    risk_events: list[RiskEvent]
    """Each row carries a derived ``source_system`` attribute from the
    associated SourceDocument (looked up at query time)."""

    regulation_total: int
    facility_total: int
    risk_event_total: int
    # 056: broad multi-material measures (is_direct=False) matching this
    # (material × country) that are EXCLUDED from the list above.  Scoring
    # still consumes them at breadth-discounted relevance; the UI can show
    # "+ N broad measures" without listing them.  See migration 056.
    risk_event_broad_total: int = 0


# Per-list caps for the dropdown.  Tuned so the panel stays short
# vertically (3 sections × ~5 rows × short text ≈ fits without scroll).
_DROPDOWN_REGULATION_LIMIT = 6
_DROPDOWN_FACILITY_LIMIT = 8
_DROPDOWN_RISK_EVENT_LIMIT = 6

# Ranking weights for regulation severity ordering — higher = more impactful.
# Keys match RegulationMaterialScope.scope_type enum.
_REG_SCOPE_RANK = {
    "banned": 4,
    "restricted": 3,
    "covered": 2,
    "disclosure_required": 1,
    "compliant": 0,
}

# Facility status ordering — operating sites first.
_FACILITY_STATUS_RANK = {
    "operating": 0,
    "planned": 1,
    "under_construction": 2,
    "care_maintenance": 3,
    "mothballed": 4,
    "closed": 5,
}


def get_evidence_for_material_x_geography(
    db: Session,
    material_id: int,
    country_code: str,
    *,
    as_of_date: Optional[date] = None,
    risk_event_window_days: int = RISK_EVENT_EVIDENCE_WINDOW_DAYS,
    regulation_limit: int = _DROPDOWN_REGULATION_LIMIT,
    facility_limit: int = _DROPDOWN_FACILITY_LIMIT,
    risk_event_limit: int = _DROPDOWN_RISK_EVENT_LIMIT,
) -> MarketScoreEvidenceBundle:
    """Three-way intersection: regulations, facilities, risk events.

    Each list is INTERSECTION-only (see module-level note above) — rows
    tagged to BOTH this material AND this country.

    Caller passes pre-uppercased ``country_code`` (ISO2) to match the
    storage convention used by RiskEventGeography, RegulationGeographyScope,
    and Facility.country.
    """
    if as_of_date is None:
        as_of_date = datetime.now(timezone.utc).date()
    cc = country_code.upper()

    # ── Regulations ─────────────────────────────────────────────────────
    # Join Regulation → both scope junctions, filtered to this material and
    # this country.  Eager-load the scope rows so we can pluck scope_type
    # without an N+1 follow-up.
    reg_stmt = (
        select(Regulation, RegulationMaterialScope.scope_type, RegulationGeographyScope.scope_type)
        .join(
            RegulationMaterialScope,
            RegulationMaterialScope.regulation_id == Regulation.id,
        )
        .join(
            RegulationGeographyScope,
            RegulationGeographyScope.regulation_id == Regulation.id,
        )
        .where(
            RegulationMaterialScope.material_id == material_id,
            RegulationGeographyScope.country_code == cc,
        )
        # Distinct per regulation — a single reg can have multiple
        # geography_scope rows for the same country (rare but possible
        # post-migration), so we collapse on regulation_id.
        .distinct(Regulation.id)
        .order_by(Regulation.id)
    )
    reg_rows = db.execute(reg_stmt).all()

    # In-memory sort by severity proxy: scope_type rank desc, then
    # effective_date desc.  Keeps the query simple (single DISTINCT ON)
    # and lets us use a derived rank without a CASE WHEN in SQL.
    def _reg_sort_key(row):
        reg, mat_scope, geo_scope = row
        return (
            -_REG_SCOPE_RANK.get(mat_scope or "covered", 0),
            -(reg.effective_date.toordinal() if reg.effective_date else 0),
        )

    reg_rows_sorted = sorted(reg_rows, key=_reg_sort_key)
    regulation_total = len(reg_rows_sorted)

    # Attach the per-pair scope_type values to each Regulation as
    # ad-hoc attributes so the API serialiser can read them without
    # another query.
    regulations: list[Regulation] = []
    for reg, mat_scope, geo_scope in reg_rows_sorted[:regulation_limit]:
        reg.material_scope_type = mat_scope or "covered"   # type: ignore[attr-defined]
        reg.geography_scope_type = geo_scope or "jurisdiction"  # type: ignore[attr-defined]
        regulations.append(reg)

    # ── Facilities ──────────────────────────────────────────────────────
    fac_stmt = (
        select(
            Facility,
            FacilityMaterialLink.is_primary_product,
            FacilityMaterialLink.annual_capacity_tpy,
            FacilityMaterialLink.supply_chain_stage,
        )
        .join(
            FacilityMaterialLink,
            FacilityMaterialLink.facility_id == Facility.id,
        )
        .where(
            FacilityMaterialLink.material_id == material_id,
            Facility.country == cc,
        )
    )
    fac_rows = db.execute(fac_stmt).all()
    facility_total = len(fac_rows)

    # Ordering: operating > planned > under_construction > mothballed > closed,
    # then primary products before co-products, then by name alphabetically.
    def _fac_sort_key(row):
        fac, is_primary, _cap, _stage = row
        return (
            _FACILITY_STATUS_RANK.get(fac.status, 99),
            0 if is_primary else 1,
            (fac.name or "").lower(),
        )

    fac_rows_sorted = sorted(fac_rows, key=_fac_sort_key)
    facilities: list[Facility] = []
    for fac, is_primary, capacity_tpy, stage in fac_rows_sorted[:facility_limit]:
        fac.link_is_primary_product = bool(is_primary)  # type: ignore[attr-defined]
        fac.link_annual_capacity_tpy = (  # type: ignore[attr-defined]
            float(capacity_tpy) if capacity_tpy is not None else None
        )
        fac.link_supply_chain_stage = stage  # type: ignore[attr-defined]
        facilities.append(fac)

    # ── Risk events ─────────────────────────────────────────────────────
    # Strict intersection: events with BOTH a RiskEventMaterial row for
    # this material AND a RiskEventGeography row for this country.  Two
    # separate inner joins do the intersection.
    cutoff = as_of_date - timedelta(days=risk_event_window_days)
    ev_stmt = (
        select(RiskEvent)
        .join(
            RiskEventMaterial,
            RiskEventMaterial.risk_event_id == RiskEvent.id,
        )
        .join(
            RiskEventGeography,
            RiskEventGeography.risk_event_id == RiskEvent.id,
        )
        .where(
            RiskEventMaterial.material_id == material_id,
            RiskEventGeography.country_code == cc,
            RiskEvent.duplicate_of_id.is_(None),  # 055: skip confirmed dupes
            RiskEventMaterial.is_direct.is_(True),  # 056: list direct only
        )
        # Window filter: events with no event_date are kept (sanctions
        # programs often lack a single anchor date) but cutoff applies to
        # those that do.
        .where(or_(RiskEvent.event_date >= cutoff, RiskEvent.event_date.is_(None)))
        # Two-hop selectinload: SourceDocument → Source so we can name the
        # ingester (global_trade_alert / federal_register / eurlex / iea /
        # opensanctions / sec_edgar) without an N+1 follow-up.
        #
        # 2026-07-15 EGRESS FIX: defer SourceDocument.raw_text (full ingested
        # source payload — Federal Register HTML, GTA notice, IEA doc, EUR-Lex
        # text; 5–100 KB per event) and metadata_json.  This call site only
        # touches ``sd.source.name`` (see ``ev.source_system = ...`` below) so
        # neither field is ever read — but SQLAlchemy was materializing them
        # anyway, driving multi-GB egress per rescore against remote Neon.
        # If any downstream caller of ``get_evidence_for_material_x_geography``
        # later needs raw_text, load it explicitly instead of un-deferring here.
        .options(
            # RiskEvent-level defers: caller uses id, event_date, event_type,
            # event_subtype, severity_score, source_document (below).  Never
            # summary or review_note.
            defer(RiskEvent.summary),
            selectinload(RiskEvent.source_document)
                .options(
                    defer(SourceDocument.raw_text),
                    defer(SourceDocument.metadata_json),
                )
                .selectinload(SourceDocument.source),
        )
        .distinct(RiskEvent.id)
        .order_by(RiskEvent.id)
    )
    ev_rows = list(db.scalars(ev_stmt).all())
    risk_event_total = len(ev_rows)

    # 056: count (don't list) the broad multi-material measures excluded
    # above — scoring consumed them at discounted relevance, so the panel
    # can honestly say "+ N broad measures" without the sift burden.
    risk_event_broad_total = db.scalar(
        select(func.count(func.distinct(RiskEvent.id)))
        .select_from(RiskEvent)
        .join(RiskEventMaterial, RiskEventMaterial.risk_event_id == RiskEvent.id)
        .join(RiskEventGeography, RiskEventGeography.risk_event_id == RiskEvent.id)
        .where(
            RiskEventMaterial.material_id == material_id,
            RiskEventGeography.country_code == cc,
            RiskEvent.duplicate_of_id.is_(None),
            RiskEventMaterial.is_direct.is_(False),
            or_(RiskEvent.event_date >= cutoff, RiskEvent.event_date.is_(None)),
        )
    ) or 0

    # In-memory sort: newest first (NULL event_date goes to end).
    ev_rows_sorted = sorted(
        ev_rows,
        key=lambda e: (e.event_date is None, -(e.event_date.toordinal() if e.event_date else 0)),
    )

    risk_events: list[RiskEvent] = []
    for ev in ev_rows_sorted[:risk_event_limit]:
        # Derive source_system from SourceDocument → Source.name so the
        # frontend can render a small badge identifying which ingester
        # surfaced this event.  Defensive None-handling: pre-Phase-2 events
        # and synthetically generated ones may lack a source_document.
        sd = ev.source_document
        ev.source_system = (  # type: ignore[attr-defined]
            sd.source.name if sd is not None and sd.source is not None else None
        )
        risk_events.append(ev)

    return MarketScoreEvidenceBundle(
        regulations=regulations,
        facilities=facilities,
        risk_events=risk_events,
        regulation_total=regulation_total,
        facility_total=facility_total,
        risk_event_total=risk_event_total,
        risk_event_broad_total=risk_event_broad_total,
    )


__all__ = [
    "EventWithRelevance",
    "SupplierEdge",
    "HIGH_CONCENTRATION_GEOS",
    "MarketScoreEvidenceBundle",
    "get_evidence_for_material_x_geography",
    "RISK_EVENT_EVIDENCE_WINDOW_DAYS",
    # Existing helpers (now scope-threaded)
    "get_events_for_company",
    "get_company_material_exposure",
    "get_active_compliance_obligations",
    "get_filing_signals",
    "get_events_for_material",
    # Supply-chain rollup helpers
    "get_facilities_for_company",
    "get_events_for_geographies",
    "get_events_for_materials",
    "get_events_for_hs_mapping",
    "get_hs_nodes_for_material",
    "get_regulations_scoping_company",
    "get_events_for_regulations",
    "get_supplier_chain",
    "get_latest_company_scores",
    # Chemistry helpers
    "get_vehicle_models_for_company",
    "get_chemistry_mix_for_company",
    "get_chemistry_material_intensities",
    "get_latest_chemistry_risk_scores",
    "get_chemistry_slugs",
]
