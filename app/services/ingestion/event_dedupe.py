"""Cross-source duplicate-event curation (migration 055).

Two halves, both human-in-the-loop (per Nicole's "fewer, confident,
strongly significant events" direction — nothing is flagged
automatically):

``find_duplicate_candidates``
    Loads all canonical events with their material/geography junctions,
    buckets them by (material, geography, calendar-year window), pairs
    events from DIFFERENT sources within a bucket, and scores each pair
    on subtype-family compatibility + title-token similarity.  Emits an
    xlsx review sheet with a blank ``confirm`` column.

``apply_duplicate_marks``
    Reads the reviewed sheet back and sets ``duplicate_of_id`` for rows
    where confirm == "Y".  Validates: both ids exist, no self-marks, and
    no chains (the canonical row must itself be canonical — if it was
    marked as someone else's duplicate in the same sheet, the mark is
    re-pointed at the ultimate canonical).

Canonical-suggestion priority (who wins when a pair is confirmed):
    1. manual_walkthrough        — human-audited severity, dating, subtype
    2. rows with a scoring-visible ``event_subtype`` over NULL-subtype
    3. higher ``confidence_score``
The suggestion is just a default — the reviewer can swap ids.

Date handling: sources disagree wildly on dates for the same event (the
DRC export ban: IEA 2025-01-01 [year precision], manual 2025-02-22, GTA
2025-09-21).  Jan-1 dates are treated as year-precision.  Two events are
date-compatible when within 180 days OR in the same calendar year.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.regulatory import (
    RiskEvent,
    RiskEventGeography,
    RiskEventMaterial,
)
from app.models.documents import SourceDocument
from app.models.source import Source

log = structlog.get_logger(__name__)

_SOURCE_PRIORITY = {"manual_walkthrough": 0}  # everything else = 1

_SUBTYPE_FAMILY = {
    "EXPORT_RESTRICTION": "export",
    "national_export_ban": "export",
    "national_export_quota": "export",
    "export_ban_inventory_lockup": "export",
    "quota_allocation_constraint": "export",
    "TARIFF": "tariff",
    "IMPORT_DISRUPTION": "tariff",
    "TRADE_DEFENSE": "tariff",
    "EXPORT_SUBSIDY": "subsidy",
    "POSITIVE_POLICY": "positive",
    "POSITIVE_DEVELOPMENT": "positive",
    "FDI_RESTRICTION": "fdi",
    "PROCUREMENT_POLICY": "procurement",
}

_STOPWORDS = frozenset(
    "the a an of to in on for from and or by with at as is are was were "
    "its their this that new amended applicable certain goods imports "
    "exports announcement announced government".split()
)

_MIN_SIMILARITY = 0.35


def _title_tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _family(subtype) -> str | None:
    if subtype is None:
        return None  # NULL matches any family (IEA rows have no subtype)
    return _SUBTYPE_FAMILY.get(subtype, str(subtype).lower())


def _is_year_precision(d: datetime | None) -> bool:
    return d is not None and d.month == 1 and d.day == 1


def _dates_compatible(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return True  # undated events can pair; similarity must carry it
    if a.year == b.year:
        return True
    if _is_year_precision(a) or _is_year_precision(b):
        return abs(a.year - b.year) <= 1  # 2025-01-01 may mean "sometime 2025"
    return abs((a - b).days) <= 180


@dataclass
class _Ev:
    id: int
    title: str
    date: datetime | None
    subtype: str | None
    severity: float | None
    confidence: float | None
    source: str
    materials: set[str] = field(default_factory=set)
    geos: set[str] = field(default_factory=set)
    tokens: set[str] = field(default_factory=set)


def _load_events(db: Session, material: str | None) -> dict[int, _Ev]:
    stmt = (
        select(
            RiskEvent.id,
            RiskEvent.title,
            RiskEvent.event_date,
            RiskEvent.event_subtype,
            RiskEvent.severity_score,
            RiskEvent.confidence_score,
            Source.name.label("source_name"),
        )
        .join(SourceDocument, SourceDocument.id == RiskEvent.source_document_id)
        .join(Source, Source.id == SourceDocument.source_id)
        .where(RiskEvent.duplicate_of_id.is_(None))
    )
    rows = db.execute(stmt).all()
    events = {
        r.id: _Ev(
            id=r.id, title=r.title, date=r.event_date, subtype=r.event_subtype,
            severity=r.severity_score, confidence=r.confidence_score,
            source=r.source_name, tokens=_title_tokens(r.title),
        )
        for r in rows
    }

    from app.models.supply import Material
    mat_rows = db.execute(
        select(RiskEventMaterial.risk_event_id, Material.canonical_name)
        .join(Material, Material.id == RiskEventMaterial.material_id)
    ).all()
    for ev_id, mat in mat_rows:
        if ev_id in events:
            events[ev_id].materials.add(mat)
    # PRIMARY-context geographies only.  Live calibration (2026-07-15):
    # bucketing on any shared geography produced 521 candidate pairs
    # (China Export-Control-List events paired with DRC quota events via
    # secondary/affected tags); primary-only produced 24 high-precision
    # pairs.  NULL context is treated as primary (manual rows pre-date
    # the context column).
    geo_rows = db.execute(
        select(
            RiskEventGeography.risk_event_id,
            RiskEventGeography.country_code,
            RiskEventGeography.geography_context,
        )
    ).all()
    for ev_id, cc, ctx in geo_rows:
        if ev_id in events and (ctx == "primary" or ctx is None):
            events[ev_id].geos.add(cc)

    if material is not None:
        events = {i: e for i, e in events.items() if material in e.materials}
    return events


def _pair_similarity(a: _Ev, b: _Ev) -> float:
    """Jaccard on title tokens, with a small boost when subtype families
    agree exactly (rather than via a NULL wildcard)."""
    if not a.tokens or not b.tokens:
        return 0.0
    jac = len(a.tokens & b.tokens) / len(a.tokens | b.tokens)
    fa, fb = _family(a.subtype), _family(b.subtype)
    if fa is not None and fb is not None and fa == fb:
        jac += 0.10
    return round(jac, 3)


def _suggest_canonical(a: _Ev, b: _Ev) -> tuple[_Ev, _Ev]:
    """Return (canonical, duplicate)."""
    def rank(e: _Ev):
        return (
            _SOURCE_PRIORITY.get(e.source, 1),   # manual first
            0 if e.subtype is not None else 1,   # scoring-visible subtype
            -(e.confidence or 0.0),
        )
    return (a, b) if rank(a) <= rank(b) else (b, a)


def find_duplicate_candidates(
    db: Session,
    out_path: str,
    *,
    material: str | None = None,
    min_similarity: float = _MIN_SIMILARITY,
) -> dict:
    events = _load_events(db, material)

    # Bucket by (material, geo) — a pair must share at least one of each.
    buckets: dict[tuple[str, str], list[int]] = {}
    for e in events.values():
        for m in e.materials:
            for g in e.geos:
                buckets.setdefault((m, g), []).append(e.id)

    seen_pairs: set[tuple[int, int]] = set()
    candidates = []
    for ids in buckets.values():
        for i, ida in enumerate(ids):
            a = events[ida]
            for idb in ids[i + 1:]:
                key = (min(ida, idb), max(ida, idb))
                if key in seen_pairs:
                    continue
                b = events[idb]
                if a.source == b.source:
                    continue  # content_hash handles same-source
                fa, fb = _family(a.subtype), _family(b.subtype)
                if fa is not None and fb is not None and fa != fb:
                    continue
                if not _dates_compatible(a.date, b.date):
                    continue
                sim = _pair_similarity(a, b)
                # Tiered bar (live-calibrated): when both subtypes are known
                # and in the same family, structure already agrees strongly
                # (family + material + primary geo + date window) — drop the
                # title bar to 0.15 so editorial manual titles can pair with
                # bureaucratic ingester titles (the DRC-ban manual<->GTA pair
                # scores 0.22).  NULL-subtype pairs keep the full bar.
                exact_family = (
                    _family(a.subtype) is not None
                    and _family(b.subtype) is not None
                    and _family(a.subtype) == _family(b.subtype)
                )
                bar = 0.15 if exact_family else min_similarity
                if sim < bar:
                    continue
                seen_pairs.add(key)
                canon, dup = _suggest_canonical(a, b)
                candidates.append((sim, dup, canon))

    candidates.sort(key=lambda t: -t[0])

    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Duplicate Candidates"
    header = [
        "confirm", "duplicate_id", "canonical_id", "similarity",
        "dup_source", "canon_source", "dup_date", "canon_date",
        "dup_subtype", "canon_subtype", "dup_severity", "canon_severity",
        "shared_materials", "shared_geos", "dup_title", "canon_title",
    ]
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True)
    for sim, dup, canon in candidates:
        ws.append([
            "", dup.id, canon.id, sim,
            dup.source, canon.source,
            dup.date.date().isoformat() if dup.date else None,
            canon.date.date().isoformat() if canon.date else None,
            dup.subtype, canon.subtype, dup.severity, canon.severity,
            ";".join(sorted(dup.materials & canon.materials)),
            ";".join(sorted(dup.geos & canon.geos)),
            dup.title[:300], canon.title[:300],
        ])
    for col, w in {"A": 9, "B": 12, "C": 12, "D": 10, "E": 22, "F": 22,
                   "G": 12, "H": 12, "I": 20, "J": 20, "K": 12, "L": 14,
                   "M": 20, "N": 12, "O": 60, "P": 60}.items():
        ws.column_dimensions[col].width = w
    wb.save(out_path)

    report = {
        "events_considered": len(events),
        "candidate_pairs": len(candidates),
        "out_path": out_path,
        "min_similarity": min_similarity,
        "material_filter": material,
    }
    log.info("event_dedupe.report_written", **report)
    return report


@dataclass
class _Anchored:
    """A single event plus the entity keys it can be matched on."""

    id: int
    title: str
    date: datetime | None
    subtype: str | None
    event_type: str | None
    primary_category: str | None
    source: str
    status: str
    anchors: set[tuple[str, int]] = field(default_factory=set)
    tokens: set[str] = field(default_factory=set)


def _event_status(primary_category, event_type, meta) -> str:
    """Where an event sits relative to scoring, for reviewer-facing hints.

    A hint is only actionable if the reviewer knows what the other row
    already is: ``scoring`` means promoting this candidate double-counts;
    ``rejected`` means a human already judged the same story to be noise;
    ``pending_triage`` means two feeds landed the same story and only one
    should be promoted; ``display_only`` is everything else (SEC signals,
    NULL-category rows).
    """
    if primary_category is not None:
        return "scoring"
    reason = (meta or {}).get("display_only_reason")
    if reason == "rejected":
        return "rejected"
    if event_type == "operational_news_candidate":
        return "pending_triage"
    return "display_only"


def _load_anchored_events(
    db: Session,
) -> tuple[dict[int, _Anchored], dict[tuple[str, int], str]]:
    """All canonical events with material/facility/company anchor keys.

    Returns ``(events_by_id, anchor_key -> display name)``.

    Deliberately LEFT-joins the source document: 126 events (manual
    curation predating the source-document convention) have none, and
    they are exactly the human-audited rows a new candidate is most
    likely to duplicate.  ``find_duplicate_candidates`` inner-joins
    because its xlsx report keys on source name; this loader does not.
    """
    from app.models.company import Company
    from app.models.facility import Facility
    from app.models.supply import Material
    from app.models.regulatory import RiskEventCompany, RiskEventFacility

    rows = db.execute(
        select(
            RiskEvent.id,
            RiskEvent.title,
            RiskEvent.event_date,
            RiskEvent.event_subtype,
            RiskEvent.event_type,
            RiskEvent.primary_category,
            RiskEvent.metadata_json,
            Source.name.label("source_name"),
        )
        .outerjoin(SourceDocument, SourceDocument.id == RiskEvent.source_document_id)
        .outerjoin(Source, Source.id == SourceDocument.source_id)
        .where(RiskEvent.duplicate_of_id.is_(None))
    ).all()
    events = {
        r.id: _Anchored(
            id=r.id, title=r.title, date=r.event_date, subtype=r.event_subtype,
            event_type=r.event_type, primary_category=r.primary_category,
            source=r.source_name or "(uncredited)",
            status=_event_status(r.primary_category, r.event_type, r.metadata_json),
            tokens=_title_tokens(r.title),
        )
        for r in rows
    }

    junctions = (
        ("material", RiskEventMaterial.risk_event_id,
         RiskEventMaterial.material_id, Material),
        ("facility", RiskEventFacility.risk_event_id,
         RiskEventFacility.facility_id, Facility),
        ("company", RiskEventCompany.risk_event_id,
         RiskEventCompany.company_id, Company),
    )
    names: dict[tuple[str, int], str] = {}
    for kind, ev_col, ent_col, model in junctions:
        label = model.name if model is Facility else model.canonical_name
        for ev_id, ent_id, ent_name in db.execute(
            select(ev_col, ent_col, label).join(model, model.id == ent_col)
        ):
            if ev_id in events:
                events[ev_id].anchors.add((kind, ent_id))
                names[(kind, ent_id)] = ent_name
    return events, names


def find_similar_events(
    db: Session,
    event_ids: list[int],
    *,
    min_similarity: float = _MIN_SIMILARITY,
    limit: int = 3,
) -> dict[int, dict]:
    """Per-event duplicate hints for a review queue (2026-07-28, Nicole).

    Read-only counterpart to ``find_duplicate_candidates``: instead of
    sweeping the whole corpus into an xlsx, it answers "does anything
    already in the DB look like THIS event?" for a short list of ids, so
    the operational triage queue can warn a reviewer before they promote
    a story the partner already curated by hand.

    Same gates as the xlsx report — shared entity anchor, compatible
    subtype family, compatible dates, title-token Jaccard over a tiered
    bar — with two deliberate differences:

    * Anchors are material OR facility OR company, not (material AND
      primary geography).  News candidates are anchored facility-first;
      requiring a geography row would miss the company-only exchange-feed
      items entirely.
    * Same-source pairs are NOT skipped.  ``content_hash`` only catches
      byte-identical re-ingestion, so two different articles about one
      halt — an 8-K and a wire story, both on the news watchlist — are a
      real duplicate pair this must surface.

    Returns ``{event_id: {"checked": bool, "reason": str|None,
    "hints": [...]}}``.  ``checked=False`` means the event carries no
    entity anchors, so no reliable comparison was possible — reported
    honestly rather than falling back to a title-only scan that would
    pair unrelated materials.  An empty ``hints`` list under
    ``checked=True`` is a real negative; the two are NOT interchangeable
    and the CLI renders them differently.
    """
    if not event_ids:
        return {}

    events, names = _load_anchored_events(db)

    by_anchor: dict[tuple[str, int], list[int]] = {}
    for e in events.values():
        for a in e.anchors:
            by_anchor.setdefault(a, []).append(e.id)

    out: dict[int, dict] = {}
    for ev_id in event_ids:
        target = events.get(ev_id)
        if target is None:
            out[ev_id] = {"checked": False, "reason": "event_not_found",
                          "hints": []}
            continue
        if not target.anchors:
            out[ev_id] = {
                "checked": False,
                "reason": "no_material_facility_or_company_links",
                "hints": [],
            }
            continue

        seen: set[int] = set()
        scored: list[tuple[float, dict]] = []
        for anchor in target.anchors:
            for other_id in by_anchor.get(anchor, ()):
                if other_id == ev_id or other_id in seen:
                    continue
                seen.add(other_id)
                other = events[other_id]
                fa, fb = _family(target.subtype), _family(other.subtype)
                if fa is not None and fb is not None and fa != fb:
                    continue
                if not _dates_compatible(target.date, other.date):
                    continue
                sim = _pair_similarity(target, other)
                # Both families known implies they MATCH (mismatches were
                # skipped above), so this is the same tiered bar the xlsx
                # report uses: family + shared anchor + date window is
                # already strong structural agreement, and the title bar
                # drops to 0.15 so an editorial headline can pair with a
                # bureaucratic ingester title.
                bar = 0.15 if (fa is not None and fb is not None) else min_similarity
                if sim < bar:
                    continue
                shared = sorted(
                    names.get(a, f"{a[0]}:{a[1]}")
                    for a in target.anchors & other.anchors
                )
                scored.append((sim, {
                    "event_id": other.id,
                    "title": other.title,
                    "source": other.source,
                    "date": other.date.date().isoformat() if other.date else None,
                    "subtype": other.subtype,
                    "primary_category": other.primary_category,
                    "status": other.status,
                    "scores_already": other.primary_category is not None,
                    "similarity": sim,
                    "shared": shared,
                }))
        scored.sort(key=lambda t: (-t[0], t[1]["event_id"]))
        out[ev_id] = {
            "checked": True,
            "reason": None,
            "hints": [h for _, h in scored[:limit]],
        }
    return out


def apply_duplicate_marks(
    db: Session,
    xlsx_path: str,
    *,
    dry_run: bool = False,
) -> dict:
    from openpyxl import load_workbook
    wb = load_workbook(xlsx_path, data_only=True, read_only=True)
    ws = wb["Duplicate Candidates"]
    rows_iter = ws.iter_rows(values_only=True)
    header = [str(h).strip().lower() if h else "" for h in next(rows_iter)]
    idx = {c: header.index(c) for c in ("confirm", "duplicate_id", "canonical_id")}

    confirmed: dict[int, int] = {}
    errors: list[str] = []
    skipped = 0
    for row_num, raw in enumerate(rows_iter, start=2):
        confirm = str(raw[idx["confirm"]] or "").strip().upper()
        if confirm != "Y":
            skipped += 1
            continue
        try:
            dup_id = int(raw[idx["duplicate_id"]])
            canon_id = int(raw[idx["canonical_id"]])
        except (TypeError, ValueError):
            errors.append(f"row {row_num}: non-integer ids")
            continue
        if dup_id == canon_id:
            errors.append(f"row {row_num}: self-mark {dup_id}")
            continue
        if dup_id in confirmed and confirmed[dup_id] != canon_id:
            errors.append(
                f"row {row_num}: {dup_id} marked duplicate of both "
                f"{confirmed[dup_id]} and {canon_id}"
            )
            continue
        confirmed[dup_id] = canon_id

    # Chain resolution: if a canonical is itself confirmed as a duplicate,
    # re-point at the ultimate canonical (with cycle guard).
    def resolve(cid: int, hops: int = 0) -> int | None:
        if hops > 10:
            return None
        return resolve(confirmed[cid], hops + 1) if cid in confirmed else cid

    resolved: dict[int, int] = {}
    for dup_id, canon_id in confirmed.items():
        ultimate = resolve(canon_id)
        if ultimate is None or ultimate == dup_id:
            errors.append(f"cycle involving event {dup_id} — skipped")
            continue
        resolved[dup_id] = ultimate

    if errors:
        return {"applied": 0, "skipped_unconfirmed": skipped, "errors": errors,
                "dry_run": dry_run}

    all_ids = set(resolved) | set(resolved.values())
    existing = set(
        db.scalars(select(RiskEvent.id).where(RiskEvent.id.in_(all_ids))).all()
    )
    missing = all_ids - existing
    if missing:
        return {"applied": 0, "errors": [f"unknown event ids: {sorted(missing)}"],
                "dry_run": dry_run}

    applied = 0
    for dup_id, canon_id in resolved.items():
        ev = db.get(RiskEvent, dup_id)
        if ev.duplicate_of_id == canon_id:
            continue
        ev.duplicate_of_id = canon_id
        applied += 1

    if dry_run:
        db.rollback()
    else:
        db.commit()
    result = {"applied": applied, "skipped_unconfirmed": skipped,
              "errors": [], "dry_run": dry_run}
    log.info("event_dedupe.applied", **result)
    return result
