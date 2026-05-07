"""Scheduled ingestion jobs registered with Inngest.

Weekly jobs (Sunday night UTC, feeds Monday scoring):

- ``ingest-opensanctions-weekly``   — Sundays 22:00 UTC
- ``ingest-federal-register-weekly``— Sundays 22:30 UTC
- ``ingest-worldbank-weekly``       — Sundays 23:00 UTC

Quarterly jobs (1st of Jan / Apr / Jul / Oct):

- ``ingest-eurlex-quarterly``       — 1st of quarter, 00:30 UTC
- ``ingest-sec-edgar-quarterly``    — 1st of quarter, 01:00 UTC

Semi-annual auto-download (1st of Jan / Jul):

- ``ingest-mrds-semiannual``        — 1st of Jan/Jul, 09:00 UTC
                                      Auto-downloads the USGS MRDS bulk CSV.

Reminder-only jobs (manual upload required — no API for these sources):

- ``gta-refresh-reminder-quarterly``       — 1st of quarter, 09:00 UTC
- ``iea-policy-tracker-reminder-quarterly``— 1st of quarter, 09:30 UTC
- ``usgs-mcs-refresh-reminder-annual``     — 15 April, 09:00 UTC

These reminder jobs do NOT move data; they emit a structured WARNING log
prompting the partner to manually export from the source (GTA dashboard,
iea.org data tool, USGS MCS landing page) and run the corresponding
``bdi-ingest`` CLI command.  Tracked here so refresh cadence is visible
in the Inngest dashboard.

Comtrade cadence note:
    Daily backfill lives in ``scoring_jobs.py::ingest_comtrade_job`` (06:00 UTC
    daily, year × prefix × flow iteration).  Once that job logs
    ``comtrade_job.backfill_complete``, swap its cron from ``0 6 * * *`` to
    ``0 6 * * SUN`` (weekly) — Comtrade publishes annual data with a 4-6 month
    lag, so weekly is sufficient post-backfill.  No quarterly job here — the
    earlier ``ingest-comtrade-quarterly`` was deleted 2026-05-06 because it
    duplicated the daily job.

SEC EDGAR cadence note:
    10-K (annual) and 10-Q (quarterly) filings make quarterly ingestion the
    right cadence. Runs with ``max_filings=12`` to capture the most recent
    filing cycle per company.

All jobs:
  - Open a fresh SQLAlchemy session (never share a session across invocations).
  - Run synchronous ingestion functions inside ``asyncio.to_thread`` so the
    Inngest async handler does not block its event loop.
  - Always close the session in a ``finally`` block.
  - The interval gates in OpenSanctions and Pink Sheet ingesters prevent
    redundant full-file downloads — pass ``min_interval_days=0`` only if you
    need to force a re-run via manual trigger.
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
    """Inngest entry point — uses the dedicated ingest_sec_edgar module
    (May 2026 refactor; replaced the generic IngestionPipeline path so
    filings get MaterialCache material attribution + event_subtype on the
    typed column).  See app/services/ingestion/ingest_sec_edgar.py.
    """
    from app.services.ingestion.ingest_sec_edgar import ingest_sec_edgar

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

    session = get_session_factory()()
    try:
        result = ingest_sec_edgar(
            session, cik_list=cik_list, max_filings=12
        )
        return {"source": "sec_edgar", **result}
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

# Removed 2026-05-06: ``ingest-comtrade-quarterly`` was a duplicate of the
# daily ``ingest-comtrade-daily`` job in scoring_jobs.py.  Both registered
# Inngest functions, both called the same ingest_comtrade() entry point —
# the quarterly run on Jan/Apr/Jul/Oct 1st would compete with that day's
# daily run for rate-limit budget without adding new data.  The daily
# job's DB idempotency check + 429 circuit breaker are sufficient.


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
# MRDS auto-download — semi-annual real ingest (no manual step needed)
# ---------------------------------------------------------------------------
# USGS MRDS publishes the bulk CSV at a stable URL with no auth.  The
# ingester's local_file=None path streams it directly.  USGS doesn't have a
# fixed publish cadence, but the dataset is mostly stable (major updates
# happen 1-2× per year), so semi-annual on Jan + Jul 1st is plenty.  Auto-
# download is cheap (~30s) and idempotent — most runs will just confirm
# "no changes" via the ingester's dep_id dedup.
# ---------------------------------------------------------------------------

def _run_ingest_mrds() -> dict:
    """Sync wrapper that calls ingest_mrds with auto-download."""
    from app.services.ingestion.mrds import ingest_mrds

    session = get_session_factory()()
    try:
        # local_file=None → streams from MRDS_CSV_ZIP_URL (mrdata.usgs.gov).
        # Active-only filter (skip Past Producer + Prospect, see N4 fix
        # 2026-05-06) keeps the run to ~26k rows of the 304k file.
        result = ingest_mrds(session)
        return {"source": "mrds", **result}
    finally:
        session.close()


@inngest_client.create_function(
    fn_id="ingest-mrds-semiannual",
    trigger=inngest.TriggerCron(cron="0 9 1 1,7 *"),
)
async def ingest_mrds_job(ctx: inngest.Context) -> dict:
    """Semi-annual USGS MRDS ingest — 1st of Jan/Jul at 09:00 UTC.

    Auto-downloads from ``mrdata.usgs.gov/mrds/mrds-csv.zip``.  Idempotent
    via dep_id (existing rows update mutable fields; new rows insert).
    Replaces the prior reminder-only job (2026-05-06) since the ingester
    actually supports headless download — the reminder pattern was
    overcautious.
    """
    ctx.logger.info("ingestion_jobs.mrds.start")
    result = await asyncio.to_thread(_run_ingest_mrds)
    ctx.logger.info("ingestion_jobs.mrds.done", extra=result)
    return result


# ---------------------------------------------------------------------------
# Reminder-only jobs — manual upload required
# ---------------------------------------------------------------------------
# These three sources don't have public APIs we can hit headlessly:
#   * GTA  — bulk CSV download requires registration + API key (paid tier)
#   * IEA Policy Tracker — interactive web tool with manual CSV export
#   * USGS MCS — annual PDF/CSV release with no stable URL
#
# Reminder jobs log a structured WARNING so the partner has a paper trail
# of when each refresh was due.  They do NOT move data — partner must
# manually export and run the corresponding ``bdi-ingest`` command.
# ---------------------------------------------------------------------------

@inngest_client.create_function(
    fn_id="gta-refresh-reminder-quarterly",
    trigger=inngest.TriggerCron(cron="0 9 1 1,4,7,10 *"),
)
async def gta_refresh_reminder_job(ctx: inngest.Context) -> dict:
    """Quarterly GTA refresh reminder — 1st of Jan/Apr/Jul/Oct at 09:00 UTC.

    Global Trade Alert's bulk download URL (``data_extraction/download``)
    no longer works without registration.  Partner must manually export
    a curated CSV from https://globaltradealert.org/data-center
    (e.g. "Harmful Trade Policy Interventions: Batteries") and run::

        bdi-ingest ingest-gta --local-file /path/to/interventions.csv

    Quarterly cadence chosen because GTA tracks tariff/export-restriction
    activity that the geopolitical pillar reads — quarterly is a
    reasonable balance between staying current and partner workload.
    """
    fired_at = _dt.datetime.utcnow().isoformat()
    ctx.logger.warning(
        "ingestion_jobs.gta_reminder.fired",
        extra={
            "action_required": "Manually export GTA CSV + run ingest-gta",
            "download_page": "https://globaltradealert.org/data-center",
            "cli_command": "bdi-ingest ingest-gta --local-file /path/to/csv",
            "fired_at": fired_at,
        },
    )
    return {
        "reminder": "gta_refresh",
        "action_required": True,
        "download_page": "https://globaltradealert.org/data-center",
        "fired_at": fired_at,
    }


@inngest_client.create_function(
    fn_id="iea-policy-tracker-reminder-quarterly",
    trigger=inngest.TriggerCron(cron="30 9 1 1,4,7,10 *"),
)
async def iea_policy_tracker_reminder_job(ctx: inngest.Context) -> dict:
    """Quarterly IEA Policy Tracker reminder — 1st of Jan/Apr/Jul/Oct at 09:30 UTC.

    The IEA Critical Minerals Policy Tracker is an interactive web tool
    with no programmatic download.  Partner exports the CSV from
    https://www.iea.org/data-and-statistics/data-tools/critical-minerals-policy-tracker
    and runs::

        bdi-ingest ingest-iea-policy-tracker --file-path /path/to/csv

    Quarterly cadence matches IEA's typical update frequency for the
    tracker.
    """
    fired_at = _dt.datetime.utcnow().isoformat()
    ctx.logger.warning(
        "ingestion_jobs.iea_policy_tracker_reminder.fired",
        extra={
            "action_required": "Manually export IEA Policy Tracker CSV + run ingest-iea-policy-tracker",
            "download_page": (
                "https://www.iea.org/data-and-statistics/data-tools/"
                "critical-minerals-policy-tracker"
            ),
            "cli_command": "bdi-ingest ingest-iea-policy-tracker --file-path /path/to/csv",
            "fired_at": fired_at,
        },
    )
    return {
        "reminder": "iea_policy_tracker_refresh",
        "action_required": True,
        "fired_at": fired_at,
    }


@inngest_client.create_function(
    fn_id="usgs-mcs-refresh-reminder-annual",
    trigger=inngest.TriggerCron(cron="0 9 15 4 *"),
)
async def usgs_mcs_refresh_reminder_job(ctx: inngest.Context) -> dict:
    """Annual USGS MCS refresh reminder — 15 April at 09:00 UTC.

    USGS publishes the Mineral Commodity Summaries (MCS) once per year,
    typically late January / early February.  The mid-April fire date
    gives the team a 6–8 week buffer after publication to wait for any
    corrections, then prompts the manual ingest.

    Three CLI commands cover the MCS family — run all three after the
    new MCS publishes::

        bdi-ingest ingest-usgs <path-to-MCS<year>_World_Data.csv>
        bdi-ingest ingest-mcs-pdf <path-to-mcs<year>.pdf> --year <year>
        bdi-ingest ingest-mcs-prices <path-to-fig10.csv>

    All three are idempotent — re-running on the same files is safe.
    """
    fired_at = _dt.datetime.utcnow().isoformat()
    ctx.logger.warning(
        "ingestion_jobs.usgs_mcs_reminder.fired",
        extra={
            "action_required": (
                "Manually download new USGS MCS files + run ingest-usgs / "
                "ingest-mcs-pdf / ingest-mcs-prices"
            ),
            "download_page": "https://pubs.usgs.gov/publication/mcs",
            "cli_commands": [
                "bdi-ingest ingest-usgs <path>",
                "bdi-ingest ingest-mcs-pdf <path> --year <year>",
                "bdi-ingest ingest-mcs-prices <path>",
            ],
            "fired_at": fired_at,
        },
    )
    return {
        "reminder": "usgs_mcs_refresh",
        "action_required": True,
        "fired_at": fired_at,
    }


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

INGESTION_FUNCTIONS = [
    # Weekly auto-download
    ingest_opensanctions_job,
    ingest_federal_register_job,
    ingest_worldbank_job,
    # Quarterly auto-download
    ingest_eurlex_job,
    ingest_sec_edgar_job,
    # Semi-annual auto-download
    ingest_mrds_job,
    # Reminder-only (manual upload)
    gta_refresh_reminder_job,
    iea_policy_tracker_reminder_job,
    usgs_mcs_refresh_reminder_job,
    # Removed 2026-05-06: ingest_comtrade_job — duplicated the daily job in
    # scoring_jobs.py.  See removal note above the deleted decorator.
]

__all__ = [
    "INGESTION_FUNCTIONS",
    "ingest_opensanctions_job",
    "ingest_federal_register_job",
    "ingest_worldbank_job",
    "ingest_eurlex_job",
    "ingest_sec_edgar_job",
    "ingest_mrds_job",
    "gta_refresh_reminder_job",
    "iea_policy_tracker_reminder_job",
    "usgs_mcs_refresh_reminder_job",
    "_run_ingest_opensanctions",
    "_run_ingest_federal_register",
    "_run_ingest_worldbank",
    "_run_ingest_eurlex",
    "_run_ingest_sec_edgar",
    "_run_ingest_mrds",
]
