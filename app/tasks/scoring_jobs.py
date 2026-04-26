"""Scheduled scoring jobs registered with Inngest.

Two weekly cron jobs spaced an hour apart so the chemistry rescore commits
finish before the market layer reads them:

- ``rescore-all-chemistries``   — Mondays 02:00 UTC
- ``rescore-market-scores``     — Mondays 03:00 UTC

Both jobs:
  - Open a fresh SQLAlchemy session via ``get_session_factory`` (do NOT share
    a session across job invocations — Inngest workers are concurrent).
  - Run the synchronous scoring function inside ``asyncio.to_thread`` so the
    Inngest async handler does not block its event loop.
  - Always close the session in a ``finally`` block, even on failure.

Both underlying scoring helpers (``rescore_all_chemistries`` and
``score_all_active_materials``) commit their own writes — do NOT wrap them
in additional transactions or call ``session.commit()`` afterwards.

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


def _run_rescore_all_chemistries() -> dict:
    """Synchronous body of the chemistry rescore job.

    Kept module-level (not nested) so ``asyncio.to_thread`` can pickle/dispatch
    it cleanly and so it is unit-testable without dragging Inngest in.
    """
    from app.services.scoring.chemistry_risk import rescore_all_chemistries

    today = _today_utc()
    session = get_session_factory()()
    try:
        results = rescore_all_chemistries(session, today)
        return {
            "as_of_date": today.isoformat(),
            "rescored": len(results),
            "scores": [
                {
                    "slug": r["slug"],
                    "composite_risk_score": r["composite_risk_score"],
                    "score_confidence": r["score_confidence"],
                }
                for r in results
            ],
        }
    finally:
        session.close()


def _run_rescore_market_scores() -> dict:
    """Synchronous body of the market rescore job."""
    from app.services.scoring.market_aggregator import score_all_active_materials

    today = _today_utc()
    run_id = f"cron-{today.isoformat()}"
    session = get_session_factory()()
    try:
        results = score_all_active_materials(session, today, run_id=run_id)
        return {
            "as_of_date": today.isoformat(),
            "run_id": run_id,
            "scored_pairs": len(results),
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Inngest registrations — Mondays 02:00 / 03:00 UTC
# ---------------------------------------------------------------------------

@inngest_client.create_function(
    fn_id="rescore-all-chemistries",
    trigger=inngest.TriggerCron(cron="0 2 * * MON"),
)
async def rescore_chemistries_job(ctx: inngest.Context) -> dict:
    """Weekly chemistry rescore — runs every Monday at 02:00 UTC."""
    ctx.logger.info("scoring_jobs.chemistry.start")
    result = await asyncio.to_thread(_run_rescore_all_chemistries)
    ctx.logger.info("scoring_jobs.chemistry.done", extra=result)
    return result


@inngest_client.create_function(
    fn_id="rescore-market-scores",
    trigger=inngest.TriggerCron(cron="0 3 * * MON"),
)
async def rescore_market_scores_job(ctx: inngest.Context) -> dict:
    """Weekly market-layer rescore — runs every Monday at 03:00 UTC.

    Scheduled an hour after the chemistry rescore so the market-aggregator
    pillar inputs that read chemistry signals see the freshest data.
    """
    ctx.logger.info("scoring_jobs.market.start")
    result = await asyncio.to_thread(_run_rescore_market_scores)
    ctx.logger.info("scoring_jobs.market.done", extra=result)
    return result


SCHEDULED_FUNCTIONS = [
    rescore_chemistries_job,
    rescore_market_scores_job,
]


__all__ = [
    "SCHEDULED_FUNCTIONS",
    "rescore_chemistries_job",
    "rescore_market_scores_job",
    "_run_rescore_all_chemistries",
    "_run_rescore_market_scores",
]
