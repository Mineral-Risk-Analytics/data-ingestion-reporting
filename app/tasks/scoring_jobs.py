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

  Job D — ``ingest-comtrade-daily``      Daily 04:00 UTC (midnight EDT)
      Fetches export + import trade flows from UN Comtrade for the 3 most
      recently complete calendar years.  Idempotent: already-committed
      (reporter × HS prefix × year) batches are skipped.  Designed to run
      daily until full coverage is reached (rate-limited to ~500 calls/day).
      Runs build-trade-signals after both flow directions complete.

      Schedule moved from 06:00 → 04:00 UTC on 2026-06-06 when the 11.2 HS-
      prefix expansion pushed expected runtime toward ~10-12 hours; starting
      at midnight EDT keeps the finish before US business hours and avoids
      overlap with the Monday 01:00-04:00 UTC rescore window.

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

        # V1 (4.0, 2026-07-18): scored-geography universe = producers +
        # trade-gate exporters.  Event-only geographies are skipped — zero
        # concentration by definition, zero L2 trade weight by
        # construction, and they were ~85% of pairs (cobalt: 127 -> ~24).
        # See stage_concentration.derive_scoring_geographies.
        from app.services.scoring.stage_concentration import (
            derive_scoring_geographies,
        )
        geos: list[str] = derive_scoring_geographies(session, material.id)

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
    """Return the 3 most recently complete calendar years, **newest first**.

    Comtrade annual data for year Y is typically available by March/April of
    Y+1.  Using current_year - 1 as the ceiling is conservative but safe for
    year-round scheduling.

    **Order matters**: newest first so the daily job's per-(year × prefix ×
    flow) iteration prioritises the most recent year.  With a daily quota
    (~500 calls), this means partial backfills always have current data
    before older years.

    Example (May 2026): [2025, 2024, 2023]
    """
    end = _today_utc().year - 1
    return [end, end - 1, end - 2]


def _derive_four_digit_prefixes(raw_prefixes: list[str]) -> list[str]:
    """Truncate raw HS prefixes (any length) to 4-digit chapters, dedupe,
    sort.  Pure helper extracted from ``_sync_get_hs_prefixes`` so the
    derivation logic can be unit-tested without a DB session.

    Mirrors ``comtrade.py::ingest_comtrade``'s prefix derivation at
    lines 913-918 so the daily Inngest job and the CLI take the same
    path on the same source data.
    """
    four_digit: set[str] = set()
    for raw in raw_prefixes:
        clean = raw.replace(".", "")
        if len(clean) >= 4:
            four_digit.add(clean[:4])
    return sorted(four_digit)


def _sync_get_hs_prefixes() -> list[str]:
    """Return all distinct 4-digit HS prefixes derived from
    ``hs_code_material_mappings``.

    Prior to the 11.2-followup fix (2026-06-06) this function pulled rows
    where ``digit_count == 4`` only.  That diverged from the inner
    ``comtrade.py::_build_hs_material_map`` logic, which derives 4-digit
    prefixes from rows where ``digit_count IN (4, 6)`` by taking the
    first four characters.  The divergence meant any newly seeded
    6-digit mapping under a chapter that had no companion 4-digit
    "umbrella" row was silently dropped from the daily Inngest job's
    fetch list — even though a manual ``ingest_comtrade`` CLI invocation
    would have picked it up.

    The 11.2 easy adds surfaced the bug: HS 3801 / 7410 / 7607 / 8505
    were seeded only at the 6-digit level (380110, 380130, 741011,
    760711, 850511), so the daily run never queried Comtrade for them.

    The current implementation matches comtrade.py: filter to
    ``digit_count IN (4, 6)`` and truncate to the first four chars.

    Ordered deterministically for Inngest replay safety.
    """
    from sqlalchemy import select
    from app.models.supply import HsCodeMaterialMapping

    session = get_session_factory()()
    try:
        # Pull all 4- and 6-digit rows; derive the 4-digit prefix set.
        # 10-digit US-scope rows are intentionally excluded — Comtrade
        # only returns up to 6-digit cmdCodes, so deriving 4-digit
        # umbrellas from 10-digit US-only rows would be misleading
        # (it would imply we have global coverage we don't have).
        raw_prefixes = session.scalars(
            select(HsCodeMaterialMapping.hs_code_prefix)
            .where(HsCodeMaterialMapping.digit_count.in_([4, 6]))
            .distinct()
        ).all()

        result = _derive_four_digit_prefixes(list(raw_prefixes))

        # Surface which prefixes are present so an operator can spot
        # newly onboarded chapters in the daily job log.  This is the
        # blast-radius diagnostic referenced in the 11.2-followup fix:
        # the very first run after this change will show a step count
        # higher than the previous day, reflecting newly fetched
        # chapters (3801/7410/7607/8505 for the 11.2 adds).
        log.info(
            "comtrade_job.prefixes_derived",
            count=len(result),
            prefixes=result,
            note=(
                "Derived from hs_code_material_mappings with "
                "digit_count IN (4, 6).  An increase vs the previous "
                "daily run indicates newly seeded 6-digit chapters."
            ),
        )
        return result
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
# Authoritative-count helpers — DB-derived, retry-immune
# ---------------------------------------------------------------------------
#
# Why these exist: ingest_comtrade_job's in-memory ``total_inserted`` and
# ``total_api_calls`` counters undercount on Inngest step retries.  When a
# step writes rows + commits the SourceDocument but its return payload
# fails to make it back to the orchestrator (network blip, serialization
# timeout right at the boundary), Inngest retries the step.  On retry,
# the SourceDocument idempotency check at comtrade.py:979-1005 short-
# circuits the inner loop and returns ``inserted=0``.  The orchestrator
# then credits 0 even though the original writes are already in the DB.
#
# The DB query below counts trade_flows + source_documents created since
# the run-start timestamp — that's authoritative regardless of how many
# retries each step took.  Run on 2026-06-03 had counter=4648 but
# authoritative=4930 (282-row undercount), which is why this exists.

def _sync_capture_now() -> str:
    """Return current UTC time as an ISO timestamp.

    Used as a memoized Inngest step so retries see the same value — that
    way the end-of-run count query has a stable lower-bound timestamp.
    """
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


async def _step_capture_now() -> str:
    return await asyncio.to_thread(_sync_capture_now)


def _sync_count_comtrade_writes_since(since_iso: str) -> dict:
    """Count Comtrade trade_flows + source_documents created since
    ``since_iso`` (UTC ISO timestamp).

    Returns ``{"inserted": N, "api_calls": M}`` where:
      * ``inserted``  = COUNT(*) FROM trade_flows joined to the Comtrade
                        SourceDocument set created since the cutoff.
      * ``api_calls`` = COUNT(*) FROM source_documents in the same window
                        (each successful API call writes exactly one
                        SourceDocument — populated or empty-marker — so
                        the document count is the call count).

    Errors (DB connection, etc.) return ``{"error": str, "inserted": -1,
    "api_calls": -1}`` so the orchestrator can fall back to the in-memory
    counters rather than reporting zero.
    """
    from sqlalchemy import func as sa_func, select

    from app.models.documents import SourceDocument
    from app.models.source import Source
    from app.models.supply import TradeFlow

    session = get_session_factory()()
    try:
        source_id = session.scalar(
            select(Source.id).where(Source.source_type == "comtrade")
        )
        if source_id is None:
            return {"inserted": 0, "api_calls": 0}

        since_dt = _dt.datetime.fromisoformat(since_iso)

        api_calls = session.scalar(
            select(sa_func.count())
            .select_from(SourceDocument)
            .where(
                SourceDocument.source_id == source_id,
                SourceDocument.created_at >= since_dt,
            )
        ) or 0

        inserted = session.scalar(
            select(sa_func.count())
            .select_from(TradeFlow)
            .join(
                SourceDocument,
                TradeFlow.source_document_id == SourceDocument.id,
            )
            .where(
                SourceDocument.source_id == source_id,
                SourceDocument.created_at >= since_dt,
            )
        ) or 0

        return {"inserted": int(inserted), "api_calls": int(api_calls)}
    except Exception as exc:
        return {
            "inserted": -1,
            "api_calls": -1,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        session.close()


async def _step_count_comtrade_writes_since(since_iso: str) -> dict:
    return await asyncio.to_thread(_sync_count_comtrade_writes_since, since_iso)


# ---------------------------------------------------------------------------
# Job D — Daily Comtrade trade flow ingestion
# ---------------------------------------------------------------------------

@inngest_client.create_function(
    fn_id="ingest-comtrade-weekly",
    # 2026-07-27 (Nicole, scheduled-jobs review): daily → weekly Wednesday.
    # Comtrade reporters update monthly at best; daily polling was 7x the
    # API traffic for no freshness gain. Wednesday keeps data fresh ahead
    # of the Sunday-ingest → Monday-rescore cycle.
    trigger=inngest.TriggerCron(cron="0 4 * * WED"),
)
async def ingest_comtrade_job(ctx: inngest.Context) -> dict:
    """Daily UN Comtrade trade flow ingestion — runs every day at 04:00 UTC
    (midnight EDT / 21:00 PDT previous day).

    Schedule history:

    * Until 2026-06-06 the job ran at 06:00 UTC (02:00 EDT).
    * Bumped to 04:00 UTC (midnight EDT) when the 11.2 HS-prefix expansion
      pushed expected runtime from ~6 hours toward ~10-12 hours.  Starting
      at midnight EDT keeps the finish before US business hours and avoids
      any Monday-window overlap with the weekly rescore jobs at 01:00-04:00
      UTC.  On Mondays the chemistry rescore (04:00 UTC) starts at the
      same instant as this job, but chemistry reads pre-computed rollups —
      not ``trade_flows`` — so there's no data-race concern.

    Fetches export (flow X) and import (flow M) annual trade data for all
    4-digit HS prefixes in hs_code_material_mappings, targeting the 3 most
    recently complete calendar years.

    Iteration order (2026-05-06 — calibrated for 500-call/day quota)
    -----------------------------------------------------------------
    Outer to inner: year (newest first) → prefix → flow (X then M).

    Why this order:
      * **Newest year first** — within budget, the daily run completes
        the most recent year across every prefix before starting older
        years.  Partial backfills always have current data, which matters
        more than historical completeness for live scoring.
      * **X and M alternate per-prefix** — both flow directions make
        balanced progress every day rather than "all exports complete
        first, then start on imports tomorrow."  Scoring uses both:
        exports drive supply-side concentration, imports drive demand-
        side dependency.

    Rate-limit behaviour
    --------------------
    The UN Comtrade API caps free / subscription tiers at ~500 calls/day.
    The inline 429 circuit breaker (``ComtradeRateLimitExhausted``) raises
    when 3 consecutive batches all 429-after-retries; that bubbles up
    here and we stop scheduling further steps for the day so we don't
    burn quota on doomed retries.  Inngest checkpoints whatever was
    completed; tomorrow's run picks up where we left off via the DB
    idempotency check (already-committed batches consume zero API quota).

    Step structure
    --------------
    One Inngest step per (year × prefix × flow) triple — fine-grained so
    Inngest memo lets a single failed combination retry without redoing
    the rest.  Steps are small (~30s typical), well under the ~2 min
    HTTP timeout.

    Post-ingest
    -----------
    ``build-trade-signals`` is run only after a clean run (no rate-limit
    halt).  Running it on a partial backfill produces less reliable
    EXPORT_DROP / IMPORT_DROP / TRADE_CONCENTRATION events; tomorrow's
    run will rebuild them after finishing.
    """
    today_iso = _today_utc().isoformat()
    years = _target_years()
    log.info("comtrade_job.start", today=today_iso, years=years)

    # Step 0: capture run-start timestamp as a memoized step so all Inngest
    # retries see the same lower bound.  The end-of-run count query uses
    # this timestamp to derive authoritative inserted/api_calls totals
    # from the DB — see _sync_count_comtrade_writes_since for why.
    run_started_at: str = await ctx.step.run(
        "capture-run-start",
        _step_capture_now,
    )

    # Step 1: resolve HS prefixes from DB (not hardcoded — picks up new mappings)
    hs_prefixes: list[str] = await ctx.step.run(
        "get-hs-prefixes",
        _step_get_hs_prefixes,
    )
    log.info("comtrade_job.prefixes_resolved", count=len(hs_prefixes))

    # Steps 2–N: one step per (year × prefix × flow) triple.
    # Year-desc × prefix × flow nesting prioritises current data and keeps
    # both flow directions advancing at the same rate.  Step IDs encode all
    # three dimensions so Inngest memoisation is tightly scoped — a single
    # failed combination retries itself, not the whole prefix or year.
    total_inserted = 0
    total_api_calls = 0
    total_errors = 0
    rate_limited = False

    for year in years:                                  # newest first
        for prefix in hs_prefixes:
            for flow_code, flow_label in (("X", "exports"), ("M", "imports")):
                step_id = f"ingest-{flow_label}-{year}-{prefix}"
                try:
                    result: dict = await ctx.step.run(
                        step_id,
                        _step_ingest_comtrade_prefix,
                        prefix,
                        [year],   # single year per step — finer granularity
                        flow_code,
                    )
                except Exception as exc:
                    # Distinguish rate-limit halts from other failures.
                    # When the circuit breaker trips, stop scheduling
                    # further steps so we don't burn quota on doomed
                    # retries; tomorrow's run resumes via DB dedup.
                    # Match by class name string so we don't have to import
                    # comtrade module-level into this scheduling layer.
                    if exc.__class__.__name__ == "ComtradeRateLimitExhausted":
                        log.warning(
                            "comtrade_job.rate_limit_halt",
                            year=year, prefix=prefix, flow=flow_code,
                            error=str(exc),
                        )
                        rate_limited = True
                        break
                    raise
                total_inserted += result.get("inserted", 0)
                total_api_calls += result.get("api_calls_made", 0)
                if result.get("error"):
                    total_errors += 1
            if rate_limited:
                break
        if rate_limited:
            break

    # Authoritative end-of-run counts derived from the DB rather than from
    # the in-memory per-step counters.  Step retries cause the counter
    # to undercount (retried steps return inserted=0 even though the
    # first attempt's writes already committed) — see the
    # _sync_count_comtrade_writes_since docstring for the full reasoning.
    # ``total_inserted_counter`` and ``total_api_calls_counter`` are
    # preserved so the gap between the two is observable in the run
    # summary; large gaps indicate frequent step retries.
    authoritative = await ctx.step.run(
        "count-run-writes",
        _step_count_comtrade_writes_since,
        run_started_at,
    )
    if authoritative.get("inserted", -1) >= 0:
        total_inserted_actual = int(authoritative["inserted"])
        total_api_calls_actual = int(authoritative["api_calls"])
        retry_undercount = total_inserted_actual - total_inserted
        if retry_undercount > 0:
            log.warning(
                "comtrade_job.retry_undercount_detected",
                counter=total_inserted,
                authoritative=total_inserted_actual,
                gap_rows=retry_undercount,
                hint=(
                    "In-memory counter undercounted the DB. Likely cause: "
                    "Inngest step retries where the first attempt committed "
                    "but the return payload failed to deliver. Data integrity "
                    "fine; only the run summary was previously affected."
                ),
            )
    else:
        # Fall back to the in-memory counters if the count query errored.
        total_inserted_actual = total_inserted
        total_api_calls_actual = total_api_calls
        log.warning(
            "comtrade_job.authoritative_count_failed",
            error=authoritative.get("error"),
            hint="Reporting in-memory counter values (may undercount).",
        )

    # Final step: refresh synthetic risk events from trade flow data —
    # only after a clean run (a rate-limit halt mid-run leaves trade_flows
    # in a partial state that distorts the synthetic signals; tomorrow's
    # run rebuilds them after finishing).
    signals: dict = {}
    if not rate_limited:
        signals = await ctx.step.run(
            "build-trade-signals",
            _step_build_trade_signals,
            years,
        )

    # Backfill-complete signal (2026-05-06): when a clean run consumed 0
    # API calls AND wasn't rate-limited, every (year × prefix × reporter ×
    # flow) combination was skipped via DB idempotency.  That means we're
    # caught up — Comtrade publishes annually with a 4-6 month lag, so
    # daily runs from this point forward are wasteful (~14k SELECT
    # queries/day for nothing).  Logging at WARNING so it's visible in
    # the Inngest dashboard as a flag.  Action: swap the cron from
    # ``0 4 * * *`` to ``0 4 * * SUN`` (or monthly) when this fires.
    # Uses the authoritative count so a counter-undercount can't spuriously
    # fire this signal.
    backfill_complete = (
        not rate_limited
        and total_api_calls_actual == 0
        and total_errors == 0
        and len(hs_prefixes) > 0
    )
    if backfill_complete:
        log.warning(
            "comtrade_job.backfill_complete",
            today=today_iso,
            years=years,
            prefixes_processed=len(hs_prefixes),
            hint=(
                "All (year × prefix × reporter × flow) combinations are "
                "already ingested.  Daily runs from now on do ~14k SELECTs "
                "for nothing.  Switch the cron in scoring_jobs.py from "
                "'0 4 * * *' to '0 4 * * SUN' (weekly) — Comtrade publishes "
                "annual data with a 4-6 month lag, so weekly is sufficient "
                "to pick up new releases within a week of publication."
            ),
        )

    log.info(
        "comtrade_job.done",
        today=today_iso,
        years=years,
        total_inserted=total_inserted_actual,
        total_inserted_counter=total_inserted,
        total_api_calls=total_api_calls_actual,
        total_api_calls_counter=total_api_calls,
        total_errors=total_errors,
        rate_limited=rate_limited,
        backfill_complete=backfill_complete,
        trade_signals=signals,
    )
    return {
        "as_of_date": today_iso,
        "years": years,
        "prefixes_processed": len(hs_prefixes),
        # Authoritative DB-derived counts (immune to Inngest step retries).
        "total_inserted": total_inserted_actual,
        "total_api_calls": total_api_calls_actual,
        # In-memory per-step counters kept for diagnostic visibility;
        # a gap between *_counter and the headline value flags retry churn.
        "total_inserted_counter": total_inserted,
        "total_api_calls_counter": total_api_calls,
        "total_errors": total_errors,
        "rate_limited": rate_limited,
        "backfill_complete": backfill_complete,
        "trade_signals": signals,
    }


SCHEDULED_FUNCTIONS = [
    ingest_comtrade_job,            # Weekly — Wed 04:00 UTC (2026-07-27: was daily)
    rescore_hs_nodes_job,           # Level 0 — Mon 01:00 UTC
    rescore_market_scores_job,      # Level 1 — Mon 02:00 UTC
    rescore_global_rollups_job,     # Level 2 — Mon 03:00 UTC
    # PARKED 2026-07-27 (Nicole): chemistry scores (L3) are unused and
    # off the launch roadmap — the weekly pass wrote rows nothing reads.
    # Function remains for manual CLI / future L3; backfillable from
    # global rollups at any time.
    # rescore_chemistries_job,      # Level 3 — Mon 04:00 UTC
]

__all__ = [
    "SCHEDULED_FUNCTIONS",
    "ingest_comtrade_job",
    "rescore_hs_nodes_job",
    "rescore_market_scores_job",
    "rescore_global_rollups_job",
    "rescore_chemistries_job",
]
