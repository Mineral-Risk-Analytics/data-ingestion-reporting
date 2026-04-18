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
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import RiskCategory
from app.models.company import CompanyMaterialExposure
from app.models.regulatory import (
    CompanyRegulationExposure,
    Regulation,
    RiskEvent,
    RiskEventCompany,
    RiskEventMaterial,
)
from app.services.scoring.decay import EVIDENCE_WINDOWS

log = structlog.get_logger(__name__)

HIGH_CONCENTRATION_GEOS = frozenset({"CN", "CD", "RU"})


@dataclass
class EventWithRelevance:
    """
    Pairs a RiskEvent with its per-company (or per-material) relevance score
    from the relevant junction table.
    relevance_score maps to the relevance_multiplier input of compute_event_impact().
    """

    event: RiskEvent
    relevance_score: float  # 0.70–1.00


def _category_window_cutoff(category: RiskCategory, as_of_date: date) -> Optional[datetime]:
    window_days = EVIDENCE_WINDOWS[category]
    if window_days is None:
        return None
    cutoff_date = as_of_date - timedelta(days=window_days)
    return datetime(cutoff_date.year, cutoff_date.month, cutoff_date.day, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Company-scoped queries
# ---------------------------------------------------------------------------

def get_events_for_company(
    db: Session,
    company_id: uuid.UUID,
    category: RiskCategory,
    as_of_date: date,
) -> list[EventWithRelevance]:
    """
    Query risk_events for a company within the category-specific evidence window,
    joined through risk_event_companies.

    For FINANCIAL_PRESSURE delegates to get_filing_signals() which applies a
    most-recent-N filter instead of a time window.
    """
    if category == RiskCategory.FINANCIAL_PRESSURE:
        return get_filing_signals(db, company_id)

    cutoff = _category_window_cutoff(category, as_of_date)

    stmt = (
        select(RiskEvent, RiskEventCompany.relevance_score)
        .join(RiskEventCompany, RiskEventCompany.risk_event_id == RiskEvent.id)
        .where(
            RiskEventCompany.company_id == company_id,
            RiskEvent.risk_categories_json.contains([category.value]),
        )
        .order_by(RiskEvent.event_date.desc())
    )
    if cutoff is not None:
        stmt = stmt.where(
            (RiskEvent.event_date >= cutoff) | (RiskEvent.event_date.is_(None))
        )

    rows = db.execute(stmt).all()
    results = [EventWithRelevance(event=row[0], relevance_score=row[1]) for row in rows]

    log.debug(
        "evidence_query.get_events_for_company",
        company_id=str(company_id),
        category=category.value,
        count=len(results),
        cutoff=cutoff.isoformat() if cutoff else None,
    )
    return results


def get_company_material_exposure(
    db: Session,
    company_id: uuid.UUID,
) -> list[CompanyMaterialExposure]:
    """
    Return all material exposure records for this company.
    Returns empty list if none — callers must supply conservative defaults (0.5)
    rather than treating empty as zero risk.
    """
    stmt = select(CompanyMaterialExposure).where(
        CompanyMaterialExposure.company_id == company_id
    )
    return list(db.scalars(stmt).all())


def get_active_compliance_obligations(
    db: Session,
    company_id: uuid.UUID,
) -> list[tuple[str, float]]:
    """
    Return active compliance obligations as (regulation_key, weight_multiplier) tuples.

    The weight_multiplier reflects compliance severity:
      non_compliant → 1.00  (confirmed violation; full uplift)
      unknown       → 0.50  (unassessed; conservative mid-weight)
      partial       → 0.40  (in transition; partial uplift)

    Primary source: company_regulation_exposure joined to regulations.
    Fallback: text-scan of recent regulatory events (worst-case weight assumed).

    Returns e.g. [("IRA_DOMESTIC", 1.0), ("UFLPA", 0.40)]
    """
    _STATUS_WEIGHT: dict[str, float] = {
        "non_compliant": 1.00,
        "unknown":       0.50,
        "partial":       0.40,
    }

    # Primary: structured compliance table
    stmt = (
        select(Regulation.regulation_key, CompanyRegulationExposure.compliance_status)
        .join(
            CompanyRegulationExposure,
            CompanyRegulationExposure.regulation_id == Regulation.id,
        )
        .where(
            CompanyRegulationExposure.company_id == company_id,
            CompanyRegulationExposure.compliance_status.in_(
                ["non_compliant", "partial", "unknown"]
            ),
        )
    )
    rows = db.execute(stmt).all()
    if rows:
        return sorted(
            [
                (key, _STATUS_WEIGHT.get(status, 0.40))
                for key, status in rows
            ],
            key=lambda t: t[0],
        )

    # Fallback: text-derive from recent regulatory events
    # Assume worst-case weight (1.0) since status is unknown from events alone.
    # TODO: remove once company_regulation_exposure is populated everywhere.
    known_obligations: dict[str, str] = {
        "uflpa":          "UFLPA",
        "eu battery":     "EU_BATTERY_REG",
        "eu_battery":     "EU_BATTERY_REG",
        "ira domestic":   "IRA_DOMESTIC",
        "ira_domestic":   "IRA_DOMESTIC",
    }
    stmt2 = (
        select(RiskEvent)
        .join(RiskEventCompany, RiskEventCompany.risk_event_id == RiskEvent.id)
        .where(
            RiskEventCompany.company_id == company_id,
            RiskEvent.risk_categories_json.contains(
                [RiskCategory.REGULATORY_COMPLIANCE.value]
            ),
        )
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
    return [(k, 1.0) for k in sorted(found)]


def get_filing_signals(
    db: Session,
    company_id: uuid.UUID,
    max_quarters: int = 4,
) -> list[EventWithRelevance]:
    """Return the most recent N FINANCIAL_PRESSURE events linked to this company."""
    stmt = (
        select(RiskEvent, RiskEventCompany.relevance_score)
        .join(RiskEventCompany, RiskEventCompany.risk_event_id == RiskEvent.id)
        .where(
            RiskEventCompany.company_id == company_id,
            RiskEvent.risk_categories_json.contains(
                [RiskCategory.FINANCIAL_PRESSURE.value]
            ),
        )
        .order_by(RiskEvent.event_date.desc().nullslast())
        .limit(max_quarters)
    )
    rows = db.execute(stmt).all()
    return [EventWithRelevance(event=row[0], relevance_score=row[1]) for row in rows]


# ---------------------------------------------------------------------------
# Material-scoped queries (no company required)
# ---------------------------------------------------------------------------

def get_events_for_material(
    db: Session,
    material_id: int,
    category: RiskCategory,
    as_of_date: date,
) -> list[EventWithRelevance]:
    """
    Query risk_events tagged to a specific material via risk_event_materials.
    Enables material-anchored scoring without any company data.
    """
    cutoff = _category_window_cutoff(category, as_of_date)

    stmt = (
        select(RiskEvent, RiskEventMaterial.relevance_score)
        .join(RiskEventMaterial, RiskEventMaterial.risk_event_id == RiskEvent.id)
        .where(
            RiskEventMaterial.material_id == material_id,
            RiskEvent.risk_categories_json.contains([category.value]),
        )
        .order_by(RiskEvent.event_date.desc())
    )
    if cutoff is not None:
        stmt = stmt.where(
            (RiskEvent.event_date >= cutoff) | (RiskEvent.event_date.is_(None))
        )

    rows = db.execute(stmt).all()
    results = [EventWithRelevance(event=row[0], relevance_score=row[1]) for row in rows]

    log.debug(
        "evidence_query.get_events_for_material",
        material_id=material_id,
        category=category.value,
        count=len(results),
    )
    return results
