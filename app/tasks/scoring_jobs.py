"""Scheduled scoring jobs registered with Inngest.

Three weekly cron jobs spaced an hour apart to enforce the pipeline dependency
order — each step reads from the table written by the previous step:

  Step 1 — ``rescore-market-scores``        Monday 02:00 UTC
      score_all_active_materials()
      → material_geography_risk_scores (one row per material × geography)

  Step 2 — ``rescore-global-rollups``        Monday 03:00 UTC
      score_all_material_global_rollups()
      → material_global_risk_scores (one row per material, trade-weighted)

  Step 3 — ``rescore-all-chemistries``       Monday 04:00 UTC
      score_all_chemistries_from_rollup()
      → chemistry_risk_scores (one row per chemistry, intensity-weighted)

All three jobs:
  - Open a fresh SQLAlchemy session via ``get_session_factory`` (do NOT share
    a session across job invocations — Inngest workers are concurrent).
  - Run the synchronous scoring function inside ``asyncio.to_thread`` so the
    Inngest async handler does not block its event loop.
  - Always close the session in a ``finally`` block, even on failure.

Each underlying scoring helper commits its own writes — do NOT wrap them in
additional transactions or call ``session.commit()`` afterwards.

How registration works
----------------------
This module attaches functions to ``inngest_client`` purely via decorator
side-effects. Importing this module is what makes the functions discoverable
to ``inngest.fast_api.serve``. ``app/tasks/__init__.py`` performs that
import; ``app/main.py`` then imports ``app.tasks`` for the same reason.

Run-id format
-------------
Each batch picks a stable ``cron-{date}`` run id so the per-pair
``rationale_json["run_id"]`` values are easy to grep in the Inngest UI.
"""

from __future__ import annotations

import asyncio
import datetime as _dt

import inngest

from app.core.inngest import inngest_client
from app.db.session import get_session_factory


def _today_utc() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


# ---------------------------------------------------------------------------
# Step 1 — Geo-level market scores
# ---------------------------------------------------------------------------

def _run_rescore_market_scores() -> dict:
    """Synchronous body of the market geo-level rescore job."""
    from app.services.scoring.market_aggregator import score_all_active_materials

    today = _today_utc()
    run_id = f"cron-{today.isoformat()}"
    session = get_session_factory()()
    try:
        results = score_all_active_materials(session, today, run_id=run_id)
        return {
            "step": "geo_scores",
            "as_of_date": today.isoformat(),
            "run_id": run_id,
            "scored_pairs": len(results),
        }
    finally:
        session.close()


@inngest_client.create_function(
    fn_id="rescore-market-scores",
    trigger=inngest.TriggerCron(cron="0 2 * * MON"),
)
async def rescore_market_scores_job(ctx: inngest.Context) -> dict:
    """Weekly geo-level market rescore — runs every Monday at 02:00 UTC.

    Populates material_geography_risk_scores. Must complete before the global
    rollup job (03:00 UTC) reads from it.
    """
    ctx.logger.info("scoring_jobs.market_geo.start")
    result = await asyncio.to_thread(_run_rescore_market_scores)
    ctx.logger.info("scoring_jobs.market_geo.done", extra=result)
    return result


# ---------------------------------------------------------------------------
# Step 2 — Global material rollup
# ---------------------------------------------------------------------------

def _run_rescore_global_rollups() -> dict:
    """Synchronous body of the global rollup job."""
    from app.services.scoring.global_rollup import score_all_material_global_rollups

    today = _today_utc()
    run_id = f"global-cron-{today.isoformat()}"
    session = get_session_factory()()
    try:
        results = score_all_material_global_rollups(session, today, run_id=run_id)
        return {
            "step": "global_rollup",
            "as_of_date": today.isoformat(),
            "run_id": run_id,
            "scored_materials": len(results),
        }
    finally:
        session.close()


@inngest_client.create_function(
    fn_id="rescore-global-rollups",
    trigger=inngest.TriggerCron(cron="0 3 * * MON"),
)
async def rescore_global_rollups_job(ctx: inngest.Context) -> dict:
    """Weekly global rollup — runs every Monday at 03:00 UTC.

    Aggregates material_geography_risk_scores → material_global_risk_scores.
    Must complete before the chemistry rescore (04:00 UTC) reads from it.
    """
    ctx.logger.info("scoring_jobs.global_rollup.start")
    result = await asyncio.to_thread(_run_rescore_global_rollups)
    ctx.logger.info("scoring_jobs.global_rollup.done", extra=result)
    return result


# ---------------------------------------------------------------------------
# Step 3 — Chemistry scores from rollup
# ---------------------------------------------------------------------------

def _run_rescore_all_chemistries() -> dict:
    """Synchronous body of the chemistry rescore job."""
    from app.services.scoring.chemistry_risk import score_all_chemistries_from_rollup

    today = _today_utc()
    session = get_session_factory()()
    try:
        results = score_all_chemistries_from_rollup(session, today)
        return {
            "step": "chemistry_scores",
            "as_of_date": today.isoformat(),
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


@inngest_client.create_function(
    fn_id="rescore-all-chemistries",
    trigger=inngest.TriggerCron(cron="0 4 * * MON"),
)
async def rescore_chemistries_job(ctx: inngest.Context) -> dict:
    """Weekly chemistry rescore — runs every Monday at 04:00 UTC.

    Reads material_global_risk_scores and intensity-weights constituent
    minerals → chemistry_risk_scores. Uses methodology_version=2.0
    (score_all_chemistries_from_rollup).
    """
    ctx.logger.info("scoring_jobs.chemistry.start")
    result = await asyncio.to_thread(_run_rescore_all_chemistries)
    ctx.logger.info("scoring_jobs.chemistry.done", extra=result)
    return result


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
    "_run_rescore_market_scores",
    "_run_rescore_global_rollups",
    "_run_rescore_all_chemistries",
]
