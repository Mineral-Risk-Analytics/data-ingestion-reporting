"""Scheduled scoring jobs registered with Inngest.

Three weekly cron jobs spaced an hour apart to enforce the pipeline dependency
order — each step reads from the table written by the previous step:

  Job 1 — ``rescore-market-scores``      Monday 02:00 UTC
      Geo-level scoring for every active material.
      → material_geography_risk_scores (one row per material × geography)

  Job 2 — ``rescore-global-rollups``     Monday 03:00 UTC
      Trade-weighted global rollup per material.
      → material_global_risk_scores (one row per material)

  Job 3 — ``rescore-all-chemistries``    Monday 04:00 UTC
      Intensity-weighted chemistry scores from global rollups.
      → chemistry_risk_scores (one row per chemistry)

Timeout strategy
----------------
Jobs 1 and 2 previously ran as a single asyncio.to_thread call and exceeded
Inngest's HTTP "first byte timeout" (~2 min).  They are now broken into
per-material Inngest steps (``ctx.step.run``).  Each step completes in a few
seconds; Inngest checkpoints progress after each one, so a timeout or transient
failure restarts from the last successful step rather than from scratch.

Job 3 (chemistry rescore) runs as a single step because it only reads from the
already-computed global rollups and does pure in-Python math — it completes in
well under a minute.

Step handler pattern
--------------------
Each step helper is:
  1. A synchronous ``_sync_*`` function that opens its own DB session and does
     the actual work.  It closes the session in a ``finally`` block.
  2. An ``async def _step_*`` wrapper that delegates to the sync helper via
     ``asyncio.to_thread`` so the Inngest event loop is not blocked.

The async wrapper is what gets passed to ``ctx.step.run``.  Positional
arguments after the handler are forwarded by the SDK via ``*handler_args``.

Registration
------------
Importing this module registers all three functions on ``inngest_client`` via
decorator side-effects.  ``app/tasks/__init__.py`` performs the import;
``app/main.py`` then imports ``app.tasks`` so the functions are discoverable
when ``inngest.fast_api.serve`` is called.
"""

from __future__ import annotations

import asyncio
import datetime as _dt

import inngest
import structlog

from app.core.inngest import inngest_client
from app.db.session import get_session_factory

log = structlog.get_logger(__name__)


def _today_utc() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


# ---------------------------------------------------------------------------
# Job 1 helpers — geo-level market scores
# ---------------------------------------------------------------------------

def _sync_get_active_material_ids() -> list[int]:
    """Return the IDs of every row in the materials table, ordered for determinism.

    Ordering by id is required: Inngest replays the handler function and
    re-executes the loop, matching each ``step.run`` call to its memoized
    result by position.  An unordered query could return a different sequence
    on replay, causing step ID mismatches.
    """
    from sqlalchemy import select
    from app.models.supply import Material

    session = get_session_factory()()
    try:
        return list(session.scalars(select(Material.id).order_by(Material.id)).all())
    finally:
        session.close()


async def _step_get_active_material_ids() -> list[int]:
    return await asyncio.to_thread(_sync_get_active_material_ids)


def _sync_score_material_geos(
    material_id: int,
    as_of_date_iso: str,
    run_id: str,
) -> dict:
    """Score all geographies for a single material.

    Mirrors the inner loop of ``score_all_active_materials`` but operates on a
    single material so it completes in a few seconds rather than 10+ minutes.
    Each geo pair is committed individually; a failure on one pair rolls back
    only that pair.

    Returns a JSON-serialisable dict with ``pairs_scored``.
    """
    from sqlalchemy import select
    from app.models.regulatory import RiskEventGeography, RiskEventMaterial
    from app.models.supply import Material
    from app.services.scoring.market_aggregator import score_material_geography

    as_of_date = _dt.date.fromisoformat(as_of_date_iso)
    session = get_session_factory()()
    try:
        material = session.get(Material, material_id)
        if not material:
            log.warning("scoring_jobs.step.geo.no_material", material_id=material_id)
            return {"material_id": material_id, "pairs_scored": 0, "skipped": True}

        # Derive geographies: seed producing countries + any geo with events
        geos: list[str] = []
        if material.primary_producing_countries:
            geos = [g.upper() for g in material.primary_producing_countries]

        event_geo_stmt = (
            select(RiskEventGeography.country_code)
            .join(
                RiskEventMaterial,
                RiskEventMaterial.risk_event_id == RiskEventGeography.risk_event_id,
            )
            .where(RiskEventMaterial.material_id == material.id)
            .distinct()
        )
        event_geos = [row[0] for row in session.execute(event_geo_stmt).all()]
        geos = list({*geos, *event_geos})

        if not geos:
            log.debug(
                "scoring_jobs.step.geo.no_geos",
                material_id=material_id,
                material_name=material.canonical_name,
            )
            return {"material_id": material_id, "pairs_scored": 0, "no_geos": True}

        pairs_scored = 0
        for geo in geos:
            try:
                score_material_geography(
                    session,
                    material.id,
                    geo,
                    as_of_date,
                    run_id=f"{run_id}-{material.id}-{geo}",
                    persist=True,
                )
                session.commit()
                pairs_scored += 1
            except Exception:
                session.rollback()
                log.exception(
                    "scoring_jobs.step.geo.pair_error",
                    material_id=material_id,
                    geography=geo,
                )

        return {"material_id": material_id, "pairs_scored": pairs_scored}
    finally:
        session.close()


async def _step_score_material_geos(
    material_id: int,
    as_of_date_iso: str,
    run_id: str,
) -> dict:
    return await asyncio.to_thread(
        _sync_score_material_geos, material_id, as_of_date_iso, run_id
    )


# ---------------------------------------------------------------------------
# Job 2 helpers — global material rollup
# ---------------------------------------------------------------------------

def _sync_get_materials_with_geo_scores(as_of_date_iso: str) -> list[int]:
    """Return material IDs that have at least one geo score on or before as_of_date.

    Ordered by material_id for replay determinism (same reason as
    ``_sync_get_active_material_ids``).
    """
    from sqlalchemy import select
    from app.models.scoring import MaterialGeographyRiskScore

    as_of_date = _dt.date.fromisoformat(as_of_date_iso)
    session = get_session_factory()()
    try:
        rows = session.execute(
            select(MaterialGeographyRiskScore.material_id)
            .where(MaterialGeographyRiskScore.as_of_date <= as_of_date)
            .distinct()
            .order_by(MaterialGeographyRiskScore.material_id)
        ).all()
        return [row[0] for row in rows]
    finally:
        session.close()


async def _step_get_materials_with_geo_scores(as_of_date_iso: str) -> list[int]:
    return await asyncio.to_thread(_sync_get_materials_with_geo_scores, as_of_date_iso)


def _sync_rollup_material(
    material_id: int,
    as_of_date_iso: str,
    run_id: str,
) -> dict:
    """Compute and persist the global rollup for a single material."""
    from app.services.scoring.global_rollup import score_material_global_rollup

    as_of_date = _dt.date.fromisoformat(as_of_date_iso)
    session = get_session_factory()()
    try:
        score_row = score_material_global_rollup(
            session,
            material_id,
            as_of_date,
            run_id=f"{run_id}-{material_id}",
            persist=True,
        )
        session.commit()
        return {
            "material_id": material_id,
            "overall_risk_score": score_row.overall_risk_score,
        }
    except ValueError:
        # No geo scores for this material yet — not an error, just skip
        session.rollback()
        log.info(
            "scoring_jobs.step.rollup.no_geo_scores",
            material_id=material_id,
        )
        return {"material_id": material_id, "skipped": True}
    except Exception:
        session.rollback()
        log.exception("scoring_jobs.step.rollup.error", material_id=material_id)
        return {"material_id": material_id, "error": True}
    finally:
        session.close()


async def _step_rollup_material(
    material_id: int,
    as_of_date_iso: str,
    run_id: str,
) -> dict:
    return await asyncio.to_thread(
        _sync_rollup_material, material_id, as_of_date_iso, run_id
    )


# ---------------------------------------------------------------------------
# Job 3 helper — chemistry rescore
# ---------------------------------------------------------------------------

def _sync_rescore_all_chemistries(as_of_date_iso: str) -> dict:
    """Rescore all chemistries using the 5-pillar rollup (methodology_version 2.0)."""
    from app.services.scoring.chemistry_risk import score_all_chemistries_from_rollup

    as_of_date = _dt.date.fromisoformat(as_of_date_iso)
    session = get_session_factory()()
    try:
        results = score_all_chemistries_from_rollup(session, as_of_date)
        return {
            "rescored": len(results),
            "scores": [
                {
                    "slug": r["slug"],
                    "composite_risk_score": r["composite_risk_score"],
                    "score_confidence": r["score_confidence"],
                    "materials_scored": r["materials_scored"],
                    "materials_missing": r["materials_missing"],
                }
                for r in results
            ],
        }
    finally:
        session.close()


async def _step_rescore_all_chemistries(as_of_date_iso: str) -> dict:
    return await asyncio.to_thread(_sync_rescore_all_chemistries, as_of_date_iso)


# ---------------------------------------------------------------------------
# Job 1 — Geo-level market scores
# ---------------------------------------------------------------------------

@inngest_client.create_function(
    fn_id="rescore-market-scores",
    trigger=inngest.TriggerCron(cron="0 2 * * MON"),
)
async def rescore_market_scores_job(ctx: inngest.Context) -> dict:
    """Weekly geo-level market rescore — runs every Monday at 02:00 UTC.

    Broken into per-material Inngest steps.  Inngest checkpoints after each
    material so a timeout or failure mid-batch does not restart from scratch.
    Must complete before the global rollup job reads from it at 03:00 UTC.
    """
    today_iso = _today_utc().isoformat()
    run_id = f"cron-{today_iso}"
    ctx.logger.info("scoring_jobs.market_geo.start", today=today_iso)

    # Step 1: discover materials
    material_ids: list[int] = await ctx.step.run(
        "get-active-materials",
        _step_get_active_material_ids,
    )

    # One step per material — each typically finishes in a few seconds
    total_pairs = 0
    for mat_id in material_ids:
        result: dict = await ctx.step.run(
            f"score-material-{mat_id}",
            _step_score_material_geos,
            mat_id,
            today_iso,
            run_id,
        )
        total_pairs += result.get("pairs_scored", 0)

    ctx.logger.info(
        "scoring_jobs.market_geo.done",
        total_pairs=total_pairs,
        run_id=run_id,
    )
    return {
        "step": "geo_scores",
        "as_of_date": today_iso,
        "run_id": run_id,
        "scored_pairs": total_pairs,
    }


# ---------------------------------------------------------------------------
# Job 2 — Global material rollup
# ---------------------------------------------------------------------------

@inngest_client.create_function(
    fn_id="rescore-global-rollups",
    trigger=inngest.TriggerCron(cron="0 3 * * MON"),
)
async def rescore_global_rollups_job(ctx: inngest.Context) -> dict:
    """Weekly global rollup — runs every Monday at 03:00 UTC.

    Aggregates material_geography_risk_scores → material_global_risk_scores.
    Per-material steps allow Inngest to checkpoint progress.
    Must complete before the chemistry rescore reads from it at 04:00 UTC.
    """
    today_iso = _today_utc().isoformat()
    run_id = f"global-cron-{today_iso}"
    ctx.logger.info("scoring_jobs.global_rollup.start", today=today_iso)

    # Step 1: find materials with geo scores to roll up
    material_ids: list[int] = await ctx.step.run(
        "get-scored-materials",
        _step_get_materials_with_geo_scores,
        today_iso,
    )

    # One step per material
    scored = 0
    for mat_id in material_ids:
        result: dict = await ctx.step.run(
            f"rollup-material-{mat_id}",
            _step_rollup_material,
            mat_id,
            today_iso,
            run_id,
        )
        if not result.get("error") and not result.get("skipped"):
            scored += 1

    ctx.logger.info(
        "scoring_jobs.global_rollup.done",
        scored_materials=scored,
        run_id=run_id,
    )
    return {
        "step": "global_rollup",
        "as_of_date": today_iso,
        "run_id": run_id,
        "scored_materials": scored,
    }


# ---------------------------------------------------------------------------
# Job 3 — Chemistry scores from rollup
# ---------------------------------------------------------------------------

@inngest_client.create_function(
    fn_id="rescore-all-chemistries",
    trigger=inngest.TriggerCron(cron="0 4 * * MON"),
)
async def rescore_chemistries_job(ctx: inngest.Context) -> dict:
    """Weekly chemistry rescore — runs every Monday at 04:00 UTC.

    Reads material_global_risk_scores and intensity-weights constituent
    minerals → chemistry_risk_scores.  Uses methodology_version=2.0
    (score_all_chemistries_from_rollup).  Runs as a single step because
    the work is fast (pure in-Python math over pre-computed rollups).
    """
    today_iso = _today_utc().isoformat()
    ctx.logger.info("scoring_jobs.chemistry.start", today=today_iso)

    result: dict = await ctx.step.run(
        "rescore-all-chemistries",
        _step_rescore_all_chemistries,
        today_iso,
    )

    ctx.logger.info(
        "scoring_jobs.chemistry.done",
        rescored=result.get("rescored"),
        today=today_iso,
    )
    return {
        "step": "chemistry_scores",
        "as_of_date": today_iso,
        **result,
    }


SCHEDULED_FUNCTIONS = [
    rescore_market_scores_job,
    rescore_global_rollups_job,
    rescore_chemistries_job,
]

__all__ = [
    "SCHEDULED_FUNCTIONS",
    "rescore_market_scores_job",
    "rescore_global_rollups_job",
    "rescore_chemistries_job",
]
