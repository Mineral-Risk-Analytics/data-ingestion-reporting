"""Material-exposure reviewer.

For every ``CompanyMaterialExposure``, find ``RiskEventMaterial`` rows on the
same material whose linked ``RiskEvent.event_date`` postdates the exposure's
``as_of_date`` (falling back to ``updated_at``). Score each hit with:

- **Signal A — material match** (+2): always present when a row is emitted.
  This is the hard gate; we only emit rows where the risk event is linked to
  exactly the material the exposure is about. Recording +2 explicitly keeps
  the score-to-relevance mapping simple and preserves provenance in
  ``match_signals_json``.
- **Signal B — geography overlap** (+1): ``RiskEvent.geography_json.primary``
  matches ``CompanyMaterialExposure.source_geography``. A material risk event
  affecting the exposure's sourcing country is meaningfully more relevant than
  one affecting a different country.
- **Signal C — severity** (+1): ``RiskEvent.severity_score`` >= 0.60. Uses the
  pipeline's existing severity calibration.

The reviewer never dedupes by ``RiskEvent`` — if two exposures reference the
same material, both get their own finding.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.company import Company, CompanyMaterialExposure
from app.models.regulatory import RiskEvent, RiskEventMaterial
from app.models.supply import Material
from app.services.review.scoring import as_utc_datetime, score_to_relevance
from app.services.review.types import Finding


def _score_event(
    exposure: CompanyMaterialExposure, event: RiskEvent
) -> tuple[int, dict[str, Any]]:
    signals: dict[str, Any] = {"material_match": {"points": 2, "hit": True}}

    primary_geo: Optional[str] = None
    if isinstance(event.geography_json, dict):
        val = event.geography_json.get("primary")
        if isinstance(val, str):
            primary_geo = val
    geo_hit = bool(
        exposure.source_geography
        and primary_geo
        and exposure.source_geography.upper() == primary_geo.upper()
    )
    geo_points = 1 if geo_hit else 0
    signals["geography_overlap"] = {
        "exposure_source_geography": exposure.source_geography,
        "event_primary_geography": primary_geo,
        "hit": geo_hit,
        "points": geo_points,
    }

    severity = event.severity_score
    severity_points = 1 if severity is not None and severity >= 0.60 else 0
    signals["severity"] = {
        "severity_score": severity,
        "points": severity_points,
    }

    total = 2 + geo_points + severity_points
    signals["total_score"] = total
    return total, signals


def _evidence_json(
    link: RiskEventMaterial, event: RiskEvent, material: Material
) -> dict[str, Any]:
    event_date = event.event_date.isoformat() if event.event_date else None
    return {
        "risk_event_id": event.id,
        "material_id": material.id,
        "material_name": material.canonical_name,
        "event_type": event.event_type,
        "event_date": event_date,
        "title": event.title,
        "severity_score": event.severity_score,
        "event_primary_geography": (
            event.geography_json.get("primary")
            if isinstance(event.geography_json, dict)
            else None
        ),
        "link_relevance_score": link.relevance_score,
        "link_match_reason": link.match_reason,
        "source_document_id": event.source_document_id,
    }


def review(
    session: Session,
    *,
    since_date: Optional[date] = None,
    company_canonical_names: Optional[Iterable[str]] = None,
) -> list[Finding]:
    """Run the material-exposure reviewer and return a list of findings."""
    stmt = (
        select(CompanyMaterialExposure, Company, Material)
        .join(Company, CompanyMaterialExposure.company_id == Company.id)
        .join(Material, CompanyMaterialExposure.material_id == Material.id)
    )
    if company_canonical_names is not None:
        names = list(company_canonical_names)
        if not names:
            return []
        stmt = stmt.where(Company.canonical_name.in_(names))

    rows = list(session.execute(stmt).all())

    findings: list[Finding] = []
    since_dt = as_utc_datetime(since_date)
    for exposure, company, material in rows:
        cutoffs: list[datetime] = []
        exposure_updated = as_utc_datetime(exposure.updated_at)
        if exposure_updated is not None:
            cutoffs.append(exposure_updated)
        as_of_dt = as_utc_datetime(exposure.as_of_date)
        if as_of_dt is not None:
            cutoffs.append(as_of_dt)
        if since_dt is not None:
            cutoffs.append(since_dt)

        link_stmt = (
            select(RiskEventMaterial, RiskEvent)
            .join(RiskEvent, RiskEventMaterial.risk_event_id == RiskEvent.id)
            .where(RiskEventMaterial.material_id == material.id)
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

        seed_key = f"{company.canonical_name}|{material.canonical_name}"
        for link, event in session.execute(link_stmt).all():
            score, signals = _score_event(exposure, event)
            relevance = score_to_relevance(score)
            if relevance == "none":
                continue
            findings.append(
                Finding(
                    seed_type="material_exposure",
                    seed_key=seed_key,
                    seed_display_name=(
                        f"{company.canonical_name} — {material.canonical_name} "
                        f"({exposure.supply_chain_stage})"
                    ),
                    seed_identifier_json={
                        "exposure_id": exposure.id,
                        "company_id": str(company.id),
                        "material_id": material.id,
                        "supply_chain_stage": exposure.supply_chain_stage,
                    },
                    evidence_type="risk_event_material_link",
                    source_document_id=event.source_document_id,
                    risk_event_id=event.id,
                    evidence_json=_evidence_json(link, event, material),
                    relevance=relevance,
                    match_signals_json=signals,
                )
            )

    return findings


__all__ = ["review"]
