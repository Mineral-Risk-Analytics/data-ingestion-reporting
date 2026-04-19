"""
Convert trade_flows data into GEOPOLITICAL_TRADE RiskEvent rows.

The UN Comtrade ingestion writes raw export volumes into trade_flows, but the
scoring engine reads risk_events (joined through risk_event_companies). This
module bridges that gap by deriving two signal types:

  TRADE_CONCENTRATION
    Raised when a single country accounts for ≥60% of total tracked reporter
    exports for a given material in a given year. Feeds trade_volatility in
    the Material Concentration pillar and country_concentration in the
    Geopolitical pillar.

  EXPORT_DROP
    Raised when a reporter country's exports of a material fall ≥20% YoY.
    Uses event_subtype = "EXPORT_RESTRICTION" so the Geopolitical pillar's
    export_restriction_exposure sub-input is activated (evidence_aggregator.py
    checks for that subtype). Only computed when we have data for both years.

Company linkage (risk_event_companies):
  Companies are linked via company_material_exposures: any company with
  source_geography matching the signal country and material_id matching
  the signal material is linked with relevance_score = 0.85.

Idempotency:
  content_hash (SHA-256 of title + event_date ISO string) prevents duplicate
  events on re-run. Re-running after new Comtrade years are ingested is safe.

Severity calibration:
  TRADE_CONCENTRATION
    60–69%  → 0.55   (elevated — diversification advisable)
    70–79%  → 0.70   (high — single supplier dominance)
    80–89%  → 0.82   (very high — critical vulnerability)
    ≥90%    → 0.90   (extreme — monopoly supply)

  EXPORT_DROP
    20–29%  → 0.50   (moderate disruption signal)
    30–39%  → 0.65   (significant — possible restriction)
    40–49%  → 0.78   (severe)
    ≥50%    → 0.88   (critical)
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.constants import RiskCategory
from app.models.company import CompanyMaterialExposure
from app.models.regulatory import (
    RiskEvent,
    RiskEventCompany,
    RiskEventMaterial,
)
from app.models.supply import TradeFlow

log = structlog.get_logger(__name__)

# Minimum reporter share to raise a TRADE_CONCENTRATION event.
_CONCENTRATION_THRESHOLD = 0.60

# Minimum YoY export decline to raise an EXPORT_DROP event.
_DROP_THRESHOLD = 0.20

# Minimum absolute trade value (USD) to avoid noise from tiny flows.
_MIN_TRADE_VALUE_USD = 1_000_000  # $1M

# Relevance score for companies linked via material + geography match.
_COMPANY_RELEVANCE = 0.85


# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------

def _concentration_severity(share: float) -> float:
    if share >= 0.90:
        return 0.90
    if share >= 0.80:
        return 0.82
    if share >= 0.70:
        return 0.70
    return 0.55  # 0.60–0.69


def _drop_severity(drop_fraction: float) -> float:
    """drop_fraction is the magnitude of decline, e.g. 0.35 for a 35% drop."""
    if drop_fraction >= 0.50:
        return 0.88
    if drop_fraction >= 0.40:
        return 0.78
    if drop_fraction >= 0.30:
        return 0.65
    return 0.50  # 0.20–0.29


def _content_hash(title: str, event_date: datetime) -> str:
    payload = f"{title}|{event_date.date().isoformat()}"
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _event_exists(db: Session, ch: str) -> bool:
    return db.scalar(
        select(RiskEvent.id).where(RiskEvent.content_hash == ch).limit(1)
    ) is not None


def _insert_event(
    db: Session,
    title: str,
    summary: str,
    event_date: datetime,
    severity: float,
    event_subtype: str,
    reporter_country: str,
    material_id: int,
    material_name: str,
    year: int,
) -> Optional[int]:
    """Insert a RiskEvent row. Returns new event id, or None if already exists."""
    ch = _content_hash(title, event_date)
    if _event_exists(db, ch):
        return None

    event = RiskEvent(
        event_type="GEOPOLITICAL_TRADE",
        event_date=event_date,
        title=title,
        summary=summary,
        severity_score=round(severity, 2),
        confidence_score=0.75,  # data-derived, moderate confidence
        risk_categories_json=[RiskCategory.GEOPOLITICAL_TRADE.value],
        geography_json={"primary": reporter_country},
        content_hash=ch,
        metadata_json={
            "event_subtype": event_subtype,
            "reporter_country": reporter_country,
            "material_id": material_id,
            "material_name": material_name,
            "reference_year": year,
            "source": "trade_flow_analysis",
        },
    )
    db.add(event)
    db.flush()
    return event.id


def _link_material(db: Session, event_id: int, material_id: int) -> None:
    db.add(RiskEventMaterial(
        risk_event_id=event_id,
        material_id=material_id,
        relevance_score=1.0,
        match_reason="hs_code",
    ))


def _link_companies(
    db: Session,
    event_id: int,
    material_id: int,
    reporter_country: str,
) -> int:
    """Link all companies sourcing material_id from reporter_country. Returns count."""
    rows = db.scalars(
        select(CompanyMaterialExposure).where(
            CompanyMaterialExposure.material_id == material_id,
            CompanyMaterialExposure.source_geography == reporter_country,
        )
    ).all()

    linked = 0
    seen_company_ids: set = set()
    for exposure in rows:
        cid = exposure.company_id
        if cid in seen_company_ids:
            continue
        seen_company_ids.add(cid)
        db.add(RiskEventCompany(
            id=uuid.uuid4(),
            risk_event_id=event_id,
            company_id=cid,
            relevance_score=_COMPANY_RELEVANCE,
            match_reason="material_hs",
        ))
        linked += 1

    return linked


# ---------------------------------------------------------------------------
# Signal derivation
# ---------------------------------------------------------------------------

def _get_annual_totals(
    db: Session,
) -> dict[tuple[int, str, int], float]:
    """
    Query trade_flows for WLD partner (total reporter exports) and sum by
    (material_id, reporter_country, year).

    Returns {(material_id, reporter_country, year): total_trade_value_usd}.
    Only includes rows with partner_country='WLD' and material_id IS NOT NULL.
    """
    rows = db.execute(
        select(
            TradeFlow.material_id,
            TradeFlow.reporter_country,
            TradeFlow.period,
            func.sum(TradeFlow.trade_value_usd).label("total_usd"),
        )
        .where(
            TradeFlow.partner_country == "WLD",
            TradeFlow.material_id.is_not(None),
            TradeFlow.trade_value_usd.is_not(None),
        )
        .group_by(TradeFlow.material_id, TradeFlow.reporter_country, TradeFlow.period)
    ).all()

    result: dict[tuple[int, str, int], float] = {}
    for material_id, country, period_str, total in rows:
        try:
            year = int(period_str)
        except (TypeError, ValueError):
            continue
        if material_id is not None and total and total > 0:
            result[(material_id, country, year)] = float(total)

    return result


def _get_material_names(db: Session) -> dict[int, str]:
    from app.models.supply import Material
    rows = db.execute(select(Material.id, Material.canonical_name)).all()
    return {r.id: r.canonical_name for r in rows}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_trade_signals(
    db: Session,
    years: Optional[list[int]] = None,
) -> dict[str, int]:
    """
    Derive GEOPOLITICAL_TRADE risk events from trade_flows and persist them.

    Args:
        db:    SQLAlchemy session. Commits internally after all events are created.
        years: Restrict analysis to these years. Default: all years in trade_flows.

    Returns:
        {
            "concentration_events": int,  # TRADE_CONCENTRATION events created
            "drop_events": int,           # EXPORT_DROP events created
            "company_links": int,         # risk_event_companies rows created
            "skipped_existing": int,      # events skipped (content_hash exists)
        }
    """
    log.info("trade_signal_builder.start")

    annual_totals = _get_annual_totals(db)
    if not annual_totals:
        log.warning("trade_signal_builder.no_trade_flows")
        return {"concentration_events": 0, "drop_events": 0,
                "company_links": 0, "skipped_existing": 0}

    material_names = _get_material_names(db)

    # Determine available years (optionally filtered)
    all_years = sorted({year for (_, _, year) in annual_totals.keys()})
    if years:
        all_years = [y for y in all_years if y in years]

    # Group by (material_id, year) → {country: total_usd}
    by_mat_year: dict[tuple[int, int], dict[str, float]] = {}
    for (mat_id, country, year), total in annual_totals.items():
        if year not in all_years:
            continue
        key = (mat_id, year)
        by_mat_year.setdefault(key, {})[country] = total

    concentration_events = 0
    drop_events = 0
    company_links = 0
    skipped_existing = 0

    for (mat_id, year), country_totals in sorted(by_mat_year.items()):
        mat_name = material_names.get(mat_id, f"material_{mat_id}")
        world_total = sum(country_totals.values())

        if world_total < _MIN_TRADE_VALUE_USD:
            continue

        # ── TRADE_CONCENTRATION signals ──────────────────────────────────
        for country, country_total in country_totals.items():
            share = country_total / world_total
            if share < _CONCENTRATION_THRESHOLD:
                continue

            severity = _concentration_severity(share)
            title = (
                f"{country} accounts for {share:.0%} of tracked "
                f"{mat_name} exports ({year})"
            )
            summary = (
                f"Trade flow analysis shows {country} controlling {share:.1%} "
                f"of total tracked reporter exports for {mat_name} in {year} "
                f"(USD {country_total:,.0f} of USD {world_total:,.0f} total). "
                f"High geographic concentration creates single-country supply dependency."
            )
            event_date = datetime(year, 12, 31, tzinfo=timezone.utc)

            event_id = _insert_event(
                db,
                title=title,
                summary=summary,
                event_date=event_date,
                severity=severity,
                event_subtype="TRADE_CONCENTRATION",
                reporter_country=country,
                material_id=mat_id,
                material_name=mat_name,
                year=year,
            )
            if event_id is None:
                skipped_existing += 1
                continue

            _link_material(db, event_id, mat_id)
            linked = _link_companies(db, event_id, mat_id, country)
            company_links += linked
            concentration_events += 1

            log.info(
                "trade_signal_builder.concentration_event",
                country=country,
                material=mat_name,
                year=year,
                share=round(share, 3),
                severity=severity,
                companies_linked=linked,
            )

        # ── EXPORT_DROP signals ───────────────────────────────────────────
        prev_year = year - 1
        prev_totals = by_mat_year.get((mat_id, prev_year), {})
        if not prev_totals:
            continue

        for country, current_total in country_totals.items():
            prev_total = prev_totals.get(country)
            if prev_total is None or prev_total < _MIN_TRADE_VALUE_USD:
                continue

            drop_fraction = (prev_total - current_total) / prev_total
            if drop_fraction < _DROP_THRESHOLD:
                continue

            severity = _drop_severity(drop_fraction)
            title = (
                f"{country} {mat_name} exports fell {drop_fraction:.0%} "
                f"YoY ({prev_year}→{year})"
            )
            summary = (
                f"{country} recorded a {drop_fraction:.1%} year-on-year decline in "
                f"{mat_name} exports ({year} vs {prev_year}): "
                f"USD {current_total:,.0f} vs USD {prev_total:,.0f}. "
                f"Significant export drops may signal emerging restrictions, "
                f"production disruptions, or policy shifts."
            )
            event_date = datetime(year, 12, 31, tzinfo=timezone.utc)

            event_id = _insert_event(
                db,
                title=title,
                summary=summary,
                event_date=event_date,
                severity=severity,
                event_subtype="EXPORT_RESTRICTION",  # activates geo pillar sub-input
                reporter_country=country,
                material_id=mat_id,
                material_name=mat_name,
                year=year,
            )
            if event_id is None:
                skipped_existing += 1
                continue

            _link_material(db, event_id, mat_id)
            linked = _link_companies(db, event_id, mat_id, country)
            company_links += linked
            drop_events += 1

            log.info(
                "trade_signal_builder.drop_event",
                country=country,
                material=mat_name,
                year=year,
                drop_fraction=round(drop_fraction, 3),
                severity=severity,
                companies_linked=linked,
            )

    db.commit()

    log.info(
        "trade_signal_builder.done",
        concentration_events=concentration_events,
        drop_events=drop_events,
        company_links=company_links,
        skipped_existing=skipped_existing,
    )
    return {
        "concentration_events": concentration_events,
        "drop_events": drop_events,
        "company_links": company_links,
        "skipped_existing": skipped_existing,
    }
