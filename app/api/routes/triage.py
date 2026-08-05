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

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, get_db
from app.constants import POSITIVE_EVENT_SUBTYPES, RiskCategory
from app.models.country import Country
from app.models.documents import SourceDocument
from app.models.regulatory import RiskEvent, RiskEventGeography, RiskEventMaterial
from app.models.source import Source
from app.models.supply import Material
from app.services.scoring.launch_list import is_launch_list_material

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/triage", tags=["triage"])

_VALID_STATUSES = {"pending_triage", "scoring", "display_only", "rejected"}
_VALID_CATEGORIES = {c.value for c in RiskCategory}

# The coverage tracker's bar and window are the dashboard's thin-events
# thresholds, imported (privates and all) rather than restated: the whole
# point of the tracker is to explain the dashboard's "thin" verdict, and two
# copies of the number would eventually disagree about what "thin" means.
from app.api.routes.dashboard import (  # noqa: E402
    _GAP_THIN_EVENTS_MIN as _COVERAGE_TARGET,
    _GAP_THIN_EVENTS_WINDOW_DAYS as _COVERAGE_WINDOW_DAYS,
)


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
    # Discriminator for the drawer's grouped link list.  Only ``material``
    # links exist today: migration 066 added ``status`` to
    # ``risk_event_materials`` alone, so company / facility / regulation
    # links have nowhere to record a triage decision.  The field is emitted
    # now so the UI groups correctly the day those tables gain a status
    # column, rather than the UI having to guess.
    kind: str = "material"


class TriageFlagRead(BaseModel):
    """A data-quality note filed against an event.

    Stored in ``metadata_json["flags"]`` (see ``flag_event``).  These were
    write-only until now — the drawer could file one and never read it back,
    so notes vanished the moment they were submitted.
    """

    id: str
    note_type: str
    note_text: str
    author: Optional[str] = None
    created_at: Optional[str] = None


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
    # The full set of pillars the event touches (risk_categories_json), as
    # distinct from ``primary_category`` — the single pillar it scores in.
    # Shown read-only in the drawer: the set is derived at ingest, only the
    # scoring pillar is a triage decision.
    risk_categories: list[str] = Field(default_factory=list)
    # ISO country codes from geography_json, primary first.
    geography_codes: list[str] = Field(default_factory=list)
    # code → display name, covering exactly the codes in ``geography_codes``.
    #
    # Resolved server-side against the ``countries`` table rather than left to
    # the browser's Intl.DisplayNames, because these codes are whatever
    # ``countries.iso2`` holds and that column explicitly admits non-ISO
    # entries — the column comment names bloc identifiers like "EU", and any
    # territory row that has been added behaves the same way.  Intl returns
    # the code unchanged for anything it does not recognise, which produces a
    # tooltip that repeats the text the user is already looking at.  A code
    # with no matching row is simply absent here and the UI shows the bare
    # code, which is the honest rendering of "nothing in the reference table
    # explains this".
    geography_names: dict[str, str] = Field(default_factory=dict)
    # Served rather than inferred: the UI previously carried its own copy of
    # POSITIVE_EVENT_SUBTYPES, which had already drifted from the backend's
    # set.  One source of truth, evaluated where the constant lives.
    is_positive: bool = False
    flags: list[TriageFlagRead] = Field(default_factory=list)


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


class MaterialCoverageItem(BaseModel):
    material_id: int
    name: str
    is_launch_list: bool
    # Direct, confirmed links on live events inside the window — the signal an
    # analyst has actually stood behind.
    confirmed: int
    # Direct, still-suggested links on live events inside the window — present
    # in the queue, contributing nothing until someone confirms them.
    pending: int


class MaterialCoverage(BaseModel):
    window_days: int
    # The "5-event bar": below this many confirmed events a material counts as
    # thin.  Served rather than hardcoded client-side so the tracker and the
    # dashboard's coverage-gaps KPI can never disagree about the bar.
    target: int
    items: list[MaterialCoverageItem]


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


def _country_names(db: Session, events: list[RiskEvent]) -> dict[str, str]:
    """Geography codes → display names, in one batch query.

    Same shape and reason as ``_material_labels``: geography_json stores bare
    codes, there is no relationship to follow, and a page of 25 events would
    otherwise be 25-plus lookups.  Unmatched codes are left out of the map
    rather than defaulted, so the caller can tell "no row in ``countries``"
    apart from "row exists, name happens to equal the code".
    """
    codes = {c for ev in events for c in _geography_codes(ev)}
    if not codes:
        return {}
    rows = db.execute(
        select(Country.iso2, Country.name).where(Country.iso2.in_(codes))
    ).all()
    return {iso2: name for iso2, name in rows}


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


def _pillars(ev: RiskEvent) -> list[str]:
    """risk_categories_json → list[str], tolerant of the shapes the ingesters
    produce (list, dict keyed by slug, or None).  Mirrors
    ``materials._coerce_pillars``; kept local so the triage surface does not
    import another router."""
    raw = ev.risk_categories_json
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if isinstance(raw, dict):
        return [str(k) for k in raw.keys() if k]
    return []


def _geography_codes(ev: RiskEvent) -> list[str]:
    """geography_json → ISO codes, primary first, de-duplicated in order.
    Shape is ``{"primary": "CN", "secondary": ["RU", "CD"]}``."""
    geo = ev.geography_json
    if not isinstance(geo, dict):
        return []
    out: list[str] = []
    primary = geo.get("primary")
    if isinstance(primary, str) and primary:
        out.append(primary)
    secondary = geo.get("secondary")
    if isinstance(secondary, list):
        for code in secondary:
            if isinstance(code, str) and code and code not in out:
                out.append(code)
    return out


def _flags(ev: RiskEvent) -> list[TriageFlagRead]:
    """metadata_json["flags"] → typed notes.

    Notes carry no database id (they live inside a JSONB blob), so the index
    is used as a stable-within-a-response key for the UI's list rendering.
    """
    meta = ev.metadata_json or {}
    raw = meta.get("flags") or []
    out: list[TriageFlagRead] = []
    for i, note in enumerate(raw):
        if not isinstance(note, dict):
            continue
        text = note.get("note_text")
        if not text:
            continue
        out.append(
            TriageFlagRead(
                id=f"{ev.id}-{i}",
                note_type=str(note.get("note_type") or "issue"),
                note_text=str(text),
                author=note.get("author"),
                created_at=note.get("created_at"),
            )
        )
    return out


def _row(
    ev: RiskEvent,
    now: datetime,
    labels: dict[int, str],
    country_names: dict[str, str],
) -> TriageEventRow:
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
                kind="material",
            )
            for l in ev.material_links
        ],
        risk_categories=_pillars(ev),
        geography_codes=_geography_codes(ev),
        # Narrowed to this event's own codes so the payload does not carry the
        # whole batch's names on every row.
        geography_names={
            c: country_names[c] for c in _geography_codes(ev) if c in country_names
        },
        is_positive=_is_positive_direction(ev),
        flags=_flags(ev),
    )


def _one(db: Session, ev: RiskEvent) -> TriageEventRow:
    """Single-event response body.

    Every write endpoint returns the mutated event, and each was repeating the
    same three-part construction.  Collapsing it means a new lookup map — this
    one added ``_country_names`` — has one place to be wired in rather than
    six, which is exactly how the geography names would otherwise have been
    served on the list and missing from every write response.
    """
    return _row(
        ev,
        datetime.now(timezone.utc),
        _material_labels(db, [ev]),
        _country_names(db, [ev]),
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


@router.get("/coverage", response_model=MaterialCoverage)
def material_coverage(
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MaterialCoverage:
    """Per-material event coverage, split into confirmed vs queue-pending.

    The dashboard's coverage-gaps KPI already counts events per launch-list
    material, but a plain count cannot distinguish the two states an analyst
    actually cares about on the triage screen: a material below the bar with
    suggestions waiting in the queue is *triage backlog* (clear the queue and
    the gap may close), while a material below the bar with nothing pending is
    a *sourcing problem* no amount of triage will fix.  This endpoint splits
    them, per link status.

    Counting rules, shared with ``_compute_coverage_gaps`` in dashboard.py:

      * ``duplicate_of_id IS NULL`` — confirmed duplicates never count (055)
      * ``is_direct IS TRUE``       — inherited links are not coverage (056)
      * 90-day window, 5-event bar  — same thresholds, served in the payload

    Two deliberate divergences from the dashboard count:

      * The window is on ``event_date``, not ``created_at``.  The dashboard
        measures ingest recency; this card measures *signal* recency — an
        event ingested yesterday about something that happened last year is
        not fresh coverage.  Future-dated events count (they are separately
        flagged as a data defect, but they are signal); events with no date
        at all do not, because "inside a window" is unanswerable for them.
      * Both buckets are gated on EVENT status as well as link status.  The
        dashboard counts every direct link regardless; here the buckets mean
        exactly what their tooltips claim:

          confirmed — link confirmed AND event ``scoring``.  What scoring
                      will actually read after the Phase 6 flip.
          pending   — link not rejected AND event ``pending_triage``.  What
                      working the queue can still promote into the count.

        A ``display_only`` event contributes to NEITHER bucket, and post-
        reset that is the difference that matters: ~half the corpus is
        machine-routed display_only (supportive-direction subsidies, AD/CVD
        procedural steps, sanction statistics).  Counting their links as
        "pending" made gaps look closable from the queue when the events
        backing them had already been routed out of scoring — confirming
        those links changes nothing.  Their exclusion is also why a
        display_only-heavy material can honestly show "no signal at all"
        while its event list looks busy.  Rejected links and rejected
        events likewise count nowhere — an analyst said no, which is not
        "pending".

    Every material row is returned, including all-zero ones: "no signal at
    all" is the most important state the tracker renders, and it cannot be
    distinguished from "not returned" client-side.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_COVERAGE_WINDOW_DAYS)

    rows = db.execute(
        select(
            RiskEventMaterial.material_id,
            func.sum(
                case(
                    (
                        and_(
                            RiskEvent.triage_status == "scoring",
                            RiskEventMaterial.status == "confirmed",
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("confirmed"),
            func.sum(
                case(
                    (
                        and_(
                            RiskEvent.triage_status == "pending_triage",
                            RiskEventMaterial.status != "rejected",
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("pending"),
        )
        .join(RiskEvent, RiskEvent.id == RiskEventMaterial.risk_event_id)
        .where(
            RiskEvent.duplicate_of_id.is_(None),
            RiskEvent.triage_status.in_(("scoring", "pending_triage")),
            RiskEvent.event_date.isnot(None),
            RiskEvent.event_date >= cutoff,
            RiskEventMaterial.is_direct.is_(True),
        )
        .group_by(RiskEventMaterial.material_id)
    ).all()
    counts = {r.material_id: (int(r.confirmed or 0), int(r.pending or 0)) for r in rows}

    materials = db.execute(
        select(Material.id, Material.canonical_name).order_by(Material.canonical_name)
    ).all()

    return MaterialCoverage(
        window_days=_COVERAGE_WINDOW_DAYS,
        target=_COVERAGE_TARGET,
        items=[
            MaterialCoverageItem(
                material_id=m.id,
                name=m.canonical_name,
                is_launch_list=is_launch_list_material(m.canonical_name),
                confirmed=counts.get(m.id, (0, 0))[0],
                pending=counts.get(m.id, (0, 0))[1],
            )
            for m in materials
        ],
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
    country: Optional[str] = Query(None, description="geography code (countries.iso2), primary or secondary"),
    defect: Optional[str] = Query(None, description="quality-defect facet, e.g. needs_material_review"),
    has_defects: bool = Query(False, description="only events carrying at least one quality defect"),
    flagged: bool = Query(False, description="only events with an open data-quality note"),
    sort: str = Query("event_date", description="event_date|severity_score|triage_status"),
    sort_dir: str = Query("desc", description="asc|desc"),
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
    if country:
        # Filtered through the risk_event_geographies junction rather than
        # geography_json: every active ingester writes junction rows, the
        # column is indexed, and JSON containment is not portable between
        # Postgres and the SQLite the tests run on.  Matches primary and
        # secondary geographies alike — "show me the China events" means any
        # event that touches China, not only the ones where it leads.
        stmt = stmt.where(
            RiskEvent.id.in_(
                select(RiskEventGeography.risk_event_id).where(
                    RiskEventGeography.country_code == country.upper()
                )
            )
        )

    now = datetime.now(timezone.utc)

    asc = sort_dir == "asc"
    if sort == "severity_score":
        col = RiskEvent.severity_score
        stmt = stmt.order_by(
            (col.asc() if asc else col.desc()).nullslast(), RiskEvent.id.desc()
        )
    elif sort == "triage_status":
        col = RiskEvent.triage_status
        stmt = stmt.order_by(col.asc() if asc else col.desc(), RiskEvent.id.desc())
    else:
        col = RiskEvent.event_date
        stmt = stmt.order_by(
            (col.asc() if asc else col.desc()).nullslast(), RiskEvent.id.desc()
        )

    # Defect facets are derived per-row, so filter in Python.  ``flagged``
    # joins them: notes live inside metadata_json, and the JSONB array-length
    # predicate that would express it in SQL has no SQLite equivalent, so the
    # test suite could not exercise it.  The corpus is a few thousand rows;
    # when it grows past that, materialise both.
    if defect or has_defects or flagged:
        events = list(db.scalars(stmt).all())

        def _keep(e: RiskEvent) -> bool:
            if flagged and not ((e.metadata_json or {}).get("flags") or []):
                return False
            if not (defect or has_defects):
                return True
            found = _quality_defects(e, now)
            if defect and defect not in found:
                return False
            if has_defects and not found:
                return False
            return True

        events = [e for e in events if _keep(e)]
        total = len(events)
        events = events[(page - 1) * limit : (page - 1) * limit + limit]
    else:
        total = int(
            db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
            or 0
        )
        events = list(db.scalars(stmt.offset((page - 1) * limit).limit(limit)).all())

    labels = _material_labels(db, events)
    country_names = _country_names(db, events)
    return TriageEventList(
        items=[_row(e, now, labels, country_names) for e in events],
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
    return _one(db, ev)


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
    return _one(db, ev2)


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
    return _one(db, ev2)


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
    return _one(db, ev2)


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
    return _one(db, ev2)


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
    return _one(db, ev2)


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
        kind="material",
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
        # Same provenance code the operational-triage service writes for a
        # hand-attached link (operational_triage.py) — one value, so the UI
        # hint and any downstream reason grouping do not split in two.
        match_reason="triage_attach",
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
        kind="material",
    )
