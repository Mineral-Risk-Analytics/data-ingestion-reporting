"""V1 Material Concentration pillar — per-stage structural concentration.

Spec: docs/design/scoring_v1_spec.md (§3 concentration, §7b recency).
SCORING_VERSION 4.0 replaces the 3.x stage-weighted node rollup with:

    sub_score(stage, geo) = hhi_cliff(HHI_stage) x sqrt(share_geo_stage) x 100
    pillar(geo)           = max over fresh stages of sub_score(stage, geo)

Design rules implemented here:

* **Stage sub-scores live only in this pillar.**  Evidence is the per-stage
  global production-share tables (USGS MCS at ore; benchmark workbooks
  downstream).  Events, facilities, criticality blends: all excluded.
* **Max, not average.**  A chokehold at any one stage is a chokehold; the
  3.x weighted average diluted structural signal with empty stages
  (DRC cobalt 39 < Belarus 50 inversion).
* **Non-producers score 0** — they simply have no per_geo entry.
* **Freshness gate (§7b):** a stage's share snapshot only scores when its
  reference year is within ``FRESHNESS_YEARS`` of the as-of date
  (year-granularity approximation of the 24-month rule).  Stale stages are
  reported in diagnostics and excluded from the max (e.g. cobalt
  battery-grade CN 85% @2022 is display-only at a 2026 as-of).
* **Parent/child dedupe:** share rows are duplicated across 4-digit and
  6-digit mappings of the same stage (2605 and 260500 both carry CD 0.7529).
  Within a stage we keep the single latest fresh reference year, then dedupe
  by country preferring the most specific mapping (highest digit_count).
  Conflicting same-specificity values keep the max share and are flagged.

The compute path is a pure function of plain rows so it unit-tests without
a database; ``load_share_rows`` is the thin SQL loader.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# Stages eligible for the concentration pillar.  ``fabricated`` and
# ``scrap`` are excluded, matching the 3.x rollup's scope.
CONCENTRATION_STAGES: frozenset[str] = frozenset({
    "ore", "concentrate", "intermediate", "refined", "battery_grade",
})

# §7b freshness gate, year-granularity: a stage snapshot is fresh iff
# ``as_of.year - reference_year <= FRESHNESS_YEARS``.  Approximates the
# 24-month rule for annual-cadence sources (USGS MCS, benchmark reports
# publish with ~5-month lag, so the current report is always fresh).
FRESHNESS_YEARS: int = 2


@dataclass(frozen=True)
class ShareRow:
    """One production-share observation, denormalised from the DB."""

    stage: str
    country_code: str
    production_share: float
    reference_year: int
    digit_count: int
    hs_mapping_id: int


@dataclass
class StageDetail:
    """Deduped, scored snapshot of one fresh stage."""

    stage: str
    reference_year: int
    hhi_raw: float
    hhi_cliff: float
    shares: dict[str, float]            # country -> share (deduped)
    source_mapping_ids: list[int]
    conflicts: list[str] = field(default_factory=list)  # dedupe warnings


@dataclass
class GeoConcentration:
    """Per-geography pillar result."""

    score: float                        # max across fresh stages, 0-100
    driving_stage: str                  # stage that produced the max
    sub_scores: dict[str, float]        # stage -> sub_score (fresh stages only)


@dataclass
class StageConcentrationResult:
    stages: dict[str, StageDetail]              # fresh stages only
    per_geo: dict[str, GeoConcentration]        # producers only
    stale_stages: list[tuple[str, int]]         # (stage, reference_year) excluded
    diagnostics: dict


def compute_stage_concentration(
    rows: list[ShareRow],
    as_of_date: date,
) -> StageConcentrationResult:
    """Pure computation: share rows -> per-stage sub-scores -> per-geo max.

    ``rows`` may contain parent/child duplicates and multiple reference
    years per stage; this function performs the year selection, freshness
    gate, and dedupe described in the module docstring.
    """
    from app.services.scoring.material_risk import hhi_concentration_risk

    by_stage: dict[str, list[ShareRow]] = {}
    for r in rows:
        if r.stage in CONCENTRATION_STAGES and r.production_share > 0:
            by_stage.setdefault(r.stage, []).append(r)

    stages: dict[str, StageDetail] = {}
    stale_stages: list[tuple[str, int]] = []

    for stage, stage_rows in by_stage.items():
        latest_year = max(r.reference_year for r in stage_rows)
        if as_of_date.year - latest_year > FRESHNESS_YEARS:
            stale_stages.append((stage, latest_year))
            continue

        # Single-year snapshot per stage: HHI must not mix vintages.
        snapshot = [r for r in stage_rows if r.reference_year == latest_year]

        # Dedupe by country: prefer most specific mapping (highest
        # digit_count); flag genuine value conflicts at equal specificity.
        best: dict[str, ShareRow] = {}
        conflicts: list[str] = []
        for r in snapshot:
            cur = best.get(r.country_code)
            if cur is None or r.digit_count > cur.digit_count:
                best[r.country_code] = r
            elif (
                r.digit_count == cur.digit_count
                and abs(r.production_share - cur.production_share) > 1e-9
            ):
                conflicts.append(
                    f"{stage}/{r.country_code}: mappings {cur.hs_mapping_id} vs "
                    f"{r.hs_mapping_id} disagree "
                    f"({cur.production_share} vs {r.production_share}); kept max"
                )
                if r.production_share > cur.production_share:
                    best[r.country_code] = r

        shares = {c: r.production_share for c, r in best.items()}
        hhi_raw = sum(s * s for s in shares.values())
        stages[stage] = StageDetail(
            stage=stage,
            reference_year=latest_year,
            hhi_raw=hhi_raw,
            hhi_cliff=hhi_concentration_risk(hhi_raw),
            shares=shares,
            source_mapping_ids=sorted({r.hs_mapping_id for r in best.values()}),
            conflicts=conflicts,
        )

    per_geo: dict[str, GeoConcentration] = {}
    for stage, detail in stages.items():
        for geo, share in detail.shares.items():
            sub = detail.hhi_cliff * math.sqrt(share) * 100.0
            existing = per_geo.get(geo)
            if existing is None:
                per_geo[geo] = GeoConcentration(
                    score=sub, driving_stage=stage, sub_scores={stage: sub},
                )
            else:
                existing.sub_scores[stage] = sub
                if sub > existing.score:
                    existing.score = sub
                    existing.driving_stage = stage

    return StageConcentrationResult(
        stages=stages,
        per_geo=per_geo,
        stale_stages=sorted(stale_stages),
        diagnostics={
            "as_of_date": as_of_date.isoformat(),
            "freshness_years": FRESHNESS_YEARS,
            "fresh_stage_count": len(stages),
            "stale_stage_count": len(stale_stages),
            "producer_count": len(per_geo),
            "conflict_count": sum(len(d.conflicts) for d in stages.values()),
        },
    )


def load_share_rows(
    db,
    material_id: int,
    market_scope: str = "global",
) -> list[ShareRow]:
    """Load all share observations for a material as plain ``ShareRow``s.

    Year selection / freshness / dedupe happen in
    ``compute_stage_concentration`` so the policy is testable without SQL.
    """
    from sqlalchemy import select

    from app.models.supply import HsCodeMaterialMapping, HsCodeProductionShare

    result = db.execute(
        select(
            HsCodeMaterialMapping.supply_chain_stage,
            HsCodeProductionShare.country_code,
            HsCodeProductionShare.production_share,
            HsCodeProductionShare.reference_year,
            HsCodeMaterialMapping.digit_count,
            HsCodeProductionShare.hs_mapping_id,
        )
        .join(
            HsCodeMaterialMapping,
            HsCodeMaterialMapping.id == HsCodeProductionShare.hs_mapping_id,
        )
        .where(
            HsCodeMaterialMapping.material_id == material_id,
            HsCodeProductionShare.market_scope == market_scope,
            HsCodeMaterialMapping.supply_chain_stage.is_not(None),
        )
    ).all()

    return [
        ShareRow(
            stage=row[0],
            country_code=row[1],
            production_share=float(row[2]),
            reference_year=int(row[3]),
            digit_count=int(row[4]),
            hs_mapping_id=int(row[5]),
        )
        for row in result
    ]


def score_material_concentration(
    db,
    material_id: int,
    as_of_date: date,
    market_scope: str = "global",
) -> StageConcentrationResult:
    """DB-facing entry point: load + compute in one call."""
    return compute_stage_concentration(
        load_share_rows(db, material_id, market_scope=market_scope),
        as_of_date,
    )


def derive_scoring_geographies(db, material_id: int) -> list[str]:
    """V1 scored-geography universe for one material (spec update 2026-07-18).

    Nicole's call: stop scoring (material x geo) pairs that have neither
    production nor meaningful trade — event-only jurisdictions (sanctions
    hubs, policy actors) carried zero weight in the L2 trade-weighted
    rollup yet cost the full per-pair event-query battery (~85%% of pairs
    for cobalt).  The universe is now:

        1. material_production_shares countries (MCS material level)
        2. stage-share countries (hs_code_production_shares — the
           concentration pillar's own producers)
        3. trade-gate countries: 4-digit-family exporters clearing the
           SAME thresholds as the L0 participation gate (>= 1%% of family
           export total AND >= $5M) — exactly the geos that can carry
           weight in the L2 trade-flow rollup, so restricting to them
           never changes L2 semantics.

    Event-only geographies are deliberately absent: their concentration
    is 0 by definition and their L2 weight is 0 by construction.
    """
    from sqlalchemy import func, select

    from app.services.scoring.hs_node_scorer import (
        _TRADE_GATE_FLOOR_USD,
        _TRADE_GATE_MIN_NODE_SHARE,
    )
    from app.models.supply import (
        HsCodeMaterialMapping,
        HsCodeProductionShare,
        MaterialProductionShare,
        TradeFlow,
    )

    geos: set[str] = set()

    # 1. Material-level production shares
    geos.update(
        g.upper() for g in db.scalars(
            select(MaterialProductionShare.country_code)
            .where(
                MaterialProductionShare.material_id == material_id,
                MaterialProductionShare.production_share > 0,
            )
            .distinct()
        ).all()
    )

    # 2. Stage-share producers (any vintage — freshness is enforced at
    #    scoring time; a stale-only producer still gets a row so the UI
    #    can show the stale context honestly)
    geos.update(
        g.upper() for g in db.scalars(
            select(HsCodeProductionShare.country_code)
            .join(
                HsCodeMaterialMapping,
                HsCodeMaterialMapping.id == HsCodeProductionShare.hs_mapping_id,
            )
            .where(
                HsCodeMaterialMapping.material_id == material_id,
                HsCodeProductionShare.production_share > 0,
            )
            .distinct()
        ).all()
    )

    # 3. Trade-gate exporters over this material's 4-digit families
    fams = [
        (p or "")[:4] for p in db.scalars(
            select(HsCodeMaterialMapping.hs_code_prefix)
            .where(HsCodeMaterialMapping.material_id == material_id)
            .distinct()
        ).all()
        if p
    ]
    fams = sorted({f for f in fams if len(f) == 4})
    if fams:
        fam_expr = func.substr(TradeFlow.hs_code, 1, 4)
        rows = db.execute(
            select(
                fam_expr.label("fam"),
                TradeFlow.reporter_country,
                func.sum(TradeFlow.trade_value_usd),
            )
            .where(
                fam_expr.in_(fams),
                TradeFlow.import_export_flag == "export",
            )
            .group_by(fam_expr, TradeFlow.reporter_country)
        ).all()
        fam_totals: dict[str, float] = {}
        for fam, _cc, v in rows:
            fam_totals[fam] = fam_totals.get(fam, 0.0) + float(v or 0)
        for fam, cc, v in rows:
            v = float(v or 0)
            total = fam_totals.get(fam, 0.0)
            if (
                cc
                and v >= _TRADE_GATE_FLOOR_USD
                and total > 0
                and v / total >= _TRADE_GATE_MIN_NODE_SHARE
            ):
                geos.add(cc.upper())

    return sorted(geos)
