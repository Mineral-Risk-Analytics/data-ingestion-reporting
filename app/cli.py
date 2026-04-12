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

        inserted = updated = signals_written = 0
        for m in materials:
            # Separate the internal _hhi_score key before creating ORM objects.
            hhi_score = m.pop("_hhi_score", None)

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

        s.commit()
        typer.echo(json.dumps({
            "ok": True,
            "source": f"USGS Mineral Commodity Summaries {mcs_year}",
            "inserted": inserted,
            "updated": updated,
            "signals_written": signals_written,
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
) -> None:
    """Match companies against OpenSanctions consolidated sanctions lists.

    Downloads the free daily bulk export (no API key required).
    Creates RiskEvent rows for company name matches and geography-level signals.
    Idempotent: skips events whose content_hash already exists.
    Re-run daily or weekly to catch new listings.
    """
    from app.services.ingestion.opensanctions import OPENSANCTIONS_CSV_URL, ingest_opensanctions

    geo_list = [g.strip().upper() for g in geos.split(",")] if geos else None

    s = _session()
    try:
        result = ingest_opensanctions(
            session=s,
            url=url or OPENSANCTIONS_CSV_URL,
            high_concentration_geos=geo_list,
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
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
) -> None:
    """Download the World Bank Pink Sheet and ingest commodity prices.

    Fetches CMO-Historical-Data-Monthly.xlsx directly from the World Bank.
    Idempotent: skips rows that already exist in commodity_prices.
    Re-run monthly after World Bank publishes an update (usually first week of month).
    """
    from app.services.ingestion.worldbank_pinksheet import PINK_SHEET_URL, ingest_pink_sheet

    s = _session()
    try:
        result = ingest_pink_sheet(
            session=s,
            url=url or PINK_SHEET_URL,
            since_year=since_year,
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
