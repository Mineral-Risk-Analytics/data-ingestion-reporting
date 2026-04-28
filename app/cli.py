"""CLI for ingestion and seed (`python -m app.cli` or `bdi-ingest`)."""

from __future__ import annotations

import json
from typing import Optional

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import configure_logging
from app.db.seed import seed_if_empty
from app.db.session import get_session_factory
from app.models import Source
from app.models.enums import SourceType
from app.services.ingestion.pipeline import IngestionPipeline

configure_logging()
app = typer.Typer(no_args_is_help=True)


def _session() -> Session:
    return get_session_factory()()


@app.command("seed")
def seed_cmd() -> None:
    """Load reference countries, materials, suppliers, aliases, and sources."""
    s = _session()
    try:
        stats = seed_if_empty(s)
        typer.echo(json.dumps({"ok": True, "inserted": stats}, indent=2))
    finally:
        s.close()


@app.command("ingest")
def ingest_cmd(
    source: str = typer.Argument(
        ...,
        help="federal-register | census-trade | sec-edgar | news",
    ),
    extra_json: Optional[str] = typer.Option(
        None,
        "--params",
        help='JSON object merged into source config, e.g. \'{"per_page":5}\'',
    ),
) -> None:
    """Run a Phase 1 ingestion pipeline by source alias."""
    mapping = {
        "federal-register": SourceType.FEDERAL_REGISTER.value,
        "census-trade": SourceType.CENSUS_TRADE.value,
        "sec-edgar": SourceType.SEC_EDGAR.value,
        "news": SourceType.NEWS.value,
    }
    st = mapping.get(source.replace("_", "-"))
    if not st:
        typer.echo(f"Unknown source {source!r}. Choose from {list(mapping.keys())}", err=True)
        raise typer.Exit(code=1)

    params = json.loads(extra_json) if extra_json else None

    s = _session()
    try:
        row = s.execute(select(Source).where(Source.source_type == st).limit(1)).scalar_one_or_none()
        if row is None:
            typer.echo("No matching Source row — run `seed` first.", err=True)
            raise typer.Exit(code=1)
        run_id = IngestionPipeline(s).run(row.id, params=params or {})
        typer.echo(json.dumps({"ok": True, "ingestion_run_id": run_id, "source_id": row.id}))
    finally:
        s.close()


@app.command("ingest-usgs")
def ingest_usgs_cmd(
    filepath: str = typer.Argument(
        ...,
        help="Path to MCS2025_World_Data.csv downloaded from https://pubs.usgs.gov/publication/mcs2025",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Upsert: update existing materials even if they already exist.",
    ),
    mcs_year: int = typer.Option(
        2025,
        "--mcs-year",
        help="Publication year of the MCS file (used as reference_year in material_criticality_signals).",
    ),
) -> None:
    """Ingest USGS Mineral Commodity Summaries data from the official CSV.

    Derives primary_producing_countries (ranked by mine production) and
    criticality_score (normalised HHI) directly from USGS data.
    No synthetic values. Re-run annually when USGS publishes a new MCS.

    Also writes source=usgs_mcs rows into material_criticality_signals and
    syncs the denormalized materials.patent_occurrence_trend cache.
    """
    import sqlalchemy as sa
    from pathlib import Path
    from app.models.criticality_signal import MaterialCriticalitySignal
    from app.models.supply import Material
    from app.services.ingestion.seeds.usgs_mcs_parser import parse_usgs_csv

    path = Path(filepath)
    if not path.exists():
        typer.echo(f"File not found: {filepath}", err=True)
        raise typer.Exit(code=1)

    materials = parse_usgs_csv(path)
    if not materials:
        typer.echo("No materials parsed — check the file format.", err=True)
        raise typer.Exit(code=1)

    s = _session()
    try:
        existing_count = s.scalar(
            select(sa.func.count()).select_from(Material)
        ) or 0

        if existing_count > 0 and not force:
            typer.echo(json.dumps({
                "ok": False,
                "reason": f"materials table already has {existing_count} rows. Use --force to upsert.",
            }, indent=2))
            raise typer.Exit(code=1)

        from app.models.supply import MaterialProductionShare

        inserted = updated = signals_written = 0
        shares_written = 0
        for m in materials:
            # Strip internal keys before creating ORM objects.
            hhi_score = m.pop("_hhi_score", None)
            production_shares = m.pop("_production_shares", [])

            row = s.scalar(select(Material).where(Material.canonical_name == m["canonical_name"]))
            if row is None:
                row = Material(**m)
                s.add(row)
                s.flush()
                inserted += 1
            elif force:
                for k, v in m.items():
                    if k != "canonical_name":
                        setattr(row, k, v)
                s.flush()
                updated += 1

            # Write / upsert a material_criticality_signals row for this material.
            # Idempotent: if the row exists, update criticality_score and hhi_score.
            existing_signal = s.scalar(
                select(MaterialCriticalitySignal).where(
                    MaterialCriticalitySignal.material_id == row.id,
                    MaterialCriticalitySignal.source == "usgs_mcs",
                    MaterialCriticalitySignal.reference_year == mcs_year,
                )
            )
            trend = m.get("patent_occurrence_trend")
            if existing_signal is None:
                s.add(MaterialCriticalitySignal(
                    material_id=row.id,
                    source="usgs_mcs",
                    reference_year=mcs_year,
                    criticality_score=row.criticality_score,
                    trend_direction=trend,
                    hhi_score=hhi_score,
                    metadata_json={"mcs_publication_year": mcs_year},
                ))
                signals_written += 1
            elif force:
                existing_signal.criticality_score = row.criticality_score
                existing_signal.hhi_score = hhi_score
                existing_signal.trend_direction = trend
                signals_written += 1

            # Upsert production share rows for each producing country.
            for share in production_shares:
                existing_share = s.scalar(
                    select(MaterialProductionShare).where(
                        MaterialProductionShare.material_id == row.id,
                        MaterialProductionShare.country_code == share["country_code"],
                        MaterialProductionShare.reference_year == mcs_year,
                    )
                )
                if existing_share is None:
                    s.add(MaterialProductionShare(
                        material_id=row.id,
                        country_code=share["country_code"],
                        reference_year=mcs_year,
                        production_volume=share["production_volume"],
                        production_share=share["production_share"],
                        unit_of_measure=share.get("unit_of_measure"),
                        data_source="usgs_mcs",
                    ))
                    shares_written += 1
                elif force:
                    existing_share.production_volume = share["production_volume"]
                    existing_share.production_share = share["production_share"]
                    existing_share.unit_of_measure = share.get("unit_of_measure")
                    shares_written += 1

        s.commit()
        typer.echo(json.dumps({
            "ok": True,
            "source": f"USGS Mineral Commodity Summaries {mcs_year}",
            "inserted": inserted,
            "updated": updated,
            "signals_written": signals_written,
            "shares_written": shares_written,
            "materials": [m["canonical_name"] for m in materials],
        }, indent=2))
    finally:
        s.close()


@app.command("seed-materials")
def seed_materials_cmd() -> None:
    """Seed non-USGS materials (individual motor REEs + Sodium) and battery chemistry
    junction data (battery_chemistry_materials).

    Run this AFTER ingest-usgs has populated the materials table and after the
    002_battery_chemistry migration has been applied.

    Idempotent — safe to run multiple times.
    """
    from app.services.ingestion.seed_materials import run_seed

    s = _session()
    try:
        result = run_seed(s)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("seed-hs-mappings")
def seed_hs_mappings_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        help="Drop and re-seed all HS mappings (use after HS nomenclature update).",
    ),
) -> None:
    """Seed the hs_code_material_mappings table from the curated reference list.

    Idempotent without --force: skips existing (hs_code_prefix, material_id) pairs.
    With --force: deletes all existing rows and re-inserts from scratch.

    Re-run when:
    - New materials are added to the materials table
    - WCO updates the HS nomenclature (every 5 years)
    - A material's HS code classification changes

    Note: materials must be seeded first (run seed-materials or ingest-usgs).
    """
    import sqlalchemy as sa
    from app.services.ingestion.seed_hs_mappings import upsert_hs_mappings
    from app.models.supply import HsCodeMaterialMapping

    s = _session()
    try:
        if force:
            s.execute(sa.delete(HsCodeMaterialMapping))
            s.commit()
            typer.echo("Cleared existing HS mappings.")

        result = upsert_hs_mappings(s)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("rescore-chemistry")
def rescore_chemistry_cmd(
    slug: Optional[str] = typer.Option(
        None,
        "--slug",
        help="Chemistry slug to rescore (e.g. 'lfp', 'nmc'). Default: all active chemistries.",
    ),
    as_of: Optional[str] = typer.Option(
        None,
        "--as-of",
        help="Point-in-time date for composition lookup (YYYY-MM-DD). Default: today.",
    ),
) -> None:
    """Compute and persist chemistry_risk_scores for battery chemistries.

    For each chemistry, fetches active material composition, resolves criticality
    signals, computes intensity-weighted risk scores, and persists a ChemistryRiskScore row.

    Re-run after: ingest-usgs, seed-materials, or whenever new criticality signals arrive.
    """
    import datetime
    from app.services.scoring.chemistry_risk import rescore_all_chemistries, rescore_one_chemistry
    from app.models.battery_chemistry import BatteryChemistry

    as_of_date = datetime.date.fromisoformat(as_of) if as_of else datetime.date.today()

    s = _session()
    try:
        if slug:
            chem = s.scalar(select(BatteryChemistry).where(BatteryChemistry.slug == slug))
            if chem is None:
                typer.echo(json.dumps({"ok": False, "error": f"Chemistry slug '{slug}' not found"}), err=True)
                raise typer.Exit(code=1)
            score = rescore_one_chemistry(s, chem.id, as_of_date)
            typer.echo(json.dumps({
                "ok": True,
                "chemistry": slug,
                "composite_risk_score": score.composite_risk_score,
                "score_confidence": score.score_confidence,
            }, indent=2))
        else:
            results = rescore_all_chemistries(s, as_of_date)
            typer.echo(json.dumps({
                "ok": True,
                "rescored": len(results),
                "scores": [
                    {
                        "chemistry": r["slug"],
                        "composite_risk_score": r["composite_risk_score"],
                        "score_confidence": r["score_confidence"],
                    }
                    for r in results
                ],
            }, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("rescore-market")
def rescore_market_cmd(
    as_of: Optional[str] = typer.Option(
        None,
        "--as-of",
        help="Point-in-time date for scoring (YYYY-MM-DD). Default: today.",
    ),
    material_id: Optional[int] = typer.Option(
        None,
        "--material-id",
        help=(
            "Score only this material across its primary producing countries "
            "(plus any geography that has events for it). "
            "Default: all active materials."
        ),
    ),
    geographies: Optional[str] = typer.Option(
        None,
        "--geographies",
        help=(
            "Comma-separated ISO2 country codes to score against (e.g. 'CN,CL,AU'). "
            "Default: derived from each material's primary_producing_countries plus "
            "any country with linked risk events."
        ),
    ),
) -> None:
    """Compute and persist material_geography_risk_scores rows.

    Runs the market-level (company-agnostic) scoring engine across all active
    materials and their associated geographies. Safe to re-run — results are
    upserted by (material_id, geography_code, as_of_date).

    Run this AFTER ingest-worldbank and ingest-comtrade so that commodity
    price and trade-flow signals are available to the scoring engine.

    \b
    Examples:
      bdi-ingest rescore-market
      bdi-ingest rescore-market --as-of 2025-01-01
      bdi-ingest rescore-market --material-id 3
      bdi-ingest rescore-market --geographies CN,CL,AU

    Intended usage order after ingestion:

    \b
      bdi-ingest ingest-worldbank --since-year 2015
      bdi-ingest ingest-comtrade --years 2021,2022,2023
      bdi-ingest rescore-market
    """
    import datetime
    import uuid

    from app.models.regulatory import RiskEventGeography, RiskEventMaterial
    from app.models.supply import Material
    from app.services.scoring.market_aggregator import (
        score_all_active_materials,
        score_material_geography,
    )

    as_of_date = datetime.date.fromisoformat(as_of) if as_of else datetime.date.today()
    geo_filter = (
        [g.strip().upper() for g in geographies.split(",") if g.strip()]
        if geographies
        else None
    )

    s = _session()
    try:
        if material_id is not None:
            # Single-material path. ``score_all_active_materials`` does not
            # currently support a material filter, so derive the geography
            # set the same way it would and call ``score_material_geography``
            # per pair, committing per pair to mirror the batch function's
            # transaction discipline.
            mat = s.get(Material, material_id)
            if mat is None:
                typer.echo(
                    f"rescore-market failed: no material with id={material_id}",
                    err=True,
                )
                raise typer.Exit(code=1)

            if geo_filter is not None:
                geos: list[str] = list(geo_filter)
            else:
                geos = []
                if mat.primary_producing_countries:
                    geos.extend(g.upper() for g in mat.primary_producing_countries)
                event_geo_rows = s.execute(
                    select(RiskEventGeography.country_code)
                    .join(
                        RiskEventMaterial,
                        RiskEventMaterial.risk_event_id == RiskEventGeography.risk_event_id,
                    )
                    .where(RiskEventMaterial.material_id == mat.id)
                    .distinct()
                ).all()
                geos = sorted({*geos, *(row[0] for row in event_geo_rows if row[0])})

            if not geos:
                typer.echo(
                    f"rescore-market: no geographies derivable for material "
                    f"id={material_id} ({mat.canonical_name}). Nothing to score.",
                )
                return

            run_id = f"cli-{uuid.uuid4()}"
            scored = 0
            for geo in geos:
                try:
                    score_material_geography(
                        s,
                        mat.id,
                        geo,
                        as_of_date,
                        run_id=f"{run_id}-{mat.id}-{geo}",
                        persist=True,
                    )
                    s.commit()
                    scored += 1
                except Exception as inner_exc:
                    s.rollback()
                    typer.echo(
                        f"  - skipped {mat.canonical_name} × {geo}: {inner_exc}",
                        err=True,
                    )

            typer.echo(
                f"rescore-market complete: {scored} (material, geography) pair(s) "
                f"scored for {mat.canonical_name} (id={mat.id}) at {as_of_date}"
            )
        else:
            results = score_all_active_materials(
                s,
                as_of_date,
                geography_codes=geo_filter,
            )
            scored = len(results)
            typer.echo(
                f"rescore-market complete: {scored} (material, geography) pair(s) "
                f"scored for {as_of_date}"
            )
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(f"rescore-market failed: {exc}", err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("rescore-global-rollups")
def rescore_global_rollups_cmd(
    as_of: Optional[str] = typer.Option(
        None,
        "--as-of",
        help="Point-in-time date for rollup scoring (YYYY-MM-DD). Default: today.",
    ),
    material_id: Optional[int] = typer.Option(
        None,
        "--material-id",
        help=(
            "Roll up only this material_id into material_global_risk_scores. "
            "Default: all materials that have geo scores."
        ),
    ),
) -> None:
    """Compute and persist material_global_risk_scores rows.

    This is step 2 in the market scoring chain:
      1) rescore-market (material × geography)
      2) rescore-global-rollups (material-only global rollup)
    """
    import datetime
    import uuid

    from app.models.supply import Material
    from app.services.scoring.global_rollup import (
        score_all_material_global_rollups,
        score_material_global_rollup,
    )

    as_of_date = datetime.date.fromisoformat(as_of) if as_of else datetime.date.today()

    s = _session()
    try:
        if material_id is not None:
            mat = s.get(Material, material_id)
            if mat is None:
                typer.echo(
                    f"rescore-global-rollups failed: no material with id={material_id}",
                    err=True,
                )
                raise typer.Exit(code=1)

            run_id = f"global-cli-{uuid.uuid4()}"
            score = score_material_global_rollup(
                s,
                material_id=mat.id,
                as_of_date=as_of_date,
                run_id=f"{run_id}-{mat.id}",
                persist=True,
            )
            s.commit()
            typer.echo(
                "rescore-global-rollups complete: "
                f"{mat.canonical_name} (id={mat.id}) overall={score.overall_risk_score} "
                f"as_of={as_of_date}"
            )
        else:
            results = score_all_material_global_rollups(s, as_of_date=as_of_date)
            typer.echo(
                "rescore-global-rollups complete: "
                f"{len(results)} material(s) rolled up for {as_of_date}"
            )
    except typer.Exit:
        raise
    except Exception as exc:
        s.rollback()
        typer.echo(f"rescore-global-rollups failed: {exc}", err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("ingest-comtrade")
def ingest_comtrade_cmd(
    years: str = typer.Option(
        "2021,2022,2023",
        "--years",
        help="Comma-separated list of years to fetch (e.g. '2021,2022,2023').",
    ),
    reporters: Optional[str] = typer.Option(
        None,
        "--reporters",
        help="Comma-separated ISO2 reporter codes to limit scope (e.g. 'CN,CL,AU'). Default: all 14 configured reporters.",
    ),
    hs_prefixes: Optional[str] = typer.Option(
        None,
        "--hs-prefixes",
        help="Comma-separated HS prefixes to query (e.g. '2604,2602'). Default: reads from supply_chain_contexts.",
    ),
) -> None:
    """Ingest UN Comtrade annual export trade flows for battery-critical HS codes.

    Fetches export data (flowCode=X) for configured reporter countries × HS prefixes × years.
    Idempotent: skips reporter/HS/year combinations already present in source_documents.
    Re-run annually when a new data year becomes available (typically ~3-month lag).

    API call count = len(reporters) × len(hs_prefixes) × len(years).
    With defaults (14 reporters × 6 HS codes × 3 years = 252 calls).
    Paid tier handles this comfortably; free tier (500/day) handles it in one run.
    """
    from app.services.ingestion.comtrade import REPORTER_COUNTRIES, ingest_comtrade

    year_list = [int(y.strip()) for y in years.split(",")]

    reporter_filter = None
    if reporters:
        codes = [r.strip().upper() for r in reporters.split(",")]
        reporter_filter = {k: v for k, v in REPORTER_COUNTRIES.items() if k in codes}
        unknown = set(codes) - set(REPORTER_COUNTRIES.keys())
        if unknown:
            typer.echo(f"Warning: unknown reporter codes ignored: {unknown}", err=True)

    hs_list = [h.strip() for h in hs_prefixes.split(",")] if hs_prefixes else None

    s = _session()
    try:
        result = ingest_comtrade(
            session=s,
            years=year_list,
            reporters=reporter_filter,
            hs_prefixes=hs_list,
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("ingest-opensanctions")
def ingest_opensanctions_cmd(
    url: Optional[str] = typer.Option(
        None,
        "--url",
        help="Override the OpenSanctions bulk CSV URL.",
    ),
    geos: Optional[str] = typer.Option(
        None,
        "--geos",
        help='Comma-separated ISO2 high-concentration geo codes. Default: "CN,CD,RU,IR,KP"',
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip the interval gate and run regardless of when the last ingest occurred.",
    ),
) -> None:
    """Match companies against OpenSanctions consolidated sanctions lists.

    Downloads the free daily bulk export (no API key required).
    Creates RiskEvent rows for company name matches and geography-level signals.
    Idempotent: skips events whose content_hash already exists.

    Skips the download if the last run was within 6 days. Use --force to override.
    """
    from app.services.ingestion.opensanctions import OPENSANCTIONS_CSV_URL, ingest_opensanctions

    geo_list = [g.strip().upper() for g in geos.split(",")] if geos else None

    s = _session()
    try:
        result = ingest_opensanctions(
            session=s,
            url=url or OPENSANCTIONS_CSV_URL,
            high_concentration_geos=geo_list,
            min_interval_days=0 if force else 6,
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("seed-companies")
def seed_companies_cmd() -> None:
    """Seed the companies table from the curated battery supply chain reference list.

    Idempotent: skips companies whose canonical_name already exists.
    Run once after initial setup, then use ingest-gleif to enrich with LEI data.
    Run before: ingest-opensanctions (name matching).
    """
    from app.services.ingestion.seed_companies import seed_companies

    s = _session()
    try:
        result = seed_companies(s)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("seed-facilities")
def seed_facilities_cmd() -> None:
    """Seed known physical facilities for all supply chain companies.

    Idempotent: deduplicates on (company_id, facility_type, country, city).
    Run after seed-companies.
    """
    from app.services.ingestion.seed_facilities import seed_facilities

    s = _session()
    try:
        result = seed_facilities(s)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("ingest-mrds")
def ingest_mrds_cmd(
    local_file: Optional[str] = typer.Option(
        None,
        "--local-file",
        help=(
            "Path to a locally-downloaded MRDS CSV or ZIP file. "
            "Download from: https://mrdata.usgs.gov/mrds/mrds-csv.zip "
            "If omitted, the ingester downloads directly from USGS."
        ),
    ),
) -> None:
    """Ingest USGS MRDS mine data into facilities + facility_material_links.

    Populates mining and processing facility records for battery-critical minerals
    (Lithium, Cobalt, Nickel, Manganese, Natural Graphite, Copper, REE) from the
    USGS Mineral Resources Data System (~300k global mine records). Feeds the
    operational scoring pillar with real facility status data.

    Deduplication: existing facilities matched by mrds_dep_id are updated in-place.
    New facilities are inserted. Records no longer in MRDS are NOT automatically removed.

    MRDS does not publish annual capacity figures — annual_capacity_tpy will be NULL
    for all MRDS-sourced rows. The operational pillar falls back to event-derived
    structural_dependency when no capacity data is available.

    \b
    Run order:
      bdi-ingest seed-companies        # companies must exist (for company_facility links)
      bdi-ingest ingest-mrds           # live download, or pass --local-file
      bdi-ingest rescore-market        # picks up updated facility data in operational pillar

    \b
    Notes:
      - Cell factories, pack plants, and recycling facilities are NOT in MRDS.
        Keep running seed-facilities for those types.
      - MRDS covers mines (surface/underground/brine) and processing (mills/smelters).
      - Re-run periodically — MRDS is updated irregularly, not on a fixed schedule.
    """
    from app.services.ingestion.mrds import ingest_mrds

    s = _session()
    try:
        # ingest_mrds commits in batches internally — already-committed rows
        # are NOT rolled back if an error occurs partway through, so re-running
        # after a failure will skip rows already persisted (dep_id dedup).
        result = ingest_mrds(session=s, local_file=local_file)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("seed-supply-relationships")
def seed_supply_relationships_cmd() -> None:
    """Seed confirmed buyer-supplier relationships across the battery supply chain."""
    from app.services.ingestion.seed_supply_relationships import seed_supply_relationships

    with _session() as s:
        result = seed_supply_relationships(s)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))

@app.command("ingest-vpic")
def ingest_vpic_cmd(
    companies: Optional[str] = typer.Option(
        None,
        "--companies",
        help=(
            "Comma-separated canonical company names to process. "
            "Defaults to all OEMs in OEM_MAKE_MAP. "
            "Example: 'Tesla,Rivian Automotive,Lucid Group'."
        ),
    ),
    year_start: int = typer.Option(
        2010,
        "--year-start",
        help="First model year to scan (inclusive).",
    ),
    year_end: Optional[int] = typer.Option(
        None,
        "--year-end",
        help="Last model year to scan (inclusive). Defaults to the current year.",
    ),
    rate_limit_delay: float = typer.Option(
        0.5,
        "--rate-limit-delay",
        help="Seconds to sleep between every vPIC API call. Increase if you see 429s.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Fetch and parse, but do NOT write any rows.",
    ),
) -> None:
    """Populate company_vehicle_models from the NHTSA vPIC API.

    Fetches officially registered make/model names per model year and upserts
    rows keyed on (company_id, model_name, model_year_start). Battery chemistry
    data is NOT available in vPIC; vehicle_model_chemistries rows are not
    written by this command.

    OEM companies are mapped to their registered vPIC make names
    (e.g. "Volkswagen Group" covers VOLKSWAGEN, AUDI, and PORSCHE). Geely
    Auto Group and Zeekr are not registered in vPIC and are skipped.

    \b
    Run order:
      bdi-ingest seed-companies    # companies must exist first
      bdi-ingest ingest-vpic       # this command
      bdi-ingest rescore-all       # picks up new vehicle model data
    """
    from app.services.ingestion.ingest_vpic import run as vpic_run

    company_list = [c.strip() for c in companies.split(",")] if companies else None

    s = _session()
    try:
        result = vpic_run(
            s,
            companies=company_list,
            year_start=year_start,
            year_end=year_end,
            dry_run=dry_run,
            rate_limit_delay=rate_limit_delay,
        )
        typer.echo(json.dumps({"ok": True, "dry_run": dry_run, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("seed-regulations")
def seed_regulations_cmd() -> None:
    """Seed regulations, material/geography scopes, and company regulation exposures.

    Populates four tables: regulations, regulation_material_scope,
    regulation_geography_scope, and company_regulation_exposure.

    This unblocks the regulatory pillar (20% of company score). The three scored
    obligations are UFLPA (25 pts), EU_BATTERY_REG (20 pts), IRA_DOMESTIC (15 pts).
    Only non_compliant / partial / unknown rows generate scoring uplift.

    Idempotent: safe to re-run after updating compliance statuses in the seed file.

    Run order:
        bdi-ingest seed-companies    # companies must exist
        bdi-ingest ingest-usgs ...   # materials must exist
        bdi-ingest seed-regulations  # this command
    """
    from app.services.ingestion.seed_regulations import seed_regulations

    s = _session()
    try:
        result = seed_regulations(s)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("ingest-eurlex")
def ingest_eurlex_cmd(
    no_fetch: bool = typer.Option(
        False,
        "--no-fetch",
        help="Skip fetching summary text from EUR-Lex (faster, no network required).",
    ),
) -> None:
    """Upsert EU battery supply chain regulations from the EUR-Lex manifest.

    Seeds the regulations table with the EU Battery Regulation (2023/1542),
    CRMA (2024/1252), CBAM, CSDDD, Conflict Minerals Regulation, and REACH
    cobalt restrictions — along with their material and geography scope records.

    Idempotent: re-running will not duplicate rows. Fetches plain-text summaries
    from EUR-Lex HTML unless --no-fetch is passed.

    Run this after seed-materials so material_id lookups resolve correctly:

    \b
      bdi-ingest seed-materials
      bdi-ingest ingest-eurlex
    """
    from app.services.ingestion.eurlex import ingest_eurlex

    s = _session()
    try:
        result = ingest_eurlex(session=s, fetch_summaries=not no_fetch)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("ingest-gta")
def ingest_gta_cmd(
    since_year: int = typer.Option(
        2018,
        "--since-year",
        help="Only ingest interventions from this year onward. Default: 2018.",
    ),
    url: Optional[str] = typer.Option(
        None,
        "--url",
        help="Override the GTA CSV download URL.",
    ),
    local_file: Optional[str] = typer.Option(
        None,
        "--local-file",
        help=(
            "Path to a locally-downloaded GTA CSV file. Skips the HTTP download. "
            "Use this when the bulk download requires authentication — download "
            "manually from https://globaltradealert.org/data-center and pass the path here."
        ),
    ),
    hs_prefixes: Optional[str] = typer.Option(
        None,
        "--hs-prefixes",
        help=(
            "Comma-separated HS prefixes to filter (e.g. '2604,2602'). "
            "Default: all battery-material prefixes."
        ),
    ),
    skip_hs_filter: bool = typer.Option(
        False,
        "--skip-hs-filter",
        help=(
            "Skip the HS code prefix filter. Use this when ingesting a pre-filtered "
            "curated export from the GTA data center (e.g. 'Harmful Trade Policy "
            "Interventions: Batteries') where GTA has already applied product-level "
            "filtering using its own internal classification codes rather than HS codes."
        ),
    ),
) -> None:
    """Ingest Global Trade Alert harmful trade interventions into risk_events.

    GTA now requires registration for bulk data access. Two modes:

    \b
    1. Local file (recommended — download from GTA data center):
         bdi-ingest ingest-gta --local-file /path/to/gta_state_acts.csv

    \b
    2. Pre-filtered curated export (e.g. "Batteries" dataset):
         bdi-ingest ingest-gta --local-file /path/to/interventions.csv --skip-hs-filter

    \b
    3. Direct download (only if GTA restores public bulk access):
         bdi-ingest ingest-gta --since-year 2018

    Ingests Red (harmful) interventions whose affected HS codes match
    battery-critical materials. All events inserted with verified=False.

    \b
      bdi-ingest seed-materials
      bdi-ingest seed-hs-mappings
      bdi-ingest ingest-gta --local-file /path/to/gta_state_acts.csv
    """
    from app.services.ingestion.gta import (
        BATTERY_HS_PREFIXES,  # noqa: F401  - re-exported for users running --help
        GTA_DOWNLOAD_URL,
        ingest_gta,
    )

    if local_file is None and url is None:
        typer.echo(
            "Warning: no --local-file provided. Attempting direct download — "
            "this may fail. Download manually from "
            "https://globaltradealert.org/data-center and use --local-file.",
            err=True,
        )

    hs_list = (
        [h.strip() for h in hs_prefixes.split(",") if h.strip()] if hs_prefixes else None
    )

    s = _session()
    try:
        result = ingest_gta(
            session=s,
            url=url or GTA_DOWNLOAD_URL,
            since_year=since_year,
            hs_prefixes=hs_list,
            local_file=local_file,
            skip_hs_filter=skip_hs_filter,
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("seed-material-exposures")
def seed_material_exposures_cmd() -> None:
    """Seed company_material_exposures — the primary data source for three scoring pillars.

    Populates exposure_score (drives Material Concentration pillar criticality),
    source_geography (drives Geopolitical pillar country_concentration), and
    supply_chain_stage for ~75 companies across miners, refiners, cell makers,
    OEMs, and recyclers.

    Idempotent: existing rows update mutable fields (exposure_score,
    source_geography, data_confidence, rationale); identity fields are immutable.

    Run order:
        bdi-ingest seed-companies       # companies must exist
        bdi-ingest ingest-usgs ...      # materials must exist
        bdi-ingest seed-material-exposures
    """
    from app.services.ingestion.seed_material_exposures import seed_material_exposures

    s = _session()
    try:
        result = seed_material_exposures(s)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("ingest-sec-edgar")
def ingest_sec_edgar_cmd(
    companies: Optional[str] = typer.Option(
        None,
        "--companies",
        help=(
            "Comma-separated canonical company names to ingest (must match seed names). "
            "Default: all configured US/foreign SEC filers."
        ),
    ),
    max_filings: int = typer.Option(
        12,
        "--max-filings",
        help="Maximum recent filings to pull per company (includes 10-K, 10-Q, 8-K, 20-F).",
    ),
) -> None:
    """Ingest SEC EDGAR filing metadata for public companies in the battery supply chain.

    Fetches the `submissions` endpoint (no API key required) for each company CIK.
    Creates SourceDocument + RiskEvent rows categorised as FINANCIAL_PRESSURE, then
    rescores each touched company. Subsequent scoring runs will produce non-default
    financial pillar values.

    EDGAR fair access policy requires a descriptive User-Agent. Set in .env:
        SEC_EDGAR_USER_AGENT="YourCompanyName contact@yourdomain.com"

    Companies without a US CIK (e.g. CATL, VW, BMW) are not covered — they do not
    file with the SEC. Add --companies to target a subset.

    Run order: seed-companies must have already run (entity resolution needs the DB).

    Example:
        bdi-ingest ingest-sec-edgar
        bdi-ingest ingest-sec-edgar --companies "Tesla,General Motors,Ford Motor Company"
        bdi-ingest ingest-sec-edgar --max-filings 20
    """
    import sqlalchemy as sa
    from app.models import Source
    from app.models.enums import ImplementationPhase, SourceType
    from app.services.ingestion.pipeline import IngestionPipeline

    # ── CIK map ─────────────────────────────────────────────────────────────
    # Maps canonical_name (must match seed_companies.py) → zero-padded CIK.
    # US domestic filers submit 10-K / 10-Q / 8-K.
    # Foreign private issuers submit 20-F / 6-K.
    # VERIFY at: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany
    # before running in production — CIKs are stable but verify after M&A events.
    CIK_MAP: dict[str, str] = {
        # ── US domestic (10-K / 10-Q / 8-K) ────────────────────────────────
        "Tesla":                  "0001318605",
        "General Motors":         "0001467858",
        "Ford Motor Company":     "0000037996",
        "Rivian Automotive":      "0001874178",
        "Lucid Group":            "0001811210",
        "Albemarle Corporation":  "0000915779",
        "Freeport-McMoRan":       "0000831259",
        "MP Materials":           "0001820302",
        # ── Foreign private issuers (20-F / 6-K) ────────────────────────────
        # Verify CIKs at SEC EDGAR before running; foreign filings are less
        # frequent (annual 20-F) so max_filings=12 will typically catch 3–4 years.
        "Vale":                   "0001099509",
        "SQM":                    "0001009672",
        "Rio Tinto":              "0001045810",
        "BHP Group":              "0001306965",
    }

    # Filter by --companies if supplied
    if companies:
        requested = [c.strip() for c in companies.split(",")]
        unknown = [r for r in requested if r not in CIK_MAP]
        if unknown:
            typer.echo(
                f"Warning: no CIK found for: {unknown}. "
                "Check name matches seed_companies.py canonical_name exactly.",
                err=True,
            )
        cik_list = [CIK_MAP[r] for r in requested if r in CIK_MAP]
        if not cik_list:
            typer.echo("No valid CIKs resolved — aborting.", err=True)
            raise typer.Exit(code=1)
    else:
        cik_list = list(CIK_MAP.values())

    typer.echo(
        f"Ingesting {len(cik_list)} companies × up to {max_filings} filings each…"
    )

    s = _session()
    try:
        # Find or create the SEC EDGAR source row.
        source = s.scalar(
            select(Source).where(
                Source.source_type == SourceType.SEC_EDGAR.value,
                Source.is_active == True,
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
            s.add(source)
            s.flush()
            typer.echo("Created new SEC EDGAR source row.")

        run_id = IngestionPipeline(s).run(
            source.id,
            params={"ciks": cik_list, "max_filings": max_filings},
        )
        typer.echo(json.dumps({"ok": True, "ingestion_run_id": run_id, "ciks_processed": len(cik_list)}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("ingest-gleif")
def ingest_gleif_cmd(
    rate_limit_delay: float = typer.Option(
        0.5,
        "--delay",
        help="Seconds to wait between GLEIF API calls.",
    ),
) -> None:
    """Enrich companies with LEI data from the GLEIF public API (no key required).

    Queries GLEIF for each company where lei IS NULL. Updates lei, legal_name,
    headquarters_country (NULL fields only), and adds name aliases.

    Re-run after adding new companies. Safe to run multiple times.
    LEI data improves OpenSanctions entity matching precision significantly.
    """
    from app.services.ingestion.gleif import enrich_all_companies
    from app.services.ingestion.seed_companies import _COMPANIES

    # Build search-name overrides from the curated seed list so abbreviations
    # like "CATL" are looked up as "Contemporary Amperex Technology" in GLEIF.
    search_overrides = {
        c["canonical_name"]: c["gleif_search_name"]
        for c in _COMPANIES
        if "gleif_search_name" in c
    }

    s = _session()
    try:
        results = enrich_all_companies(
            s,
            rate_limit_delay=rate_limit_delay,
            search_name_overrides=search_overrides,
        )
        matched = sum(1 for r in results if r["matched"])
        typer.echo(
            json.dumps(
                {
                    "ok": True,
                    "total_queried": len(results),
                    "matched": matched,
                    "unmatched": len(results) - matched,
                },
                indent=2,
            )
        )
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("ingest-worldbank")
def ingest_worldbank_cmd(
    since_year: Optional[int] = typer.Option(
        2015,
        "--since-year",
        help="Only ingest data from this year onward. Use 1960 for full history.",
    ),
    url: Optional[str] = typer.Option(
        None,
        "--url",
        help="Override the Pink Sheet download URL (default: current World Bank URL).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip the interval gate and run regardless of when the last ingest occurred.",
    ),
) -> None:
    """Download the World Bank Pink Sheet and ingest commodity prices.

    Fetches CMO-Historical-Data-Monthly.xlsx directly from the World Bank.
    Idempotent: skips rows that already exist in commodity_prices.

    Skips the download if the most recent price row is less than 25 days old.
    Use --force to override (e.g. if the World Bank re-publishes a correction).
    """
    from app.services.ingestion.worldbank_pinksheet import PINK_SHEET_URL, ingest_pink_sheet

    s = _session()
    try:
        result = ingest_pink_sheet(
            session=s,
            url=url or PINK_SHEET_URL,
            since_year=since_year,
            min_interval_days=0 if force else 25,
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("build-trade-signals")
def build_trade_signals_cmd(
    years: Optional[str] = typer.Option(
        None,
        "--years",
        help=(
            "Comma-separated years to analyse (e.g. '2021,2022,2023'). "
            "Default: all years present in trade_flows."
        ),
    ),
) -> None:
    """Derive GEOPOLITICAL_TRADE risk events from ingested Comtrade data.

    Reads the trade_flows table (populated by ingest-comtrade) and creates
    RiskEvent rows for two signal types:

    \b
      TRADE_CONCENTRATION   country ≥60% share of tracked exports for a material
                            → feeds trade_volatility (Material pillar) and
                              country_concentration (Geopolitical pillar)
      EXPORT_DROP           ≥20% YoY decline in a country's material exports
                            → feeds export_restriction_exposure (Geopolitical pillar)
                              via event_subtype = "EXPORT_RESTRICTION"

    Companies are linked via company_material_exposures: any company with
    source_geography matching the signal country gets a risk_event_companies row
    (relevance_score = 0.85).

    Run this AFTER ingest-comtrade and BEFORE rescore-all so that trade-derived
    signals are available when the scoring engine queries risk_events.

    Idempotent: skips events whose content_hash already exists.

    \b
      bdi-ingest ingest-comtrade --years 2021,2022,2023
      bdi-ingest build-trade-signals
      bdi-ingest rescore-all
    """
    from app.services.ingestion.trade_signal_builder import build_trade_signals

    year_list = None
    if years:
        try:
            year_list = [int(y.strip()) for y in years.split(",")]
        except ValueError:
            typer.echo("--years must be a comma-separated list of integers.", err=True)
            raise typer.Exit(code=1)

    s = _session()
    try:
        result = build_trade_signals(s, years=year_list)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("ingest-federal-register")
def ingest_federal_register_cmd(
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help=(
            "Only fetch documents published on or after this date (YYYY-MM-DD). "
            "Default: 90 days ago. Pass '2020-01-01' for a full historical backfill."
        ),
    ),
    max_pages: int = typer.Option(
        5,
        "--max-pages",
        help=(
            "Maximum pages to fetch per query batch (100 docs/page). "
            "Default 5 = up to 500 docs per query, 2 000 total across 4 queries."
        ),
    ),
    queries: Optional[str] = typer.Option(
        None,
        "--queries",
        help=(
            "Comma-separated query names to run. Default: all four "
            "(export_control,tariff,uflpa_ira,critical_minerals)."
        ),
    ),
) -> None:
    """Ingest Federal Register policy notices into GEOPOLITICAL_TRADE / REGULATORY_COMPLIANCE risk events.

    Runs four targeted query batches in priority order, each classified with a
    specific event_subtype so the geo pillar's sub-inputs fire correctly:

    \b
      export_control     → event_subtype EXPORT_RESTRICTION
                           feeds export_restriction_exposure in Geopolitical pillar
      tariff             → event_subtype TARIFF
                           feeds tariff_exposure in Geopolitical pillar
      uflpa_ira          → event_subtype REGULATORY_COMPLIANCE  (+0.20 severity boost)
                           feeds regulatory pillar risk events
      critical_minerals  → event_subtype TRADE_POLICY
                           feeds both Geopolitical and Regulatory pillars

    Documents are deduplicated by content_hash (SHA-256 of document_number +
    publication_date). Re-running is safe and will only create new events for
    documents not yet seen.

    Companies are linked via existing entity-resolution (resolve_companies_for_event
    + persist_company_links) with relevance_score proportional to severity.

    Run order:
    \b
      bdi-ingest ingest-federal-register
      bdi-ingest rescore-all

    Examples:
    \b
      bdi-ingest ingest-federal-register
      bdi-ingest ingest-federal-register --since 2020-01-01 --max-pages 20
      bdi-ingest ingest-federal-register --queries export_control,uflpa_ira
    """
    from datetime import date as _date
    from app.services.ingestion.ingest_federal_register import (
        TARGETED_QUERIES,
        ingest_federal_register,
    )

    since_date = _date.fromisoformat(since) if since else None

    query_filter: Optional[list] = None
    if queries:
        requested_names = [q.strip() for q in queries.split(",")]
        valid_names = {qc.name for qc in TARGETED_QUERIES}
        unknown = set(requested_names) - valid_names
        if unknown:
            typer.echo(
                f"Unknown query names: {sorted(unknown)}. "
                f"Valid options: {sorted(valid_names)}",
                err=True,
            )
            raise typer.Exit(code=1)
        query_filter = [qc for qc in TARGETED_QUERIES if qc.name in requested_names]

    typer.echo(
        f"Ingesting Federal Register documents"
        f"{f' since {since_date}' if since_date else ' (last 90 days)'}"
        f", up to {max_pages} pages per query…"
    )

    s = _session()
    try:
        result = ingest_federal_register(
            s,
            since_date=since_date,
            max_pages=max_pages,
            queries=query_filter,
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("rescore-all")
def rescore_all_cmd(
    as_of: Optional[str] = typer.Option(
        None,
        "--as-of",
        help="Point-in-time date for scoring (YYYY-MM-DD). Default: today.",
    ),
    skip_existing: bool = typer.Option(
        False,
        "--skip-existing",
        help="Skip companies that already have a score row for --as-of date. Prevents duplicate rows.",
    ),
    companies_filter: Optional[str] = typer.Option(
        None,
        "--companies",
        help=(
            "Comma-separated canonical company names to rescore. "
            "Default: all companies in the database."
        ),
    ),
) -> None:
    """Compute and persist company_scores rows for all (or selected) companies.

    Runs the full six-pillar scoring engine for each company:

    \b
      Material Concentration      25%
      Geopolitical / Trade        20%
      Regulatory / Compliance     20%
      Operational                 10%
      Financial                   10%
      Supply-Chain Propagation    15%  (inherited risk from upstream suppliers; null → weight redistributed)

    company_scores is append-only — each run inserts a new row per company.
    Use --skip-existing to avoid duplicate rows when re-running on the same date.

    Seed data prerequisites (run in order before this command):
    \b
      bdi-ingest seed-companies
      bdi-ingest ingest-usgs <path/to/MCS2025.csv>
      bdi-ingest seed-materials
      bdi-ingest seed-material-exposures   # unblocks Material + Geopolitical pillars
      bdi-ingest seed-regulations          # unblocks Regulatory pillar
      bdi-ingest seed-supply-relationships # unblocks Supply-Chain Propagation pillar
      bdi-ingest ingest-sec-edgar          # partial Financial pillar signal
      bdi-ingest rescore-all               # first pass: scores each company individually
      bdi-ingest rescore-all               # second pass: propagation pillar uses upstream scores

    Examples:
    \b
      bdi-ingest rescore-all
      bdi-ingest rescore-all --skip-existing
      bdi-ingest rescore-all --as-of 2025-01-01
      bdi-ingest rescore-all --companies "Tesla,General Motors,Albemarle Corporation"
    """
    import datetime

    from app.models.company import Company, CompanyScore
    from app.services.scoring.orchestrator import rescore_company

    as_of_date = datetime.date.fromisoformat(as_of) if as_of else datetime.date.today()
    run_id = f"rescore-all-{as_of_date.isoformat()}"

    s = _session()
    try:
        # Build company query — optionally filtered by canonical name
        q = select(Company)
        if companies_filter:
            names = [n.strip() for n in companies_filter.split(",") if n.strip()]
            q = q.where(Company.canonical_name.in_(names))
            unknown = set(names) - {
                row.canonical_name
                for row in s.scalars(select(Company).where(Company.canonical_name.in_(names))).all()
            }
            if unknown:
                typer.echo(f"Warning: no DB match for: {sorted(unknown)}", err=True)

        all_companies = list(s.scalars(q).all())
        if not all_companies:
            typer.echo(
                json.dumps({"ok": False, "error": "No companies found — run seed-companies first."}),
                err=True,
            )
            raise typer.Exit(code=1)

        typer.echo(f"Scoring {len(all_companies)} companies as of {as_of_date} (run_id={run_id})…")

        # Pre-build set of company IDs already scored today if --skip-existing
        already_scored: set = set()
        if skip_existing:
            already_scored = set(
                s.scalars(
                    select(CompanyScore.company_id).where(CompanyScore.as_of_date == as_of_date)
                ).all()
            )
            if already_scored:
                typer.echo(f"  {len(already_scored)} companies already scored today — will skip.")

        scored = skipped = failed = 0
        failures: list[dict] = []

        for co in all_companies:
            if skip_existing and co.id in already_scored:
                skipped += 1
                continue
            try:
                score_row = rescore_company(s, co.id, run_id=run_id, as_of_date=as_of_date)
                s.commit()
                scored += 1
                typer.echo(
                    f"  ✓ {co.canonical_name:<45}  overall={score_row.overall_risk_score:.1f}"
                )
            except Exception as exc:
                s.rollback()
                failed += 1
                failures.append({"company": co.canonical_name, "error": str(exc)})
                typer.echo(f"  ✗ {co.canonical_name}: {exc}", err=True)

        result: dict = {
            "ok": True,
            "as_of_date": as_of_date.isoformat(),
            "run_id": run_id,
            "scored": scored,
            "skipped": skipped,
            "failed": failed,
        }
        if failures:
            result["failures"] = failures

        typer.echo("\n" + json.dumps(result, indent=2))

    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("show-scores")
def show_scores_cmd(
    as_of: Optional[str] = typer.Option(
        None,
        "--as-of",
        help="Show scores for this date (YYYY-MM-DD). Default: most recent score date.",
    ),
    min_score: Optional[float] = typer.Option(
        None,
        "--min-score",
        help="Filter: only show companies with overall_risk_score >= this value.",
    ),
    pillar: Optional[str] = typer.Option(
        None,
        "--pillar",
        help=(
            "Sort by a specific pillar instead of overall score. "
            "One of: material, geo, regulatory, operational, financial, propagation"
        ),
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        help="Max rows to display. Default: 50.",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Output raw JSON instead of the formatted table.",
    ),
) -> None:
    """Display the most recent company risk scores as a ranked table.

    Shows overall and per-pillar scores (Mat, Geo, Reg, Op, Fin, Prop) for each
    company, sorted by overall risk descending (or by --pillar). Prop (supply-chain
    propagation) shows — for companies with no scored upstream suppliers.

    Examples:
    \b
      bdi-ingest show-scores
      bdi-ingest show-scores --min-score 60
      bdi-ingest show-scores --pillar regulatory
      bdi-ingest show-scores --as-of 2025-01-01 --json
    """
    import datetime
    import sqlalchemy as sa

    from app.models.company import Company, CompanyScore

    PILLAR_COLS = {
        "material":    CompanyScore.material_concentration_risk_score,
        "geo":         CompanyScore.geopolitical_trade_risk_score,
        "regulatory":  CompanyScore.regulatory_risk_score,
        "operational": CompanyScore.operational_risk_score,
        "financial":   CompanyScore.financial_pressure_score,
        "propagation": CompanyScore.supply_chain_propagation_score,
    }

    s = _session()
    try:
        # Determine target date
        if as_of:
            target_date = datetime.date.fromisoformat(as_of)
        else:
            target_date = s.scalar(sa.select(sa.func.max(CompanyScore.as_of_date)))
            if target_date is None:
                typer.echo("No scores found — run `rescore-all` first.", err=True)
                raise typer.Exit(code=1)

        # Validate --pillar
        if pillar and pillar not in PILLAR_COLS:
            typer.echo(
                f"Unknown pillar {pillar!r}. Choose from: {', '.join(PILLAR_COLS)}",
                err=True,
            )
            raise typer.Exit(code=1)

        sort_col = PILLAR_COLS[pillar] if pillar else CompanyScore.overall_risk_score

        # Latest score per company on target_date (multiple runs on same date → take max score_id)
        subq = (
            sa.select(
                CompanyScore.company_id,
                sa.func.max(CompanyScore.id).label("latest_id"),
            )
            .where(CompanyScore.as_of_date == target_date)
            .group_by(CompanyScore.company_id)
            .subquery()
        )

        rows = s.execute(
            sa.select(Company.canonical_name, CompanyScore)
            .join(subq, CompanyScore.id == subq.c.latest_id)
            .join(Company, Company.id == CompanyScore.company_id)
            .where(
                *(
                    [CompanyScore.overall_risk_score >= min_score]
                    if min_score is not None
                    else []
                )
            )
            .order_by(sort_col.desc())
            .limit(limit)
        ).all()

        if not rows:
            typer.echo(
                f"No scores found for {target_date}. Run `rescore-all --as-of {target_date}` first."
            )
            raise typer.Exit(code=0)

        if json_out:
            out = []
            for name, sc in rows:
                out.append({
                    "company":      name,
                    "overall":      round(sc.overall_risk_score, 1),
                    "material":     round(sc.material_concentration_risk_score, 1),
                    "geo":          round(sc.geopolitical_trade_risk_score, 1),
                    "regulatory":   round(sc.regulatory_risk_score, 1),
                    "operational":  round(sc.operational_risk_score, 1),
                    "financial":    round(sc.financial_pressure_score, 1),
                    "propagation":  round(sc.supply_chain_propagation_score, 1) if sc.supply_chain_propagation_score is not None else None,
                    "version":      sc.scoring_version,
                })
            typer.echo(json.dumps({"as_of_date": target_date.isoformat(), "scores": out}, indent=2))
        else:
            # Formatted table
            header = f"{'Company':<45} {'Overall':>7} {'Mat':>6} {'Geo':>6} {'Reg':>6} {'Op':>6} {'Fin':>6} {'Prop':>6}"
            typer.echo(f"\nRisk scores — {target_date}  ({len(rows)} companies)")
            typer.echo("=" * len(header))
            typer.echo(header)
            typer.echo("-" * len(header))
            for name, sc in rows:
                band = (
                    "CRIT" if sc.overall_risk_score >= 75
                    else "HIGH" if sc.overall_risk_score >= 55
                    else "MOD " if sc.overall_risk_score >= 35
                    else "LOW "
                )
                prop = sc.supply_chain_propagation_score
                prop_str = f"{prop:>5.1f}" if prop is not None else "    —"
                typer.echo(
                    f"{name:<45} "
                    f"{sc.overall_risk_score:>6.1f} "
                    f"[{band}]  "
                    f"{sc.material_concentration_risk_score:>5.1f}  "
                    f"{sc.geopolitical_trade_risk_score:>5.1f}  "
                    f"{sc.regulatory_risk_score:>5.1f}  "
                    f"{sc.operational_risk_score:>5.1f}  "
                    f"{sc.financial_pressure_score:>5.1f}  "
                    f"{prop_str}"
                )
            typer.echo("=" * len(header))

    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("review-seeds")
def review_seeds_cmd(
    seed_type: Optional[str] = typer.Option(
        None,
        "--seed-type",
        help=(
            "Which seed types to review. Comma-separated, or 'all' (default). "
            "Valid: regulation, company, material_exposure, supply_relationship, "
            "facility, all."
        ),
    ),
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help=(
            "Only consider evidence dated on or after this date (YYYY-MM-DD). "
            "Default: 180 days ago."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Compute findings in memory and print; do not write to the DB.",
    ),
    fmt: str = typer.Option(
        "markdown",
        "--format",
        help="Output format: markdown | json | both.",
    ),
    seed_key: Optional[str] = typer.Option(
        None,
        "--seed-key",
        help=(
            "Restrict to one subject (e.g. 'IRA_DOMESTIC' or 'CATL'). "
            "Seed type is inferred when possible, or combine with --seed-type."
        ),
    ),
) -> None:
    """Cross-reference seeded rows against ingested evidence and emit findings.

    Examples:
    \b
      bdi-ingest review-seeds
      bdi-ingest review-seeds --seed-type company
      bdi-ingest review-seeds --seed-type regulation,company
      bdi-ingest review-seeds --since 2025-01-01
      bdi-ingest review-seeds --dry-run
      bdi-ingest review-seeds --format json
      bdi-ingest review-seeds --seed-key IRA_DOMESTIC
    """
    from datetime import date as _date, timedelta as _timedelta

    from app.services.review.seed_staleness import (
        SEED_TYPES,
        run_seed_review,
    )

    requested_types: Optional[list[str]]
    if seed_type is None or seed_type.strip().lower() in {"", "all"}:
        requested_types = None
    else:
        requested_types = [t.strip() for t in seed_type.split(",") if t.strip()]
        unknown = [t for t in requested_types if t not in SEED_TYPES]
        if unknown:
            typer.echo(
                f"Unknown --seed-type value(s): {unknown}. "
                f"Valid options: {list(SEED_TYPES)}",
                err=True,
            )
            raise typer.Exit(code=1)

    if since is not None:
        try:
            since_date = _date.fromisoformat(since)
        except ValueError:
            typer.echo(f"Invalid --since value: {since!r}", err=True)
            raise typer.Exit(code=1)
    else:
        since_date = _date.today() - _timedelta(days=180)

    fmt_norm = fmt.lower().strip()
    if fmt_norm not in {"markdown", "json", "both"}:
        typer.echo(
            f"Invalid --format value: {fmt!r}. Use markdown | json | both.",
            err=True,
        )
        raise typer.Exit(code=1)

    s = _session()
    try:
        result = run_seed_review(
            s,
            seed_types=requested_types,
            since_date=since_date,
            persist=not dry_run,
            seed_key=seed_key,
        )
        if not dry_run:
            s.commit()

        if fmt_norm in {"markdown", "both"}:
            typer.echo(_format_review_markdown(result, dry_run=dry_run))
        if fmt_norm in {"json", "both"}:
            typer.echo(json.dumps(_format_review_json(result), indent=2))
    except typer.Exit:
        raise
    except Exception as exc:
        if not dry_run:
            s.rollback()
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


def _format_review_json(result) -> dict:
    """Convert a ReviewRunResult into a serializable dict."""
    findings_out: dict[str, list[dict]] = {}
    for seed_type_key, findings in result.findings_by_type.items():
        findings_out[seed_type_key] = [
            {
                "seed_type": f.seed_type,
                "seed_key": f.seed_key,
                "seed_display_name": f.seed_display_name,
                "seed_identifier": f.seed_identifier_json,
                "evidence_type": f.evidence_type,
                "source_document_id": f.source_document_id,
                "risk_event_id": f.risk_event_id,
                "evidence": f.evidence_json,
                "relevance": f.relevance,
                "signals": f.match_signals_json,
            }
            for f in findings
        ]
    return {
        "ok": True,
        "run_id": str(result.run_id),
        "persisted": result.persisted,
        "seed_types": result.seed_types,
        "since_date": (
            result.parameters_json.get("since_date")
            if isinstance(result.parameters_json, dict)
            else None
        ),
        "seed_key": result.seed_key,
        "total_seeds_checked": result.total_seeds_checked,
        "total_findings": result.total_findings,
        "started_at": result.started_at.isoformat(),
        "completed_at": result.completed_at.isoformat(),
        "findings_by_type": findings_out,
    }


def _format_review_markdown(result, *, dry_run: bool) -> str:
    """Render a ReviewRunResult as a markdown report grouped by seed_type."""
    lines: list[str] = []
    lines.append(f"# Seed Staleness Review — {result.run_id}")
    lines.append("")
    mode = "dry-run" if dry_run else "persisted"
    lines.append(
        f"- Mode: **{mode}**  \n"
        f"- Seed types: `{', '.join(result.seed_types)}`  \n"
        f"- Seeds checked: **{result.total_seeds_checked}**  \n"
        f"- Findings: **{result.total_findings}**  \n"
        f"- Started: {result.started_at.isoformat()}  \n"
        f"- Completed: {result.completed_at.isoformat()}"
    )
    lines.append("")

    if result.total_findings == 0:
        lines.append("_No findings._")
        return "\n".join(lines)

    for seed_type_key in result.seed_types:
        findings = result.findings_by_type.get(seed_type_key, [])
        if not findings:
            continue
        lines.append(f"## {seed_type_key} ({len(findings)})")
        lines.append("")
        grouped: dict[str, list] = {}
        for f in findings:
            grouped.setdefault(f.seed_key, []).append(f)
        for seed_key_val in sorted(grouped.keys()):
            subject_findings = grouped[seed_key_val]
            display = subject_findings[0].seed_display_name or seed_key_val
            lines.append(f"### {seed_key_val}")
            if display != seed_key_val:
                lines.append(f"_{display}_")
            lines.append("")
            for f in subject_findings:
                evidence = f.evidence_json or {}
                title = evidence.get("title") or "(no title)"
                published = evidence.get("published_at") or evidence.get("event_date") or "—"
                url = evidence.get("url")
                fired = [
                    key
                    for key, val in (f.match_signals_json or {}).items()
                    if isinstance(val, dict) and val.get("points", 0) > 0
                ]
                lines.append(
                    f"- **[{f.relevance.upper()}]** {title} ({published})"
                    + (f"  \n  {url}" if url else "")
                    + f"  \n  Signals: {', '.join(fired) if fired else 'none'}"
                )
            lines.append("")
    return "\n".join(lines)


@app.command("ingest-iea-policy-tracker")
def ingest_iea_policy_tracker_cmd(
    file_path: str = typer.Option(
        "data/iea_policy_tracker.csv",
        "--file-path",
        help=(
            "Path to the downloaded IEA Policy Tracker CSV (or XLSX). "
            "Download from https://www.iea.org/data-and-statistics/data-tools/"
            "critical-minerals-policy-tracker"
        ),
    ),
    run_id: Optional[str] = typer.Option(
        None,
        "--run-id",
        help="Optional identifier for this ingestion run (logged in metadata_json).",
    ),
) -> None:
    """Ingest IEA Critical Minerals Policy Tracker into risk_events.

    Parses the downloadable CSV export from IEA's Policy and Measures database.
    Creates low-severity RiskEvent rows (POLICY_MILESTONE | INVESTMENT_PLEDGE)
    for positive-policy signals: investment pledges, recycling mandates,
    domestic-content milestones, and strategic reserve announcements.

    These are constructive signals, not risk events — severity is capped at 0.20
    so they contribute minimally to scores.  Their primary value is rationale
    context: explaining why a geography scores lower than raw supply-concentration
    data alone would suggest.

    Idempotent: rows are deduplicated by content_hash (title + countries + year).

    \b
    Run order:
      bdi-ingest seed-materials    # material lookups must exist
      bdi-ingest ingest-iea-policy-tracker --file-path data/iea_policy_tracker.csv
      bdi-ingest rescore-market    # picks up new policy events
    """
    from app.services.ingestion.iea_policy_tracker import ingest_policy_tracker

    s = _session()
    try:
        result = ingest_policy_tracker(s, xls_path=file_path, run_id=run_id)
        s.commit()
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        s.rollback()
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("ingest-iea-reports")
def ingest_iea_reports_cmd(
    report_year: Optional[int] = typer.Option(
        None,
        "--report-year",
        help=(
            "Only ingest reports whose reference_year matches this value. "
            "Default: all enabled reports in the catalogue."
        ),
    ),
    timeout: int = typer.Option(
        120,
        "--timeout",
        help="HTTP timeout in seconds for each PDF download.",
    ),
) -> None:
    """Download IEA Critical Minerals PDF reports and extract criticality signals.

    Fetches enabled reports from the IEA_REPORTS catalogue (currently the 2024
    and 2023 Critical Minerals Market Reviews).  Extracts MaterialCriticalitySignal
    rows using pdfplumber — section text and tables are parsed for supply
    concentration figures and demand trend keywords per mineral.

    Signals are upserted with source="iea_report", which ranks above "usgs_mcs"
    in the market_aggregator scoring hierarchy, so these forward-looking IEA
    signals will override USGS-derived criticality scores where available.

    Idempotent: re-running refreshes signals from the latest parsed data.

    \b
    Run order:
      bdi-ingest seed-materials        # material lookups must exist
      bdi-ingest ingest-iea-reports    # this command (downloads PDFs)
      bdi-ingest rescore-market        # picks up updated criticality signals

    \b
    Examples:
      bdi-ingest ingest-iea-reports
      bdi-ingest ingest-iea-reports --report-year 2024
      bdi-ingest ingest-iea-reports --timeout 180
    """
    from app.services.ingestion.iea_reports import IEA_REPORTS, ingest_iea_reports

    reports = [r for r in IEA_REPORTS if r.enabled]
    if report_year is not None:
        reports = [r for r in reports if r.reference_year == report_year]
        if not reports:
            typer.echo(
                f"No enabled reports found for reference_year={report_year}. "
                f"Available years: {sorted({r.reference_year for r in IEA_REPORTS if r.enabled})}",
                err=True,
            )
            raise typer.Exit(code=1)

    typer.echo(f"Ingesting {len(reports)} IEA report(s)…")
    for r in reports:
        typer.echo(f"  • {r.title}")

    s = _session()
    try:
        result = ingest_iea_reports(s, reports=reports, timeout=timeout)
        s.commit()
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        s.rollback()
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
