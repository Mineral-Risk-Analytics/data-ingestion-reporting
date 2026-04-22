"""Ingest vehicle model data from the NHTSA vPIC API.

vPIC (Vehicle Product Information Catalog) provides officially registered
make/model names per model year. It does not carry battery, chemistry, or
EV-specific data, so ``vehicle_model_chemistries`` rows are NOT written here.

The service maps corporate OEM ``canonical_name`` values to one or more vPIC
make names (e.g. "Volkswagen Group" → ["VOLKSWAGEN", "AUDI", "PORSCHE"]).
For each make it:

1. Fetches all models via ``GetModelsForMake``.
2. Scans ``GetModelsForMakeYear`` across a year range (default 2010–present) to
   determine ``model_year_start`` and ``model_year_end`` for every model.
3. Upserts rows into ``company_vehicle_models`` on the unique key
   ``(company_id, model_name, model_year_start)``.

Geely Auto Group and Zeekr are not registered in vPIC and are skipped with a
warning.

Run order:
    bdi-ingest seed-companies    # companies must exist first
    bdi-ingest ingest-vpic       # this module
    bdi-ingest rescore-all       # picks up new vehicle model data
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Optional
from urllib.parse import quote

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.vehicle import CompanyVehicleModel

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VPIC_BASE = "https://vpic.nhtsa.dot.gov/api/vehicles"
DATA_SOURCE = "vpic.nhtsa.dot.gov"
CURRENT_YEAR = date.today().year

# Corporate OEM canonical_name → list of vPIC make names.
# Make names must match vPIC exactly (case-insensitive in the API but kept
# upper-case here for clarity). Companies absent from vPIC are omitted or
# mapped to an empty list and logged as warnings.
OEM_MAKE_MAP: dict[str, list[str]] = {
    "Tesla": ["TESLA"],
    "Volkswagen Group": ["VOLKSWAGEN", "AUDI", "PORSCHE"],
    "BMW Group": ["BMW"],
    "General Motors": ["CHEVROLET", "GMC", "CADILLAC", "BUICK"],
    "Ford Motor Company": ["FORD", "LINCOLN"],
    "Hyundai Motor Company": ["HYUNDAI"],
    "Stellantis": ["DODGE", "CHRYSLER", "JEEP", "RAM"],
    "Mercedes-Benz Group": ["MERCEDES-BENZ"],
    "Toyota Motor Corporation": ["TOYOTA", "LEXUS"],
    "Honda Motor Company": ["HONDA", "ACURA"],
    "Nissan Motor Company": ["NISSAN", "INFINITI"],
    "Subaru Corporation": ["SUBARU"],
    "Rivian Automotive": ["RIVIAN"],
    "Lucid Group": ["LUCID"],
    "Volvo Car Group": ["VOLVO"],
    "Polestar Automotive": ["POLESTAR"],
    "Kia Corporation": ["KIA"],
    # Geely Auto Group and Zeekr are not registered in vPIC — skipped.
    "Geely Auto Group": [],
    "Zeekr": [],
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _fetch_json(
    url: str,
    retries: int = 3,
    backoff_base: float = 5.0,
    rate_limit_delay: float = 0.0,
) -> list[dict[str, Any]]:
    """GET *url*, return the ``Results`` list from the vPIC JSON envelope.

    Retries on HTTP 429 or 5xx with exponential back-off. Returns an empty
    list on permanent failure (after exhausting retries).
    """
    if rate_limit_delay:
        time.sleep(rate_limit_delay)

    for attempt in range(retries):
        try:
            resp = httpx.get(url, timeout=30.0)
        except httpx.RequestError as exc:
            log.warning("vpic_request_error", url=url, attempt=attempt, error=str(exc))
            if attempt < retries - 1:
                time.sleep(backoff_base * (2**attempt))
            continue

        if resp.status_code == 429:
            sleep = backoff_base * (2**attempt)
            log.warning("vpic_rate_limited", url=url, attempt=attempt, sleep=sleep)
            time.sleep(sleep)
            continue

        if resp.status_code >= 500:
            sleep = backoff_base * (2**attempt)
            log.warning(
                "vpic_server_error",
                url=url,
                status=resp.status_code,
                attempt=attempt,
                sleep=sleep,
            )
            time.sleep(sleep)
            continue

        if resp.status_code != 200:
            log.warning("vpic_unexpected_status", url=url, status=resp.status_code)
            return []

        try:
            data = resp.json()
        except Exception as exc:
            log.warning("vpic_json_decode_error", url=url, error=str(exc))
            return []

        return data.get("Results") or []

    log.error("vpic_exhausted_retries", url=url, retries=retries)
    return []


def get_models_for_make(
    make_name: str,
    rate_limit_delay: float = 0.0,
) -> list[dict[str, Any]]:
    """Return all models registered under *make_name* in vPIC.

    Each entry has at least ``Make_ID``, ``Make_Name``, ``Model_ID``,
    ``Model_Name``.
    """
    url = f"{VPIC_BASE}/GetModelsForMake/{quote(make_name)}?format=json"
    results = _fetch_json(url, rate_limit_delay=rate_limit_delay)
    log.debug("vpic_models_for_make", make=make_name, count=len(results))
    return results


def get_models_for_make_year(
    make_name: str,
    year: int,
    rate_limit_delay: float = 0.0,
) -> set[str]:
    """Return the set of model names registered for *make_name* in *year*."""
    url = (
        f"{VPIC_BASE}/GetModelsForMakeYear/make/{quote(make_name)}"
        f"/modelyear/{year}?format=json"
    )
    results = _fetch_json(url, rate_limit_delay=rate_limit_delay)
    return {r["Model_Name"] for r in results if r.get("Model_Name")}


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def derive_year_windows(
    make_name: str,
    model_names: list[str],
    year_start: int,
    year_end: int,
    rate_limit_delay: float = 0.0,
) -> dict[str, tuple[int | None, int | None]]:
    """Scan *year_start*–*year_end* to find the first/last year each model appears.

    Returns a dict mapping ``model_name`` →
    ``(model_year_start, model_year_end)`` where ``model_year_end`` is
    ``None`` when the model is still present in the latest year scanned.

    API calls: (year_end - year_start + 1) calls for *make_name*.
    """
    # year → set of model names present that year
    year_presence: dict[int, set[str]] = {}

    for year in range(year_start, year_end + 1):
        present = get_models_for_make_year(
            make_name, year, rate_limit_delay=rate_limit_delay
        )
        year_presence[year] = present
        log.debug(
            "vpic_year_scan",
            make=make_name,
            year=year,
            models_present=len(present),
        )

    target_set = set(model_names)
    windows: dict[str, tuple[int | None, int | None]] = {}

    for model in target_set:
        years_seen = sorted(y for y, present in year_presence.items() if model in present)
        if not years_seen:
            # Model exists in GetModelsForMake but not in any year scan — use
            # None for both ends (store it but mark as unresolved).
            windows[model] = (None, None)
        else:
            first = years_seen[0]
            last = years_seen[-1]
            # If the model was present in the latest year we scanned, treat
            # model_year_end as open (still in production).
            end = None if last == year_end else last
            windows[model] = (first, end)

    return windows


# ---------------------------------------------------------------------------
# DB upsert
# ---------------------------------------------------------------------------


def upsert_models(
    session: Session,
    company: Company,
    make_name: str,
    make_id: int,
    windows: dict[str, tuple[int | None, int | None]],
    model_id_map: dict[str, int],
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Upsert ``CompanyVehicleModel`` rows for one make.

    Returns ``(inserted, updated, skipped)``.
    """
    inserted = updated = skipped = 0

    for model_name, (year_start, year_end) in windows.items():
        # Skip models that never appeared in any year scan — model_year_start
        # would be NULL which makes the unique key ambiguous and the row
        # effectively useless for scoring.
        if year_start is None:
            log.debug(
                "vpic_skip_no_year_start",
                company=company.canonical_name,
                make=make_name,
                model=model_name,
            )
            skipped += 1
            continue

        is_active = year_end is None or year_end >= CURRENT_YEAR

        # Wrap the SELECT in no_autoflush so pending objects from earlier in
        # this make's loop don't trigger an unexpected flush mid-lookup, which
        # can cause StaleDataError (gkpj) when NULL is in the identity key.
        with session.no_autoflush:
            existing = session.scalar(
                select(CompanyVehicleModel).where(
                    CompanyVehicleModel.company_id == company.id,
                    CompanyVehicleModel.model_name == model_name,
                    CompanyVehicleModel.model_year_start == year_start,
                )
            )

        desired_meta: dict[str, Any] = {
            "vpic_make_id": make_id,
            "vpic_model_id": model_id_map.get(model_name),
            "vpic_make_name": make_name,
        }

        if existing is None:
            if not dry_run:
                row = CompanyVehicleModel(
                    company_id=company.id,
                    model_name=model_name,
                    model_year_start=year_start,
                    model_year_end=year_end,
                    is_active=is_active,
                    data_source=DATA_SOURCE,
                    metadata_json=desired_meta,
                )
                session.add(row)
            inserted += 1
            log.debug(
                "vpic_insert",
                company=company.canonical_name,
                make=make_name,
                model=model_name,
                year_start=year_start,
                year_end=year_end,
                dry_run=dry_run,
            )
        else:
            changed = False
            if existing.model_year_end != year_end:
                if not dry_run:
                    existing.model_year_end = year_end
                changed = True
            if existing.is_active != is_active:
                if not dry_run:
                    existing.is_active = is_active
                changed = True
            # Preserve richer existing metadata (e.g. from ev-database);
            # only update vpic_* keys so chemistry data is never clobbered.
            existing_meta: dict = existing.metadata_json or {}
            meta_update = {**existing_meta, **desired_meta}
            if existing_meta != meta_update:
                if not dry_run:
                    existing.metadata_json = meta_update
                changed = True
            # Always refresh data_source to reflect vPIC augmented it, unless
            # an ev-database row already has richer data — keep both sources.
            if existing.data_source and DATA_SOURCE not in existing.data_source:
                new_source = f"{existing.data_source},{DATA_SOURCE}"
                if not dry_run:
                    existing.data_source = new_source
                changed = True
            elif existing.data_source is None:
                if not dry_run:
                    existing.data_source = DATA_SOURCE
                changed = True

            if changed:
                updated += 1
            else:
                skipped += 1

    return inserted, updated, skipped


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(
    session: Session,
    companies: Optional[list[str]] = None,
    year_start: int = 2010,
    year_end: Optional[int] = None,
    dry_run: bool = False,
    rate_limit_delay: float = 0.5,
) -> dict[str, Any]:
    """Fetch vPIC data and upsert ``company_vehicle_models`` rows.

    Args:
        session: SQLAlchemy session (caller is responsible for commit/rollback).
        companies: Canonical company names to process. Defaults to all OEMs in
            ``OEM_MAKE_MAP``.
        year_start: First model year to scan (inclusive). Default 2010.
        year_end: Last model year to scan (inclusive). Defaults to current year.
        dry_run: Parse and log without writing to the DB.
        rate_limit_delay: Seconds to sleep between every vPIC API call.

    Returns:
        Summary dict with ``inserted``, ``updated``, ``skipped``,
        ``companies_not_found``, ``makes_skipped``, ``makes_processed``.
    """
    if year_end is None:
        year_end = CURRENT_YEAR

    target_names: list[str] = companies if companies else list(OEM_MAKE_MAP.keys())

    totals: dict[str, Any] = {
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "companies_not_found": [],
        "makes_skipped": [],
        "makes_processed": 0,
    }

    for canonical_name in target_names:
        make_names = OEM_MAKE_MAP.get(canonical_name)
        if make_names is None:
            log.warning("vpic_unknown_company", canonical_name=canonical_name)
            continue
        if not make_names:
            log.info(
                "vpic_company_not_in_vpic",
                canonical_name=canonical_name,
                reason="no vPIC makes mapped",
            )
            totals["makes_skipped"].append(canonical_name)
            continue

        company = session.scalar(
            select(Company).where(Company.canonical_name == canonical_name)
        )
        if company is None:
            log.warning("vpic_company_not_found", canonical_name=canonical_name)
            totals["companies_not_found"].append(canonical_name)
            continue

        for make_name in make_names:
            log.info(
                "vpic_processing_make",
                company=canonical_name,
                make=make_name,
                year_start=year_start,
                year_end=year_end,
            )

            raw_models = get_models_for_make(make_name, rate_limit_delay=rate_limit_delay)
            if not raw_models:
                log.warning("vpic_no_models_found", make=make_name)
                totals["makes_skipped"].append(make_name)
                continue

            make_id: int = raw_models[0].get("Make_ID", 0)
            model_id_map: dict[str, int] = {
                r["Model_Name"]: r["Model_ID"]
                for r in raw_models
                if r.get("Model_Name") and r.get("Model_ID")
            }
            model_names = list(model_id_map.keys())

            windows = derive_year_windows(
                make_name,
                model_names,
                year_start,
                year_end,
                rate_limit_delay=rate_limit_delay,
            )

            ins, upd, skp = upsert_models(
                session,
                company,
                make_name,
                make_id,
                windows,
                model_id_map,
                dry_run=dry_run,
            )

            # Flush after each make so pending objects are written to the DB
            # before the next make's SELECT queries run. This prevents
            # StaleDataError (gkpj) caused by the identity map holding
            # unflushed rows across make iterations. expire_all() clears
            # the local attribute cache so the next iteration re-reads fresh
            # state from the DB.
            if not dry_run:
                session.flush()
                session.expire_all()

            log.info(
                "vpic_make_done",
                company=canonical_name,
                make=make_name,
                inserted=ins,
                updated=upd,
                skipped=skp,
            )
            totals["inserted"] += ins
            totals["updated"] += upd
            totals["skipped"] += skp
            totals["makes_processed"] += 1

    if not dry_run:
        session.commit()

    log.info("vpic_run_complete", **{k: v for k, v in totals.items()})
    return totals
