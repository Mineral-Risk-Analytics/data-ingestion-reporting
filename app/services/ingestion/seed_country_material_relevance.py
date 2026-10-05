"""Seed ``country_material_relevance`` from ``material_production_shares``
and ``is_major_consumer`` countries for the launch-list materials.

What this seeds
---------------
For each launch-list material (``app.services.scoring.launch_list``):

  Producer rows — auto-derived from ``material_production_shares``
    Picks the latest reference year per material and creates one row
    per producing country (any row with production_share > 0).

      tier='top'      when production_share >= 0.30
      tier='mid'      when 0.10 <= production_share < 0.30
      tier='minor'    when 0.01 <= production_share < 0.10

      is_hcg=TRUE     when production_share >= 0.40

      source='mcs_share'
      derived_share = production_share
      reference_year = the year used

    Countries with shares < 1% are skipped — they're noise.  Partner can
    promote them to ``tier='emerging'`` manually if there's a reason.

  Consumer rows — partner-curated placeholders
    For every country with ``is_major_consumer=TRUE`` AND
    ``comtrade_code IS NOT NULL`` (i.e. the consumers we can actually
    ingest Comtrade imports for), one row per launch material gets
    written with:

      tier='minor'
      is_hcg=FALSE
      source='partner_curated'
      notes='Initial placeholder — refine tier after Comtrade import
              backfill completes (see country_material_relevance Phase 2)'

    The consumer side does not have an MCS-equivalent share source.
    These rows sit at tier='minor' until either partner curates them
    or Phase 2 wires a Comtrade-derived consumer-share calculation.

Idempotency
-----------
``ON CONFLICT (material_id, country_code, role) DO UPDATE`` — re-runs
refresh the derived fields (``tier``, ``is_hcg``, ``derived_share``,
``reference_year``) WHEN ``source='mcs_share'``.  Rows with
``source='partner_curated'`` are LEFT ALONE on re-seed so partner edits
survive subsequent MCS vintage updates.  Force-override via
``--force-update`` (rewrites all rows including partner-curated ones).
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.country import Country
from app.models.supply import (
    CountryMaterialRelevance,
    Material,
    MaterialProductionShare,
)
from app.services.scoring.launch_list import LAUNCH_LIST_CANONICAL_NAMES

log = structlog.get_logger(__name__)


# Thresholds (confirmed with partner 2026-06)
_TIER_TOP_THRESHOLD = 0.30
_TIER_MID_THRESHOLD = 0.10
_TIER_MINOR_THRESHOLD = 0.01
_HCG_THRESHOLD = 0.40


def _share_to_tier(share: float) -> str | None:
    """Bucket a production share into the relevance tier."""
    if share >= _TIER_TOP_THRESHOLD:
        return "top"
    if share >= _TIER_MID_THRESHOLD:
        return "mid"
    if share >= _TIER_MINOR_THRESHOLD:
        return "minor"
    return None  # below noise floor; row is skipped


def _seed_producers(
    session: Session,
    material: Material,
    *,
    force_update: bool,
) -> dict[str, int]:
    """Auto-seed producer rows for one material from material_production_shares.

    Picks the latest reference_year that has data for this material.
    """
    # Latest year for this material
    latest_year = session.scalar(
        select(MaterialProductionShare.reference_year)
        .where(MaterialProductionShare.material_id == material.id)
        .order_by(MaterialProductionShare.reference_year.desc())
        .limit(1)
    )
    if latest_year is None:
        log.warning(
            "country_material_relevance.seed.no_production_data",
            material=material.canonical_name,
            note="No material_production_shares rows — run ingest-usgs first.",
        )
        return {"inserted": 0, "updated": 0, "below_threshold": 0}

    rows = session.scalars(
        select(MaterialProductionShare)
        .where(
            MaterialProductionShare.material_id == material.id,
            MaterialProductionShare.reference_year == latest_year,
        )
    ).all()

    # Pre-pass: snapshot which (country_code, role='producer') already
    # exist so we can distinguish new vs existing rows after the upsert.
    pre_existing = set(
        session.scalars(
            select(CountryMaterialRelevance.country_code).where(
                CountryMaterialRelevance.material_id == material.id,
                CountryMaterialRelevance.role == "producer",
            )
        ).all()
    )

    upserts = 0
    new_rows = 0
    refreshed = 0
    skipped_partner_curated = 0
    below_threshold = 0

    for share_row in rows:
        share = float(share_row.production_share or 0.0)
        tier = _share_to_tier(share)
        if tier is None:
            below_threshold += 1
            continue

        is_hcg = share >= _HCG_THRESHOLD
        was_pre_existing = share_row.country_code in pre_existing

        values = {
            "material_id": material.id,
            "country_code": share_row.country_code,
            "role": "producer",
            "tier": tier,
            "is_hcg": is_hcg,
            "source": "mcs_share",
            "derived_share": share,
            "reference_year": latest_year,
        }

        set_clause: dict[str, Any] = {
            "tier": values["tier"],
            "is_hcg": values["is_hcg"],
            "derived_share": values["derived_share"],
            "reference_year": values["reference_year"],
            "source": "mcs_share",
        }
        # Partner-curated rows survive re-seed unless --force-update.
        # MCS-share-sourced rows always refresh.
        if force_update:
            stmt = pg_insert(CountryMaterialRelevance).values(**values).on_conflict_do_update(
                constraint="uq_country_material_relevance",
                set_=set_clause,
            )
        else:
            stmt = pg_insert(CountryMaterialRelevance).values(**values).on_conflict_do_update(
                constraint="uq_country_material_relevance",
                set_=set_clause,
                where=CountryMaterialRelevance.source == "mcs_share",
            )

        session.execute(stmt)
        upserts += 1
        if not was_pre_existing:
            new_rows += 1
        else:
            # Verify the upsert actually applied by reading source back.
            current_source = session.scalar(
                select(CountryMaterialRelevance.source).where(
                    CountryMaterialRelevance.material_id == material.id,
                    CountryMaterialRelevance.country_code == share_row.country_code,
                    CountryMaterialRelevance.role == "producer",
                )
            )
            if current_source == "mcs_share":
                refreshed += 1
            else:
                # Existing row was partner_curated; WHERE clause suppressed update.
                skipped_partner_curated += 1

    return {
        "attempted": upserts,
        "new_rows": new_rows,
        "refreshed_existing": refreshed,
        "skipped_partner_curated": skipped_partner_curated,
        "below_threshold": below_threshold,
    }


def _seed_consumers(
    session: Session,
    material: Material,
    consumer_countries: list[Country],
    *,
    force_update: bool,
) -> dict[str, int]:
    """Seed consumer placeholder rows for one material × all major consumers."""
    pre_existing = set(
        session.scalars(
            select(CountryMaterialRelevance.country_code).where(
                CountryMaterialRelevance.material_id == material.id,
                CountryMaterialRelevance.role == "consumer",
            )
        ).all()
    )

    attempted = 0
    new_rows = 0
    skipped = 0

    for country in consumer_countries:
        values = {
            "material_id": material.id,
            "country_code": country.iso2,
            "role": "consumer",
            "tier": "minor",
            "is_hcg": False,
            "source": "partner_curated",
            "derived_share": None,
            "reference_year": None,
            "notes": (
                "Initial placeholder. Refine tier after Comtrade import "
                "backfill completes (see country_material_relevance Phase 2)."
            ),
        }
        was_pre_existing = country.iso2 in pre_existing

        # Consumer rows are ALWAYS partner_curated.  On re-seed they're
        # left alone unless --force-update.
        if force_update:
            stmt = pg_insert(CountryMaterialRelevance).values(**values).on_conflict_do_update(
                constraint="uq_country_material_relevance",
                set_={"tier": "minor", "notes": values["notes"]},
            )
        else:
            stmt = pg_insert(CountryMaterialRelevance).values(**values).on_conflict_do_nothing(
                constraint="uq_country_material_relevance",
            )

        session.execute(stmt)
        attempted += 1
        if not was_pre_existing:
            new_rows += 1
        else:
            skipped += 1

    return {
        "attempted": attempted,
        "new_rows": new_rows,
        "skipped_existing": skipped,
    }


def run_seed(session: Session, *, force_update: bool = False) -> dict[str, Any]:
    """Run the full seed.  Returns per-material counters.

    Args:
        session:       SQLAlchemy session (caller commits).
        force_update:  When True, partner-curated rows are overwritten
                       too.  Default False preserves partner edits.

    Returns:
        Dict with overall + per-material counts of inserted/updated/skipped
        rows for both producer and consumer roles.
    """
    # Resolve launch-list materials
    materials = session.scalars(
        select(Material).where(
            Material.canonical_name.in_(LAUNCH_LIST_CANONICAL_NAMES)
        )
    ).all()
    found_names = {m.canonical_name for m in materials}
    missing = [n for n in LAUNCH_LIST_CANONICAL_NAMES if n not in found_names]
    if missing:
        log.warning(
            "country_material_relevance.seed.missing_materials",
            missing=missing,
            note="Run seed-materials first.",
        )

    # Major consumers with a Comtrade code (the ones we can actually fetch)
    consumer_countries = session.scalars(
        select(Country)
        .where(Country.is_major_consumer.is_(True))
        .where(Country.comtrade_code.is_not(None))
    ).all()

    log.info(
        "country_material_relevance.seed.start",
        launch_materials=len(materials),
        consumer_countries=len(consumer_countries),
        force_update=force_update,
    )

    per_material: dict[str, dict[str, Any]] = {}
    total_producer_new = 0
    total_producer_refreshed = 0
    total_producer_skipped_partner = 0
    total_producer_below = 0
    total_consumer_new = 0
    total_consumer_skipped = 0

    for material in materials:
        prod = _seed_producers(session, material, force_update=force_update)
        cons = _seed_consumers(session, material, consumer_countries, force_update=force_update)
        per_material[material.canonical_name] = {
            "producer": prod,
            "consumer": cons,
        }
        total_producer_new += prod["new_rows"]
        total_producer_refreshed += prod["refreshed_existing"]
        total_producer_skipped_partner += prod["skipped_partner_curated"]
        total_producer_below += prod["below_threshold"]
        total_consumer_new += cons["new_rows"]
        total_consumer_skipped += cons["skipped_existing"]

    session.commit()

    summary = {
        "materials_seeded": len(materials),
        "materials_missing": missing,
        "producer_rows_new": total_producer_new,
        "producer_rows_refreshed": total_producer_refreshed,
        "producer_rows_skipped_partner_curated": total_producer_skipped_partner,
        "producer_rows_below_threshold": total_producer_below,
        "consumer_rows_new": total_consumer_new,
        "consumer_rows_skipped_existing": total_consumer_skipped,
        "by_material": per_material,
    }
    log.info("country_material_relevance.seed.done", **{
        k: v for k, v in summary.items() if k != "by_material"
    })
    return summary


__all__ = ["run_seed"]
