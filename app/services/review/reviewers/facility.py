"""Facility reviewer — v1 weak-signal implementation.

Text co-occurrence is inherently noisy for facility status changes, so this
reviewer's relevance is always capped at ``medium`` via
``score_to_relevance(..., max_bucket="medium")``. The intent is "flag a human
to read the article", not "assert the facility's status changed".

Scope
-----
Only facilities whose status is ``operating``, ``under_construction``, or
``planned`` are reviewed. Mothballed or closed facilities are already
terminal for our purposes and produce a lot of false positives on
retrospective news articles.

Candidate documents are pulled from news sources (``source_type='news'``)
published after the facility's ``updated_at``.

Signals
-------
- **Signal A — company + city both mentioned** (+1): hard gate. We require
  the facility's parent company's ``canonical_name`` **and** the
  ``facility.city`` to both appear in ``title`` + ``raw_text``. Facilities
  without a ``city`` value are skipped entirely.
- **Signal B — status-change keyword** (+1): doc contains any of
  ``shutdown``, ``closure``, ``expansion``, ``commissioned``, ``delay``,
  ``postponed``.

A maximum score of 2 caps cleanly at ``medium`` after bucketing.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.documents import SourceDocument
from app.models.enums import FacilityStatus, SourceType
from app.models.facility import Facility
from app.models.source import Source
from app.services.review.scoring import as_utc_datetime, score_to_relevance
from app.services.review.types import Finding


_REVIEWABLE_STATUSES = (
    FacilityStatus.OPERATING.value,
    FacilityStatus.UNDER_CONSTRUCTION.value,
    FacilityStatus.PLANNED.value,
)

_STATUS_CHANGE_KEYWORDS = (
    "shutdown",
    "closure",
    "expansion",
    "commissioned",
    "delay",
    "postponed",
)


def _score_document(
    haystack_lower: str,
) -> tuple[int, dict[str, Any], list[str]]:
    """Score a document (company+city already matched). Returns (score, signals, matched_kws)."""
    signals: dict[str, Any] = {
        "company_and_city_match": {"hit": True, "points": 1},
    }
    matched = [kw for kw in _STATUS_CHANGE_KEYWORDS if kw in haystack_lower]
    kw_points = 1 if matched else 0
    signals["status_change_keyword"] = {
        "matched": matched,
        "points": kw_points,
    }
    total = 1 + kw_points
    signals["total_score"] = total
    return total, signals, matched


def _evidence_json(
    doc: SourceDocument,
    facility: Facility,
    company: Company,
    matched_keywords: list[str],
) -> dict[str, Any]:
    published = doc.published_at.isoformat() if doc.published_at else None
    return {
        "document_id": doc.id,
        "external_id": doc.external_id,
        "title": doc.title,
        "url": doc.url,
        "published_at": published,
        "facility_id": str(facility.id),
        "facility_city": facility.city,
        "facility_country": facility.country,
        "facility_status": facility.status,
        "company_canonical_name": company.canonical_name,
        "matched_status_keywords": matched_keywords,
    }


def review(
    session: Session,
    *,
    since_date: Optional[date] = None,
    facility_ids: Optional[Iterable] = None,
) -> list[Finding]:
    """Run the facility reviewer and return a list of findings."""
    stmt = (
        select(Facility, Company)
        .join(Company, Facility.company_id == Company.id)
        .where(Facility.status.in_(_REVIEWABLE_STATUSES))
        .where(Facility.city.isnot(None))
    )
    if facility_ids is not None:
        ids = list(facility_ids)
        if not ids:
            return []
        stmt = stmt.where(Facility.id.in_(ids))

    rows = list(session.execute(stmt).all())

    findings: list[Finding] = []
    since_dt = as_utc_datetime(since_date)
    for facility, company in rows:
        if not facility.city or not company.canonical_name:
            continue

        company_name_lower = company.canonical_name.lower()
        city_lower = facility.city.lower()

        cutoffs: list[datetime] = []
        facility_updated = as_utc_datetime(facility.updated_at)
        if facility_updated is not None:
            cutoffs.append(facility_updated)
        if since_dt is not None:
            cutoffs.append(since_dt)

        doc_stmt = (
            select(SourceDocument)
            .join(Source, SourceDocument.source_id == Source.id)
            .where(Source.source_type == SourceType.NEWS.value)
        )
        if cutoffs:
            cutoff = max(cutoffs)
            doc_stmt = doc_stmt.where(
                or_(
                    SourceDocument.published_at.is_(None),
                    SourceDocument.published_at > cutoff,
                )
            )
        doc_stmt = doc_stmt.order_by(SourceDocument.published_at.desc().nullslast())

        for doc in session.execute(doc_stmt).scalars().all():
            haystack_lower = " ".join([
                (doc.title or ""),
                (doc.raw_text or ""),
            ]).lower()
            if company_name_lower not in haystack_lower:
                continue
            if city_lower not in haystack_lower:
                continue

            score, signals, matched_kws = _score_document(haystack_lower)
            relevance = score_to_relevance(score, max_bucket="medium")
            if relevance == "none":
                continue

            findings.append(
                Finding(
                    seed_type="facility",
                    seed_key=(
                        f"{company.canonical_name}|"
                        f"{facility.city}|{facility.country}"
                    ),
                    seed_display_name=(
                        f"{company.canonical_name} — "
                        f"{facility.city}, {facility.country} "
                        f"({facility.facility_type})"
                    ),
                    seed_identifier_json={
                        "facility_id": str(facility.id),
                        "company_id": str(company.id),
                        "facility_type": facility.facility_type,
                        "country": facility.country,
                        "city": facility.city,
                        "status": facility.status,
                    },
                    evidence_type="facility_text_mention",
                    source_document_id=doc.id,
                    risk_event_id=None,
                    evidence_json=_evidence_json(
                        doc, facility, company, matched_kws
                    ),
                    relevance=relevance,
                    match_signals_json=signals,
                )
            )

    return findings


__all__ = ["review"]
