"""Triage API — the write surface for the event triage queue (Phase 3).

Built 2026-08-02 against docs/design/event_triage_pipeline_plan.md and the
MRa Data Platform design system's risk-events screen.  Until now the four-
state model (migration 066) had no HTTP surface: promote/reject existed only
as CLI wrappers for operational candidates, and nothing could confirm a
suggestion.  This router is the ONLY sanctioned write path for triage state;
it never touches machine-owned fields (summaries, hashes, provenance) and
the ingesters never touch the fields written here — the same ownership split
enforced ingester-side since the suggestion inversion.

Semantics encoded here (each traceable to a session decision):

* Four states, every transition reversible.  Returning to ``pending_triage``
  clears ``primary_category``, ``verified``, and the triage stamps.
* Moving to ``scoring`` requires a pillar: the caller's explicit choice, or
  the machine's ``suggested_category`` as default.  No pillar → 400.
* A positive-direction event can never reach ``scoring`` — promoting one
  would mark it verified while contributing nothing to risk arithmetic
  (constants.POSITIVE_EVENT_SUBTYPES; 2026-07-28 rule).
* ``operational_news_candidate`` events cannot be approved through the plain
  status endpoint: they arrive with subtype and severity deliberately NULL
  ("keyword-suggest only", 2026-07-27), so approval MUST supply them — use
  ``POST /{id}/promote-operational``, which wraps
  ``operational_triage.promote_candidate`` and its severity ladder.
* Any human move to ``scoring`` sets ``verified=True`` (a human has read and
  confirmed the event); leaving ``scoring`` clears it.
* Material-link decisions only ever touch link ``status`` (and analyst-added
  links).  Relevance weights are read-only in v1 — deferred deliberately.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, get_db
from app.constants import POSITIVE_EVENT_SUBTYPES, RiskCategory
from app.models.documents import SourceDocument
from app.models.regulatory import RiskEvent, RiskEventMaterial
from app.models.source import Source
from app.models.supply import Material

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/triage", tags=["triage"])

_VALID_STATUSES = {"pending_triage", "scoring", "display_only", "rejected"}
_VALID_CATEGORIES = {c.value for c in RiskCategory}


# ---------------------------------------------------------------------------
# Schemas (local — this surface is consumed only by the triage UI)
# ---------------------------------------------------------------------------

class TriageLinkRead(BaseModel):
    id: int
    material_id: int
    label: str
    status: str
    match_reason: Optional[str] = None
    relevance_score: Optional[float] = None
    is_direct: bool = True


class TriageEventRow(BaseModel):
    id: int
    title: str
    summary: Optional[str] = None
    event_type: Optional[str] = None
    event_subtype: Optional[str] = None
    event_date: Optional[datetime] = None
    severity_score: Optional[float] = None
    confidence_score: Optional[float] = None
    triage_status: str
    suggested_category: Optional[str] = None
    primary_category: Optional[str] = None
    direction: Optional[str] = None
    verified: bool = False
    triaged_by: Optional[str] = None
    triaged_at: Optional[datetime] = None
    source_system: Optional[str] = None
    source_url: Optional[str] = None
    geography_primary: Optional[str] = None
    suggested_subtype: Optional[str] = None
    quality_defects: list[str] = Field(default_factory=list)
    flags_count: int = 0
    links: list[TriageLinkRead] = Field(default_factory=list)


class TriageEventList(BaseModel):
    items: list[TriageEventRow]
    total: int
    page: int
    limit: int


class TriageSummary(BaseModel):
    pending_triage: int
    scoring: int
    display_only: int
    rejected: int
    total: int


class StatusUpdate(BaseModel):
    status: str
    primary_category: Optional[str] = None


class CategoryUpdate(BaseModel):
    primary_category: str


class LinkStatusUpdate(BaseModel):
    status: str  # confirmed | rejected | suggested


class LinkCreate(BaseModel):
    material_id: int


class PromoteOperational(BaseModel):
    subtype: str
    severity: Optional[float] = None
    note: Optional[str] = None
    facility_name: Optional[str] = None


class FlagCreate(BaseModel):
    note_type: str = "issue"
    note_text: str


class DuplicateHints(BaseModel):
    checked: bool
    reason: Optional[str] = None
    hints: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_event_or_404(db: Session, event_id: int) -> RiskEvent:
    ev = db.scalars(
        select(RiskEvent)
        .where(RiskEvent.id == event_id)
        .options(
            selectinload(RiskEvent.material_links),
            selectinload(RiskEvent.source_document).selectinload(SourceDocument.source),
        )
    ).first()
    if ev is None:
        raise HTTPException(status_code=404, detail="Risk event not found")
    return ev


def _material_labels(db: Session, events: list[RiskEvent]) -> dict[int, str]:
    """RiskEventMaterial has no ``material`` relationship — resolve names in
    one batch query instead of N lazy loads."""
    ids = {l.material_id for ev in events for l in ev.material_links}
    if not ids:
        return {}
    rows = db.execute(
        select(Material.id, Material.canonical_name).where(Material.id.in_(ids))
    ).all()
    return {mid: name for mid, name in rows}


def _is_positive_direction(ev: RiskEvent) -> bool:
    return bool(ev.event_subtype) and ev.event_subtype in POSITIVE_EVENT_SUBTYPES


def _actor(user: dict[str, Any]) -> str:
    return str(user.get("email") or user.get("sub") or "unknown")


def _quality_defects(ev: RiskEvent, now: datetime) -> list[str]:
    """Measured ingest defects — the audits' findings as row-level facets.

    Deliberately derived at read time rather than stored: each is a pure
    function of the row, and deriving keeps them honest as rows get
    repaired (a summary refresh clears ``truncated_summary`` with no
    bookkeeping).
    """
    out: list[str] = []
    meta = ev.metadata_json or {}
    if ev.summary is not None and len(ev.summary) == 500:
        # The legacy cap sliced at exactly 500; a natural 500-char summary is
        # possible but rare enough that the flag is worth the reviewer's
        # glance (the two known false positives are complete sentences).
        out.append("truncated_summary")
    if ev.event_date is not None:
        # SQLite returns naive datetimes; Postgres returns aware.  Compare in
        # UTC either way.
        ed = ev.event_date if ev.event_date.tzinfo else ev.event_date.replace(tzinfo=timezone.utc)
        if ed > now:
            out.append("future_date")
    if ev.source_document_id is None:
        out.append("no_provenance")
    else:
        doc = ev.source_document
        if (doc is None or not doc.url) and not meta.get("permalink"):
            out.append("no_source_url")
    if meta.get("needs_material_review"):
        # IEA/FR review-queue flag (2026-05-12 pattern) — folded into the
        # triage UI as a facet rather than living as a parallel queue.
        out.append("needs_material_review")
    return out


def _row(ev: RiskEvent, now: datetime, labels: dict[int, str]) -> TriageEventRow:
    meta = ev.metadata_json or {}
    doc = ev.source_document
    src = doc.source if doc is not None else None
    source_url = (doc.url if doc is not None else None) or meta.get("permalink")
    geo = ev.geography_json or {}
    return TriageEventRow(
        id=ev.id,
        title=ev.title,
        summary=ev.summary,
        event_type=ev.event_type,
        event_subtype=ev.event_subtype,
        event_date=ev.event_date,
        severity_score=ev.severity_score,
        confidence_score=ev.confidence_score,
        triage_status=ev.triage_status,
        suggested_category=ev.suggested_category,
        primary_category=ev.primary_category,
        direction=ev.direction,
        verified=bool(ev.verified),
        triaged_by=ev.triaged_by,
        triaged_at=ev.triaged_at,
        source_system=src.name if src is not None else None,
        source_url=source_url,
        geography_primary=geo.get("primary") if isinstance(geo, dict) else None,
        suggested_subtype=meta.get("suggested_subtype"),
        quality_defects=_quality_defects(ev, now),
        flags_count=len(meta.get("flags") or []),
        links=[
            TriageLinkRead(
                id=l.id,
                material_id=l.material_id,
                label=labels.get(l.material_id, str(l.material_id)),
                status=l.status,
                match_reason=l.match_reason,
                relevance_score=l.relevance_score,
                is_direct=bool(l.is_direct),
            )
            for l in ev.material_links
        ],
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

@router.get("/operational-subtypes")
def operational_subtypes(
    _user: dict = Depends(get_current_user),
) -> list[dict]:
    """The operational promote dialog's subtype ladder — served from
    constants so the UI can never drift from the backend's defaults."""
    from app.constants import OPERATIONAL_SUBTYPE_DEFAULT_SEVERITY

    return [
        {
            "value": subtype,
            "label": subtype.replace("_", " "),
            "default_severity": severity,
        }
        for subtype, severity in OPERATIONAL_SUBTYPE_DEFAULT_SEVERITY.items()
        if subtype not in POSITIVE_EVENT_SUBTYPES
    ]



@router.get("/summary", response_model=TriageSummary)
def triage_summary(
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TriageSummary:
    rows = db.execute(
        select(RiskEvent.triage_status, func.count())
        .where(RiskEvent.duplicate_of_id.is_(None))
        .group_by(RiskEvent.triage_status)
    ).all()
    counts = {status: int(n) for status, n in rows}
    return TriageSummary(
        pending_triage=counts.get("pending_triage", 0),
        scoring=counts.get("scoring", 0),
        display_only=counts.get("display_only", 0),
        rejected=counts.get("rejected", 0),
        total=sum(counts.values()),
    )


@router.get("/events", response_model=TriageEventList)
def list_triage_events(
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = Query(None, description="pending_triage|scoring|display_only|rejected; omit = all except rejected"),
    search: Optional[str] = Query(None),
    source: Optional[str] = Query(None, description="sources.name exact match"),
    pillar: Optional[str] = Query(None, description="suggested OR confirmed pillar"),
    direction: Optional[str] = Query(None, description="restrictive|supportive|neutral"),
    event_type: Optional[str] = Query(None),
    severity_min: Optional[float] = Query(None, ge=0, le=1),
    material: Optional[str] = Query(None, description="material canonical_name with a non-rejected link"),
    defect: Optional[str] = Query(None, description="quality-defect facet, e.g. needs_material_review"),
    sort: str = Query("event_date", description="event_date|severity_score|triage_status"),
) -> TriageEventList:
    stmt: Select = (
        select(RiskEvent)
        .where(RiskEvent.duplicate_of_id.is_(None))
        .options(
            selectinload(RiskEvent.material_links),
            selectinload(RiskEvent.source_document).selectinload(SourceDocument.source),
        )
    )

    if status:
        if status not in _VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"Unknown status {status!r}")
        stmt = stmt.where(RiskEvent.triage_status == status)
    else:
        # "Everything" never silently includes dismissed rows — soft dismiss
        # means hidden unless explicitly asked for.
        stmt = stmt.where(RiskEvent.triage_status != "rejected")

    if search:
        q = f"%{search.strip()}%"
        stmt = stmt.where(or_(RiskEvent.title.ilike(q), RiskEvent.summary.ilike(q)))
    if pillar:
        stmt = stmt.where(
            or_(
                RiskEvent.suggested_category == pillar,
                RiskEvent.primary_category == pillar,
            )
        )
    if direction:
        stmt = stmt.where(RiskEvent.direction == direction)
    if event_type:
        stmt = stmt.where(RiskEvent.event_type == event_type)
    if severity_min is not None:
        stmt = stmt.where(RiskEvent.severity_score >= severity_min)
    if source:
        stmt = stmt.where(
            RiskEvent.source_document_id.in_(
                select(SourceDocument.id).where(
                    SourceDocument.source_id.in_(
                        select(Source.id).where(Source.name == source)
                    )
                )
            )
        )
    if material:
        stmt = stmt.where(
            RiskEvent.id.in_(
                select(RiskEventMaterial.risk_event_id)
                .join(Material, Material.id == RiskEventMaterial.material_id)
                .where(
                    Material.canonical_name == material,
                    RiskEventMaterial.status != "rejected",
                )
            )
        )

    now = datetime.now(timezone.utc)

    if sort == "severity_score":
        stmt = stmt.order_by(RiskEvent.severity_score.desc().nullslast(), RiskEvent.id.desc())
    elif sort == "triage_status":
        stmt = stmt.order_by(RiskEvent.triage_status.asc(), RiskEvent.id.desc())
    else:
        stmt = stmt.order_by(RiskEvent.event_date.desc().nullslast(), RiskEvent.id.desc())

    # Defect facets are derived per-row, so filter in Python.  The corpus is
    # a few thousand rows; when it grows past that, materialise the flags.
    if defect:
        events = list(db.scalars(stmt).all())
        events = [e for e in events if defect in _quality_defects(e, now)]
        total = len(events)
        events = events[(page - 1) * limit : (page - 1) * limit + limit]
    else:
        total = int(
            db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
            or 0
        )
        events = list(db.scalars(stmt.offset((page - 1) * limit).limit(limit)).all())

    labels = _material_labels(db, events)
    return TriageEventList(
        items=[_row(e, now, labels) for e in events],
        total=total,
        page=page,
        limit=limit,
    )


@router.get("/events/{event_id}", response_model=TriageEventRow)
def get_triage_event(
    event_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TriageEventRow:
    ev = _get_event_or_404(db, event_id)
    return _row(ev, datetime.now(timezone.utc), _material_labels(db, [ev]))


@router.get("/events/{event_id}/duplicate-hints", response_model=DuplicateHints)
def get_duplicate_hints(
    event_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DuplicateHints:
    _get_event_or_404(db, event_id)
    # Lazy import: event_dedupe pulls openpyxl at module scope for its xlsx
    # report, which this endpoint has no use for.
    from app.services.ingestion.event_dedupe import find_similar_events

    hints = find_similar_events(db, [event_id]).get(
        event_id, {"checked": False, "reason": "not_checked", "hints": []}
    )
    return DuplicateHints(**hints)


# ---------------------------------------------------------------------------
# Writes — event level
# ---------------------------------------------------------------------------

def _apply_status(
    db: Session, ev: RiskEvent, next_status: str, actor: str,
    primary_override: Optional[str] = None,
) -> None:
    if next_status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unknown status {next_status!r}")

    if next_status == "scoring":
        if _is_positive_direction(ev):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Positive-direction events are excluded from risk "
                    "arithmetic — promoting one would have no effect."
                ),
            )
        if ev.event_type == "operational_news_candidate":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Operational news candidates arrive with subtype and "
                    "severity deliberately unset — approve them via "
                    "promote-operational, which requires both."
                ),
            )
        primary = primary_override or ev.primary_category or ev.suggested_category
        if not primary:
            raise HTTPException(
                status_code=400,
                detail="No pillar: set primary_category or wait for a suggestion.",
            )
        if primary not in _VALID_CATEGORIES:
            raise HTTPException(status_code=400, detail=f"Unknown pillar {primary!r}")
        ev.primary_category = primary
        ev.verified = True
    else:
        ev.primary_category = None
        if next_status == "pending_triage":
            ev.verified = False

    ev.triage_status = next_status
    if next_status == "pending_triage":
        # Back in the queue — the previous decision is undone, stamps and all.
        ev.triaged_by = None
        ev.triaged_at = None
    else:
        ev.triaged_by = actor
        ev.triaged_at = datetime.now(timezone.utc)
    db.flush()
    log.info(
        "triage.status_set",
        event_id=ev.id, status=next_status, by=actor,
        primary=ev.primary_category,
    )


@router.patch("/events/{event_id}/status", response_model=TriageEventRow)
def set_triage_status(
    event_id: int,
    body: StatusUpdate,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TriageEventRow:
    ev = _get_event_or_404(db, event_id)
    _apply_status(db, ev, body.status, _actor(user), body.primary_category)
    db.commit()
    ev2 = _get_event_or_404(db, event_id)
    return _row(ev2, datetime.now(timezone.utc), _material_labels(db, [ev2]))


@router.patch("/events/{event_id}/category", response_model=TriageEventRow)
def set_primary_category(
    event_id: int,
    body: CategoryUpdate,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TriageEventRow:
    ev = _get_event_or_404(db, event_id)
    if ev.triage_status != "scoring":
        raise HTTPException(
            status_code=400,
            detail="Pillar can only be set on a scoring event — approve it first.",
        )
    if body.primary_category not in _VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Unknown pillar {body.primary_category!r}")
    ev.primary_category = body.primary_category
    ev.triaged_by = _actor(user)
    ev.triaged_at = datetime.now(timezone.utc)
    db.commit()
    ev2 = _get_event_or_404(db, event_id)
    return _row(ev2, datetime.now(timezone.utc), _material_labels(db, [ev2]))


@router.post("/events/{event_id}/accept", response_model=TriageEventRow)
def accept_suggestions(
    event_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TriageEventRow:
    """One-action confirm: the machine's proposal was right.

    Sets ``scoring`` with the suggested pillar and confirms every suggested
    material link.  This is the action that makes the queue only as slow as
    the events that actually need correcting.
    """
    ev = _get_event_or_404(db, event_id)
    _apply_status(db, ev, "scoring", _actor(user))
    for link in ev.material_links:
        if link.status == "suggested":
            link.status = "confirmed"
    db.commit()
    ev2 = _get_event_or_404(db, event_id)
    return _row(ev2, datetime.now(timezone.utc), _material_labels(db, [ev2]))


@router.post("/events/{event_id}/promote-operational", response_model=TriageEventRow)
def promote_operational(
    event_id: int,
    body: PromoteOperational,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TriageEventRow:
    """Approve an operational news candidate — the second confirm flavor.

    Wraps ``operational_triage.promote_candidate``: subtype required,
    severity defaulting from the operational taxonomy ladder with explicit
    override (a strike at Escondida isn't a strike at a 5-kt mine), positive
    subtypes refused, facility attachment optional, verified set in the same
    write.
    """
    from app.services.ingestion.operational_triage import TriageError, promote_candidate

    ev = _get_event_or_404(db, event_id)
    if ev.event_type != "operational_news_candidate":
        raise HTTPException(
            status_code=400,
            detail="promote-operational only applies to operational news candidates.",
        )
    try:
        promote_candidate(
            db,
            event_id,
            subtype=body.subtype,
            severity=body.severity,
            facility_name=body.facility_name,
            note=body.note,
        )
    except TriageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ev = _get_event_or_404(db, event_id)
    ev.triaged_by = _actor(user)
    ev.triaged_at = datetime.now(timezone.utc)
    db.commit()
    ev2 = _get_event_or_404(db, event_id)
    return _row(ev2, datetime.now(timezone.utc), _material_labels(db, [ev2]))


@router.post("/events/{event_id}/flags", response_model=TriageEventRow)
def flag_event(
    event_id: int,
    body: FlagCreate,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TriageEventRow:
    ev = _get_event_or_404(db, event_id)
    if not body.note_text.strip():
        raise HTTPException(status_code=400, detail="Flag text is required")
    meta = dict(ev.metadata_json or {})
    flags = list(meta.get("flags") or [])
    flags.append(
        {
            "note_type": body.note_type,
            "note_text": body.note_text.strip(),
            "author": _actor(user),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    meta["flags"] = flags
    ev.metadata_json = meta  # reassign — JSONB in-place mutation is untracked
    db.commit()
    ev2 = _get_event_or_404(db, event_id)
    return _row(ev2, datetime.now(timezone.utc), _material_labels(db, [ev2]))


# ---------------------------------------------------------------------------
# Writes — link level
# ---------------------------------------------------------------------------

@router.patch("/links/{link_id}", response_model=TriageLinkRead)
def set_link_status(
    link_id: int,
    body: LinkStatusUpdate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TriageLinkRead:
    if body.status not in {"confirmed", "rejected", "suggested"}:
        raise HTTPException(status_code=400, detail=f"Unknown link status {body.status!r}")
    link = db.scalars(
        select(RiskEventMaterial).where(RiskEventMaterial.id == link_id)
    ).first()
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found")
    link.status = body.status
    db.commit()
    return TriageLinkRead(
        id=link.id,
        material_id=link.material_id,
        label=db.scalar(
            select(Material.canonical_name).where(Material.id == link.material_id)
        ) or str(link.material_id),
        status=link.status,
        match_reason=link.match_reason,
        relevance_score=link.relevance_score,
        is_direct=bool(link.is_direct),
    )


@router.post("/events/{event_id}/links", response_model=TriageLinkRead)
def add_material_link(
    event_id: int,
    body: LinkCreate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TriageLinkRead:
    """Analyst-added link — born confirmed, provenance says so."""
    ev = _get_event_or_404(db, event_id)
    mat = db.get(Material, body.material_id)
    if mat is None:
        raise HTTPException(status_code=404, detail="Material not found")
    existing = db.scalar(
        select(RiskEventMaterial).where(
            RiskEventMaterial.risk_event_id == ev.id,
            RiskEventMaterial.material_id == mat.id,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Link already exists — confirm it instead")
    link = RiskEventMaterial(
        risk_event_id=ev.id,
        material_id=mat.id,
        relevance_score=1.0,
        is_direct=True,
        match_reason="analyst",
        status="confirmed",
    )
    db.add(link)
    db.commit()
    return TriageLinkRead(
        id=link.id,
        material_id=mat.id,
        label=mat.canonical_name,
        status=link.status,
        match_reason=link.match_reason,
        relevance_score=link.relevance_score,
        is_direct=True,
    )
