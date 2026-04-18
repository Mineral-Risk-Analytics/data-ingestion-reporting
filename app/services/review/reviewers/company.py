"""Company reviewer.

For every seeded ``Company``, find ``RiskEventCompany`` rows whose linked
``RiskEvent.event_date`` postdates the company's ``updated_at`` (or the
supplied ``since_date``, whichever is later) and score each link with three
signals:

- **Signal A — severity** (0 / +1 / +2): ``RiskEvent.severity_score`` >= 0.70
  contributes +2, >= 0.50 contributes +1, else 0. Uses the pipeline's already
  computed severity so we don't re-implement classification here.
- **Signal B — event subtype** (+1): ``RiskEvent.metadata_json.event_subtype``
  matches one of the high-impact subtypes (``SANCTION_ADD``,
  ``EXPORT_RESTRICTION``, ``ENTITY_LIST_UPDATE``). These subtypes change the
  legal-compliance picture for a seeded company and almost always warrant
  human review of the row.
- **Signal C — entity-resolution confidence** (+1):
  ``RiskEventCompany.relevance_score`` >= 0.80. Low-confidence matches are
  still surfaced but kept out of ``high`` relevance.

Note on the column name: ``RiskEventCompany`` exposes entity-resolution
confidence as ``relevance_score`` (not ``match_confidence``).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.regulatory import RiskEvent, RiskEventCompany
from app.services.review.scoring import as_utc_datetime, score_to_relevance
from app.services.review.types import Finding


_HIGH_IMPACT_SUBTYPES = frozenset(
    {"SANCTION_ADD", "EXPORT_RESTRICTION", "ENTITY_LIST_UPDATE"}
)


def _score_link(
    link: RiskEventCompany, event: RiskEvent
) -> tuple[int, dict[str, Any]]:
    signals: dict[str, Any] = {}

    severity = event.severity_score
    if severity is not None and severity >= 0.70:
        severity_points = 2
    elif severity is not None and severity >= 0.50:
        severity_points = 1
    else:
        severity_points = 0
    signals["severity"] = {
        "severity_score": severity,
        "points": severity_points,
    }

    subtype: Optional[str] = None
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    raw_subtype = metadata.get("event_subtype")
    if isinstance(raw_subtype, str):
        subtype = raw_subtype
    subtype_points = 1 if subtype in _HIGH_IMPACT_SUBTYPES else 0
    signals["event_subtype"] = {
        "subtype": subtype,
        "high_impact": bool(subtype_points),
        "points": subtype_points,
    }

    confidence = link.relevance_score
    confidence_points = 1 if confidence is not None and confidence >= 0.80 else 0
    signals["match_confidence"] = {
        "relevance_score": confidence,
        "points": confidence_points,
    }

    total = severity_points + subtype_points + confidence_points
    signals["total_score"] = total
    return total, signals


def _evidence_json(link: RiskEventCompany, event: RiskEvent) -> dict[str, Any]:
    event_date = event.event_date.isoformat() if event.event_date else None
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    return {
        "risk_event_id": event.id,
        "event_type": event.event_type,
        "event_subtype": metadata.get("event_subtype"),
        "event_date": event_date,
        "title": event.title,
        "severity_score": event.severity_score,
        "source_document_id": event.source_document_id,
        "link_relevance_score": link.relevance_score,
        "link_match_reason": link.match_reason,
    }


def review(
    session: Session,
    *,
    since_date: Optional[date] = None,
    company_canonical_names: Optional[Iterable[str]] = None,
) -> list[Finding]:
    """Run the company reviewer and return a list of findings."""
    stmt = select(Company)
    if company_canonical_names is not None:
        names = list(company_canonical_names)
        if not names:
            return []
        stmt = stmt.where(Company.canonical_name.in_(names))
    companies: list[Company] = list(session.execute(stmt).scalars().all())

    findings: list[Finding] = []
    since_dt = as_utc_datetime(since_date)
    for company in companies:
        cutoffs: list[datetime] = []
        company_updated = as_utc_datetime(company.updated_at)
        if company_updated is not None:
            cutoffs.append(company_updated)
        if since_dt is not None:
            cutoffs.append(since_dt)

        link_stmt = (
            select(RiskEventCompany, RiskEvent)
            .join(RiskEvent, RiskEventCompany.risk_event_id == RiskEvent.id)
            .where(RiskEventCompany.company_id == company.id)
        )
        if cutoffs:
            cutoff = max(cutoffs)
            link_stmt = link_stmt.where(
                or_(
                    RiskEvent.event_date.is_(None),
                    RiskEvent.event_date > cutoff,
                )
            )
        link_stmt = link_stmt.order_by(RiskEvent.event_date.desc().nullslast())

        rows = list(session.execute(link_stmt).all())
        for link, event in rows:
            score, signals = _score_link(link, event)
            if score <= 0:
                continue
            relevance = score_to_relevance(score)
            if relevance == "none":
                continue
            findings.append(
                Finding(
                    seed_type="company",
                    seed_key=company.canonical_name,
                    seed_display_name=company.legal_name or company.canonical_name,
                    seed_identifier_json={
                        "company_id": str(company.id),
                        "canonical_name": company.canonical_name,
                    },
                    evidence_type="risk_event_company_link",
                    source_document_id=event.source_document_id,
                    risk_event_id=event.id,
                    evidence_json=_evidence_json(link, event),
                    relevance=relevance,
                    match_signals_json=signals,
                )
            )

    return findings


__all__ = ["review"]
