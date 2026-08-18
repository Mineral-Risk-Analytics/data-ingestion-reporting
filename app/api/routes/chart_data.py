"""Public chart-data series for insight-post ``dataRef`` charts.

Serves whitelisted, read-only series in the exact shape the frontend
``ChartBlock`` consumes: ``{"data": [{"label": str, "value": float}, ...]}``.
A chart spec in an article body references a series as e.g.::

    {"type": "bar", "dataRef": "scores/latest", "title": "..."}
    {"type": "line", "dataRef": "scores/history?material=Cobalt"}
    {"type": "bar", "dataRef": "events/monthly?min_severity=0.5"}

Unauthenticated by design — these back charts on the PUBLIC intelligence
hub, and every series is an aggregate the hub already displays elsewhere
(sidebar risk bars, event ticker). Anything sensitive stays out of the
whitelist. Refs are explicit routes, not dynamic SQL — there is no generic
query passthrough.

Added 2026-07-08 (insight content pipeline; see ChartBlock.tsx).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.regulatory import RiskEvent
from app.models.scoring import MaterialGeographyRiskScore
from app.models.supply import Material

router = APIRouter(prefix="/chart-data", tags=["chart-data"])


class ChartDatum(BaseModel):
    label: str
    value: float


class ChartSeries(BaseModel):
    ref: str
    data: list[ChartDatum]
    as_of: Optional[str] = None


def _latest_score_rows(db: Session):
    """Latest row per (material, geography) — same approach as /risk-summary."""
    latest = (
        select(
            MaterialGeographyRiskScore.material_id.label("mid"),
            MaterialGeographyRiskScore.geography_code.label("geo"),
            func.max(MaterialGeographyRiskScore.as_of_date).label("max_date"),
        )
        .group_by(
            MaterialGeographyRiskScore.material_id,
            MaterialGeographyRiskScore.geography_code,
        )
        .subquery()
    )
    return db.execute(
        select(
            Material.canonical_name,
            MaterialGeographyRiskScore.overall_risk_score,
            MaterialGeographyRiskScore.as_of_date,
        )
        .join(
            latest,
            (MaterialGeographyRiskScore.material_id == latest.c.mid)
            & (MaterialGeographyRiskScore.geography_code == latest.c.geo)
            & (MaterialGeographyRiskScore.as_of_date == latest.c.max_date),
        )
        .join(Material, Material.id == MaterialGeographyRiskScore.material_id)
    ).all()


@router.get("/scores/latest", response_model=ChartSeries)
def scores_latest(db: Session = Depends(get_db)) -> ChartSeries:
    """Latest overall risk score per material (highest-risk geography wins).

    Mirrors the hub sidebar's Material Risk bars so article charts and the
    sidebar can never disagree.
    """
    best: dict[str, tuple[float, str]] = {}
    max_date = None
    for name, score, as_of in _latest_score_rows(db):
        if score is None:
            continue
        if name not in best or score > best[name][0]:
            best[name] = (float(score), str(as_of))
        if max_date is None or (as_of and str(as_of) > max_date):
            max_date = str(as_of)
    data = [
        ChartDatum(label=name, value=round(score, 3))
        for name, (score, _) in sorted(best.items(), key=lambda kv: -kv[1][0])
    ]
    return ChartSeries(ref="scores/latest", data=data, as_of=max_date)


@router.get("/scores/history", response_model=ChartSeries)
def scores_history(
    material: str = Query(..., description="Material canonical name, e.g. Cobalt"),
    months: int = Query(12, ge=1, le=60),
    db: Session = Depends(get_db),
) -> ChartSeries:
    """Score-over-time for one material (max across geographies per date)."""
    since = datetime.now(timezone.utc) - timedelta(days=31 * months)
    rows = db.execute(
        select(
            MaterialGeographyRiskScore.as_of_date,
            func.max(MaterialGeographyRiskScore.overall_risk_score),
        )
        .join(Material, Material.id == MaterialGeographyRiskScore.material_id)
        .where(
            func.lower(Material.canonical_name) == material.strip().lower(),
            MaterialGeographyRiskScore.as_of_date >= since.date(),
        )
        .group_by(MaterialGeographyRiskScore.as_of_date)
        .order_by(MaterialGeographyRiskScore.as_of_date)
    ).all()
    if not rows:
        raise HTTPException(
            status_code=404, detail=f"no score history for material '{material}'"
        )
    data = [
        ChartDatum(label=str(as_of), value=round(float(score), 3))
        for as_of, score in rows
        if score is not None
    ]
    return ChartSeries(
        ref=f"scores/history?material={material}",
        data=data,
        as_of=data[-1].label if data else None,
    )


@router.get("/events/monthly", response_model=ChartSeries)
def events_monthly(
    months: int = Query(12, ge=1, le=36),
    min_severity: float = Query(0.0, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
) -> ChartSeries:
    """Risk-event counts per calendar month (optionally severity-filtered).

    Material-level filtering is deliberately absent in v1: the event→material
    junction shape differs between ingested and manual events. Add it here
    once the loader lands the unified junction (do NOT bolt on a JSON scan).
    """
    since = datetime.now(timezone.utc) - timedelta(days=31 * months)
    month = func.to_char(RiskEvent.event_date, "YYYY-MM")
    conditions = [
        RiskEvent.event_date.is_not(None),
        RiskEvent.event_date >= since,
        # 2026-08-03: the chart used to count every stored row, which is not
        # what any other surface calls "an event" — confirmed duplicates (055)
        # and soft-dismissed rows (066) are invisible everywhere else, so a
        # month bar that includes them disagrees with the lists it links to.
        RiskEvent.duplicate_of_id.is_(None),
        RiskEvent.triage_status != "rejected",
    ]
    if min_severity > 0:
        conditions.append(RiskEvent.severity_score >= min_severity)
    rows = db.execute(
        select(month, func.count(RiskEvent.id))
        .where(*conditions)
        .group_by(month)
        .order_by(month)
    ).all()
    data = [
        ChartDatum(label=str(m), value=float(n)) for m, n in rows if m is not None
    ]
    return ChartSeries(ref="events/monthly", data=data, as_of=data[-1].label if data else None)
