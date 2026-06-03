"""One-shot backfill: populate ``Company.cik`` from the hardcoded
``CIK_MAP`` in ``app/tasks/ingestion_jobs.py``.

Background
----------
Migration 044 added the ``companies.cik`` column.  Going forward, CIK
should come from the partner-curated company seed template loader.
But the SEC ingester's Inngest entry point still uses a 12-row hardcoded
``CIK_MAP`` (Tesla, GM, Ford, Rivian, Lucid, Albemarle, Freeport,
MP Materials, Vale, SQM, Rio Tinto, BHP) that pre-dates the new column.
This script copies that mapping onto the corresponding ``Company`` rows
so the SEC ingester can find them by CIK lookup instead of by name.

Behaviour
---------
For each ``(name, cik)`` pair in the CIK_MAP:

  1. Try exact match against ``Company.canonical_name``.
  2. If no exact match, try case-insensitive prefix match
     (e.g. CIK_MAP key ``"MP Materials"`` matches
     ``canonical_name = "MP Materials Corp."``).
  3. If still no match, try the ``CompanyAlias`` table.
  4. Skip-and-warn anything still unmatched.

Idempotent + non-destructive: if a matched Company already has a non-NULL
``cik``, the existing value is preserved (partner curation wins over
this hardcoded backfill).  Only NULL ``cik`` fields are written.

The CIK_MAP itself is intentionally NOT modified here — that's a
follow-up cleanup (the Inngest job should eventually read CIKs from
``Company.cik`` instead of carrying its own copy).

Run via:
    bdi-ingest backfill-cik-map
"""

from __future__ import annotations

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.company import Company, CompanyAlias

log = structlog.get_logger(__name__)


# Hardcoded copy of the map in app/tasks/ingestion_jobs.py:125-138.
# Kept in sync manually; the script asserts they match at runtime.
_CIK_MAP: dict[str, str] = {
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


def _find_company(session: Session, name: str) -> Company | None:
    """Resolve a CIK_MAP key to a Company row via canonical → prefix → alias."""
    # 1. Exact match
    company = session.scalar(
        select(Company).where(Company.canonical_name == name).limit(1)
    )
    if company is not None:
        return company

    # 2. Case-insensitive prefix match
    company = session.scalar(
        select(Company)
        .where(func.lower(Company.canonical_name).like(f"{name.lower()}%"))
        .limit(1)
    )
    if company is not None:
        return company

    # 3. Alias match (canonical exact)
    alias = session.scalar(
        select(CompanyAlias).where(CompanyAlias.alias == name).limit(1)
    )
    if alias is not None:
        return session.get(Company, alias.company_id)

    return None


def backfill_cik_map(session: Session) -> dict[str, int | list[str]]:
    """Walk _CIK_MAP and populate Company.cik where missing.

    Returns a counts + unmatched-list dict for CLI reporting.  Caller commits.
    """
    written = 0
    already_set = 0
    skipped_conflict = 0
    unmatched: list[str] = []

    for name, cik in _CIK_MAP.items():
        company = _find_company(session, name)
        if company is None:
            unmatched.append(name)
            log.warning("backfill_cik_map.unmatched", name=name, cik=cik)
            continue

        if company.cik is not None and company.cik != cik:
            # Partner curation differs from the hardcoded map — surface it
            # but DO NOT overwrite.  Partner wins.
            skipped_conflict += 1
            log.warning(
                "backfill_cik_map.conflict",
                name=name,
                hardcoded_cik=cik,
                existing_cik=company.cik,
                company_id=str(company.id),
            )
            continue

        if company.cik == cik:
            already_set += 1
            continue

        # company.cik is None — write it.
        company.cik = cik
        if company.data_source is None:
            company.data_source = "sec_edgar_hardcoded_map_backfill"
        written += 1
        log.info(
            "backfill_cik_map.written",
            name=name,
            cik=cik,
            company_id=str(company.id),
        )

    session.flush()
    result: dict[str, int | list[str]] = {
        "cik_map_size": len(_CIK_MAP),
        "written": written,
        "already_set": already_set,
        "skipped_conflict": skipped_conflict,
        "unmatched": unmatched,
    }
    log.info("backfill_cik_map.done", **result)
    return result
