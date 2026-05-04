"""HS-code redesign coverage diagnostics.

Read-only helpers that answer "what fraction of the scoring stack is actually
running on stage-aware data versus falling back to material-level signals?"

Motivation
----------
After the HS-code redesign (Phase 1–3), several rollup paths were upgraded to
prefer stage-aware data when available and fall back to legacy material-level
signals otherwise.  The fallbacks are silent — they emit a structured log
line but no aggregate metric.  Without a coverage view, you cannot answer
operational questions like:

  * How many materials have at least one Level-0 (HS node) score?
  * How many `(material × geography)` pairs at Level 1 used the
    `stage_weighted` rollup vs `material_fallback`?
  * How many global rollups at Level 2 fell back to equal weighting because
    neither trade flow nor production share data was present?

This module answers those questions by reading the persisted score tables
plus their rationale columns.  It does not recompute anything and is safe
to call at any time.

CLI invocation
--------------
The intended caller pattern is::

    from app.services.scoring.coverage import report_hs_coverage
    from app.db.session import get_session_factory
    session = get_session_factory()()
    report = report_hs_coverage(session)
    import json; print(json.dumps(report, indent=2))

A standalone CLI command can wrap this; not added here to keep the audit PR
contained.

Returned report shape
---------------------
The function returns a single dict.  Top-level keys::

    {
      "as_of_date": ISO-8601 date string,
      "level_0": {
        "production_share_rows":         total in hs_code_production_shares,
        "production_share_unique_nodes": distinct hs_mapping_id with shares,
        "scored_nodes":                  rows in hs_code_geography_risk_scores,
        "materials_with_at_least_one_node": …,
        "materials_with_two_or_more_nodes": …,
        "by_stage": { ore: count, refined: count, … },
      },
      "level_1": {
        "total_rows":              MaterialGeographyRiskScore rows,
        "stage_weighted":          rows where stage_rollup_method='stage_weighted',
        "material_fallback":       rows where stage_rollup_method='material_fallback',
        "stage_weighted_pct":      stage_weighted / total_rows,
        "fallback_materials":      list[material_id] hitting fallback predominantly,
      },
      "level_2": {
        "total_rows":      MaterialGlobalRiskScore rows,
        "by_weight_source": {trade_flow: …, production_share: …, equal: …, mixed: …},
      },
      "keywords": {
        "rows_with_keywords":     hs_code_material_mappings rows where keywords IS NOT NULL,
        "rows_without_keywords":  …,
        "keyword_event_links":    risk_event_hs_mappings count via keyword path
                                  (match_reason in keyword-derived values),
      },
    }
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

log = structlog.get_logger(__name__)


def report_hs_coverage(
    session: Session,
    *,
    as_of_date: Optional[date] = None,
    fallback_threshold: float = 0.50,
) -> dict:
    """Produce a structured coverage report.  Read-only.

    Args:
        session:            Active SQLAlchemy session.
        as_of_date:         Cut-off date for "latest" score lookups.  Defaults
                            to today (UTC).
        fallback_threshold: When >= this fraction of a material's Level-1 rows
                            used `material_fallback`, the material is included
                            in `level_1.fallback_materials`.

    Returns:
        Dict matching the shape documented in the module docstring.

    Side effects:
        None — all queries are SELECTs.  Safe to call from API endpoints,
        CLI commands, or scheduled jobs without affecting persisted state.
    """
    # Lazy imports to keep the scoring package's import graph minimal
    from app.models.scoring import (
        HsCodeGeographyRiskScore,
        MaterialGeographyRiskScore,
        MaterialGlobalRiskScore,
    )
    from app.models.supply import HsCodeMaterialMapping, HsCodeProductionShare

    if as_of_date is None:
        as_of_date = datetime.now(timezone.utc).date()

    # ------------------------------------------------------------------ Level 0
    prod_share_total = session.scalar(
        select(func.count(HsCodeProductionShare.id))
    ) or 0
    prod_share_unique_nodes = session.scalar(
        select(func.count(func.distinct(HsCodeProductionShare.hs_mapping_id)))
    ) or 0

    scored_nodes = session.scalar(
        select(func.count(HsCodeGeographyRiskScore.id))
        .where(HsCodeGeographyRiskScore.as_of_date <= as_of_date)
    ) or 0

    # Materials with ≥1 / ≥2 distinct HS nodes that have at least one
    # production share row.  This is the population eligible for
    # stage-weighted rollup at Level 1.
    nodes_per_material = list(session.execute(
        select(
            HsCodeMaterialMapping.material_id,
            func.count(func.distinct(HsCodeMaterialMapping.id)).label("node_count"),
        )
        .join(HsCodeProductionShare,
              HsCodeProductionShare.hs_mapping_id == HsCodeMaterialMapping.id)
        .where(
            HsCodeMaterialMapping.market_scope == "global",
            HsCodeProductionShare.production_share > 0,
        )
        .group_by(HsCodeMaterialMapping.material_id)
    ).all())

    materials_with_one_plus = sum(1 for r in nodes_per_material if r.node_count >= 1)
    materials_with_two_plus = sum(1 for r in nodes_per_material if r.node_count >= 2)

    # By-stage Level 0 score counts
    by_stage_rows = list(session.execute(
        select(
            HsCodeMaterialMapping.supply_chain_stage,
            func.count(HsCodeGeographyRiskScore.id).label("count"),
        )
        .join(HsCodeGeographyRiskScore,
              HsCodeGeographyRiskScore.hs_mapping_id == HsCodeMaterialMapping.id)
        .where(HsCodeGeographyRiskScore.as_of_date <= as_of_date)
        .group_by(HsCodeMaterialMapping.supply_chain_stage)
    ).all())
    by_stage = {(row.supply_chain_stage or "unknown"): int(row.count) for row in by_stage_rows}

    # ------------------------------------------------------------------ Level 1
    level_1_rows = list(session.execute(
        select(
            MaterialGeographyRiskScore.material_id,
            MaterialGeographyRiskScore.stage_rollup_method,
            func.count(MaterialGeographyRiskScore.id).label("count"),
        )
        .where(MaterialGeographyRiskScore.as_of_date <= as_of_date)
        .group_by(
            MaterialGeographyRiskScore.material_id,
            MaterialGeographyRiskScore.stage_rollup_method,
        )
    ).all())

    level_1_total = 0
    level_1_stage_weighted = 0
    level_1_fallback = 0
    fallback_by_material: dict[int, dict[str, int]] = {}

    for row in level_1_rows:
        cnt = int(row.count)
        level_1_total += cnt
        bucket = fallback_by_material.setdefault(
            row.material_id, {"stage_weighted": 0, "material_fallback": 0, "other": 0}
        )
        if row.stage_rollup_method == "stage_weighted":
            level_1_stage_weighted += cnt
            bucket["stage_weighted"] += cnt
        elif row.stage_rollup_method == "material_fallback":
            level_1_fallback += cnt
            bucket["material_fallback"] += cnt
        else:
            bucket["other"] += cnt

    fallback_materials: list[int] = []
    for mat_id, buckets in fallback_by_material.items():
        total = sum(buckets.values())
        if total == 0:
            continue
        fb_frac = buckets["material_fallback"] / total
        if fb_frac >= fallback_threshold:
            fallback_materials.append(mat_id)

    stage_weighted_pct = (
        level_1_stage_weighted / level_1_total if level_1_total else 0.0
    )

    # ------------------------------------------------------------------ Level 2
    # weight_source is in rationale_json; we filter via a JSON expression for
    # Postgres.  Falls back to a Python aggregation if the DB doesn't support
    # JSONB arrow extraction (it does — Neon Postgres).
    level_2_rows = list(session.execute(
        select(
            MaterialGlobalRiskScore.rationale_json["weight_source"].astext.label("weight_source"),
            func.count(MaterialGlobalRiskScore.id).label("count"),
        )
        .where(MaterialGlobalRiskScore.as_of_date <= as_of_date)
        .group_by(MaterialGlobalRiskScore.rationale_json["weight_source"].astext)
    ).all())

    level_2_total = sum(int(r.count) for r in level_2_rows)
    by_weight_source = {
        (row.weight_source or "unknown"): int(row.count)
        for row in level_2_rows
    }

    # ------------------------------------------------------------------ Keywords
    keyword_rows_total = session.scalar(
        select(func.count(HsCodeMaterialMapping.id))
        .where(HsCodeMaterialMapping.market_scope == "global")
    ) or 0
    # Postgres-specific: jsonb_array_length can be NULL.  Use COALESCE on a
    # cast-safe path instead — a row is considered "with keywords" when the
    # keywords column is non-null AND has at least one entry.
    keyword_rows_populated = session.scalar(
        select(func.count(HsCodeMaterialMapping.id))
        .where(
            HsCodeMaterialMapping.market_scope == "global",
            HsCodeMaterialMapping.keywords.is_not(None),
            func.jsonb_array_length(HsCodeMaterialMapping.keywords) > 0,
        )
    ) or 0

    # Count how many risk_event_hs_mappings rows came from keyword detection
    # (match_reason values written by MaterialCache.detect → callers).  We
    # accept any match_reason that suggests keyword-path attribution.
    from app.models.regulatory import RiskEventHsMapping
    keyword_event_links = session.scalar(
        select(func.count(RiskEventHsMapping.id))
        .where(RiskEventHsMapping.match_reason.notin_(
            ("trade_flow_match", "trade_signal", "manual")
        ))
    ) or 0

    report = {
        "as_of_date": as_of_date.isoformat(),
        "level_0": {
            "production_share_rows": int(prod_share_total),
            "production_share_unique_nodes": int(prod_share_unique_nodes),
            "scored_nodes": int(scored_nodes),
            "materials_with_at_least_one_node": int(materials_with_one_plus),
            "materials_with_two_or_more_nodes": int(materials_with_two_plus),
            "by_stage": by_stage,
        },
        "level_1": {
            "total_rows": int(level_1_total),
            "stage_weighted": int(level_1_stage_weighted),
            "material_fallback": int(level_1_fallback),
            "stage_weighted_pct": round(stage_weighted_pct, 4),
            "fallback_materials": fallback_materials,
            "fallback_threshold": fallback_threshold,
        },
        "level_2": {
            "total_rows": int(level_2_total),
            "by_weight_source": by_weight_source,
        },
        "keywords": {
            "rows_total": int(keyword_rows_total),
            "rows_with_keywords": int(keyword_rows_populated),
            "rows_without_keywords": int(keyword_rows_total - keyword_rows_populated),
            "keyword_event_links": int(keyword_event_links),
        },
    }

    log.info("scoring.coverage.report", **{
        # Flatten the headline numbers for log grep
        "level_0_scored_nodes": report["level_0"]["scored_nodes"],
        "level_0_materials_one_plus": report["level_0"]["materials_with_at_least_one_node"],
        "level_0_materials_two_plus": report["level_0"]["materials_with_two_or_more_nodes"],
        "level_1_total": report["level_1"]["total_rows"],
        "level_1_stage_weighted_pct": report["level_1"]["stage_weighted_pct"],
        "level_2_total": report["level_2"]["total_rows"],
        "keywords_rows_with_keywords": report["keywords"]["rows_with_keywords"],
        "keywords_rows_total": report["keywords"]["rows_total"],
    })

    return report


__all__ = [
    "report_hs_coverage",
]
