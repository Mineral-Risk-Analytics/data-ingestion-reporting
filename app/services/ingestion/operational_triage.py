"""Triage flow for operational news candidates (2026-07-27).

The counterpart to ``ingest_operational_news.py``: that module lands
display-only ``operational_news_candidate`` events; this one is the ONLY
sanctioned write path that promotes them into scoring. Until the admin UI
exists, the CLI wrappers (``list-operational-candidates`` /
``promote-operational-event`` / ``reject-operational-event``) ARE the triage
surface — no more raw SQL.

Promotion semantics (mirrors GTA's type-driven severity on the geopolitical
pillar):

* The reviewer picks a subtype. If it's in
  ``constants.OPERATIONAL_SUBTYPE_DEFAULT_SEVERITY`` (the controlled
  taxonomy), severity defaults from the ladder; an explicit severity
  overrides (scale matters — a strike at Escondida isn't a strike at a 5kt
  mine). A subtype outside the taxonomy is allowed (the walkthrough
  vocabulary is free-form) but then an explicit severity is REQUIRED.
* Promotion sets ``primary_category='operational'``, writes the subtype and
  severity, marks the event verified, and rewrites the display-only
  metadata into an audit trail (``triage`` block). After the next rescore
  the event participates in ``_score_operational_market``.
* Optionally attaches a curated facility (for exchange-feed candidates that
  landed company-only): adds the facility link plus the geography/material/
  operator-company links exactly as the ingester would have.

Rejection keeps the row (append-only spirit — the DB is the audit trail)
but moves it out of the pending queue: ``display_only_reason='rejected'``
with a reason string. Rejected events never score (primary_category stays
NULL) and the pending list stops showing them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import OPERATIONAL_SUBTYPE_DEFAULT_SEVERITY
from app.models import (
    CompanyFacility,
    Facility,
    FacilityMaterialLink,
    RiskEvent,
    RiskEventCompany,
    RiskEventFacility,
    RiskEventGeography,
    RiskEventMaterial,
)
from app.services.ingestion.ingest_operational_news import EVENT_TYPE_CANDIDATE

log = structlog.get_logger(__name__)


class TriageError(ValueError):
    """Raised on invalid triage operations — caller-facing message."""


def _load_candidate(session: Session, event_id: int) -> RiskEvent:
    event = session.get(RiskEvent, event_id)
    if event is None:
        raise TriageError(f"No risk_event with id={event_id}")
    if event.event_type != EVENT_TYPE_CANDIDATE:
        raise TriageError(
            f"Event {event_id} is {event.event_type!r}, not an "
            f"{EVENT_TYPE_CANDIDATE!r} — triage only operates on news "
            "candidates. Walkthrough/manual events are curated directly."
        )
    return event


def list_pending_candidates(session: Session, *, limit: int = 100) -> list[dict]:
    """The triage queue: candidates still pending (not promoted, not rejected).

    Returns plain dicts (newest first) so the CLI can render without ORM
    coupling.
    """
    events = session.scalars(
        select(RiskEvent)
        .where(
            RiskEvent.event_type == EVENT_TYPE_CANDIDATE,
            RiskEvent.primary_category.is_(None),
        )
        .order_by(RiskEvent.event_date.desc().nullslast(), RiskEvent.id.desc())
        .limit(limit * 2)  # over-fetch; rejected rows are filtered in Python
    ).all()

    out: list[dict] = []
    for ev in events:
        meta = ev.metadata_json or {}
        if meta.get("display_only_reason") == "rejected":
            continue
        fac_names = [
            name for (name,) in session.execute(
                select(Facility.name)
                .join(RiskEventFacility, RiskEventFacility.facility_id == Facility.id)
                .where(RiskEventFacility.risk_event_id == ev.id)
            )
        ]
        out.append({
            "id": ev.id,
            "date": ev.event_date.date().isoformat() if ev.event_date else None,
            "title": ev.title,
            "feed": meta.get("source_feed"),
            "suggested_subtype": meta.get("suggested_subtype"),
            "linked_facilities": fac_names,
            "candidate_facilities": [
                c.get("name") for c in meta.get("candidate_facilities", [])
            ],
            "url": ev.source_document.url if ev.source_document else None,
        })
        if len(out) >= limit:
            break
    return out


def promote_candidate(
    session: Session,
    event_id: int,
    *,
    subtype: str,
    severity: Optional[float] = None,
    facility_name: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Promote a candidate into the operational pillar. Caller commits.

    Severity resolution: explicit ``severity`` wins; else the taxonomy
    default for ``subtype``; else error (free-form subtypes need an explicit
    number). Range-checked to [0, 1].
    """
    event = _load_candidate(session, event_id)
    if event.primary_category is not None:
        raise TriageError(f"Event {event_id} is already promoted")

    subtype = subtype.strip()
    if not subtype:
        raise TriageError("subtype is required")

    default = OPERATIONAL_SUBTYPE_DEFAULT_SEVERITY.get(subtype)
    if severity is None:
        if default is None:
            raise TriageError(
                f"Subtype {subtype!r} is not in the controlled taxonomy — "
                "an explicit severity is required. Taxonomy: "
                + ", ".join(sorted(OPERATIONAL_SUBTYPE_DEFAULT_SEVERITY))
            )
        severity = default
        severity_source = "taxonomy_default"
    else:
        severity_source = "override" if default is not None else "explicit"
    if not 0.0 <= severity <= 1.0:
        raise TriageError(f"severity must be in [0, 1], got {severity}")

    links_added = {"facility": 0, "geography": 0, "material": 0, "company": 0}
    if facility_name:
        links_added = _attach_facility(session, event, facility_name)

    event.event_subtype = subtype
    event.severity_score = severity
    event.primary_category = "operational"
    event.verified = True

    meta = dict(event.metadata_json or {})
    meta.pop("scoring", None)
    meta.pop("display_only_reason", None)
    meta["triage"] = {
        "action": "promoted",
        "at": datetime.now(timezone.utc).isoformat(),
        "severity_source": severity_source,
        **({"note": note} if note else {}),
    }
    event.metadata_json = meta
    session.flush()

    result = {
        "event_id": event.id,
        "subtype": subtype,
        "severity": severity,
        "severity_source": severity_source,
        "links_added": links_added,
    }
    log.info("operational_triage.promoted", **result)
    return result


def reject_candidate(
    session: Session,
    event_id: int,
    *,
    reason: str,
) -> dict:
    """Move a candidate out of the pending queue without deleting it.

    The row stays as the audit trail; ``primary_category`` stays NULL so it
    can never score. Re-ingestion won't resurrect it — the content_hash
    dedupe still sees the row.
    """
    event = _load_candidate(session, event_id)
    if event.primary_category is not None:
        raise TriageError(f"Event {event_id} is already promoted — cannot reject")
    if not reason.strip():
        raise TriageError("A rejection reason is required (audit trail)")

    meta = dict(event.metadata_json or {})
    meta["display_only_reason"] = "rejected"
    meta["triage"] = {
        "action": "rejected",
        "at": datetime.now(timezone.utc).isoformat(),
        "reason": reason.strip(),
    }
    event.metadata_json = meta
    session.flush()
    log.info("operational_triage.rejected", event_id=event.id, reason=reason)
    return {"event_id": event.id, "rejected": True}


def _attach_facility(
    session: Session, event: RiskEvent, facility_name: str,
) -> dict:
    """Attach a CURATED facility + its geography/material/operator links.

    Mirrors the ingester's anchoring exactly (same relevance/match_reason
    conventions). Curated facilities only — MRDS rows never enter the
    operational pillar (spec §6). Skips links that already exist.
    """
    facility = session.scalar(
        select(Facility).where(
            Facility.name == facility_name,
            Facility.data_source == "partner_facility_seed",
        )
    )
    if facility is None:
        raise TriageError(
            f"No curated facility named {facility_name!r} "
            "(exact match against partner_facility_seed rows)"
        )

    added = {"facility": 0, "geography": 0, "material": 0, "company": 0}

    existing_fac = set(session.scalars(
        select(RiskEventFacility.facility_id)
        .where(RiskEventFacility.risk_event_id == event.id)
    ))
    if facility.id not in existing_fac:
        session.add(RiskEventFacility(
            risk_event_id=event.id, facility_id=facility.id,
            relevance_score=1.0, match_reason="triage_attach",
        ))
        added["facility"] = 1

    existing_geo = set(session.scalars(
        select(RiskEventGeography.country_code)
        .where(RiskEventGeography.risk_event_id == event.id)
    ))
    if facility.country and facility.country not in existing_geo:
        session.add(RiskEventGeography(
            risk_event_id=event.id, country_code=facility.country,
            geography_context="primary", relevance_score=1.0,
        ))
        added["geography"] = 1

    existing_mats = set(session.scalars(
        select(RiskEventMaterial.material_id)
        .where(RiskEventMaterial.risk_event_id == event.id)
    ))
    for mid in session.scalars(
        select(FacilityMaterialLink.material_id)
        .where(FacilityMaterialLink.facility_id == facility.id)
    ):
        if mid not in existing_mats:
            session.add(RiskEventMaterial(
                risk_event_id=event.id, material_id=mid,
                relevance_score=1.0, is_direct=True,
                match_reason="facility_link",
            ))
            added["material"] += 1

    existing_cos = set(session.scalars(
        select(RiskEventCompany.company_id)
        .where(RiskEventCompany.risk_event_id == event.id)
    ))
    for cid in session.scalars(
        select(CompanyFacility.company_id)
        .where(CompanyFacility.facility_id == facility.id)
    ):
        if cid not in existing_cos:
            session.add(RiskEventCompany(
                risk_event_id=event.id, company_id=cid,
                relevance_score=0.85, match_reason="facility_operator",
            ))
            added["company"] += 1

    session.flush()
    return added
