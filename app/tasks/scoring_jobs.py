"""Scheduled scoring jobs registered with Inngest.

Weekly scoring pipeline (four jobs, Monday UTC):

  Job 0 — ``rescore-hs-nodes``           Monday 01:00 UTC
      Level-0 HS node scores (HHI + trade events per stage × country).
      → hs_code_geography_risk_scores

  Job 1 — ``rescore-market-scores``      Monday 02:00 UTC
      Geo-level scoring for every active material.
      → material_geography_risk_scores (one row per material × geography)

  Job 2 — ``rescore-global-rollups``     Monday 03:00 UTC
      Trade-weighted global rollup per material.
      → material_global_risk_scores (one row per material)

  Job 3 — ``rescore-all-chemistries``    Monday 04:00 UTC
      Intensity-weighted chemistry scores from global rollups.
      → chemistry_risk_scores (one row per chemistry)

Daily trade flow ingestion (one job):

  Job D — ``ingest-comtrade-daily``      Daily 06:00 UTC
      Fetches export + import trade flows from UN Comtrade for the 3 most
      recently complete calendar years.  Idempotent: already-committed
      (reporter × HS prefix × year) batches are skipped.  Designed to run
      daily until full coverage is reached (rate-limited to ~500 calls/day).
      Runs build-trade-signals after both flow directions complete.

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

Job D (Comtrade) is broken into per-HS-prefix steps for the same timeout reason.
Each step fetches one prefix across all reporters and target years; already-
committed batches are skipped immediately.  One step per prefix × flow direction
= ~96 steps per daily run.

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
Importing this module registers all functions on ``inngest_client`` via
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
    from app.models.supply import Material, MaterialProductionShare
    from app.services.scoring.market_aggregator import score_material_geography

    as_of_date = _dt.date.fromisoformat(as_of_date_iso)
    session = get_session_factory()()
    try:
        material = session.get(Material, material_id)
        if not material:
            log.warning("scoring_jobs.step.geo.no_material", material_id=material_id)
            return {"material_id": material_id, "pairs_scored": 0, "skipped": True}

        # Derive geographies: production share countries + any geo with events.
        # primary_producing_countries was removed in migration 023; use
        # material_production_shares as the authoritative source instead.
        prod_share_geos = list(session.scalars(
            select(MaterialProductionShare.country_code)
            .where(
                MaterialProductionShare.material_id == material.id,
                MaterialProductionShare.production_share > 0,
            )
            .distinct()
        ).all())
        geos: list[str] = [g.upper() for g in prod_share_geos]

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
# Job 0 helpers — Level-0 HS node scores
# ---------------------------------------------------------------------------

def _sync_rescore_hs_nodes(as_of_date_iso: str) -> dict:
    """Score every (HS node × country) pair that has production share data.

    Must complete before ``rescore-market-scores`` runs so that
    ``score_material_geography()`` can find Level-0 rows for the stage rollup.
    """
    from app.services.scoring.hs_node_scorer import score_all_hs_nodes

    as_of_date = _dt.date.fromisoformat(as_of_date_iso)
    session = get_session_factory()()
    try:
        result = score_all_hs_nodes(session, as_of_date)
        return result
    finally:
        session.close()


async def _step_rescore_hs_nodes(as_of_date_iso: str) -> dict:
    return await asyncio.to_thread(_sync_rescore_hs_nodes, as_of_date_iso)


# ---------------------------------------------------------------------------
# Job 0 — Level-0 HS node scores
# ---------------------------------------------------------------------------

@inngest_client.create_function(
    fn_id="rescore-hs-nodes",
    trigger=inngest.TriggerCron(cron="0 1 * * MON"),
)
async def rescore_hs_nodes_job(ctx: inngest.Context) -> dict:
    """Weekly Level-0 HS node rescore — runs every Monday at 01:00 UTC.

    Computes HsCodeGeographyRiskScore rows for all (HS mapping × country)
    pairs that have production share data.  Results feed into the stage-weighted
    Material Concentration rollup in ``rescore-market-scores`` (02:00 UTC).

    Pipeline order (Monday):
        01:00 UTC  rescore-hs-nodes           ← this job (Level 0)
        02:00 UTC  rescore-market-scores      ← Level 1; reads Level-0 rows
        03:00 UTC  rescore-global-rollups     ← Level 2
        04:00 UTC  rescore-all-chemistries    ← Level 3
    """
    today_iso = _today_utc().isoformat()
    log.info("scoring_jobs.hs_nodes.start", today=today_iso)

    result: dict = await ctx.step.run(
        "rescore-hs-nodes",
        _step_rescore_hs_nodes,
        today_iso,
    )

    log.info(
        "scoring_jobs.hs_nodes.done",
        pairs_scored=result.get("pairs_scored"),
        nodes_processed=result.get("nodes_processed"),
        today=today_iso,
    )
    return {
        "step": "hs_node_scores",
        "as_of_date": today_iso,
        **result,
    }


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
    log.info("scoring_jobs.market_geo.start", today=today_iso)

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

    log.info(
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
    log.info("scoring_jobs.global_rollup.start", today=today_iso)

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

    log.info(
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
    log.info("scoring_jobs.chemistry.start", today=today_iso)

    result: dict = await ctx.step.run(
        "rescore-all-chemistries",
        _step_rescore_all_chemistries,
        today_iso,
    )

    log.info(
        "scoring_jobs.chemistry.done",
        rescored=result.get("rescored"),
        today=today_iso,
    )
    return {
        "step": "chemistry_scores",
        "as_of_date": today_iso,
        **result,
    }


# ---------------------------------------------------------------------------
# Job D helpers — daily Comtrade trade flow ingestion
# ---------------------------------------------------------------------------

def _target_years() -> list[int]:
    """Return the 3 most recently complete calendar years.

    Comtrade annual data for year Y is typically available by March/April of
    Y+1.  Using current_year - 1 as the ceiling is conservative but safe for
    year-round scheduling.

    Example (May 2026): [2023, 2024, 2025]
    """
    end = _today_utc().year - 1
    return [end - 2, end - 1, end]


def _sync_get_hs_prefixes() -> list[str]:
    """Return all distinct 4-digit HS prefixes from hs_code_material_mappings.

    Ordered deterministically for Inngest replay safety.
    """
    from sqlalchemy import select
    from app.models.supply import HsCodeMaterialMapping

    session = get_session_factory()()
    try:
        rows = session.scalars(
            select(HsCodeMaterialMapping.hs_code_prefix)
            .where(HsCodeMaterialMapping.digit_count == 4)
            .distinct()
            .order_by(HsCodeMaterialMapping.hs_code_prefix)
        ).all()
        return list(rows)
    finally:
        session.close()


async def _step_get_hs_prefixes() -> list[str]:
    return await asyncio.to_thread(_sync_get_hs_prefixes)


def _sync_ingest_comtrade_prefix(
    hs_prefix: str,
    years: list[int],
    flow_code: str,
) -> dict:
    """Fetch and persist Comtrade trade flows for one HS prefix.

    Idempotent: batches with an existing source_document external_id are
    skipped immediately, so already-committed (reporter × prefix × year)
    combinations consume no API quota.

    Args:
        hs_prefix:  4-digit HS prefix to query (e.g. ``"2604"``).
        years:      Calendar years to fetch.
        flow_code:  ``"X"`` (exports) or ``"M"`` (imports).

    Returns:
        JSON-serialisable dict with ``inserted``, ``skipped_existing_doc``,
        ``api_calls_made``, and ``errors``.
    """
    from app.services.ingestion.comtrade import ingest_comtrade

    session = get_session_factory()()
    try:
        result = ingest_comtrade(
            session,
            years=years,
            hs_prefixes=[hs_prefix],
            flow_code=flow_code,
        )
        return {**result, "hs_prefix": hs_prefix, "flow_code": flow_code}
    except ValueError as exc:
        # Missing API key — surface clearly rather than silently skip
        log.error(
            "comtrade_job.prefix.api_key_missing",
            hs_prefix=hs_prefix,
            flow_code=flow_code,
            error=str(exc),
        )
        return {
            "hs_prefix": hs_prefix,
            "flow_code": flow_code,
            "error": "api_key_missing",
            "inserted": 0,
            "api_calls_made": 0,
        }
    except Exception:
        log.exception(
            "comtrade_job.prefix.error",
            hs_prefix=hs_prefix,
            flow_code=flow_code,
        )
        return {
            "hs_prefix": hs_prefix,
            "flow_code": flow_code,
            "error": True,
            "inserted": 0,
            "api_calls_made": 0,
        }
    finally:
        session.close()


async def _step_ingest_comtrade_prefix(
    hs_prefix: str,
    years: list[int],
    flow_code: str,
) -> dict:
    return await asyncio.to_thread(
        _sync_ingest_comtrade_prefix, hs_prefix, years, flow_code
    )


def _sync_build_trade_signals(years: list[int]) -> dict:
    """Derive synthetic risk events from trade flow extremes."""
    from app.services.ingestion.trade_signal_builder import build_trade_signals

    session = get_session_factory()()
    try:
        result = build_trade_signals(session, years=years)
        return result
    finally:
        session.close()


async def _step_build_trade_signals(years: list[int]) -> dict:
    return await asyncio.to_thread(_sync_build_trade_signals, years)


# ---------------------------------------------------------------------------
# Job D — Daily Comtrade trade flow ingestion
# ---------------------------------------------------------------------------

@inngest_client.create_function(
    fn_id="ingest-comtrade-daily",
    trigger=inngest.TriggerCron(cron="0 6 * * *"),
)
async def ingest_comtrade_job(ctx: inngest.Context) -> dict:
    """Daily UN Comtrade trade flow ingestion — runs every day at 06:00 UTC.

    Fetches export (flow X) and import (flow M) annual trade data for all
    4-digit HS prefixes in hs_code_material_mappings, targeting the 3 most
    recently complete calendar years.

    Rate-limit behaviour
    --------------------
    The UN Comtrade API allows ~500 calls/day on a subscription key.  Full
    coverage across 58 reporters × 48 prefixes × 3 years requires ~8,300
    calls (~17 days).  Each already-committed (reporter × prefix × year)
    batch is skipped without an API call, so the job makes steady daily
    progress until coverage is complete.  Once complete, daily runs are
    near-instant (all batches skipped, 0 API calls).

    Step structure
    --------------
    One Inngest step per (hs_prefix × flow_code) keeps each step well under
    the ~2 min HTTP timeout.  Inngest checkpoints after every step so a
    mid-run failure or rate-limit error resumes from the last successful
    prefix rather than from scratch.

    Post-ingest
    -----------
    After all prefixes complete, ``build-trade-signals`` is run to refresh
    EXPORT_DROP, IMPORT_DROP, and TRADE_CONCENTRATION risk events.
    """
    today_iso = _today_utc().isoformat()
    years = _target_years()
    log.info("comtrade_job.start", today=today_iso, years=years)

    # Step 1: resolve HS prefixes from DB (not hardcoded — picks up new mappings)
    hs_prefixes: list[str] = await ctx.step.run(
        "get-hs-prefixes",
        _step_get_hs_prefixes,
    )
    log.info("comtrade_job.prefixes_resolved", count=len(hs_prefixes))

    # Steps 2–N: one step per prefix for exports, then imports.
    # Keeping flow directions in separate named steps lets Inngest memo them
    # independently — if the X pass completes fully and M fails mid-run, the
    # replay skips all X steps and retries only the failed M step.
    total_inserted = 0
    total_api_calls = 0
    total_errors = 0

    for prefix in hs_prefixes:
        result: dict = await ctx.step.run(
            f"ingest-exports-{prefix}",
            _step_ingest_comtrade_prefix,
            prefix,
            years,
            "X",
        )
        total_inserted += result.get("inserted", 0)
        total_api_calls += result.get("api_calls_made", 0)
        if result.get("error"):
            total_errors += 1

    for prefix in hs_prefixes:
        result = await ctx.step.run(
            f"ingest-imports-{prefix}",
            _step_ingest_comtrade_prefix,
            prefix,
            years,
            "M",
        )
        total_inserted += result.get("inserted", 0)
        total_api_calls += result.get("api_calls_made", 0)
        if result.get("error"):
            total_errors += 1

    # Final step: refresh synthetic risk events from trade flow data
    signals: dict = await ctx.step.run(
        "build-trade-signals",
        _step_build_trade_signals,
        years,
    )

    log.info(
        "comtrade_job.done",
        today=today_iso,
        years=years,
        total_inserted=total_inserted,
        total_api_calls=total_api_calls,
        total_errors=total_errors,
        trade_signals=signals,
    )
    return {
        "as_of_date": today_iso,
        "years": years,
        "prefixes_processed": len(hs_prefixes),
        "total_inserted": total_inserted,
        "total_api_calls": total_api_calls,
        "total_errors": total_errors,
        "trade_signals": signals,
    }


SCHEDULED_FUNCTIONS = [
    ingest_comtrade_job,            # Daily  — 06:00 UTC
    rescore_hs_nodes_job,           # Level 0 — Mon 01:00 UTC
    rescore_market_scores_job,      # Level 1 — Mon 02:00 UTC
    rescore_global_rollups_job,     # Level 2 — Mon 03:00 UTC
    rescore_chemistries_job,        # Level 3 — Mon 04:00 UTC
]

__all__ = [
    "SCHEDULED_FUNCTIONS",
    "ingest_comtrade_job",
    "rescore_hs_nodes_job",
    "rescore_market_scores_job",
    "rescore_global_rollups_job",
    "rescore_chemistries_job",
]
