"""Scheduled ingestion jobs registered with Inngest.

Weekly jobs (Sunday night UTC, feeds Monday scoring):

- ``ingest-opensanctions-weekly``   — Sundays 22:00 UTC
- ``ingest-federal-register-weekly``— Sundays 22:30 UTC
- ``ingest-worldbank-weekly``       — Sundays 23:00 UTC

Quarterly jobs (1st of Jan / Apr / Jul / Oct):

- ``ingest-comtrade-quarterly``     — 1st of quarter, 00:00 UTC
- ``ingest-eurlex-quarterly``       — 1st of quarter, 00:30 UTC
- ``ingest-sec-edgar-quarterly``    — 1st of quarter, 01:00 UTC

All jobs:
  - Open a fresh SQLAlchemy session (never share a session across invocations).
  - Run synchronous ingestion functions inside ``asyncio.to_thread`` so the
    Inngest async handler does not block its event loop.
  - Always close the session in a ``finally`` block.
  - The interval gates in OpenSanctions and Pink Sheet ingesters prevent
    redundant full-file downloads — pass ``min_interval_days=0`` only if you
    need to force a re-run via manual trigger.

Comtrade cadence note:
    UN Comtrade data lags by 3–6 months. Running quarterly on the 1st of
    Jan/Apr/Jul/Oct is sufficient — more frequent runs return the same dataset.
    The ``--years`` flag is not set here so it defaults to the most recent
    available year(s) as configured in the CLI default.

SEC EDGAR cadence note:
    10-K (annual) and 10-Q (quarterly) filings make quarterly ingestion the
    right cadence. Runs with ``max_filings=12`` to capture the most recent
    filing cycle per company.
"""

from __future__ import annotations

import asyncio
import datetime as _dt

import inngest

from app.core.inngest import inngest_client
from app.db.session import get_session_factory

# ---------------------------------------------------------------------------
# Synchronous job bodies — module-level so asyncio.to_thread can dispatch
# them cleanly and they are unit-testable without Inngest overhead.
# ---------------------------------------------------------------------------

def _run_ingest_opensanctions() -> dict:
    from app.services.ingestion.opensanctions import ingest_opensanctions

    session = get_session_factory()()
    try:
        result = ingest_opensanctions(session, min_interval_days=6)
        return {"source": "opensanctions", **result}
    finally:
        session.close()


def _run_ingest_federal_register() -> dict:
    from app.services.ingestion.ingest_federal_register import ingest_federal_register

    # Default since_date is last 90 days — sufficient for weekly runs.
    session = get_session_factory()()
    try:
        result = ingest_federal_register(session)
        return {"source": "federal_register", **result}
    finally:
        session.close()


def _run_ingest_worldbank() -> dict:
    from app.services.ingestion.worldbank_pinksheet import ingest_pink_sheet

    session = get_session_factory()()
    try:
        result = ingest_pink_sheet(session, min_interval_days=25)
        return {"source": "worldbank_pink_sheet", **result}
    finally:
        session.close()


def _run_ingest_comtrade() -> dict:
    from app.services.ingestion.comtrade import ingest_comtrade

    session = get_session_factory()()
    try:
        # No year filter — ingest_comtrade defaults to the most recent
        # available year. Idempotent: existing rows are skipped.
        result = ingest_comtrade(session)
        return {"source": "comtrade", **result}
    finally:
        session.close()


def _run_ingest_eurlex() -> dict:
    from app.services.ingestion.eurlex import ingest_eurlex

    session = get_session_factory()()
    try:
        # fetch_summaries=True — backfills any summaries that are still None.
        # Manifest regulations rarely change so this is cheap.
        result = ingest_eurlex(session, fetch_summaries=True)
        return {"source": "eurlex", **result}
    finally:
        session.close()


def _run_ingest_sec_edgar() -> dict:
    from sqlalchemy import select

    from app.models import Source
    from app.models.enums import ImplementationPhase, SourceType
    from app.services.ingestion.pipeline import IngestionPipeline

    CIK_MAP: dict[str, str] = {
        "Tesla":                 "0001318605",
        "General Motors":        "0001467858",
        "Ford Motor Company":    "0000037996",
        "Rivian Automotive":     "0001874178",
        "Lucid Group":           "0001811210",
        "Albemarle Corporation": "0000915779",
        "Freeport-McMoRan":      "0000831259",
        "MP Materials":          "0001820302",
        "Vale":                  "0001099509",
        "SQM":                   "0001009672",
        "Rio Tinto":             "0001045810",
        "BHP Group":             "0001306965",
    }
    cik_list = list(CIK_MAP.values())
    max_filings = 12

    session = get_session_factory()()
    try:
        source = session.scalar(
            select(Source).where(
                Source.source_type == SourceType.SEC_EDGAR.value,
                Source.is_active == True,  # noqa: E712
            ).limit(1)
        )
        if source is None:
            source = Source(
                name="SEC EDGAR — battery supply chain filers",
                source_type=SourceType.SEC_EDGAR.value,
                phase=ImplementationPhase.PHASE_1.value,
                is_active=True,
                config_json={"ciks": cik_list, "max_filings": max_filings},
            )
            session.add(source)
            session.flush()

        run_id = IngestionPipeline(session).run(
            source.id,
            params={"ciks": cik_list, "max_filings": max_filings},
        )
        return {"source": "sec_edgar", "ingestion_run_id": run_id, "ciks_processed": len(cik_list)}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Weekly ingestion jobs — Sunday night UTC
# ---------------------------------------------------------------------------

@inngest_client.create_function(
    fn_id="ingest-opensanctions-weekly",
    trigger=inngest.TriggerCron(cron="0 22 * * SUN"),
)
async def ingest_opensanctions_job(ctx: inngest.Context) -> dict:
    """Weekly OpenSanctions ingest — Sundays 22:00 UTC."""
    ctx.logger.info("ingestion_jobs.opensanctions.start")
    result = await asyncio.to_thread(_run_ingest_opensanctions)
    ctx.logger.info("ingestion_jobs.opensanctions.done", extra=result)
    return result


@inngest_client.create_function(
    fn_id="ingest-federal-register-weekly",
    trigger=inngest.TriggerCron(cron="30 22 * * SUN"),
)
async def ingest_federal_register_job(ctx: inngest.Context) -> dict:
    """Weekly Federal Register ingest — Sundays 22:30 UTC."""
    ctx.logger.info("ingestion_jobs.federal_register.start")
    result = await asyncio.to_thread(_run_ingest_federal_register)
    ctx.logger.info("ingestion_jobs.federal_register.done", extra=result)
    return result


@inngest_client.create_function(
    fn_id="ingest-worldbank-weekly",
    trigger=inngest.TriggerCron(cron="0 23 * * SUN"),
)
async def ingest_worldbank_job(ctx: inngest.Context) -> dict:
    """Weekly World Bank Pink Sheet ingest — Sundays 23:00 UTC.

    The interval gate in ingest_pink_sheet (min_interval_days=25) means this
    is a no-op in 3 out of 4 weekly runs — only the first run after the World
    Bank publishes new monthly data (~first week of month) does real work.
    """
    ctx.logger.info("ingestion_jobs.worldbank.start")
    result = await asyncio.to_thread(_run_ingest_worldbank)
    ctx.logger.info("ingestion_jobs.worldbank.done", extra=result)
    return result


# ---------------------------------------------------------------------------
# Quarterly ingestion jobs — 1st of Jan / Apr / Jul / Oct
# ---------------------------------------------------------------------------

@inngest_client.create_function(
    fn_id="ingest-comtrade-quarterly",
    trigger=inngest.TriggerCron(cron="0 0 1 1,4,7,10 *"),
)
async def ingest_comtrade_job(ctx: inngest.Context) -> dict:
    """Quarterly Comtrade ingest — 1st of Jan/Apr/Jul/Oct at 00:00 UTC."""
    ctx.logger.info("ingestion_jobs.comtrade.start")
    result = await asyncio.to_thread(_run_ingest_comtrade)
    ctx.logger.info("ingestion_jobs.comtrade.done", extra=result)
    return result


@inngest_client.create_function(
    fn_id="ingest-eurlex-quarterly",
    trigger=inngest.TriggerCron(cron="30 0 1 1,4,7,10 *"),
)
async def ingest_eurlex_job(ctx: inngest.Context) -> dict:
    """Quarterly EUR-Lex ingest — 1st of Jan/Apr/Jul/Oct at 00:30 UTC."""
    ctx.logger.info("ingestion_jobs.eurlex.start")
    result = await asyncio.to_thread(_run_ingest_eurlex)
    ctx.logger.info("ingestion_jobs.eurlex.done", extra=result)
    return result


@inngest_client.create_function(
    fn_id="ingest-sec-edgar-quarterly",
    trigger=inngest.TriggerCron(cron="0 1 1 1,4,7,10 *"),
)
async def ingest_sec_edgar_job(ctx: inngest.Context) -> dict:
    """Quarterly SEC EDGAR ingest — 1st of Jan/Apr/Jul/Oct at 01:00 UTC."""
    ctx.logger.info("ingestion_jobs.sec_edgar.start")
    result = await asyncio.to_thread(_run_ingest_sec_edgar)
    ctx.logger.info("ingestion_jobs.sec_edgar.done", extra=result)
    return result


# ---------------------------------------------------------------------------
# MRDS periodic reminder — fires 15th of Jan / Apr / Jul / Oct at 09:00 UTC.
#
# USGS MRDS is available as a bulk CSV download from mrdata.usgs.gov but has
# no fixed update schedule. This job logs a structured reminder so the ops
# team knows to re-run the ingester. Alternatively, omit --local-file and let
# the ingester download directly: `bdi-ingest ingest-mrds`.
# ---------------------------------------------------------------------------

@inngest_client.create_function(
    fn_id="mrds-refresh-reminder-quarterly",
    trigger=inngest.TriggerCron(cron="0 9 15 1,4,7,10 *"),
)
async def mrds_refresh_reminder_job(ctx: inngest.Context) -> dict:
    """Quarterly MRDS refresh reminder — 15th of Jan/Apr/Jul/Oct at 09:00 UTC.

    USGS MRDS does not publish on a fixed schedule. This reminder fires quarterly
    as a prompt to re-run the ingester and pick up any new or updated mine records.

    Two options:
      1. Live download (recommended — no manual step):
           bdi-ingest ingest-mrds
      2. Manual download then ingest:
           # Download from https://mrdata.usgs.gov/mrds/mrds-csv.zip
           bdi-ingest ingest-mrds --local-file /path/to/mrds.csv

    After ingesting, run rescore-market to pick up updated facility data in the
    operational scoring pillar.
    """
    import datetime as _dt

    ctx.logger.info(
        "ingestion_jobs.mrds_reminder.fired",
        extra={
            "action_required": "Re-run USGS MRDS ingester to refresh mine facility data",
            "download_url": "https://mrdata.usgs.gov/mrds/mrds-csv.zip",
            "cli_command": "bdi-ingest ingest-mrds",
            "fired_at": _dt.datetime.utcnow().isoformat(),
        },
    )
    return {
        "reminder": "mrds_refresh",
        "action_required": True,
        "download_url": "https://mrdata.usgs.gov/mrds/mrds-csv.zip",
    }


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

INGESTION_FUNCTIONS = [
    ingest_opensanctions_job,
    ingest_federal_register_job,
    ingest_worldbank_job,
    ingest_comtrade_job,
    ingest_eurlex_job,
    ingest_sec_edgar_job,
    mrds_refresh_reminder_job,
]

__all__ = [
    "INGESTION_FUNCTIONS",
    "ingest_opensanctions_job",
    "ingest_federal_register_job",
    "ingest_worldbank_job",
    "ingest_comtrade_job",
    "ingest_eurlex_job",
    "ingest_sec_edgar_job",
    "mrds_refresh_reminder_job",
    "_run_ingest_opensanctions",
    "_run_ingest_federal_register",
    "_run_ingest_worldbank",
    "_run_ingest_comtrade",
    "_run_ingest_eurlex",
    "_run_ingest_sec_edgar",
]
