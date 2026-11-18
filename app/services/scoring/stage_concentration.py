"""V1 Material Concentration pillar — per-stage structural concentration.

Spec: docs/design/scoring_v1_spec.md (§3 concentration, §7b recency).
SCORING_VERSION 4.0 replaces the 3.x stage-weighted node rollup with:

    sub_score(stage, geo) = hhi_cliff(HHI_stage) x sqrt(share_geo_stage) x 100
    pillar(geo)           = max over fresh stages of sub_score(stage, geo)

SCORING_VERSION 4.1 (2026-07-20, Nicole) adds the EU-CRMA-aligned
**governance amplifier**: the per-geo pillar value is amplified by that
geography's jurisdiction instability (1 - WGI composite percentile/100),
so the same concentration scores HIGHER in an unstable country than a
stable one ("75% in DRC is worse than 75% in Australia").  See
``apply_governance_amplifier`` for the bounded soft-ceiling form.  As part
of the same change the WGI overlay was REMOVED from the geopolitical
pillar's country_concentration input (market_aggregator) so instability
is not double-counted; geopolitical is now events + tariff/export only.

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

# 4.1 governance amplifier strength (beta).  0.15 chosen from the 2026-07-20
# prototype across the launch list: cobalt (75% mined in CD, WGI ~27) rises
# ~+5-7 concentration points and CD overtakes CN as driving geo, while
# stable-base materials (iron ore / AU) move <1 point.  0 disables.
GOVERNANCE_AMPLIFIER_BETA: float = 0.15

# Mirror of geopolitical_risk._WGI_MIN_DIMENSIONS_FOR_OVERLAY: a country's
# WGI composite is usable only when at least 4 of 6 dimensions are present.
_WGI_MIN_DIMENSIONS: int = 4


def apply_governance_amplifier(
    score01: float,
    instability: Optional[float],
    beta: float = GOVERNANCE_AMPLIFIER_BETA,
) -> float:
    """Amplify a [0,1] concentration score by jurisdiction instability.

    Form (bounded soft-ceiling; ``p`` = score01, ``a`` = beta x instability):

        p' = p + (1 - p) * (1 - exp(-a * p / (1 - p)))

    Properties: p'=p when instability is 0/None (stable or unknown -> no-op);
    for small amplification it reduces to the linear p*(1 + a) of the EU
    HHI x WGI form; as p -> 1 the gain compresses smoothly into the ceiling,
    so near-max materials stay DIFFERENTIATED instead of pinning at 100
    (the flaw of a hard min(100, .) cap).  Monotonic in both p and
    instability; never exceeds 1.
    """
    if instability is None or instability <= 0.0 or beta <= 0.0 or score01 <= 0.0:
        return score01
    p = min(score01, 1.0)
    d = 1.0 - p
    if d <= 1e-9:
        return 1.0
    a = beta * min(max(instability, 0.0), 1.0)
    return p + d * (1.0 - math.exp(-(a * p) / d))


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

    score: float                        # max across fresh stages, 0-100 (4.1: governance-amplified)
    driving_stage: str                  # stage that produced the max
    sub_scores: dict[str, float]        # stage -> sub_score (fresh stages only, RAW)
    raw_score: float = 0.0              # pre-amplifier stage-max (== score when no amplifier)
    governance: Optional[dict] = None   # 4.1 amplifier diagnostics (None = not applied)


@dataclass
class StageConcentrationResult:
    stages: dict[str, StageDetail]              # fresh stages only
    per_geo: dict[str, GeoConcentration]        # producers only
    stale_stages: list[tuple[str, int]]         # (stage, reference_year) excluded
    diagnostics: dict


def compute_stage_concentration(
    rows: list[ShareRow],
    as_of_date: date,
    wgi_by_geo: Optional[dict[str, float]] = None,
) -> StageConcentrationResult:
    """Pure computation: share rows -> per-stage sub-scores -> per-geo max.

    ``rows`` may contain parent/child duplicates and multiple reference
    years per stage; this function performs the year selection, freshness
    gate, and dedupe described in the module docstring.

    ``wgi_by_geo`` (4.1): optional {country_code: WGI composite percentile
    0-100}.  When provided, each geo's stage-max is passed through
    ``apply_governance_amplifier`` with instability = 1 - pct/100.  Geos
    absent from the map (or None) are left unamplified — data-honest no-op.
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

    # 4.1 governance amplifier: amplify each geo's stage-max by its
    # jurisdiction instability.  Monotonic in the score, so amplifying the
    # max is equivalent to amplifying every stage then taking the max;
    # sub_scores stay RAW for display honesty.
    for geo, gc in per_geo.items():
        gc.raw_score = gc.score
        pct = (wgi_by_geo or {}).get(geo)
        if pct is None:
            continue
        inst = 1.0 - (min(max(float(pct), 0.0), 100.0) / 100.0)
        amplified = apply_governance_amplifier(gc.score / 100.0, inst) * 100.0
        gc.governance = {
            "applied": True,
            "beta": GOVERNANCE_AMPLIFIER_BETA,
            "wgi_composite_pct": round(float(pct), 2),
            "instability": round(inst, 4),
            "raw_score": round(gc.raw_score, 2),
            "amplified_score": round(amplified, 2),
        }
        gc.score = amplified

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


def _load_wgi_by_geo(db, geos: set[str]) -> dict[str, float]:
    """Latest usable WGI composite percentile per country (4.1 amplifier).

    Usable = composite_pct present AND >= _WGI_MIN_DIMENSIONS of 6
    dimensions reported (mirrors the old geopolitical overlay's gate).
    Countries with no usable row are simply absent (amplifier no-op).
    """
    if not geos:
        return {}
    from sqlalchemy import select
    from app.models import CountryGovernanceSignal as _CGS

    rows = db.execute(
        select(_CGS.country_code, _CGS.composite_pct, _CGS.n_dimensions_present)
        .where(_CGS.country_code.in_(sorted(geos)))
        .order_by(_CGS.country_code, _CGS.reference_year.desc())
    ).all()
    out: dict[str, float] = {}
    for cc, pct, ndim in rows:
        if cc in out:            # first row per country = latest year
            continue
        if pct is None or (ndim is not None and ndim < _WGI_MIN_DIMENSIONS):
            continue
        out[cc] = float(pct)
    return out


def score_material_concentration(
    db,
    material_id: int,
    as_of_date: date,
    market_scope: str = "global",
) -> StageConcentrationResult:
    """DB-facing entry point: load + compute (incl. 4.1 governance amplifier)."""
    rows = load_share_rows(db, material_id, market_scope=market_scope)
    wgi = _load_wgi_by_geo(db, {r.country_code for r in rows})
    return compute_stage_concentration(rows, as_of_date, wgi_by_geo=wgi)


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
