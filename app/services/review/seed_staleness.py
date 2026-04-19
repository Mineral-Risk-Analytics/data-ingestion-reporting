"""Top-level entrypoint for the seed staleness review.

Responsibilities
----------------
- Accept a subset of ``SEED_TYPES`` (or ``None`` meaning "all").
- Optionally accept a single ``seed_key`` to re-check one specific subject
  (the type is inferred if not supplied).
- Dispatch to each per-type reviewer in
  :mod:`app.services.review.reviewers`.
- Manage the ``seed_review_runs`` row lifecycle when ``persist=True``:
  INSERT on start, UPDATE counters + ``completed_at`` on finish, and bulk
  INSERT every returned :class:`Finding` into ``seed_review_findings``.
- Return a :class:`ReviewRunResult` with the run_id, findings grouped by
  seed_type, and timing.

``persist=False`` runs are non-mutating and are what the CLI's ``--dry-run``
flag uses — nothing touches the database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.company import (
    Company,
    CompanyMaterialExposure,
    CompanySupplyRelationship,
)
from app.models.enums import FacilityStatus
from app.models.facility import Facility
from app.models.regulatory import Regulation
from app.models.review import SeedReviewFinding, SeedReviewRun
from app.services.review.reviewers import (
    company as company_reviewer,
    facility as facility_reviewer,
    material_exposure as material_exposure_reviewer,
    regulation as regulation_reviewer,
    supply_relationship as supply_relationship_reviewer,
)
from app.services.review.types import Finding

log = structlog.get_logger(__name__)


SEED_TYPES: tuple[str, ...] = (
    "regulation",
    "company",
    "material_exposure",
    "supply_relationship",
    "facility",
)


@dataclass
class ReviewRunResult:
    run_id: uuid.UUID
    seed_types: list[str]
    findings_by_type: dict[str, list[Finding]]
    total_seeds_checked: int
    total_findings: int
    started_at: datetime
    completed_at: datetime
    persisted: bool
    seed_key: Optional[str] = None
    parameters_json: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _subject_count(session: Session, seed_type: str, seed_key: Optional[str]) -> int:
    """Return the number of seeded rows the reviewer could have inspected."""
    if seed_type == "regulation":
        stmt = select(func.count(Regulation.id))
        if seed_key is not None:
            stmt = stmt.where(Regulation.regulation_key == seed_key)
    elif seed_type == "company":
        stmt = select(func.count(Company.id))
        if seed_key is not None:
            stmt = stmt.where(Company.canonical_name == seed_key)
    elif seed_type == "material_exposure":
        stmt = select(func.count(CompanyMaterialExposure.id))
        if seed_key is not None:
            # seed_key here is "{canonical_name}|{material_name}" — best-effort
            # narrow by company canonical_name.
            company_part = seed_key.split("|", 1)[0]
            stmt = stmt.join(
                Company, CompanyMaterialExposure.company_id == Company.id
            ).where(Company.canonical_name == company_part)
    elif seed_type == "supply_relationship":
        stmt = select(func.count(CompanySupplyRelationship.id))
    elif seed_type == "facility":
        stmt = select(func.count(Facility.id)).where(
            Facility.status.in_(
                (
                    FacilityStatus.OPERATING.value,
                    FacilityStatus.UNDER_CONSTRUCTION.value,
                    FacilityStatus.PLANNED.value,
                )
            )
        )
    else:
        return 0
    result = session.execute(stmt).scalar()
    return int(result or 0)


def _infer_seed_type(session: Session, seed_key: str) -> Optional[str]:
    """Best-effort type inference when the user supplies only a seed_key.

    Tries regulation_key, then company canonical_name. Returns the first match.
    """
    if session.execute(
        select(Regulation.id).where(Regulation.regulation_key == seed_key)
    ).first():
        return "regulation"
    if session.execute(
        select(Company.id).where(Company.canonical_name == seed_key)
    ).first():
        return "company"
    return None


def _dispatch(
    session: Session,
    seed_type: str,
    *,
    since_date: Optional[date],
    seed_key: Optional[str],
) -> list[Finding]:
    """Call the right reviewer for a single seed_type."""
    if seed_type == "regulation":
        regulation_keys = [seed_key] if seed_key is not None else None
        return regulation_reviewer.review(
            session, since_date=since_date, regulation_keys=regulation_keys
        )
    if seed_type == "company":
        names = [seed_key] if seed_key is not None else None
        return company_reviewer.review(
            session, since_date=since_date, company_canonical_names=names
        )
    if seed_type == "material_exposure":
        names: Optional[list[str]] = None
        if seed_key is not None:
            names = [seed_key.split("|", 1)[0]]
        return material_exposure_reviewer.review(
            session, since_date=since_date, company_canonical_names=names
        )
    if seed_type == "supply_relationship":
        relationship_ids: Optional[list[int]] = None
        if seed_key is not None and seed_key.isdigit():
            relationship_ids = [int(seed_key)]
        return supply_relationship_reviewer.review(
            session,
            since_date=since_date,
            relationship_ids=relationship_ids,
        )
    if seed_type == "facility":
        facility_ids: Optional[list[uuid.UUID]] = None
        if seed_key is not None:
            try:
                facility_ids = [uuid.UUID(seed_key)]
            except ValueError:
                facility_ids = None
        return facility_reviewer.review(
            session, since_date=since_date, facility_ids=facility_ids
        )
    log.warning("seed_staleness.unknown_seed_type", seed_type=seed_type)
    return []


def _persist_run(
    session: Session,
    *,
    run_id: uuid.UUID,
    seed_types: list[str],
    since_date: Optional[date],
    parameters_json: dict[str, Any],
) -> None:
    run = SeedReviewRun(
        run_id=run_id,
        seed_types=seed_types,
        since_date=since_date,
        parameters_json=parameters_json,
    )
    session.add(run)
    session.flush()


def _persist_findings(
    session: Session,
    *,
    run_id: uuid.UUID,
    findings: list[Finding],
) -> None:
    for f in findings:
        session.add(
            SeedReviewFinding(
                run_id=run_id,
                seed_type=f.seed_type,
                seed_key=f.seed_key,
                seed_display_name=f.seed_display_name,
                seed_identifier_json=f.seed_identifier_json,
                evidence_type=f.evidence_type,
                source_document_id=f.source_document_id,
                risk_event_id=f.risk_event_id,
                evidence_json=f.evidence_json,
                relevance=f.relevance,
                match_signals_json=f.match_signals_json,
            )
        )


def _complete_run(
    session: Session,
    *,
    run_id: uuid.UUID,
    total_seeds_checked: int,
    total_findings: int,
    completed_at: datetime,
) -> None:
    run = session.execute(
        select(SeedReviewRun).where(SeedReviewRun.run_id == run_id)
    ).scalar_one()
    run.total_seeds_checked = total_seeds_checked
    run.total_findings = total_findings
    run.completed_at = completed_at


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run_seed_review(
    session: Session,
    *,
    seed_types: Optional[list[str]] = None,
    since_date: Optional[date] = None,
    persist: bool = True,
    seed_key: Optional[str] = None,
) -> ReviewRunResult:
    """Run the seed staleness review across one or more seed types.

    Parameters
    ----------
    session
        Active SQLAlchemy session. If ``persist=True`` the caller is
        responsible for committing; the orchestrator calls ``session.flush()``
        but not ``commit()``.
    seed_types
        Which reviewers to run. ``None`` means all of :data:`SEED_TYPES`.
        Unknown types are rejected before any work starts.
    since_date
        Lower bound on candidate evidence dates. Evidence rows whose
        publication / event date is older than this are ignored.
    persist
        When ``True``, write a ``seed_review_runs`` row and one
        ``seed_review_findings`` row per finding. When ``False``, compute
        findings in memory only.
    seed_key
        Optionally restrict the review to a single subject. When
        ``seed_types`` is ``None`` the orchestrator tries to infer the type
        from the key.
    """
    if seed_types is None:
        effective_types = list(SEED_TYPES)
    else:
        unknown = [t for t in seed_types if t not in SEED_TYPES]
        if unknown:
            raise ValueError(
                f"Unknown seed_type(s): {unknown}. Valid options: {list(SEED_TYPES)}"
            )
        effective_types = list(seed_types)

    if seed_key is not None and seed_types is None:
        inferred = _infer_seed_type(session, seed_key)
        if inferred is not None:
            effective_types = [inferred]

    run_id = uuid.uuid4()
    started_at = datetime.now(tz=timezone.utc)
    parameters_json: dict[str, Any] = {
        "seed_types": effective_types,
        "since_date": since_date.isoformat() if since_date else None,
        "seed_key": seed_key,
    }

    if persist:
        _persist_run(
            session,
            run_id=run_id,
            seed_types=effective_types,
            since_date=since_date,
            parameters_json=parameters_json,
        )

    findings_by_type: dict[str, list[Finding]] = {}
    total_seeds_checked = 0
    for seed_type in effective_types:
        total_seeds_checked += _subject_count(session, seed_type, seed_key)
        log.info(
            "seed_staleness.dispatch",
            run_id=str(run_id),
            seed_type=seed_type,
            seed_key=seed_key,
        )
        findings = _dispatch(
            session,
            seed_type,
            since_date=since_date,
            seed_key=seed_key,
        )
        findings_by_type[seed_type] = findings
        log.info(
            "seed_staleness.dispatch_complete",
            run_id=str(run_id),
            seed_type=seed_type,
            findings=len(findings),
        )

    all_findings: list[Finding] = [
        f for lst in findings_by_type.values() for f in lst
    ]
    total_findings = len(all_findings)

    completed_at = datetime.now(tz=timezone.utc)
    if persist:
        _persist_findings(session, run_id=run_id, findings=all_findings)
        _complete_run(
            session,
            run_id=run_id,
            total_seeds_checked=total_seeds_checked,
            total_findings=total_findings,
            completed_at=completed_at,
        )
        session.flush()

    return ReviewRunResult(
        run_id=run_id,
        seed_types=effective_types,
        findings_by_type=findings_by_type,
        total_seeds_checked=total_seeds_checked,
        total_findings=total_findings,
        started_at=started_at,
        completed_at=completed_at,
        persisted=persist,
        seed_key=seed_key,
        parameters_json=parameters_json,
    )


__all__ = ["run_seed_review", "ReviewRunResult", "SEED_TYPES"]
