"""CLI for ingestion and seed (`python -m app.cli` or `bdi-ingest`)."""

from __future__ import annotations

import json
from typing import Optional

import typer
from sqlalchemy import delete, select
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


@app.command("backfill-cik-map")
def backfill_cik_map_cmd() -> None:
    """One-shot backfill of Company.cik from the hardcoded CIK_MAP.

    Copies the 12 (name, CIK) pairs hardcoded in
    ``app/tasks/ingestion_jobs.py`` onto the matching Company rows so
    the SEC ingester can find them by CIK lookup.  Resolution order:
    exact canonical_name → prefix → alias.  Skips rows that already
    have a non-NULL cik (partner curation wins).  Logs anything
    unmatched for manual review.

    Run this once after applying migration 044 + the company seed
    template load.  Safe to re-run — non-destructive.

    \b
    Examples:
      bdi-ingest backfill-cik-map
    """
    from app.services.ingestion.backfill_cik_map import backfill_cik_map

    s = _session()
    try:
        result = backfill_cik_map(s)
        s.commit()
        typer.echo(json.dumps({"ok": True, **result}, indent=2, default=str))
    except Exception as exc:
        s.rollback()
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("seed-supply-chain-stages")
def seed_supply_chain_stages_cmd() -> None:
    """Upsert the canonical supply_chain_stages reference table.

    Seeds the 16 stage codes (mining, beneficiation, refining,
    precursor_production, cathode_active_material, anode_active_material,
    separator_production, electrolyte_production, cell_making,
    module_assembly, pack_assembly, vehicle_assembly, recycling, trading,
    financial, integrated) with display names, long-form descriptions,
    bottleneck_weight multipliers, and HS-chapter hints.

    Run this after applying migration 044 and before any partner-curated
    Company seed load — companies.primary_supply_chain_stage_fk references
    this table.

    Idempotent: re-running refreshes display_name / description /
    bottleneck_weight / etc. without touching created_at.

    \b
    Examples:
      bdi-ingest seed-supply-chain-stages
    """
    from app.services.ingestion.seed_supply_chain_stages import (
        seed_supply_chain_stages,
    )

    s = _session()
    try:
        result = seed_supply_chain_stages(s)
        s.commit()
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        s.rollback()
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("seed-countries")
def seed_countries_cmd() -> None:
    """Upsert the countries reference table.

    Inserts or updates all rows in the ``countries`` table — ISO2 codes,
    canonical names, Comtrade numeric reporter codes, common-name aliases
    (used for name→ISO2 resolution in GTA and other ingesters), and
    ``is_major_producer`` / ``is_major_consumer`` flags (used by
    ingest-comtrade to select reporter country sets).

    Idempotent: re-running refreshes all columns without touching
    ``created_at``.  Safe to run after adding or renaming entries.

    Run this before ingest-gta and ingest-comtrade.

    \b
    Examples:
      bdi-ingest seed-countries
    """
    from app.services.ingestion.seed_countries import seed_countries

    s = _session()
    try:
        result = seed_countries(s)
        s.commit()
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        s.rollback()
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("ingest")
def ingest_cmd(
    source: str = typer.Argument(
        ...,
        help="federal-register | news",
    ),
    extra_json: Optional[str] = typer.Option(
        None,
        "--params",
        help='JSON object merged into source config, e.g. \'{"per_page":5}\'',
    ),
) -> None:
    """Run a Phase 1 ingestion pipeline by source alias.

    Note: ``sec-edgar`` was removed from this mapping in the May 2026
    refactor — use the dedicated ``bdi-ingest ingest-sec-edgar`` command
    instead, which routes through the new ``ingest_sec_edgar`` module
    with full material attribution.  ``federal-register`` is also
    preferentially served by ``bdi-ingest ingest-federal-register`` for
    the same reason; this generic command is kept as a fallback debugging
    entry point for the remaining pipeline-served sources (currently
    just ``news``) until they get dedicated modules of their own.

    ``census-trade`` was REMOVED from this mapping in the June 2026
    Trade Volumes audit — the Census adapter is parked as a Phase 2
    scaffold (see ``app/services/ingestion/adapters/census_trade.py``
    docstring).  Re-add this entry only when the Phase 2 unpark
    prerequisites are complete.
    """
    mapping = {
        "federal-register": SourceType.FEDERAL_REGISTER.value,
        "news": SourceType.NEWS.value,
    }
    st = mapping.get(source.replace("_", "-"))
    if not st:
        typer.echo(
            f"Unknown source {source!r}. Choose from {list(mapping.keys())}.\n"
            f"For sec-edgar use the dedicated `bdi-ingest ingest-sec-edgar` command.",
            err=True,
        )
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
        # 2026-06-14: defaults to the current-year USGS MCS long-format
        # CSV checked into the repo at data/usgs/2026/.  Pass an explicit
        # path when ingesting a different vintage (e.g. when USGS
        # publishes MCS2027 in early 2027 and the CSV lands in
        # data/usgs/2027/).
        "data/usgs/2026/MCS2026_Commodities_Data.csv",
        help=(
            "Path to MCS world data CSV. Defaults to "
            "data/usgs/2026/MCS2026_Commodities_Data.csv (the current "
            "long-format publication checked into the repo). Pass an "
            "explicit path to ingest an older or different vintage. "
            "Format is auto-detected from headers unless --csv-format "
            "is set."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Upsert: update existing materials even if they already exist.",
    ),
    mcs_year: Optional[int] = typer.Option(
        None,
        "--mcs-year",
        help=(
            "Publication year of the MCS file (used as reference_year in "
            "material_criticality_signals).  Defaults to 2025 for the wide "
            "format and 2026 for the long format."
        ),
    ),
    csv_format: str = typer.Option(
        "2026",
        "--csv-format",
        help="CSV format (kept for compatibility; only '2026' supported).",
    ),
) -> None:
    """Ingest USGS Mineral Commodity Summaries data from the official CSV.

    Phase B refactor (May 2026): canonical-name resolution now goes
    through ``material_source_aliases``.  Materials must already exist
    in the ``materials`` table (run ``seed-materials`` first); this
    command writes signals only — it does NOT create materials.

    Skipped chapters (e.g. ABRASIVES, ARSENIC) are silently dropped per
    the alias table's ``is_skipped=true`` rows.  Unknown chapters log a
    warning so partner can decide whether to add an alias or a skip row.

    Writes per-HS-node production shares for sub-typed materials (Silicon
    ferrosilicon vs metal, Copper mine vs refinery) AND US import-source
    shares (with market_scope='us') from the 'Import Sources' section.

    Note (2026-05-24): the 2025 wide-format parser was removed.  The
    --csv-format flag is kept for backwards-compatible invocation but
    only '2026' is accepted.
    """
    from pathlib import Path
    from app.models.criticality_signal import MaterialCriticalitySignal
    from app.services.ingestion.seeds.mcs2026_parser import parse_mcs2026_csv
    from app.services.ingestion.material_resolver import MaterialAliasResolver

    path = Path(filepath)
    if not path.exists():
        typer.echo(f"File not found: {filepath}", err=True)
        raise typer.Exit(code=1)

    # Format selection — only 2026 supported as of 2026-05-24.  Reject
    # explicit --csv-format=2025 calls and accept either "2026" or "auto"
    # (auto is a no-op; the parser handles encoding detection internally).
    if csv_format not in ("2026", "auto"):
        typer.echo(
            f"--csv-format {csv_format!r} is not supported — only '2026' "
            "is available. The 2025 wide-format parser was removed.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Default mcs_year if the user didn't pass --mcs-year.
    if mcs_year is None:
        mcs_year = 2026

    records = parse_mcs2026_csv(path)
    if not records:
        typer.echo("No records parsed — check the file format.", err=True)
        raise typer.Exit(code=1)

    s = _session()
    try:
        from datetime import date as _date  # local import — narrow blast radius
        from app.models.supply import (
            CommodityPrice,
            HsCodeMaterialMapping,
            HsCodeProductionShare,
            MaterialCapacityShare,
            MaterialProductionShare,
        )
        from app.services.ingestion.normalizers.hs_resolver import (
            resolve_hs_for_price_descriptor,
        )

        resolver = MaterialAliasResolver(s)
        signals_written = 0
        shares_written = 0
        capacity_shares_written = 0
        hs_shares_written = 0
        hs_shares_skipped_no_mapping = 0
        # ── Same-stage HHI propagation counters (Step 2A, 2026-06) ──────────
        # MCS publishes country production shares at the material × stage
        # level, but the parser writes them to only the ONE hs_code_material_
        # mappings row resolved as that stage's anchor.  Result: cobalt has
        # 24 HS mappings but only 1 with shares (4% coverage), so 96% of
        # cobalt HS nodes fall to ``event_only_no_hhi`` in hs_node_scorer.
        # Fix: after writing the anchor row, replicate the same country
        # distribution to every OTHER same-material same-stage mapping that
        # has no production-share data, tagged with source='usgs_mcs_propagated'
        # so consumers can tell propagated rows from primary ones.
        hs_shares_propagated = 0
        hs_shares_propagation_targets_skipped = 0
        us_import_shares_written = 0
        us_import_shares_skipped_no_mapping = 0
        # 2026-06-14: counters for the USGS Salient Price extraction
        # (mcs2026_parser.{_extract_prices_from_salient, _derive_yoy_and_cagr_from_prices}).
        commodity_prices_inserted = 0
        commodity_prices_skipped_existing = 0
        criticality_growth_filled = 0
        criticality_growth_skipped_existing = 0
        # 2026-06-14: HS-attribution counters (hs_resolver path).
        commodity_prices_hs_resolved = 0   # benchmark descriptor matched an HS row
        commodity_prices_hs_fallback = 0   # no match → material-level (hs_mapping_id=NULL)
        resolved_canonicals: list[str] = []
        skipped_aliases: list[str] = []
        unknown_aliases: list[str] = []

        # ── Old-convention cleanup (freshness fix, 2026-08-05) ──────────
        # Rows written before this fix were stamped with the MCS EDITION
        # year (reference_year = mcs_year) instead of the data year the
        # CSV Year column actually carries (2024/2025 for the 2026
        # edition).  The corrected writers below key on the data year, so
        # a plain re-run would leave the mis-stamped rows in place as the
        # "latest" vintage — every consumer takes max(reference_year), so
        # the stale rows would keep winning.  Delete them up front.
        # Scoped strictly to USGS-sourced GLOBAL rows at the edition year:
        # benchmark-workbook rows, us-scope import shares, and correctly
        # stamped rows from any source are untouched.  Idempotent — a
        # second run finds nothing to delete.
        stale_material_rows = s.execute(
            delete(MaterialProductionShare).where(
                MaterialProductionShare.data_source == "usgs_mcs",
                MaterialProductionShare.reference_year == mcs_year,
            )
        ).rowcount
        stale_hs_rows = s.execute(
            delete(HsCodeProductionShare).where(
                HsCodeProductionShare.source.in_(
                    ["usgs_mcs", "usgs_mcs_propagated"]
                ),
                HsCodeProductionShare.market_scope == "global",
                HsCodeProductionShare.reference_year == mcs_year,
            )
        ).rowcount
        if stale_material_rows or stale_hs_rows:
            typer.echo(
                f"Old-convention cleanup: deleted {stale_material_rows} "
                f"material share rows and {stale_hs_rows} HS share rows "
                f"stamped with edition year {mcs_year}."
            )

        for rec in records:
            source_system = rec.pop("source_system", None)
            source_name = rec.pop("source_name", None)
            if not (source_system and source_name):
                continue

            result = resolver.resolve(source_system, source_name)
            if result.status == "skipped":
                skipped_aliases.append(source_name)
                continue
            if result.status == "unknown":
                unknown_aliases.append(source_name)
                typer.echo(
                    f"  [unknown] no alias for ({source_system!r}, "
                    f"{source_name!r}) — add a row to "
                    f"seed_material_source_aliases.py and re-run "
                    f"`seed-material-aliases --force-update`.",
                    err=True,
                )
                continue
            material = result.material
            assert material is not None  # status == "ok"
            # Secondary chapters (e.g. BAUXITE AND ALUMINA → Aluminum)
            # share a canonical with a primary chapter; only the primary
            # writes material-level signals.  Secondary still contributes
            # per-HS-prefix shares so its country distributions land in
            # hs_code_production_shares.
            writes_material_signals = result.writes_material_signals
            if writes_material_signals:
                resolved_canonicals.append(material.canonical_name)
            else:
                resolved_canonicals.append(
                    f"{material.canonical_name} (secondary: {source_name!r})"
                )

            # ── Pull signal fields out of the record ──────────────────────
            criticality_score = rec.get("criticality_score")
            hhi_score = rec.get("hhi_score")
            reserve_hhi_score = rec.get("reserve_hhi_score")
            reserve_life_index = rec.get("reserve_life_index")
            production_yoy_pct = rec.get("production_yoy_pct")
            capacity_utilization = rec.get("capacity_utilization")
            production_shares = rec.get("production_shares") or []
            hs_production_shares = rec.get("hs_production_shares") or []
            us_import_sources = rec.get("us_import_sources") or []
            us_net_import_reliance = rec.get("us_net_import_reliance")
            apparent_consumption = rec.get("apparent_consumption")
            price_unit_usgs = rec.get("price_unit_usgs")
            # 2026-06-14: full Salient Price observations + derived growth
            # rates.  See mcs2026_parser._extract_prices_from_salient.
            prices = rec.get("prices") or []
            price_yoy_pct_derived = rec.get("price_yoy_pct_derived")
            price_cagr_5yr_pct_derived = rec.get("price_cagr_5yr_pct_derived")

            # ── Material-level writes: skip for secondary chapters ────────
            # Secondary chapters share a canonical with a primary sibling
            # (e.g. BAUXITE AND ALUMINA → Aluminum, where ALUMINUM is the
            # primary).  Writing material-level signals from the secondary
            # would overwrite the primary's values via the unique key
            # (material_id, source, reference_year).  Per-HS-prefix shares
            # below are still written because they route to distinct HS
            # prefixes (bauxite to 2606, aluminum to 7601 — no collision).
            if writes_material_signals:
                # USGS-derived price_unit: only write when material.price_unit
                # is currently NULL.  Protects partner-set values (e.g. Sodium
                # "per_mt", REE "per_kg") from being overwritten.
                if price_unit_usgs is not None and material.price_unit is None:
                    material.price_unit = price_unit_usgs

                # Back-sync the materials.criticality_score cache.
                if criticality_score is not None:
                    material.criticality_score = criticality_score

                # ── material_criticality_signals upsert ───────────────────
                existing_signal = s.scalar(
                    select(MaterialCriticalitySignal).where(
                        MaterialCriticalitySignal.material_id == material.id,
                        MaterialCriticalitySignal.source == "usgs_mcs",
                        MaterialCriticalitySignal.reference_year == mcs_year,
                    )
                )
                # us_net_import_reliance / apparent_consumption now live in
                # typed columns (migration 038) — write directly there.
                # metadata_json keeps lighter-weight extras only
                # (mcs_publication_year, fig10_source_rows etc.).
                signal_meta: dict = {"mcs_publication_year": mcs_year}

                if existing_signal is None:
                    s.add(MaterialCriticalitySignal(
                        material_id=material.id,
                        source="usgs_mcs",
                        reference_year=mcs_year,
                        criticality_score=criticality_score,
                        hhi_score=hhi_score,
                        reserve_hhi_score=reserve_hhi_score,
                        reserve_life_index=reserve_life_index,
                        production_yoy_pct=production_yoy_pct,
                        capacity_utilization=capacity_utilization,
                        us_net_import_reliance_pct=us_net_import_reliance,
                        us_apparent_consumption=apparent_consumption,
                        metadata_json=signal_meta,
                    ))
                    signals_written += 1
                elif force:
                    existing_signal.criticality_score = criticality_score
                    existing_signal.hhi_score = hhi_score
                    existing_signal.reserve_hhi_score = reserve_hhi_score
                    existing_signal.reserve_life_index = reserve_life_index
                    existing_signal.production_yoy_pct = production_yoy_pct
                    existing_signal.capacity_utilization = capacity_utilization
                    existing_signal.us_net_import_reliance_pct = us_net_import_reliance
                    existing_signal.us_apparent_consumption = apparent_consumption
                    merged = dict(existing_signal.metadata_json or {})
                    merged.update(signal_meta)
                    existing_signal.metadata_json = merged
                    signals_written += 1

                # ── 2026-06-14: fill price_yoy_pct / price_cagr_5yr_pct from
                # derived Salient prices IF NULL.  Don't overwrite values the
                # Fig 10 parser already wrote — Fig 10 has authority for
                # multi-source chapters because USGS publishes the growth
                # rates explicitly there.  Single-source chapters (most of
                # them) are silent in Fig 10 and our derivation fills the
                # gap.  ``force=True`` does NOT overwrite either — partner
                # can clear the column manually if they want a re-fill.
                signal_row = s.scalar(
                    select(MaterialCriticalitySignal).where(
                        MaterialCriticalitySignal.material_id == material.id,
                        MaterialCriticalitySignal.source == "usgs_mcs",
                        MaterialCriticalitySignal.reference_year == mcs_year,
                    )
                )
                if signal_row is not None:
                    updated_any = False
                    if (
                        signal_row.price_yoy_pct is None
                        and price_yoy_pct_derived is not None
                    ):
                        signal_row.price_yoy_pct = price_yoy_pct_derived
                        updated_any = True
                    elif price_yoy_pct_derived is not None:
                        criticality_growth_skipped_existing += 1
                    if (
                        signal_row.price_cagr_5yr_pct is None
                        and price_cagr_5yr_pct_derived is not None
                    ):
                        signal_row.price_cagr_5yr_pct = price_cagr_5yr_pct_derived
                        updated_any = True
                    if updated_any:
                        criticality_growth_filled += 1

                # ── 2026-06-14: upsert commodity_prices rows from Salient
                # Price observations.  One row per (year, benchmark) so
                # multi-benchmark commodities (Cobalt US-spot + LME, Copper 3
                # benchmarks) preserve every datum.  Idempotent via the
                # uq_commodity_price unique constraint
                # (material_id, price_date, source, hs_mapping_id, price_form).
                #
                # price_date is set to Dec 31 of the reporting year — USGS
                # publishes annual averages, not point-in-time observations,
                # so any date in the year is defensible.  Year-end keeps the
                # series chronologically intuitive when plotted alongside
                # Pink Sheet monthly rows.
                for p in prices:
                    pdate = _date(p["year"], 12, 31)
                    price_form = p["statistics_detail"][:200]  # column limit
                    value_usd = p["value_usd_per_mt"]
                    if value_usd is None:
                        # DMTU / MTU rows — skip insert (no normalised USD
                        # value to store) but preserve in metadata if we
                        # land them elsewhere later.
                        continue

                    # 2026-06-14: HS-attribution via hs_resolver.  Matches
                    # the USGS Statistics_detail string against the same
                    # material's hs_code_material_mappings rows by keyword
                    # + description overlap.  When a row clears the
                    # confidence floor, the price is HS-attributed (stage-
                    # scoped); when no candidate clears, hs_mapping_id
                    # stays None and the row is material-level (same as
                    # before the resolver landed).  The resolution score +
                    # method are written into metadata_json["hs_resolution"]
                    # so partner can audit attribution decisions.
                    resolved_hs_id, hs_resolve_score, hs_resolve_via = (
                        resolve_hs_for_price_descriptor(
                            s, material.id, p["statistics_detail"],
                        )
                    )
                    if resolved_hs_id is not None:
                        commodity_prices_hs_resolved += 1
                    else:
                        commodity_prices_hs_fallback += 1

                    # Idempotency probe: lookup keyed on the same columns
                    # as the uq_commodity_price constraint so re-runs
                    # neither duplicate nor flip an HS-attributed row to
                    # NULL (or vice versa).  IS-comparison for the
                    # nullable hs_mapping_id column matches Postgres'
                    # NULL-distinct semantics.
                    if resolved_hs_id is None:
                        existing_clause = CommodityPrice.hs_mapping_id.is_(None)
                    else:
                        existing_clause = CommodityPrice.hs_mapping_id == resolved_hs_id
                    existing_price = s.scalar(
                        select(CommodityPrice).where(
                            CommodityPrice.material_id == material.id,
                            CommodityPrice.price_date == pdate,
                            CommodityPrice.source == "usgs_mcs",
                            CommodityPrice.price_form == price_form,
                            existing_clause,
                        )
                    )
                    if existing_price is None:
                        s.add(CommodityPrice(
                            material_id=material.id,
                            price_date=pdate,
                            price_usd=value_usd,
                            price_unit="per_mt",   # we normalised
                            source="usgs_mcs",
                            price_form=price_form,
                            hs_mapping_id=resolved_hs_id,
                            metadata_json={
                                "raw_value": p["value_raw"],
                                "raw_unit": p["unit_raw"],
                                "statistics_detail": p["statistics_detail"],
                                "mcs_year": mcs_year,
                                "hs_resolution": {
                                    "hs_mapping_id": resolved_hs_id,
                                    "score": round(hs_resolve_score, 2),
                                    "matched_via": hs_resolve_via,
                                },
                            },
                        ))
                        commodity_prices_inserted += 1
                    else:
                        commodity_prices_skipped_existing += 1

                # ── material_production_shares upsert ─────────────────────
                # reference_year is the DATA year from the CSV Year column
                # (freshness fix, 2026-08-05) — NOT the MCS edition year.
                # Pre-fix rows said 2026 for data USGS collected in 2024/25,
                # making every freshness gate one year optimistic.  Edition
                # year remains the fallback for records without a parseable
                # year (none in the 2026 file today).
                for share in production_shares:
                    share_ref_year = share.get("reference_year") or mcs_year
                    existing_share = s.scalar(
                        select(MaterialProductionShare).where(
                            MaterialProductionShare.material_id == material.id,
                            MaterialProductionShare.country_code == share["country_code"],
                            MaterialProductionShare.reference_year == share_ref_year,
                        )
                    )
                    if existing_share is None:
                        s.add(MaterialProductionShare(
                            material_id=material.id,
                            country_code=share["country_code"],
                            reference_year=share_ref_year,
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

                # ── material_capacity_shares upsert (migration 045) ───────
                # MCS publishes Capacity rows separately from Production for
                # ~8 tracked chapters today.  detail_type carries the verbatim
                # Statistics_detail so TITANIUM's sponge-metal vs TiO2-pigment
                # capacities stay distinct.  Unique key includes detail_type.
                for cap in rec.get("capacity_shares") or []:
                    ref_year = cap.get("reference_year") or mcs_year
                    existing_cap = s.scalar(
                        select(MaterialCapacityShare).where(
                            MaterialCapacityShare.material_id == material.id,
                            MaterialCapacityShare.country_code == cap["country_code"],
                            MaterialCapacityShare.reference_year == ref_year,
                            MaterialCapacityShare.detail_type == cap["detail_type"],
                        )
                    )
                    if existing_cap is None:
                        s.add(MaterialCapacityShare(
                            material_id=material.id,
                            country_code=cap["country_code"],
                            reference_year=ref_year,
                            detail_type=cap["detail_type"],
                            capacity_volume=cap["capacity_volume"],
                            capacity_share=cap["capacity_share"],
                            unit_of_measure=cap.get("unit_of_measure"),
                            data_source="usgs_mcs",
                        ))
                        capacity_shares_written += 1
                    elif force:
                        existing_cap.capacity_volume = cap["capacity_volume"]
                        existing_cap.capacity_share = cap["capacity_share"]
                        existing_cap.unit_of_measure = cap.get("unit_of_measure")
                        capacity_shares_written += 1

            # ── Per-HS-node global production shares ─────────────────────
            # Two lookup modes (refactored 2026-05-09):
            #
            #   Stage-based: parser emits ``stage`` and empty ``hs_code_prefix``.
            #     Looks up by (material_id, supply_chain_stage, scope=global)
            #     preferring the lowest digit_count (most general HS prefix
            #     for that stage).  Used for auto-detected mine/smelter/
            #     refinery/alumina rows — the bulk of MCS data.
            #
            #   Prefix-based: parser emits explicit ``hs_code_prefix``.
            #     Looks up by (material_id, prefix, scope=global).  Used for
            #     sub-type overrides where multiple HS prefixes share a stage
            #     (Silicon ferrosilicon vs silicon metal — both refined).
            # Step 2A (2026-06) — track each (stage → anchor) we write so we
            # can fan out the same country distribution to sibling HS
            # mappings after the loop ends.  Keyed by supply_chain_stage so
            # we only propagate within the same stage of the material.
            # ``stage_anchors`` value shape:
            #   {stage: {"anchor_mapping_id": int,
            #            "type_substring": str,
            #            "shares": [(country_code, share, volume), ...]}}
            stage_anchors: dict[str, dict] = {}

            for hs_share in hs_production_shares:
                hs_prefix = hs_share.get("hs_code_prefix") or ""
                stage = hs_share.get("stage")
                hs_mapping = None
                if hs_prefix:
                    hs_mapping = s.scalar(
                        select(HsCodeMaterialMapping).where(
                            HsCodeMaterialMapping.material_id == material.id,
                            HsCodeMaterialMapping.hs_code_prefix == hs_prefix,
                            HsCodeMaterialMapping.market_scope == "global",
                        )
                    )
                elif stage:
                    # Stage-based: prefer the lowest digit_count (most
                    # general 4-digit prefix) so production data lands on
                    # the canonical stage prefix rather than a sub-prefix.
                    hs_mapping = s.scalar(
                        select(HsCodeMaterialMapping).where(
                            HsCodeMaterialMapping.material_id == material.id,
                            HsCodeMaterialMapping.supply_chain_stage == stage,
                            HsCodeMaterialMapping.market_scope == "global",
                        )
                        .order_by(HsCodeMaterialMapping.digit_count.asc())
                        .limit(1)
                    )
                if hs_mapping is None:
                    hs_shares_skipped_no_mapping += 1
                    qualifier = (
                        f"prefix={hs_prefix!r}" if hs_prefix
                        else f"stage={stage!r}"
                    )
                    typer.echo(
                        f"  [skip] no hs_code_material_mappings row for "
                        f"({material.canonical_name!r}, {qualifier}, global) — "
                        f"run seed-hs-mappings first",
                        err=True,
                    )
                    continue
                # Data year, not edition year (freshness fix, 2026-08-05).
                hs_ref_year = hs_share.get("reference_year") or mcs_year
                existing_hs_share = s.scalar(
                    select(HsCodeProductionShare).where(
                        HsCodeProductionShare.hs_mapping_id == hs_mapping.id,
                        HsCodeProductionShare.country_code == hs_share["country_code"],
                        HsCodeProductionShare.reference_year == hs_ref_year,
                        HsCodeProductionShare.market_scope == "global",
                        HsCodeProductionShare.source == "usgs_mcs",
                    )
                )
                if existing_hs_share is None:
                    s.add(HsCodeProductionShare(
                        hs_mapping_id=hs_mapping.id,
                        country_code=hs_share["country_code"],
                        reference_year=hs_ref_year,
                        production_share=hs_share["production_share"],
                        production_volume=hs_share["production_volume"],
                        # 2026-08-05: parser has always emitted the unit;
                        # the writer silently dropped it, leaving every HS
                        # row's unit NULL — surfaced when the Producers
                        # table moved onto these rows.
                        unit_of_measure=hs_share.get("unit_of_measure"),
                        market_scope="global",
                        source="usgs_mcs",
                        notes=(
                            f"CSV sub-type: {hs_share['type_substring']!r} — "
                            f"derived from MCS {mcs_year} World Data CSV"
                        ),
                    ))
                    hs_shares_written += 1
                elif force:
                    existing_hs_share.production_share = hs_share["production_share"]
                    existing_hs_share.production_volume = hs_share["production_volume"]
                    existing_hs_share.unit_of_measure = hs_share.get("unit_of_measure")
                    hs_shares_written += 1

                # Step 2A — collect for same-stage propagation.  We propagate
                # only when ``supply_chain_stage`` is known on the resolved
                # anchor mapping (parser ``stage`` field may be empty for
                # prefix-resolved rows; fall back to the DB column).
                anchor_stage = hs_mapping.supply_chain_stage or stage
                if anchor_stage is None:
                    continue
                bucket = stage_anchors.setdefault(anchor_stage, {
                    "anchor_mapping_id": hs_mapping.id,
                    "type_substring": hs_share.get("type_substring", ""),
                    # Propagated rows must carry the anchor's DATA year —
                    # stages can differ in vintage within one material.
                    "reference_year": hs_ref_year,
                    "shares": [],
                })
                # If multiple sub-types resolve to the same stage via
                # DIFFERENT anchor mappings (e.g. ferrosilicon vs silicon
                # metal — both "refined"), keep only the first anchor.
                # The other sub-types remain primary rows on their own
                # mappings; we just don't fan their distribution out a
                # second time on top of the first anchor's fan-out.
                if bucket["anchor_mapping_id"] == hs_mapping.id:
                    bucket["shares"].append((
                        hs_share["country_code"],
                        hs_share["production_share"],
                        hs_share.get("production_volume"),
                    ))

            # ── Step 2A — same-stage HHI propagation ────────────────────
            # For each (material, stage) anchor that received primary share
            # data, copy the SAME country distribution to every OTHER
            # hs_code_material_mappings row for this material with the same
            # supply_chain_stage that has NO existing production share data
            # for this reference year.  Rows are written with
            # source='usgs_mcs_propagated' so the unique constraint
            # (..., source) allows primary + propagated rows to coexist for
            # the same (hs_mapping, country, year, scope) if both ever apply,
            # and so consumers can distinguish them.
            #
            # Why this exists: MCS publishes a single country distribution
            # per (material × stage), and the parser writes it to one anchor
            # mapping.  For cobalt with 24 HS mappings that produces 4%
            # coverage, leaving 96% of cobalt HS nodes to fall back to
            # event_only_no_hhi in hs_node_scorer.  Same-stage propagation
            # is the cheapest bridge to lift coverage without inventing
            # numbers: the country mix at the anchor's stage is methodo-
            # logically the closest available proxy for siblings at the
            # same stage.  Cross-stage propagation is NOT done — that's a
            # bigger assumption (mining mix ≠ refining mix).
            if force and stage_anchors:
                # Clear stale propagated rows for this material before
                # refanning.  Primary 'usgs_mcs' rows are NOT touched.
                # Scoped to the anchor data years this run will rewrite
                # (was mcs_year pre-2026-08-05).
                anchor_years = {
                    b["reference_year"] for b in stage_anchors.values()
                }
                s.execute(
                    delete(HsCodeProductionShare)
                    .where(
                        HsCodeProductionShare.source == "usgs_mcs_propagated",
                        HsCodeProductionShare.reference_year.in_(anchor_years),
                        HsCodeProductionShare.market_scope == "global",
                        HsCodeProductionShare.hs_mapping_id.in_(
                            select(HsCodeMaterialMapping.id).where(
                                HsCodeMaterialMapping.material_id == material.id
                            )
                        ),
                    )
                )

            for stage_name, bucket in stage_anchors.items():
                anchor_id = bucket["anchor_mapping_id"]
                anchor_shares = bucket["shares"]
                anchor_ref_year = bucket["reference_year"]
                if not anchor_shares:
                    continue
                sibling_mappings = list(s.scalars(
                    select(HsCodeMaterialMapping).where(
                        HsCodeMaterialMapping.material_id == material.id,
                        HsCodeMaterialMapping.supply_chain_stage == stage_name,
                        HsCodeMaterialMapping.market_scope == "global",
                        HsCodeMaterialMapping.id != anchor_id,
                    )
                ).all())
                for sibling in sibling_mappings:
                    # Skip siblings that already carry data of any source
                    # for this (year, scope) — don't clobber genuine data.
                    has_existing = s.scalar(
                        select(HsCodeProductionShare.id).where(
                            HsCodeProductionShare.hs_mapping_id == sibling.id,
                            HsCodeProductionShare.reference_year == anchor_ref_year,
                            HsCodeProductionShare.market_scope == "global",
                        ).limit(1)
                    )
                    if has_existing is not None:
                        hs_shares_propagation_targets_skipped += 1
                        continue
                    for country_code, share, _volume in anchor_shares:
                        s.add(HsCodeProductionShare(
                            hs_mapping_id=sibling.id,
                            country_code=country_code,
                            reference_year=anchor_ref_year,
                            production_share=share,
                            # production_volume intentionally NULL on
                            # propagated rows — the country MIX is what
                            # propagates defensibly, not absolute tonnages.
                            production_volume=None,
                            market_scope="global",
                            source="usgs_mcs_propagated",
                            notes=(
                                f"Propagated from anchor hs_mapping_id={anchor_id} "
                                f"(stage={stage_name!r}, sub-type="
                                f"{bucket['type_substring']!r}).  Country mix "
                                f"only — volumes intentionally omitted.  HHI "
                                f"is methodologically valid; absolute tonnage "
                                f"comparisons are not."
                            ),
                        ))
                        hs_shares_propagated += 1

            # ── US import-source rows (market_scope='us', 2026 only) ──────
            # Each Import Sources row tells us what fraction of US imports
            # of a given sub-type came from a given country.  Stored with
            # market_scope='us' so global HHI computations aren't
            # contaminated.
            #
            # Sub-type → HS-prefix resolution happens in three layers
            # (May 2026):
            #   1. Parser-internal _DETAIL_TO_HS_PREFIX  (Silicon ferrosilicon
            #      vs metal, Copper mine vs refinery — explicit per-chapter rules)
            #   2. Keyword-based DB lookup  — match the sub-type substring
            #      against partner-curated keywords on this material's HS
            #      mappings (e.g. "oxide" → 282580 for Antimony).  Covers
            #      ~55 of the 60 collision cases observed in MCS 2026.
            #   3. Fall back to material.hs_codes[0]  — last-resort default.
            # The dedup pass below handles any remaining collisions.
            material_primary_prefix = ""
            if material.hs_codes:
                material_primary_prefix = material.hs_codes[0].replace(".", "")

            # Build keyword lookup once per material: list of
            # (hs_prefix, [lowercase_keywords]).  Used by the keyword-
            # resolution step below.
            kw_rows = s.execute(
                select(
                    HsCodeMaterialMapping.hs_code_prefix,
                    HsCodeMaterialMapping.keywords,
                ).where(
                    HsCodeMaterialMapping.material_id == material.id,
                    HsCodeMaterialMapping.market_scope == "global",
                    HsCodeMaterialMapping.keywords.is_not(None),
                )
            ).all()
            kw_lookup: list[tuple[str, list[str]]] = []
            for prefix, kws in kw_rows:
                if not kws:
                    continue
                kw_lookup.append((prefix, [str(k).lower() for k in kws]))

            def _resolve_via_keywords(detail: str) -> Optional[str]:
                """Match an MCS sub-type against partner-curated keywords.

                Substring match in either direction (keyword in detail OR
                detail in keyword).  When multiple HS prefixes match, the
                LONGEST keyword wins — encodes specificity ("ferrochromium,
                low-carbon" > "ferrochromium" > "metal").
                """
                sub_low = (detail or "").lower().strip()
                if not sub_low:
                    return None
                matches: list[tuple[int, str]] = []
                for prefix, kws in kw_lookup:
                    for kw in kws:
                        if kw in sub_low or sub_low in kw:
                            matches.append((len(kw), prefix))
                            break  # one match per prefix is enough
                if not matches:
                    return None
                matches.sort(reverse=True)
                return matches[0][1]

            # Many MCS chapters publish multiple Import Sources sub-types
            # (e.g. Antimony: 'oxide' / 'unwrought metal' / 'total metal
            # and oxide').  After keyword resolution, sub-types that
            # didn't match a keyword fall back to material.hs_codes[0]
            # and may collide on the unique key.  The dedup pass keeps
            # one row per (prefix, country), preferring the row whose
            # sub-type label contains 'total' / 'all forms' / etc.; ties
            # broken by largest share.  All collapsed sub-types are
            # recorded in the notes field so the audit trail survives.
            _TOTAL_SUBSTRINGS = ("total", "all forms", "all imports", "all countries")

            def _is_total_subtype(s: str) -> bool:
                low = (s or "").lower()
                return any(t in low for t in _TOTAL_SUBSTRINGS)

            deduped: dict[tuple[str, str], dict] = {}
            for src in us_import_sources:
                # Resolve hs_code_prefix in priority order:
                # parser-routed → keyword-resolved → primary-fallback
                hs_prefix = src.get("hs_code_prefix")
                if not hs_prefix:
                    hs_prefix = _resolve_via_keywords(src.get("type_substring", ""))
                if not hs_prefix:
                    hs_prefix = material_primary_prefix
                if not hs_prefix:
                    us_import_shares_skipped_no_mapping += 1
                    continue
                key = (hs_prefix, src["country_code"])
                cand_total = _is_total_subtype(src.get("type_substring", ""))
                if key not in deduped:
                    deduped[key] = {
                        "src": src,
                        "is_total": cand_total,
                        "subtypes": [src.get("type_substring", "") or "(unspecified)"],
                        "hs_prefix": hs_prefix,
                    }
                    continue
                existing = deduped[key]
                existing["subtypes"].append(
                    src.get("type_substring", "") or "(unspecified)",
                )
                # Decide whether to swap.  Keep existing unless the
                # candidate strictly outranks it.
                if cand_total and not existing["is_total"]:
                    existing["src"] = src
                    existing["is_total"] = True
                elif cand_total == existing["is_total"]:
                    if src.get("production_share", 0) > existing["src"].get("production_share", 0):
                        existing["src"] = src
                # else: existing wins (it's a 'total' row, candidate isn't)

            for (hs_prefix, country_code), entry in deduped.items():
                src = entry["src"]
                subtypes = entry["subtypes"]
                hs_mapping = s.scalar(
                    select(HsCodeMaterialMapping).where(
                        HsCodeMaterialMapping.material_id == material.id,
                        HsCodeMaterialMapping.hs_code_prefix == hs_prefix,
                        HsCodeMaterialMapping.market_scope == "global",
                    )
                )
                if hs_mapping is None:
                    us_import_shares_skipped_no_mapping += 1
                    typer.echo(
                        f"  [skip-us] no hs_code_material_mappings row for "
                        f"({material.canonical_name!r}, {hs_prefix!r}, global) — "
                        f"run seed-hs-mappings first",
                        err=True,
                    )
                    continue
                # Build the notes — flag when multiple sub-types collapsed.
                if len(subtypes) == 1:
                    notes_text = (
                        f"US import source: {subtypes[0]!r} "
                        f"({src.get('reference_year_range', '')}) — "
                        f"derived from MCS {mcs_year} Import Sources section"
                    )
                else:
                    chosen = src.get("type_substring", "") or "(unspecified)"
                    notes_text = (
                        f"US import source: {chosen!r} chosen from "
                        f"{len(subtypes)} sub-types {subtypes} "
                        f"({src.get('reference_year_range', '')}) — "
                        f"derived from MCS {mcs_year} Import Sources section. "
                        f"Multi-sub-type collapse — see hs_code_production_shares "
                        f"notes for the chosen-row provenance."
                    )

                existing_us_share = s.scalar(
                    select(HsCodeProductionShare).where(
                        HsCodeProductionShare.hs_mapping_id == hs_mapping.id,
                        HsCodeProductionShare.country_code == country_code,
                        HsCodeProductionShare.reference_year == mcs_year,
                        HsCodeProductionShare.market_scope == "us",
                        HsCodeProductionShare.source == "usgs_mcs",
                    )
                )
                if existing_us_share is None:
                    s.add(HsCodeProductionShare(
                        hs_mapping_id=hs_mapping.id,
                        country_code=country_code,
                        reference_year=mcs_year,
                        production_share=src["production_share"],
                        production_volume=None,
                        market_scope="us",
                        source="usgs_mcs",
                        notes=notes_text,
                    ))
                    us_import_shares_written += 1
                elif force:
                    existing_us_share.production_share = src["production_share"]
                    existing_us_share.notes = notes_text
                    us_import_shares_written += 1

        s.commit()
        typer.echo(json.dumps({
            "ok": True,
            "source": f"USGS Mineral Commodity Summaries {mcs_year}",
            "csv_format": csv_format,
            "records_resolved": len(resolved_canonicals),
            "records_skipped_via_alias": len(skipped_aliases),
            "records_unknown": len(unknown_aliases),
            "signals_written": signals_written,
            "shares_written": shares_written,
            "capacity_shares_written": capacity_shares_written,
            "hs_shares_written": hs_shares_written,
            "hs_shares_skipped_no_mapping": hs_shares_skipped_no_mapping,
            # Step 2A (2026-06): same-stage HHI propagation — see CLI
            # initialisation block for full rationale.
            "hs_shares_propagated": hs_shares_propagated,
            "hs_shares_propagation_targets_skipped": hs_shares_propagation_targets_skipped,
            "us_import_shares_written": us_import_shares_written,
            "us_import_shares_skipped_no_mapping": us_import_shares_skipped_no_mapping,
            # 2026-06-14: USGS Salient Price extraction (mcs2026_parser.{_extract_prices_from_salient,
            # _derive_yoy_and_cagr_from_prices}).  Flows annual unit values from
            # the Salient Statistics → Price column rows into commodity_prices,
            # and derives YoY / 5-yr CAGR signals into material_criticality_signals
            # (only when those columns are NULL — Fig 10 retains authority).
            "commodity_prices_inserted": commodity_prices_inserted,
            "commodity_prices_skipped_existing": commodity_prices_skipped_existing,
            "commodity_prices_hs_resolved": commodity_prices_hs_resolved,
            "commodity_prices_hs_fallback": commodity_prices_hs_fallback,
            "criticality_growth_filled": criticality_growth_filled,
            "criticality_growth_skipped_existing": criticality_growth_skipped_existing,
            "materials": sorted(set(resolved_canonicals)),
            "skipped_via_alias": sorted(set(skipped_aliases)),
            "unknown_source_names": sorted(set(unknown_aliases)),
        }, indent=2))
    finally:
        s.close()


@app.command("ingest-mcs-prices")
def ingest_mcs_prices_cmd(
    filepath: str = typer.Argument(
        ...,
        help=(
            "Path to MCS Fig 10 price growth CSV (e.g. "
            "data/usgs/2026/MCS2026_Fig10_Price_Growth_Rates.csv)."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Update price columns even if already populated for this material+year.",
    ),
    mcs_year: int = typer.Option(
        2026,
        "--mcs-year",
        help=(
            "Publication year of the MCS file (joins to the same "
            "material_criticality_signals row written by ingest-usgs)."
        ),
    ),
) -> None:
    """Ingest USGS MCS Fig 10 price-growth rates into material_criticality_signals.

    Reads the supplementary Fig 10 CSV and writes price_yoy_pct and
    price_cagr_5yr_pct onto the existing usgs_mcs signal row for each
    material (creating the row if it doesn't exist yet — useful for
    materials in seed_materials but not in the main MCS chapter map,
    e.g. individual REEs).

    Run after ingest-usgs so the signal row already exists; otherwise
    this command will create skeleton rows with only price metrics
    populated.

    Idempotent without --force: skips rows where price_yoy_pct is
    already non-null.  --force overwrites.
    """
    from collections import defaultdict
    from pathlib import Path
    from app.models.criticality_signal import MaterialCriticalitySignal
    from app.services.ingestion.seeds.mcs2026_fig10_parser import (
        parse_mcs2026_fig10_csv,
    )
    from app.services.ingestion.material_resolver import MaterialAliasResolver

    path = Path(filepath)
    if not path.exists():
        typer.echo(f"File not found: {filepath}", err=True)
        raise typer.Exit(code=1)

    raw_rows = parse_mcs2026_fig10_csv(path)
    if not raw_rows:
        typer.echo("No price rows parsed — check the file format.", err=True)
        raise typer.Exit(code=1)

    s = _session()
    try:
        resolver = MaterialAliasResolver(s)

        # ── Resolve each raw row, then aggregate by canonical material ────
        # Multiple Fig 10 rows can map to the same canonical (e.g. both
        # Fluorspar grades → "Fluorspar"; 10 individual REE oxides →
        # "Rare Earth Elements"); we average the metrics across them.
        # arithmetic mean is the simplest defensible aggregation given
        # Fig 10 carries no volume weights.
        bucket: dict[int, dict] = defaultdict(
            lambda: {"yoy": [], "cagr": [], "sources": [], "material": None}
        )
        skipped_aliases: list[str] = []
        unknown_aliases: list[str] = []

        for r in raw_rows:
            result = resolver.resolve(r["source_system"], r["source_name"])
            material, status = result.material, result.status
            if status == "skipped":
                skipped_aliases.append(r["source_name"])
                continue
            if status == "unknown":
                unknown_aliases.append(r["source_name"])
                typer.echo(
                    f"  [unknown] no alias for ('fig10_prices', "
                    f"{r['source_name']!r}) — add a row to "
                    f"seed_material_source_aliases.py.",
                    err=True,
                )
                continue
            assert material is not None
            b = bucket[material.id]
            b["material"] = material
            if r["price_yoy_pct"] is not None:
                b["yoy"].append(r["price_yoy_pct"])
            if r["price_cagr_5yr_pct"] is not None:
                b["cagr"].append(r["price_cagr_5yr_pct"])
            b["sources"].append(r["source_name"])

        rows_inserted = 0
        rows_updated = 0
        rows_skipped_existing = 0

        for mat_id, b in bucket.items():
            material = b["material"]
            avg_yoy = (
                round(sum(b["yoy"]) / len(b["yoy"]), 4) if b["yoy"] else None
            )
            avg_cagr = (
                round(sum(b["cagr"]) / len(b["cagr"]), 4) if b["cagr"] else None
            )
            sources = b["sources"]

            signal = s.scalar(
                select(MaterialCriticalitySignal).where(
                    MaterialCriticalitySignal.material_id == mat_id,
                    MaterialCriticalitySignal.source == "usgs_mcs",
                    MaterialCriticalitySignal.reference_year == mcs_year,
                )
            )
            if signal is None:
                # Create a skeleton signal row.  Other supply metrics
                # (HHI, production_yoy_pct etc.) stay NULL until
                # ingest-usgs runs for this material.
                signal = MaterialCriticalitySignal(
                    material_id=mat_id,
                    source="usgs_mcs",
                    reference_year=mcs_year,
                    price_yoy_pct=avg_yoy,
                    price_cagr_5yr_pct=avg_cagr,
                    metadata_json={
                        "mcs_publication_year": mcs_year,
                        "fig10_source_rows": sources,
                    },
                )
                s.add(signal)
                rows_inserted += 1
                continue

            if signal.price_yoy_pct is not None and not force:
                rows_skipped_existing += 1
                continue

            signal.price_yoy_pct = avg_yoy
            signal.price_cagr_5yr_pct = avg_cagr
            meta = dict(signal.metadata_json or {})
            meta["fig10_source_rows"] = sources
            signal.metadata_json = meta
            rows_updated += 1

        s.commit()
        typer.echo(json.dumps({
            "ok": True,
            "source": f"USGS MCS {mcs_year} Fig 10 — Price Growth Rates",
            "rows_in_csv": len(raw_rows),
            "materials_resolved": len(bucket),
            "rows_inserted": rows_inserted,
            "rows_updated": rows_updated,
            "rows_skipped_existing_use_force": rows_skipped_existing,
            "skipped_via_alias": sorted(set(skipped_aliases)),
            "unknown_source_names": sorted(set(unknown_aliases)),
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


@app.command("seed-material-aliases")
def seed_material_aliases_cmd(
    force_update: bool = typer.Option(
        False,
        "--force-update",
        help=(
            "Update existing alias rows (canonical mapping, is_skipped, "
            "skip_reason) when re-seeding.  Use after editing the alias "
            "lists in seed_material_source_aliases.py."
        ),
    ),
) -> None:
    """Seed the material_source_aliases reference table.

    Translates external commodity names (USGS MCS chapters, Fig 10
    commodity rows, MCS PDF headings, etc.) to canonical material rows.
    Replaces the four legacy Python dicts (``_CHAPTER_TO_MATERIAL``,
    ``_PRICE_NAME_TO_MATERIAL``, ``_MCS_COMMODITY_MAP``,
    ``_COMMODITY_CONFIG``) with one DB-backed register.

    Run AFTER ``seed-materials`` (alias rows FK-reference materials.id).

    Idempotent without --force-update: skips existing alias rows so a
    re-seed is a no-op.  With --force-update: rewrites the canonical
    mapping / skip flag / skip reason so partner edits propagate.
    """
    from app.services.ingestion.seed_material_source_aliases import run_seed

    s = _session()
    try:
        result = run_seed(s, force_update=force_update)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("seed-country-material-relevance")
def seed_country_material_relevance_cmd(
    force_update: bool = typer.Option(
        False,
        "--force-update",
        help=(
            "Overwrite partner-curated rows in addition to the auto-derived "
            "MCS-share rows.  Without this flag, partner edits to tier / "
            "is_hcg / notes survive re-seed; MCS-sourced producer rows "
            "always refresh from the latest material_production_shares."
        ),
    ),
) -> None:
    """Seed per-(country, material, role) relevance flags.

    Producer rows are auto-derived from material_production_shares using
    share-based thresholds:
        tier='top'   when share >= 30%
        tier='mid'   when 10% <= share < 30%
        tier='minor' when 1%  <= share < 10%
        is_hcg=TRUE  when share >= 40%

    Consumer rows are seeded as partner-curated placeholders at
    tier='minor' for each launch material x is_major_consumer country.
    Partner refines tier values over time; Phase 2 may auto-derive from
    Comtrade import shares once that data is fully backfilled.

    Scope: the 10 launch-list materials (Lithium, Cobalt, Nickel,
    Manganese, Natural Graphite, Phosphate, Iron Ore
    (LFP Grade), Copper, Aluminum, Rare Earth Elements).

    Prerequisites: seed-materials, seed-countries, ingest-usgs (so
    material_production_shares has data).

    Idempotent without --force-update: re-running refreshes MCS-derived
    rows from the latest USGS data while preserving partner edits.
    """
    from app.services.ingestion.seed_country_material_relevance import run_seed

    s = _session()
    try:
        result = run_seed(s, force_update=force_update)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("seed-regulation-aliases")
def seed_regulation_aliases_cmd(
    force_update: bool = typer.Option(
        False,
        "--force-update",
        help=(
            "Update existing alias rows (regulation_id mapping, is_skipped, "
            "skip_reason) when re-seeding.  Use after editing the alias "
            "lists in seed_regulation_aliases.py."
        ),
    ),
) -> None:
    """Seed the regulation_aliases reference table.

    Translates external regulation identifiers (EUR-Lex CELEX numbers,
    Federal Register document IDs, US Code citations, etc.) to canonical
    regulation rows.  Mirrors seed-material-aliases for the regulations
    schema.

    Run AFTER ``seed-regulations`` (alias rows FK-reference regulations.id).

    Idempotent without --force-update: skips existing alias rows so a
    re-seed is a no-op.  With --force-update: rewrites the canonical
    mapping / skip flag / skip reason so partner edits propagate.
    """
    from app.services.ingestion.seed_regulation_aliases import run_seed

    s = _session()
    try:
        result = run_seed(s, force_update=force_update)
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
        help=(
            "Update curated fields (description, confidence, supply_chain_stage, "
            "stage_sequence, digit_count, keywords) on existing rows.  "
            "Non-destructive — does NOT delete rows added by ingest-mcs-pdf "
            "or other ingesters; preserves auto-created 6-digit stubs and "
            "10-digit HTS rows.  Use after editing _MAPPINGS or "
            "_HS_KEYWORDS_BY_MAPPING."
        ),
    ),
) -> None:
    """Seed hs_code_material_mappings from the curated _MAPPINGS table.

    Idempotent without --force: skips existing (hs_code_prefix, material_id)
    pairs.  Inserts rows from _MAPPINGS that don't yet exist.

    With --force: in-place UPDATE of curated fields on existing rows.  Does
    NOT delete any rows — auto-added rows from ingest-mcs-pdf (6-digit stubs
    and 10-digit HTS codes) and any partner-curated entries are preserved.
    Pass-through to ``upsert_hs_mappings(force=True)``; also overwrites
    keywords on rows whose ``keywords`` array is already populated.

    Re-run when:
      * New materials are added to the materials table
      * A material's HS code classification changes in _MAPPINGS
      * Stage assignments or confidence values are corrected
      * Keyword definitions in _HS_KEYWORDS_BY_MAPPING change

    Note: materials must be seeded first (run seed-materials).

    For a true rebuild from scratch (e.g. WCO HS nomenclature update), do
    that as a targeted SQL migration — blanket-deleting this table
    cascade-deletes hs_code_production_shares and SET NULLs
    risk_events.hs_mapping_id, which is rarely what you want.
    """
    from app.services.ingestion.seed_hs_mappings import upsert_hs_mappings

    s = _session()
    try:
        result = upsert_hs_mappings(s, force=force)
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
    from app.services.scoring.chemistry_risk import (
        score_all_chemistries_from_rollup,
        score_chemistry_from_rollup,
    )
    from app.models.battery_chemistry import BatteryChemistry

    as_of_date = datetime.date.fromisoformat(as_of) if as_of else datetime.date.today()

    s = _session()
    try:
        if slug:
            chem = s.scalar(select(BatteryChemistry).where(BatteryChemistry.slug == slug))
            if chem is None:
                typer.echo(json.dumps({"ok": False, "error": f"Chemistry slug '{slug}' not found"}), err=True)
                raise typer.Exit(code=1)
            score = score_chemistry_from_rollup(s, chem.id, as_of_date)
            s.commit()
            typer.echo(json.dumps({
                "ok": True,
                "chemistry": slug,
                "methodology_version": score.methodology_version,
                "composite_risk_score": score.composite_risk_score,
                "score_confidence": score.score_confidence,
            }, indent=2))
        else:
            results = score_all_chemistries_from_rollup(s, as_of_date)
            typer.echo(json.dumps({
                "ok": True,
                "rescored": len(results),
                "scores": [
                    {
                        "chemistry": r["slug"],
                        "methodology_version": "2.0",
                        "composite_risk_score": r["composite_risk_score"],
                        "score_confidence": r["score_confidence"],
                        "materials_scored": r.get("materials_scored"),
                        "materials_missing": r.get("materials_missing"),
                    }
                    for r in results
                ],
            }, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("rescore-hs-nodes")
def rescore_hs_nodes_cmd(
    as_of: Optional[str] = typer.Option(
        None,
        "--as-of",
        help="Point-in-time date for scoring (YYYY-MM-DD). Default: today.",
    ),
    market_scope: str = typer.Option(
        "global",
        "--market-scope",
        help="'global' (default) or 'us'. Must match the scope of the production share data.",
    ),
    material_id: Optional[int] = typer.Option(
        None,
        "--material-id",
        help=(
            "Restrict scoring to the HS mappings belonging to this material. "
            "Useful for iterative validation runs that avoid a full rescore. "
            "Resolves to the set of ``hs_code_material_mappings.id`` rows where "
            "``material_id`` matches, and passes that set to score_all_hs_nodes."
        ),
    ),
) -> None:
    """Compute and persist Level-0 HS node geography scores.

    Scores every (hs_mapping_id × country) pair that has production share data
    in hs_code_production_shares.  Results feed into the stage-weighted Material
    Concentration rollup in rescore-market.

    Run this BEFORE rescore-market to ensure Level-0 rows are available.

    \b
    Examples:
      bdi-ingest rescore-hs-nodes
      bdi-ingest rescore-hs-nodes --as-of 2025-01-01
      bdi-ingest rescore-hs-nodes --market-scope us
      bdi-ingest rescore-hs-nodes --material-id 3          # score Copper's HS nodes only

    Pipeline order:
      bdi-ingest rescore-hs-nodes    ← Level 0 (this command)
      bdi-ingest rescore-market      ← Level 1
      bdi-ingest rescore-chemistry   ← Level 3
    """
    import datetime

    from sqlalchemy import select

    from app.models.supply import HsCodeMaterialMapping, Material
    from app.services.scoring.hs_node_scorer import score_all_hs_nodes

    as_of_date = datetime.date.fromisoformat(as_of) if as_of else datetime.date.today()
    s = _session()
    try:
        hs_mapping_ids: Optional[list[int]] = None
        material_name: Optional[str] = None
        if material_id is not None:
            # Resolve the material row so we can log its name and confirm it exists
            # before scoping the HS mapping lookup.
            material = s.get(Material, material_id)
            if material is None:
                typer.echo(
                    f"rescore-hs-nodes failed: no material with id={material_id}",
                    err=True,
                )
                raise typer.Exit(code=2)
            material_name = material.canonical_name
            mapping_ids = list(
                s.scalars(
                    select(HsCodeMaterialMapping.id).where(
                        HsCodeMaterialMapping.material_id == material_id
                    )
                ).all()
            )
            if not mapping_ids:
                typer.echo(
                    f"rescore-hs-nodes: no HS mappings for material id={material_id} "
                    f"({material_name}); nothing to score.",
                    err=True,
                )
                raise typer.Exit(code=1)
            hs_mapping_ids = mapping_ids
            typer.echo(
                f"Scoping to material_id={material_id} ({material_name}) — "
                f"{len(mapping_ids)} HS mapping(s)."
            )

        typer.echo(
            f"Scoring HS nodes as of {as_of_date} (market_scope={market_scope}"
            + (f", material={material_name}" if material_name else "")
            + ") ..."
        )
        result = score_all_hs_nodes(
            s,
            as_of_date,
            market_scope=market_scope,
            hs_mapping_ids=hs_mapping_ids,
        )
        typer.echo(
            f"Done. pairs_scored={result['pairs_scored']}, "
            f"pairs_skipped={result['pairs_skipped']}, "
            f"nodes_processed={result['nodes_processed']}"
        )
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
            "Default: derived from each material's material_production_shares plus "
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
    from app.models.supply import Material, MaterialProductionShare
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
                # V1 (4.0, 2026-07-18): producers + trade-gate exporters
                # only — event-only geographies are skipped (zero
                # concentration, zero L2 trade weight; were ~85% of
                # pairs).  See stage_concentration.derive_scoring_geographies.
                from app.services.scoring.stage_concentration import (
                    derive_scoring_geographies,
                )
                geos = derive_scoring_geographies(s, mat.id)

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
        help=(
            "Comma-separated ISO2 reporter codes to limit scope (e.g. 'CN,CL,AU'). "
            "Default: REPORTER_COUNTRIES for exports, CONSUMER_COUNTRIES for imports."
        ),
    ),
    hs_prefixes: Optional[str] = typer.Option(
        None,
        "--hs-prefixes",
        help=(
            "Comma-separated HS prefixes to query (e.g. '2604,2602'). "
            "Default: derives from hs_code_material_mappings (run "
            "`bdi-ingest seed-hs-mappings` first); falls back to "
            "supply_chain_contexts when the mappings table is empty."
        ),
    ),
    flow_code: str = typer.Option(
        "X",
        "--flow-code",
        help="'X' for exports (default) or 'M' for imports.",
    ),
) -> None:
    """Ingest UN Comtrade annual trade flows for battery-critical HS codes.

    Run with --flow-code X (default) to capture supply-side concentration from
    producing countries. Run with --flow-code M to capture demand-side dependency
    from consuming countries (US, JP, KR, DE, FR, GB, BE, IN).

    Both runs are idempotent. Re-run annually when new data becomes available
    (~3-month lag from Comtrade). Run build-trade-signals after both runs to
    generate EXPORT_DROP and IMPORT_DROP risk events.

    \b
    Examples:
      bdi-ingest ingest-comtrade --years 2021,2022,2023
      bdi-ingest ingest-comtrade --years 2021,2022,2023 --flow-code M
      bdi-ingest ingest-comtrade --years 2023 --flow-code M --reporters US,JP,KR,DE
    """
    from app.services.ingestion.comtrade import (
        CONSUMER_COUNTRIES,
        REPORTER_COUNTRIES,
        ComtradeRateLimitExhausted,
        ingest_comtrade,
    )

    year_list = [int(y.strip()) for y in years.split(",")]
    flow = flow_code.strip().upper()
    if flow not in ("X", "M"):
        typer.echo("--flow-code must be 'X' (exports) or 'M' (imports).", err=True)
        raise typer.Exit(code=1)

    reporter_filter = None
    if reporters:
        all_countries = {**REPORTER_COUNTRIES, **CONSUMER_COUNTRIES}
        codes = [r.strip().upper() for r in reporters.split(",")]
        reporter_filter = {k: v for k, v in all_countries.items() if k in codes}
        unknown = set(codes) - set(all_countries.keys())
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
            flow_code=flow,
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except ComtradeRateLimitExhausted as exc:
        # Distinct exit path so the user sees a clear "rate-limited, try
        # again later" message instead of a stack trace.  Rows fetched
        # before the breaker tripped are already committed; re-running
        # will skip them via the source-document dedup.
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "rate_limited": True,
                    "hint": (
                        "Comtrade free tier caps at ~500 calls/day. "
                        "Re-run after the quota resets — already-ingested "
                        "(reporter × prefix × year) combinations will be skipped."
                    ),
                },
                indent=2,
            ),
            err=True,
        )
        raise typer.Exit(code=2)
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("backfill-trade-flow-hs-mappings")
def backfill_trade_flow_hs_mappings_cmd(
    batch_size: int = typer.Option(
        2000,
        "--batch-size",
        help="Number of TradeFlow rows to fetch and update per pass.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "If set, count rows that WOULD be updated without modifying any "
            "data.  Pass --no-dry-run (default) to actually write."
        ),
    ),
) -> None:
    """Populate ``TradeFlow.hs_mapping_id`` for rows missing it.

    Migration 027 added the ``hs_mapping_id`` column but rows ingested
    before then have it set to NULL.  After the 2026-05-09 confidence-
    weighting changes those NULL rows fall through at confidence=1.0 via
    ``COALESCE``, masking the very signal the weighting exists to expose.

    This command re-resolves each affected row's HS code against the
    current ``hs_code_material_mappings`` and:

      * Updates ``hs_mapping_id`` when the resolved material matches the
        row's stored ``material_id``.
      * Logs (does NOT touch) rows where the resolved material differs
        from the stored one — that's mapping drift, surfaced for review.
      * Logs and skips rows whose HS code no longer resolves at all.

    Idempotent: re-running after a successful pass is a no-op because the
    filter excludes rows that already have ``hs_mapping_id`` set.

    Use ``--dry-run`` to see how many rows would change without writing.
    """
    from app.services.ingestion.comtrade import backfill_trade_flow_hs_mappings
    from app.models.supply import TradeFlow
    from sqlalchemy import select, func as sqlfunc

    s = _session()
    try:
        if dry_run:
            # Count without mutating: same filter the backfill uses.
            null_rows = s.scalar(
                select(sqlfunc.count())
                .select_from(TradeFlow)
                .where(
                    TradeFlow.hs_mapping_id.is_(None),
                    TradeFlow.material_id.is_not(None),
                )
            )
            typer.echo(json.dumps({
                "ok": True,
                "dry_run": True,
                "candidate_rows": int(null_rows or 0),
                "hint": "drop --dry-run to actually write",
            }, indent=2))
            return

        result = backfill_trade_flow_hs_mappings(s, batch_size=batch_size)
        typer.echo(json.dumps({"ok": True, **result}, indent=2, default=str))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("reattribute-unmapped-trade-flows")
def reattribute_unmapped_trade_flows_cmd(
    batch_size: int = typer.Option(
        2000,
        "--batch-size",
        help="Number of TradeFlow rows to fetch and update per pass.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "If set, count rows that WOULD be examined without modifying "
            "any data.  Pass --no-dry-run (default) to actually write."
        ),
    ),
) -> None:
    """Re-resolve TradeFlow rows whose ``material_id`` is NULL.

    Use this after expanding ``hs_code_material_mappings`` (via
    ``bdi-ingest seed-hs-mappings --force``) to pick up the new mappings
    against historical TradeFlow rows that were previously unresolvable.

    Sister command to ``backfill-trade-flow-hs-mappings``:

      backfill-trade-flow-hs-mappings
        Fills in ``hs_mapping_id`` for rows that already have
        ``material_id`` set.  Conservative — never overwrites attribution.

      reattribute-unmapped-trade-flows (this command)
        Writes BOTH ``material_id`` and ``hs_mapping_id`` for rows where
        the previous resolution produced None and the current seed gives
        a valid answer.  Safe — only adds attribution to previously-
        unattributed rows; nothing existing gets overwritten.  Logged at
        WARNING per row so the data trail is discoverable.

    Idempotent.  After running, the rows that now have ``material_id``
    set fall off the NULL filter.  Genuinely unresolvable rows (HS code
    still doesn't map to anything) are re-examined on subsequent runs
    but produce zero updates.

    Re-run scoring after this completes (``rescore-market``,
    ``rescore-global-rollups``) so the new attribution flows through.

    Use ``--dry-run`` to see how many rows would be examined.
    """
    from app.services.ingestion.comtrade import (
        reattribute_unmapped_trade_flows,
    )
    from app.models.supply import TradeFlow
    from sqlalchemy import select, func as sqlfunc

    s = _session()
    try:
        if dry_run:
            null_rows = s.scalar(
                select(sqlfunc.count())
                .select_from(TradeFlow)
                .where(TradeFlow.material_id.is_(None))
            )
            typer.echo(json.dumps({
                "ok": True,
                "dry_run": True,
                "candidate_rows": int(null_rows or 0),
                "hint": (
                    "drop --dry-run to actually re-resolve.  Some of these "
                    "may still be unresolvable after running — that's "
                    "expected for HS prefixes the seed doesn't cover."
                ),
            }, indent=2))
            return

        result = reattribute_unmapped_trade_flows(s, batch_size=batch_size)
        typer.echo(json.dumps({"ok": True, **result}, indent=2, default=str))
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


@app.command("load-manual-risk-events")
def load_manual_risk_events_cmd(
    seed_path: str = typer.Option(
        ...,
        "--seed-path",
        help="Path to manual_risk_events.xlsx (relative or absolute).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate everything; do not write to DB or spreadsheet.",
    ),
    reload: bool = typer.Option(
        False,
        "--reload",
        help="Re-process rows even if load_status=loaded "
             "(idempotency still applies via content_hash).",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Per-row status output."
    ),
) -> None:
    """Load manually-extracted risk events from xlsx into risk_events + junctions.

    Reads the "Risk Events" sheet in the given xlsx, validates each row,
    resolves FK references (companies, materials, facilities, regulations)
    against seed tables, and writes to risk_events + junction tables.

    Idempotent via content_hash. Writes load_status back to the spreadsheet
    so you can see what landed.

    Example:
        bdi-ingest load-manual-risk-events \\
            --seed-path "Automotive Data Solutions/manual_risk_events.xlsx" \\
            --dry-run --verbose
    """
    from pathlib import Path
    from scripts.load_manual_risk_events import load

    path = Path(seed_path).expanduser().resolve()
    if not path.exists():
        typer.echo(json.dumps({"ok": False, "error": f"file_not_found:{path}"}), err=True)
        raise typer.Exit(code=1)

    try:
        counts = load(
            seed_path=path,
            dry_run=dry_run,
            only_new=not reload,
            verbose=verbose,
        )
        typer.echo(json.dumps({
            "ok": True,
            "mode": "dry-run" if dry_run else "load",
            "seed_path": str(path),
            **counts,
        }, indent=2))
        if counts.get("error", 0) > 0:
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)


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


@app.command("seed-facilities-partner")
def seed_facilities_partner_cmd(
    xlsx_path: str = typer.Argument(
        ...,
        help="Path to the partner-curated facility XLSX template "
             "(see facility_seed_template.xlsx).",
    ),
    sheet: str = typer.Option(
        "Facility Seed", "--sheet",
        help="Sheet name within the workbook.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Validate + report without committing any changes.",
    ),
    include_examples: bool = typer.Option(
        False, "--include-examples",
        help="Load the Albemarle/Mineral Resources example rows. "
             "Off by default so partners can keep them as documentation.",
    ),
) -> None:
    """Load a partner-curated facility seed spreadsheet (G4c).

    Reads ``Facility Seed`` sheet from the provided XLSX and upserts:
        companies → facilities → company_facilities → facility_material_links

    Idempotent: re-running on the same file is a no-op for unchanged rows.
    JV facilities (multiple companies on the same site) are captured by
    repeating the facility row for each owner with their respective
    ownership_pct.

    \b
    Run order to close the operational pillar:
      bdi-ingest seed-materials                       # canonical materials
      bdi-ingest seed-hs-mappings                     # HS code mappings
      bdi-ingest seed-facilities-partner <file.xlsx>  # this command
      bdi-ingest rescore-hs-nodes                     # picks up new facilities
      bdi-ingest rescore-market                       # operational pillar refresh
    """
    from app.services.ingestion.seed_facilities_partner import (
        load_partner_facility_seed,
    )

    s = _session()
    try:
        report = load_partner_facility_seed(
            session=s,
            xlsx_path=xlsx_path,
            sheet=sheet,
            dry_run=dry_run,
            include_examples=include_examples,
        )
        out = report.to_dict()
        out["dry_run"] = dry_run
        typer.echo(json.dumps({"ok": True, **out}, indent=2, default=str))
        if report.errors:
            # Non-zero exit when there were per-row failures so CI / cron
            # picks up partial-load issues.
            raise typer.Exit(code=2)
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("find-duplicate-events")
def find_duplicate_events_cmd(
    out_path: str = typer.Option(
        "duplicate_events_review.xlsx", "--out",
        help="Path for the xlsx review sheet.",
    ),
    material: str = typer.Option(
        None, "--material",
        help="Limit to one material (canonical name, e.g. 'Cobalt').",
    ),
    min_similarity: float = typer.Option(
        0.35, "--min-similarity",
        help="Title-similarity floor for candidate pairs (0-1).",
    ),
) -> None:
    """Cross-source duplicate-event similarity report (migration 055).

    Pairs canonical events from DIFFERENT sources that share a material +
    geography, have compatible subtypes and dates, and similar titles.
    Writes an xlsx with a blank ``confirm`` column — review it, set
    confirm=Y on real duplicates (swap ids if the suggested canonical is
    wrong; manual-walkthrough rows are suggested canonical by default),
    then run ``mark-duplicate-events``.  Nothing is flagged automatically.
    """
    from app.services.ingestion.event_dedupe import find_duplicate_candidates

    s = _session()
    try:
        report = find_duplicate_candidates(
            s, out_path, material=material, min_similarity=min_similarity,
        )
        typer.echo(json.dumps(report, indent=2, default=str))
    finally:
        s.close()


@app.command("mark-duplicate-events")
def mark_duplicate_events_cmd(
    xlsx_path: str = typer.Argument(
        ..., help="Reviewed duplicate_events_review.xlsx (confirm column set).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate and report without committing.",
    ),
) -> None:
    """Apply confirmed duplicate marks from the reviewed similarity report.

    Sets ``risk_events.duplicate_of_id`` for rows with confirm=Y.  Flagged
    events are excluded from scoring, event counts, and public listings;
    the canonical row carries the event.  All-or-nothing: any validation
    error (self-marks, conflicting marks, cycles, unknown ids) aborts the
    whole apply.  Rescore afterwards for scores to reflect the change.
    """
    from app.services.ingestion.event_dedupe import apply_duplicate_marks

    s = _session()
    try:
        result = apply_duplicate_marks(s, xlsx_path, dry_run=dry_run)
        typer.echo(json.dumps(result, indent=2, default=str))
        if result["errors"]:
            raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("seed-benchmark-shares")
def seed_benchmark_shares_cmd(
    xlsx_path: str = typer.Argument(
        ...,
        help="Path to the benchmark shares workbook (benchmark_shares.xlsx).",
    ),
    sheet: str = typer.Option(
        "Benchmark Shares", "--sheet",
        help="Sheet name containing the share rows.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Validate and report without committing any changes.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Update existing benchmark rows in place (default: skip).",
    ),
) -> None:
    """Load human-audited benchmark production shares (gap-coverage).

    Fills hs_code_production_shares for midstream / battery-grade nodes
    that USGS MCS does not cover — see
    docs/design/concentration_coverage_gaps.md.  Rows load ONLY when
    audited == "Y"; every row must cite basis + source_org + source_url.
    DB source becomes benchmark_<org> so MCS re-runs never touch these
    rows.  All-or-nothing per file: any validation error aborts the load.

    \b
    Run order (cobalt pilot):
      bdi-ingest seed-hs-mappings                    # 2822 stage fix
      bdi-ingest seed-benchmark-shares <file.xlsx> --dry-run
      bdi-ingest seed-benchmark-shares <file.xlsx>
      bdi-ingest rescore-hs-nodes && bdi-ingest rescore-market ...
    """
    from app.services.ingestion.seed_benchmark_shares import (
        seed_benchmark_shares,
    )

    s = _session()
    try:
        report = seed_benchmark_shares(
            session=s,
            xlsx_path=xlsx_path,
            sheet=sheet,
            dry_run=dry_run,
            force=force,
        )
        typer.echo(json.dumps(report.as_dict(), indent=2, default=str))
        if report.errors:
            raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("seed-company-workbook")
def seed_company_workbook_cmd(
    xlsx_path: str = typer.Argument(
        ...,
        help="Path to the partner company-seed workbook "
             "(company_seed_expanded_v32.xlsx or later — must have the "
             "'publish' column).",
    ),
    sheet: str = typer.Option(
        "Company Seed", "--sheet",
        help="Company identity/enrichment sheet name.",
    ),
    cme_sheet: str = typer.Option(
        "Company Material Exposure", "--cme-sheet",
        help="Material-exposure sheet name.",
    ),
    sr_sheet: str = typer.Option(
        "Supply Relationships", "--sr-sheet",
        help="Supply-relationships sheet name.",
    ),
    skip_exposures: bool = typer.Option(
        False, "--skip-exposures",
        help="Do not load the Company Material Exposure tab.",
    ),
    skip_relationships: bool = typer.Option(
        False, "--skip-relationships",
        help="Do not load the Supply Relationships tab.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Report without committing any changes.",
    ),
) -> None:
    """Load the partner company-seed workbook (G4d) — enrichment layer.

    Company Seed tab: enriches companies already created by
    ``seed-companies`` (cik/lei/sic/duns/tickers/compliance flags/notes),
    resolving via canonical name OR alias; creates only publish=TRUE rows
    that resolve nowhere. Never renames, never re-parents a company whose
    parent is already set.

    Company Material Exposure tab: loads filing facts + hybrid
    exposure_score (curated seed dict where the pair exists, else derived
    from revenue_share_pct at low confidence — flagged via
    score_derivation for partner review). Gated on the company's publish
    flag.

    Supply Relationships tab: loads agreement detail; publish gates
    company CREATION only — rows between companies already in the DB load
    regardless of their publish flag. Never creates a company from this
    tab.

    Idempotent: re-running on the same file updates in place.

    \b
    Run order:
      bdi-ingest seed-companies                     # identity layer first
      bdi-ingest seed-company-workbook <file.xlsx>  # this command
      bdi-ingest seed-facilities-partner <file.xlsx>
      python scripts/load_manual_risk_events.py <file.xlsx>
    """
    from app.services.ingestion.seed_company_workbook import (
        seed_company_workbook,
    )

    s = _session()
    try:
        report = seed_company_workbook(
            session=s,
            xlsx_path=xlsx_path,
            sheet=sheet,
            cme_sheet=cme_sheet,
            sr_sheet=sr_sheet,
            load_exposures=not skip_exposures,
            load_relationships=not skip_relationships,
            dry_run=dry_run,
        )
        out = report.as_dict()
        out["dry_run"] = dry_run
        typer.echo(json.dumps({"ok": True, **out}, indent=2, default=str))
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


@app.command("ingest-regulation-workbook")
def ingest_regulation_workbook_cmd(
    xlsx_path: str = typer.Argument(
        ..., help="Path to the regulation curation workbook (regulations_workbook_v1.xlsx)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Build the full diff report (created/updated/archived/rejected) and roll back.",
    ),
) -> None:
    """Sync curated regulations to the partner workbook (SOURCE OF TRUTH).

    Upserts regulations/weights/scopes from the workbook; curated regulations
    missing from it are archived (never deleted). SUGGESTED_* discovery rows
    are ignored. New regulations require source_url. Run with --dry-run first
    to review the diff.

    Example:
    \b
      bdi-ingest ingest-regulation-workbook "Automotive Data Solutions/regulations_workbook_v1.xlsx" --dry-run
    """
    from app.services.ingestion.regulation_workbook import load_regulation_workbook

    s = _session()
    try:
        report = load_regulation_workbook(s, xlsx_path, dry_run=dry_run)
        typer.echo(json.dumps(report.to_dict(), indent=2, default=str))
    finally:
        s.close()

@app.command("seed-regulations")
def seed_regulations_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Update scope_type and notes on existing regulation_material_scope and "
            "regulation_geography_scope rows with current seed values.  "
            "Non-destructive — does NOT delete scope rows that exist in the DB "
            "but are absent from the seed.  Use after correcting scope "
            "classifications in _MATERIAL_SCOPES or _GEOGRAPHY_SCOPES."
        ),
    ),
) -> None:
    """Seed regulations, material/geography scopes, and company regulation exposures.

    Populates four tables: regulations, regulation_material_scope,
    regulation_geography_scope, and company_regulation_exposure.

    This unblocks the regulatory pillar (20% of company score). The three scored
    obligations are UFLPA (25 pts), EU_BATTERY_REG_2023 (20 pts), IRA_DOMESTIC (15 pts).
    Only non_compliant / partial / unknown rows generate scoring uplift.

    Idempotent: safe to re-run after updating compliance statuses in the seed file.
    With --force: also overwrites scope_type/notes on existing material and
    geography scope rows.

    Run order:
        bdi-ingest seed-companies    # companies must exist
        bdi-ingest ingest-usgs ...   # materials must exist
        bdi-ingest seed-regulations  # this command
    """
    from app.services.ingestion.seed_regulations import seed_regulations

    s = _session()
    try:
        result = seed_regulations(s, force=force)
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
    """Refresh EUR-Lex regulation summaries and create regulatory risk events.

    Iterates every active CELEX number in ``regulation_aliases``
    (source_system='eurlex_celex') and:
      * Backfills ``Regulation.summary`` from EUR-Lex HTML when NULL.
      * Bumps ``metadata_json.last_seen`` for freshness tracking.
      * Attaches ``source_document_id`` if not yet set.
      * Creates one ``RiskEvent`` + ``RiskEventRegulation`` junction
        per regulation (idempotent).

    Does NOT create regulations.  CELEX numbers without an alias row
    are surfaced as warnings — to track a new EU regulation, add it to
    ``seed_regulations.py:_REGULATIONS`` and ``seed_regulation_aliases.py:_EURLEX_CELEX``,
    then re-run ``seed-regulations`` and ``seed-regulation-aliases``.

    Idempotent: re-runs only refresh mutable fields and never duplicate
    risk events.  Pass ``--no-fetch`` to skip the HTTP fetch entirely
    (faster, no network required).

    Run order on a fresh DB:

    \b
      bdi-ingest seed-materials
      bdi-ingest seed-regulations
      bdi-ingest seed-regulation-aliases
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


@app.command("list-regulation-status-changes")
def list_regulation_status_changes_cmd() -> None:
    """List regulations whose EUR-Lex bibliographic notice suggests the
    status has changed since seed time.

    \b
    Reads ``Regulation.metadata_json["status_review_pending"]`` entries,
    populated by ``ingest-eurlex``.  Surfaces each one for partner
    confirmation: detected_status, current_status, observed dates +
    repealed-by reference, and the next-step hint for clearing it.

    \b
    The ingester never auto-mutates ``Regulation.status`` — partner is
    the authoritative source of truth and must confirm any detected
    change by editing ``seed_regulations.py`` and re-running
    ``seed-regulations``.

    \b
    Example output:
      {
        "regulation_key": "EU_REACH_COBALT",
        "celex": "32006R1907",
        "current_status": "effective",
        "detected_status": "superseded",
        "detected_at": "2026-05-17T12:34:56+00:00",
        "observed": {
          "date_of_end_of_validity": "2025-12-31",
          "repealed_by_celex": "32025R1234"
        },
        "next_step": "Confirm whether status='superseded' is correct..."
      }
    """
    from app.services.ingestion.eurlex import list_pending_status_changes

    s = _session()
    try:
        pending = list_pending_status_changes(s)
        typer.echo(json.dumps({
            "ok": True,
            "pending_count": len(pending),
            "pending": pending,
        }, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("list-suggested-regulations")
def list_suggested_regulations_cmd() -> None:
    """List auto-staged regulations awaiting partner review.

    \b
    When an ingester encounters a regulation external identifier
    (CELEX, FR document number, etc.) that isn't in our curated
    regulation roster, the resolver can auto-create a placeholder
    Regulation row with verified=False.  These rows are invisible to
    scoring (verified=True filter) but tracked in this review queue.

    \b
    Partner workflow:
      1. Run this command to see the queue.  Most-observed
         suggestions appear first (they're the most likely to matter).
      2. For each suggestion, decide approve / reject / merge:
         - Approve: add a proper Regulation row to seed_regulations.py
           with the same regulation_key, plus full material/geography
           scopes.  Re-seed; the seed function will upgrade the
           existing row to verified=True.
         - Reject: mark the alias is_skipped=True with a skip_reason
           in seed_regulation_aliases.py.  Re-seed; the suggested
           Regulation row remains as audit trail (verified=False so
           still invisible to scoring).
         - Merge with existing: rename the regulation_key on the
           suggested row to match an existing canonical regulation,
           then re-link the alias.

    \b
    Output is sorted by observation_count descending (most-observed
    first).  Each entry includes the external identifier, where it
    was first observed, how many times we've encountered it, and a
    short sample-context snippet for triage.
    """
    from app.services.ingestion.regulation_resolver import list_suggested_regulations

    s = _session()
    try:
        queue = list_suggested_regulations(s)
        typer.echo(json.dumps({
            "ok": True,
            "pending_count": len(queue),
            "pending": queue,
        }, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("discover-fr-regulation-aliases")
def discover_fr_regulation_aliases_cmd(
    regulations: Optional[str] = typer.Option(
        None,
        "--regulations",
        help=(
            "Comma-separated regulation_keys to search.  Default: all "
            "configured (UFLPA, IRA_DOMESTIC, SEC_CLIMATE_2024)."
        ),
    ),
    max_pages: int = typer.Option(
        5,
        "--max-pages",
        help="Max pages to fetch per query (100 docs/page).  Default 5.",
    ),
    use_haiku: bool = typer.Option(
        False,
        "--use-haiku",
        help=(
            "Classify each candidate via Haiku to filter tangential "
            "mentions.  Costs roughly $1-8 per full run; off by default."
        ),
    ),
    min_confidence: float = typer.Option(
        0.0,
        "--min-confidence",
        help=(
            "Filter the formatted seed_tuples output by minimum "
            "confidence (0.0-1.0).  Default 0.0 = include all."
        ),
    ),
    format_seed: bool = typer.Option(
        False,
        "--format-seed",
        help=(
            "Output as Python tuple literals ready to paste into "
            "seed_regulation_aliases.py:_FEDERAL_REGISTER.  Default JSON."
        ),
    ),
) -> None:
    """Discover Federal Register doc numbers for tracked regulations.

    \b
    Surfaces FR documents that should be added to
    seed_regulation_aliases.py:_FEDERAL_REGISTER so RiskEvents
    auto-link to canonical Regulation rows for UFLPA, IRA_DOMESTIC,
    SEC_CLIMATE_2024.

    \b
    Each regulation has multiple search configurations (agency + term
    + doc_type combinations).  Results are deduplicated across
    queries.  When --use-haiku is set, Haiku classifies each
    candidate to filter tangential mentions; otherwise all candidates
    are returned for manual partner review.

    \b
    Examples:
      bdi-ingest discover-fr-regulation-aliases
      bdi-ingest discover-fr-regulation-aliases --regulations SEC_CLIMATE_2024
      bdi-ingest discover-fr-regulation-aliases --use-haiku --min-confidence 0.85 --format-seed
    """
    from app.services.ingestion.discover_fr_regulation_aliases import (
        discover_fr_regulation_aliases,
        format_seed_tuples,
    )

    reg_keys: Optional[list[str]] = None
    if regulations:
        reg_keys = [k.strip() for k in regulations.split(",") if k.strip()]

    try:
        results = discover_fr_regulation_aliases(
            regulation_keys=reg_keys,
            max_pages=max_pages,
            use_haiku=use_haiku,
        )
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)

    if format_seed:
        typer.echo(format_seed_tuples(results, min_confidence=min_confidence))
        return

    summary = {
        "ok": True,
        "total_candidates": sum(len(v) for v in results.values()),
        "by_regulation": {
            k: {
                "count": len(v),
                "candidates": [s.to_dict() for s in v if s.confidence >= min_confidence],
            }
            for k, v in results.items()
        },
    }
    typer.echo(json.dumps(summary, indent=2))


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
    use_api: bool = typer.Option(
        False,
        "--use-api",
        help=(
            "Use the GTA REST API (/api/v2/gta/data/) instead of the bulk CSV. "
            "Requires settings.gta_api_key (env: GTA_API_KEY).  Applies server-side "
            "HS-prefix and date filtering — substantially less data over the wire than CSV.  "
            "Recommended for nightly runs; --local-file remains for offline testing."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Parse and classify normally but write nothing.  Reports what would "
            "be inserted and which existing rows would be refreshed (with "
            "per-field counts), then rolls back.  Run this before a repair "
            "re-ingest to see the blast radius.  Added 2026-07-30."
        ),
    ),
    api_raw_cache: Optional[str] = typer.Option(
        None,
        "--api-raw-cache",
        help=(
            "Path to a JSON cache of the raw API payload (API mode only).  "
            "If the file exists it is read instead of calling the API — no "
            "metered records consumed.  Otherwise the API is fetched and the "
            "payload saved there on success.  Use the same path for a "
            "--dry-run and the real run so GTA is only pulled once.  "
            "Delete the file to force a fresh pull.  Added 2026-07-31."
        ),
    ),
) -> None:
    """Ingest Global Trade Alert harmful trade interventions into risk_events.

    GTA now requires registration for bulk data access. Three modes:

    \b
    1. API (recommended — needs GTA_API_KEY in env):
         bdi-ingest ingest-gta --use-api --since-year 2018

    \b
    2. Local file (offline / no API key):
         bdi-ingest ingest-gta --local-file /path/to/gta_state_acts.csv

    \b
    3. Pre-filtered curated export (e.g. "Batteries" dataset):
         bdi-ingest ingest-gta --local-file /path/to/interventions.csv --skip-hs-filter

    Ingests Red (harmful) interventions whose affected HS codes match
    battery-critical materials. All events inserted with verified=False.

    \b
      bdi-ingest seed-materials
      bdi-ingest seed-hs-mappings
      bdi-ingest ingest-gta --use-api --since-year 2018
    """
    from app.services.ingestion.gta import (
        BATTERY_HS_PREFIXES,  # noqa: F401  - re-exported for users running --help
        GTA_DOWNLOAD_URL,
        ingest_gta,
    )

    if not use_api and local_file is None and url is None:
        typer.echo(
            "Warning: neither --use-api nor --local-file provided. Attempting direct "
            "download — this will likely fail since the bulk CSV endpoint is gated. "
            "Use --use-api (with GTA_API_KEY set) or --local-file instead.",
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
            use_api=use_api,
            dry_run=dry_run,
            api_raw_cache=api_raw_cache,
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

    ⏸  PAUSED (2026-07-31, Nicole) — do not run on a schedule or by hand.
    The stream produces orphan display-only rows nothing consumes (78%
    FILING_INDEX noise; zero material/company links; stub summaries), and
    the Phase 4 reset deletes its events without re-ingesting them.  See
    docs/design/sec_edgar_stream_audit.md for the revival criteria
    (admission filtering + company links + body text, shipped together).

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
    # 2026-05-06 refactor: switched from generic IngestionPipeline to a
    # dedicated ingester module so SEC filings get MaterialCache-driven
    # material attribution + event_subtype='FINANCIAL_PRESSURE' (typed
    # column from migration 040).  See app/services/ingestion/ingest_sec_edgar.py.
    from app.services.ingestion.ingest_sec_edgar import ingest_sec_edgar

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
        result = ingest_sec_edgar(
            s, cik_list=cik_list, max_filings=max_filings
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("sec-body-fetch")
def sec_body_fetch_cmd(
    cik: Optional[str] = typer.Option(
        None,
        "--cik",
        help=(
            "Comma-separated list of CIKs to limit fetching to.  "
            "Mutually exclusive with --launch-list (CIK list wins if both)."
        ),
    ),
    launch_list: bool = typer.Option(
        False,
        "--launch-list",
        help=(
            "Restrict to companies linked to launch-list materials via "
            "CompanyMaterialExposure or CompanyFacility.  Falls back to "
            "ALL CIK-having companies if both tables are empty (today's "
            "state) with a warning."
        ),
    ),
    forms: str = typer.Option(
        "10-K,20-F",
        "--forms",
        help=(
            "Comma-separated form codes to process.  Default 10-K,20-F.  "
            "10-Q + 8-K body extraction deferred to v1.1."
        ),
    ),
    max_filings: Optional[int] = typer.Option(
        None,
        "--max-filings",
        help="Cap on number of filings processed (useful for dry-runs).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Re-parse filings that already have FilingBodySection rows.  "
            "Default: skip filings where every target section is already "
            "present (idempotent re-runs)."
        ),
    ),
) -> None:
    """Fetch SEC filing bodies and parse them into filing_body_sections.

    SEC Workstream B (migration 049, 2026-06).  Walks existing SEC
    SourceDocument rows, fetches each filing's primary_document_url
    with the SEC-required User-Agent, runs the three-layer section
    parser (edgartools → regex → LLM-locator stub), and upserts the
    extracted sections.

    Prerequisites:
    \b
      bdi-ingest ingest-sec-edgar         # populate SourceDocument metadata
      bdi-ingest backfill-cik-map         # link Companies to CIKs

    Examples:
    \b
      bdi-ingest sec-body-fetch --launch-list --max-filings 10  # smoke test
      bdi-ingest sec-body-fetch --launch-list                   # full launch-10 backfill
      bdi-ingest sec-body-fetch --cik 0000915779 --force        # re-parse Albemarle
      bdi-ingest sec-body-fetch --forms 10-K                    # 10-K only

    Per filing the fetcher commits independently so a crash mid-batch
    preserves partial progress.  Rate-limited at ~6.7 req/s (under SEC's
    10 req/s ceiling).
    """
    from app.services.ingestion.sec_body_fetcher import (
        DEFAULT_TARGET_FORMS,
        fetch_filing_bodies,
    )

    target_forms = frozenset(
        {f.strip() for f in forms.split(",") if f.strip()}
    )
    if not target_forms:
        target_forms = DEFAULT_TARGET_FORMS

    cik_filter: Optional[list[str]] = None
    if cik:
        cik_filter = [
            c.strip().zfill(10) for c in cik.split(",") if c.strip()
        ]

    s = _session()
    try:
        summary = fetch_filing_bodies(
            s,
            target_forms=target_forms,
            cik_filter=cik_filter,
            launch_list_only=launch_list and cik_filter is None,
            max_filings=max_filings,
            force=force,
        )
        # Final commit is a safety net — fetch_filing_bodies commits per
        # filing internally, so this is usually a no-op.
        s.commit()
        typer.echo(json.dumps(summary.to_dict(), indent=2))
    except Exception as exc:
        s.rollback()
        typer.echo(
            json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
            err=True,
        )
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


@app.command("ingest-wgi")
def ingest_wgi_cmd(
    local_path: Optional[str] = typer.Option(
        None,
        "--local-path",
        help=(
            "Path to a local WGI file (.xlsx OR .csv).  Recommended over "
            "--url since the World Bank rotates the published URL each "
            "January.  Long-format CSV is the most reliable input — see "
            "the module docstring for the expected column layout."
        ),
    ),
    url: Optional[str] = typer.Option(
        None,
        "--url",
        help=(
            "Override the World Bank WGI bulk-Excel URL.  Defaults to "
            "WORLDBANK_WGI_URL env var or the module's documented default."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="UPDATE existing country-year-source rows in place.",
    ),
) -> None:
    """Ingest World Bank Worldwide Governance Indicators (WGI) per country.

    Populates ``country_governance_signals``.  Used by Step 3's
    Geopolitical-pillar governance overlay.  Six WGI dimensions per
    country × reference_year, plus a denormalised composite percentile.

    Examples:

    \b
      bdi-ingest ingest-wgi --local-path data/governance/wgi_2024_long.csv
      bdi-ingest ingest-wgi --url https://example.com/wgi.xlsx
      bdi-ingest ingest-wgi --local-path wgi.xlsx --force
    """
    from app.services.ingestion.ingest_worldbank_wgi import ingest_worldbank_wgi

    s = _session()
    try:
        summary = ingest_worldbank_wgi(
            s, local_path=local_path, url=url, force=force,
        )
        s.commit()
        typer.echo(json.dumps(summary, indent=2, default=str))
    except Exception as exc:
        s.rollback()
        typer.echo(
            json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
            err=True,
        )
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


@app.command("backfill-trade-flow-mappings")
def backfill_trade_flow_mappings_cmd(
    batch_size: int = typer.Option(
        2_000,
        "--batch-size",
        help="Rows fetched + updated per pass. Default 2000.",
    ),
    sample_cap: int = typer.Option(
        20,
        "--sample-cap",
        help=(
            "Maximum number of mismatch samples to include in the result "
            "summary (the full mismatch count is still reported)."
        ),
    ),
) -> None:
    """Populate TradeFlow.hs_mapping_id for rows missing the FK.

    Targets rows where material_id IS NOT NULL AND hs_mapping_id IS NULL.
    Resolves each row's hs_code against the current hs_code_material_mappings
    seed; if the resolved material matches the row's stored material, fills
    in hs_mapping_id.  If the resolved material differs (mapping drift), the
    row is left alone and logged as a mismatch — backfill never overwrites
    attribution.  Idempotent: re-running is a no-op since updated rows fall
    off the filter.

    \b
      bdi-ingest backfill-trade-flow-mappings
      bdi-ingest backfill-trade-flow-mappings --batch-size 5000
    """
    from app.services.ingestion.comtrade import backfill_trade_flow_hs_mappings

    s = _session()
    try:
        result = backfill_trade_flow_hs_mappings(
            s, batch_size=batch_size, log_mismatch_samples=sample_cap,
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("reattribute-unmapped-trade-flows")
def reattribute_unmapped_trade_flows_cmd(
    batch_size: int = typer.Option(
        2_000,
        "--batch-size",
        help="Rows fetched + updated per pass. Default 2000.",
    ),
    sample_cap: int = typer.Option(
        20,
        "--sample-cap",
        help=(
            "Maximum number of attribution samples to include in the result "
            "summary (the full attributed count is still reported)."
        ),
    ),
) -> None:
    """Re-resolve TradeFlow rows that have material_id IS NULL.

    Use this after expanding hs_code_material_mappings (e.g. partner adds
    new 6-digit curations for previously-ambiguous codes) to pick up the
    new mappings against historical rows.  Writes BOTH material_id AND
    hs_mapping_id when the current seed produces a valid resolution; also
    preserves the resolver's confidence in metadata_json so re-attributed
    rows match the shape of fresh-ingest rows.  Idempotent: rows that now
    have material_id set fall off the filter.

    \b
      bdi-ingest reattribute-unmapped-trade-flows
      bdi-ingest reattribute-unmapped-trade-flows --batch-size 5000
    """
    from app.services.ingestion.comtrade import reattribute_unmapped_trade_flows

    s = _session()
    try:
        result = reattribute_unmapped_trade_flows(
            s, batch_size=batch_size, log_attribution_samples=sample_cap,
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2, default=str))
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


@app.command("patch-fr-events")
def patch_fr_events_cmd(
    fix_uflpa_routing: bool = typer.Option(
        True,
        "--fix-uflpa-routing/--no-fix-uflpa-routing",
        help=(
            "Add OPERATIONAL to risk_categories_json for all UFLPA-query events. "
            "Pure DB update — no API calls. Default: enabled."
        ),
    ),
    fix_effective_dates: bool = typer.Option(
        True,
        "--fix-effective-dates/--no-fix-effective-dates",
        help=(
            "Re-fetch effective_on from the FR API and update event_date for "
            "final rules, executive orders, and presidential documents where "
            "publication date and effective date differ. Default: enabled."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Compute and report changes without writing anything to the DB.",
    ),
) -> None:
    """Retroactively fix two data quality gaps in existing FR events.

    \b
    Fix 1 — UFLPA operational routing:
      Existing UFLPA events were tagged REGULATORY_COMPLIANCE only. UFLPA
      Entity List additions also disrupt supplier/mine continuity (operational
      risk). This adds OPERATIONAL to their risk_categories_json so they feed
      the operational scoring pillar. Pure DB update, no API calls.

    \b
    Fix 2 — effective_on dates:
      Existing FR events use publication_date as event_date. For final rules
      and executive orders, the effective date may be weeks or months later,
      causing time-weighting to treat active regulations as older than they
      are. Re-fetches effective_on from the FR API in batches and updates
      event_date where they differ. Only targets rule/exec-order doc types.

    \b
    Examples:
      bdi-ingest patch-fr-events --dry-run
      bdi-ingest patch-fr-events
      bdi-ingest patch-fr-events --no-fix-effective-dates
      bdi-ingest patch-fr-events --no-fix-uflpa-routing
    """
    from app.services.ingestion.ingest_federal_register import patch_fr_events

    typer.echo(
        f"Patching FR events"
        f"{' [DRY RUN]' if dry_run else ''}"
        f" — uflpa_routing={'yes' if fix_uflpa_routing else 'no'}"
        f", effective_dates={'yes' if fix_effective_dates else 'no'}…"
    )

    s = _session()
    try:
        result = patch_fr_events(
            s,
            fix_uflpa_routing=fix_uflpa_routing,
            fix_effective_dates=fix_effective_dates,
            dry_run=dry_run,
        )
        typer.echo(json.dumps({"ok": True, "dry_run": dry_run, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("backfill-fr-links")
def backfill_fr_links_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Re-scan ALL FR events, including those that already have links. "
            "The per-row uniqueness constraint prevents duplicates, so this is "
            "safe to run after adding new material aliases or geography patterns."
        ),
    ),
    batch_size: int = typer.Option(
        100,
        "--batch-size",
        help="Number of events to process before each DB commit. Default: 100.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Detect and report counts, but do NOT write or commit any rows.",
    ),
) -> None:
    """Retroactively tag existing FR events with material and geography links.

    Federal Register events ingested before material/geography detection was added
    have no RiskEventMaterial or RiskEventGeography rows, making them invisible to
    get_events_for_material() and geography-filtered queries. This command scans
    all historical FR events and fills in the missing junction rows.

    Idempotent: re-runs skip pairs that already exist. Use --force to re-scan
    fully-tagged events after updating hs_code_material_mappings.keywords or _GEO_PATTERNS.

    \b
    Examples:
      bdi-ingest backfill-fr-links
      bdi-ingest backfill-fr-links --dry-run
      bdi-ingest backfill-fr-links --force
      bdi-ingest backfill-fr-links --batch-size 50
    """
    from app.services.ingestion.ingest_federal_register import backfill_fr_links

    typer.echo(
        f"Backfilling FR event links"
        f"{' [DRY RUN — no writes]' if dry_run else ''}"
        f"{' [--force: re-scanning all events]' if force else ''}…"
    )

    s = _session()
    try:
        result = backfill_fr_links(
            s,
            force=force,
            batch_size=batch_size,
            dry_run=dry_run,
        )
        typer.echo(json.dumps({"ok": True, "dry_run": dry_run, **result}, indent=2))
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
                from app.services.scoring.bands import score_to_band
                band = f"{score_to_band(sc.overall_risk_score) or '—':<4}"
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
        "data/iea/policy_tracker.csv",
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


@app.command("setup-all")
def setup_all_cmd(
    usgs_file: Optional[str] = typer.Option(
        None,
        "--usgs-file",
        help=(
            "Path to MCS world data CSV (e.g. MCS2025_World_Data.csv or "
            "MCS2026_Commodities_Data.csv). "
            "Download from https://pubs.usgs.gov/publication/mcs<year>. "
            "If omitted, the USGS step is skipped and must be run separately "
            "with: bdi-ingest ingest-usgs <path>"
        ),
    ),
    mcs_year: int = typer.Option(
        2025,
        "--mcs-year",
        help="MCS publication year (forwarded to ingest-usgs / ingest-mcs-pdf / ingest-mcs-prices).",
    ),
    mcs_pdf_file: Optional[str] = typer.Option(
        None,
        "--mcs-pdf-file",
        help=(
            "Path to the USGS Mineral Commodity Summaries PDF (e.g. mcs2025.pdf). "
            "Populates hs_code_production_shares (global + US) and appends salient "
            "notes to material_criticality_signals.  Must run after seed-hs-mappings. "
            "If omitted, the PDF step is skipped and must be run separately with: "
            "bdi-ingest ingest-mcs-pdf <path> --year <year>"
        ),
    ),
    mcs_prices_file: Optional[str] = typer.Option(
        None,
        "--mcs-prices-file",
        help=(
            "Path to the USGS MCS Fig 10 Price Growth Rates CSV "
            "(e.g. MCS2026_Fig10_Price_Growth_Rates.csv).  Writes "
            "price_yoy_pct and price_cagr_5yr_pct onto the same "
            "material_criticality_signals row written by ingest-usgs.  "
            "If omitted, the Fig 10 step is skipped and must be run "
            "separately with: bdi-ingest ingest-mcs-prices <path>"
        ),
    ),
    mrds_file: str = typer.Option(
        "/data/facilities/mrds.csv",
        "--mrds-file",
        help=(
            "Path to the local MRDS CSV file for facility ingestion. "
            "Defaults to /data/facilities/mrds.csv. "
            "Override with a different path or pass --skip-mrds to skip entirely."
        ),
    ),
    skip_gleif: bool = typer.Option(
        False,
        "--skip-gleif",
        help="Skip GLEIF enrichment (useful when offline or rate-limited).",
    ),
    skip_mrds: bool = typer.Option(
        False,
        "--skip-mrds",
        help="Skip MRDS facility ingest.",
    ),
) -> None:
    """One-shot first-time environment bootstrap.

    Runs every seed and reference-data ingest command in dependency order so
    you don't have to remember the sequence.  Safe to re-run — all steps are
    idempotent.

    \b
    Execution order (Phase B / May 2026 — alias-resolver based):
      1.  seed-countries           ISO country reference table (Comtrade codes, name aliases)
      2.  seed                     sources, reference aliases
      3.  seed-materials           39 canonical materials + battery chemistry junctions
      4.  seed-material-aliases    external commodity name → canonical material lookup
      5.  ingest-usgs              USGS MCS world data CSV  (requires --usgs-file)
      6.  seed-hs-mappings         curated HS code → material lookup table
      7.  ingest-mcs-pdf           HS-stage production shares + US import shares (requires --mcs-pdf-file)
      8.  seed-hs-mappings --force backfill curated fields onto rows added by ingest-mcs-pdf
      9.  ingest-mcs-prices        Fig 10 price-growth rates → criticality signals (requires --mcs-prices-file)
      10. seed-companies           curated supply-chain company list
      11. ingest-mrds              mine/processing facilities from local MRDS CSV
      12. seed-supply-relationships  upstream/downstream supplier graph
      13. seed-material-exposures    company × material exposure weights
      14. seed-regulations         curated regulatory seed rows
      15. ingest-gleif             LEI enrichment for company entities

    \b
    Dependency notes:
      * ingest-usgs / ingest-mcs-pdf / ingest-mcs-prices all use the
        MaterialAliasResolver, so seed-materials AND seed-material-aliases
        must run first (steps 3–4).  Without aliases, every chapter
        resolves to "unknown" and is silently skipped.
      * ingest-mcs-pdf (step 7) writes hs_code_production_shares rows that
        FK to hs_code_material_mappings, so seed-hs-mappings (step 6) must
        run first.
      * The seed-hs-mappings --force pass at step 8 is non-destructive: it
        UPDATEs curated fields (description, confidence, stage, keywords)
        onto rows added by ingest-mcs-pdf (6-digit stubs, 10-digit HTS).
        It does NOT delete any rows.
      * ingest-mcs-prices (step 9) joins to the same material_criticality_signals
        row that ingest-usgs created at step 5; running it without ingest-usgs
        leaves the supply-side metric columns NULL.

    \b
    Step 11 (ingest-mrds) reads from /data/facilities/mrds.csv by default.
    Override with --mrds-file.  Facility type → supply_chain_stage is mapped
    automatically (mine→ore, refinery→intermediate).  hs_mapping_id is set
    NULL and requires manual confirmation post-ingest.

    \b
    After setup-all, run the periodic ingestion commands (ingest-comtrade,
    ingest-federal-register, etc.) and then full-score to compute scores.

    \b
    Examples:
      bdi-ingest setup-all --usgs-file ~/Downloads/MCS2026_Commodities_Data.csv \\
                           --mcs-pdf-file ~/Downloads/mcs2026.pdf \\
                           --mcs-prices-file ~/Downloads/MCS2026_Fig10_Price_Growth_Rates.csv \\
                           --mcs-year 2026
      bdi-ingest setup-all --usgs-file path/to/MCS.csv --mcs-pdf-file path/to/mcs.pdf --skip-gleif
      bdi-ingest setup-all --skip-mrds   # skip facility ingest entirely
    """
    import sys
    import subprocess

    def _run(step_name: str, args: list[str]) -> None:
        typer.echo(f"\n{'='*60}")
        typer.echo(f"  setup-all  ▶  {step_name}")
        typer.echo(f"{'='*60}")
        result = subprocess.run(
            [sys.executable, "-m", "app.cli"] + args,
            check=False,
        )
        if result.returncode != 0:
            typer.echo(
                f"\n✗  setup-all aborted: '{step_name}' exited with code {result.returncode}.",
                err=True,
            )
            raise typer.Exit(code=result.returncode)

    # Steps 1–2: reference tables
    _run("seed-countries", ["seed-countries"])
    _run("seed", ["seed"])

    # Steps 3–4: materials + alias resolver inputs.  MUST run before any
    # ingester that uses MaterialAliasResolver (ingest-usgs, ingest-mcs-pdf,
    # ingest-mcs-prices).  Without these, every external chapter / commodity
    # name resolves to "unknown" and gets dropped.
    _run("seed-materials", ["seed-materials"])
    _run("seed-material-aliases", ["seed-material-aliases"])

    # Step 5: USGS MCS CSV → material_criticality_signals + production_shares
    if usgs_file:
        _run("ingest-usgs", ["ingest-usgs", usgs_file, "--mcs-year", str(mcs_year), "--force"])
    else:
        typer.echo(
            "\n⚠  Skipping ingest-usgs (no --usgs-file provided). "
            "Run 'bdi-ingest ingest-usgs <path>' manually before scoring.",
            err=True,
        )

    # Step 6: curated HS code → material mappings (depends on seed-materials)
    _run("seed-hs-mappings", ["seed-hs-mappings"])

    # Step 7: MCS PDF → hs_code_production_shares (global + US) + 6-digit
    # stubs + 10-digit HTS rows.  Must follow seed-hs-mappings so curated
    # parents exist for the FK to land on.
    if mcs_pdf_file:
        _run(
            "ingest-mcs-pdf",
            ["ingest-mcs-pdf", mcs_pdf_file, "--year", str(mcs_year), "--force"],
        )
    else:
        typer.echo(
            "\n⚠  Skipping ingest-mcs-pdf (no --mcs-pdf-file provided). "
            "Run 'bdi-ingest ingest-mcs-pdf <path> --year <year>' manually. "
            "Without this, Level-0 HS node scoring falls back to equal-weighting.",
            err=True,
        )

    # Step 8: non-destructive backfill.  --force here UPDATEs curated fields
    # (description / confidence / stage / stage_sequence / digit_count /
    # keywords) on rows added by ingest-mcs-pdf at step 7.  It does NOT
    # delete any rows — auto-added 6-digit stubs and 10-digit HTS rows are
    # preserved.
    _run("seed-hs-mappings (backfill)", ["seed-hs-mappings", "--force"])

    # Step 9: USGS MCS Fig 10 prices → price_yoy_pct + price_cagr_5yr_pct
    # on the same material_criticality_signals row written at step 5.
    if mcs_prices_file:
        _run(
            "ingest-mcs-prices",
            ["ingest-mcs-prices", mcs_prices_file, "--mcs-year", str(mcs_year), "--force"],
        )
    else:
        typer.echo(
            "\n⚠  Skipping ingest-mcs-prices (no --mcs-prices-file provided). "
            "Run 'bdi-ingest ingest-mcs-prices <path> --mcs-year <year>' manually. "
            "Without this, Financial Pressure Tier 1.5 (price-growth) signals "
            "are absent and scoring falls back to Pink Sheet alone.",
            err=True,
        )

    # Steps 10–11: company + facility data
    _run("seed-companies", ["seed-companies"])

    if not skip_mrds:
        _run(
            "ingest-mrds",
            ["ingest-mrds", "--local-file", mrds_file],
        )
    else:
        typer.echo("\n⚠  Skipping ingest-mrds (--skip-mrds set).")

    # Steps 12–14: relationship and regulatory seeds
    _run("seed-supply-relationships", ["seed-supply-relationships"])
    _run("seed-material-exposures", ["seed-material-exposures"])
    _run("seed-regulations", ["seed-regulations"])
    # External regulation IDs (CELEX, FR doc number, etc.) → regulation_key.
    # MUST run after seed-regulations (FK references regulations.id) and
    # BEFORE eurlex.py / federal-register / any future regulation-attaching
    # ingester so they can resolve external IDs through the alias table.
    _run("seed-regulation-aliases", ["seed-regulation-aliases"])

    # Step 15: LEI enrichment
    if not skip_gleif:
        _run("ingest-gleif", ["ingest-gleif"])
    else:
        typer.echo("\n⚠  Skipping ingest-gleif (--skip-gleif set).")

    typer.echo("\n✓  setup-all complete.")
    typer.echo(
        "\nNext steps:\n"
        "  bdi-ingest ingest-comtrade --years <years>           # export flows (runs daily via Inngest)\n"
        "  bdi-ingest ingest-comtrade --flow-code M --years <years>  # import flows\n"
        "  bdi-ingest ingest-federal-register\n"
        "  bdi-ingest ingest-gta\n"
        "  bdi-ingest ingest-opensanctions\n"
        "  bdi-ingest ingest-eurlex\n"
        "  bdi-ingest ingest-iea-policy-tracker\n"
        "  bdi-ingest full-score\n"
    )


@app.command("ingest-mcs-pdf")
def ingest_mcs_pdf_cmd(
    path: str = typer.Argument(
        ...,
        help="Path to the USGS Mineral Commodity Summaries PDF (e.g. mcs2026.pdf).",
    ),
    year: int = typer.Option(
        ...,
        "--year",
        help=(
            "MCS publication year (e.g. 2026).  Used as the reference_year for "
            "production share rows where an explicit year cannot be parsed from the PDF. "
            "Actual data years in the production tables are typically year-1."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Parse the PDF and log what would be inserted, without writing to the DB.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Suppress the confirmation prompt and proceed immediately.",
    ),
    use_llm_sections: bool = typer.Option(
        True,
        "--use-llm-sections/--no-llm-sections",
        help=(
            "When True (default), use the Anthropic LLM locator to identify "
            "commodity chapter bounds.  More robust to MCS layout variants "
            "(footnoted headings, multi-page chapters).  Requires "
            "ANTHROPIC_API_KEY and the `anthropic` package; result is cached "
            "at data/mcs<year>_locator_result.json so re-runs skip the LLM "
            "call.  When False, falls back to regex heading detection."
        ),
    ),
) -> None:
    """Seed hs_code_material_mappings and hs_code_production_shares from a USGS MCS PDF.

    Parses the annual USGS Mineral Commodity Summaries PDF using pdfplumber and:

    \b
      1. Inserts 10-digit US HTS rows into hs_code_material_mappings (market_scope='us')
      2. Derives and inserts 6-digit global rows (market_scope='global', ON CONFLICT DO NOTHING)
      3. Appends salient notes to MaterialCriticalitySignal.metadata_json (write-only;
         no current reader — forward-compatibility hook)

    All inserts are idempotent: re-running with the same PDF is safe.

    Section 5.1 cleanup (2026-06): per-country world-production share writes
    and US import-source share writes have been removed.  Both tables
    (``hs_code_production_shares`` for market_scope='global' and 'us') are
    populated by the CSV path (``bdi-ingest ingest-usgs``) which extracts
    the same data more reliably.

    Prerequisites:

    \b
      - alembic upgrade head  (migrations 022–028 must be applied)
      - bdi-ingest seed-countries  (country ISO-2 codes must exist for name resolution)
      - bdi-ingest seed  (materials must exist for FK linkage)

    \b
    Examples:
      bdi-ingest ingest-mcs-pdf mcs2026.pdf --year 2026
      bdi-ingest ingest-mcs-pdf mcs2026.pdf --year 2026 --dry-run
    """
    from pathlib import Path as _Path
    from app.services.ingestion.mcs_pdf_parser import MCSPdfParser

    pdf_path = _Path(path)
    if not pdf_path.exists():
        typer.echo(f"File not found: {path}", err=True)
        raise typer.Exit(code=1)

    if not dry_run and not force:
        typer.confirm(
            f"This will seed hs_code_material_mappings and hs_code_production_shares "
            f"from {pdf_path.name} (year={year}).  Continue?",
            abort=True,
        )

    s = _session()
    try:
        # Construct the parser with a resolver so the regex fallback path
        # can translate PDF headings via the alias table.  The LLM path
        # doesn't need it (canonical names come from the materials list
        # passed into ``seed_to_db``).
        from app.services.ingestion.material_resolver import MaterialAliasResolver
        resolver = MaterialAliasResolver(s)
        parser = MCSPdfParser(pdf_path, reference_year=year, resolver=resolver)

        stats = parser.seed_to_db(
            s, dry_run=dry_run, use_llm_sections=use_llm_sections,
        )
        if not dry_run:
            s.commit()

        # 2026-05-31: seed_to_db now returns per-chapter stats PLUS a
        # special ``__run_meta__`` key carrying run-level counters
        # (chapters skipped due to missing material or per-section
        # exception).  Filter the meta key out of per-chapter sums.
        run_meta = stats.pop("__run_meta__", {})
        total_us = sum(v["us_rows_processed"] for v in stats.values())
        total_global = sum(v["global_rows_processed"] for v in stats.values())
        # Section 5.1 cleanup (2026-06): ``production_share_rows`` removed
        # from the CLI summary along with the parser's Step 4a write block.
        # CSV path (``ingest-usgs``) is the sole populator of
        # ``hs_code_production_shares`` for market_scope='global'.
        total_imp_skipped = sum(
            v.get("import_source_shares_skipped", 0) for v in stats.values()
        )
        total_salient = sum(
            v.get("salient_notes_appended", 0) for v in stats.values()
        )

        typer.echo(json.dumps({
            "ok": True,
            "dry_run": dry_run,
            "commodities_processed": len(stats),
            "commodities_skipped_no_material": run_meta.get("skipped_no_material", 0),
            "commodities_skipped_on_error": run_meta.get("skipped_on_error", 0),
            "us_mapping_rows": total_us,
            "global_mapping_rows": total_global,
            "import_source_rows_skipped": total_imp_skipped,
            "salient_notes_appended": total_salient,
            "by_commodity": stats,
        }, indent=2))
    except Exception as exc:
        s.rollback()
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("full-score")
def full_score_cmd(
    as_of: Optional[str] = typer.Option(
        None,
        "--as-of",
        help="Point-in-time date for all scoring steps (YYYY-MM-DD). Default: today.",
    ),
    skip_trade_signals: bool = typer.Option(
        False,
        "--skip-trade-signals",
        help="Skip build-trade-signals (use when trade_flows has not changed).",
    ),
) -> None:
    """Run the full scoring pipeline from trade signals through company scores.

    Executes all scoring steps in dependency order.  Two passes of rescore-all
    are intentional: the first pass scores each company independently; the second
    pass picks up supply-chain propagation scores that depend on upstream results
    from the first pass.

    \b
    Execution order:
      1.  build-trade-signals     derive GEOPOLITICAL_TRADE events from trade_flows
      2.  rescore-market          material × geography risk scores
      3.  rescore-global-rollups  material-level global rollup from geo scores
      4.  rescore-chemistry       chemistry risk from material rollups
      5.  rescore-all  (pass 1)   company six-pillar scores
      6.  rescore-all  (pass 2)   re-score with propagation scores now available

    \b
    Prerequisites: setup-all must have run, plus at minimum:
      bdi-ingest ingest-comtrade  (for trade signals + market scoring)
      bdi-ingest ingest-worldbank (for commodity price signal)

    \b
    Examples:
      bdi-ingest full-score
      bdi-ingest full-score --as-of 2025-01-01
      bdi-ingest full-score --skip-trade-signals
    """
    import sys
    import subprocess

    def _run(step_name: str, args: list[str]) -> None:
        typer.echo(f"\n{'='*60}")
        typer.echo(f"  full-score  ▶  {step_name}")
        typer.echo(f"{'='*60}")
        result = subprocess.run(
            [sys.executable, "-m", "app.cli"] + args,
            check=False,
        )
        if result.returncode != 0:
            typer.echo(
                f"\n✗  full-score aborted: '{step_name}' exited with code {result.returncode}.",
                err=True,
            )
            raise typer.Exit(code=result.returncode)

    as_of_args = ["--as-of", as_of] if as_of else []

    if not skip_trade_signals:
        _run("build-trade-signals", ["build-trade-signals"])
    else:
        typer.echo("\n⚠  Skipping build-trade-signals (--skip-trade-signals set).")

    _run("rescore-market", ["rescore-market"] + as_of_args)
    _run("rescore-global-rollups", ["rescore-global-rollups"] + as_of_args)
    _run("rescore-chemistry", ["rescore-chemistry"] + as_of_args)

    typer.echo("\n  rescore-all pass 1 of 2 (individual company scores)…")
    _run("rescore-all (pass 1)", ["rescore-all", "--skip-existing"] + as_of_args)

    typer.echo("\n  rescore-all pass 2 of 2 (propagation)…")
    _run("rescore-all (pass 2)", ["rescore-all"] + as_of_args)

    typer.echo("\n✓  full-score complete.")


@app.command("reset-events")
def reset_events_cmd(
    confirm: bool = typer.Option(
        False,
        "--yes",
        help=(
            "Required confirmation flag. Without --yes the command prints the "
            "row counts that WOULD be deleted and exits without touching data."
        ),
    ),
    keep_source_documents: bool = typer.Option(
        True,
        "--keep-source-documents/--purge-source-documents",
        help=(
            "Default keeps source_documents so re-ingest can reuse cached "
            "payloads (SEC EDGAR / Federal Register) and skip the fetch. "
            "Pass --purge-source-documents only if you actually want to "
            "re-download every source — slower and costs API quota."
        ),
    ),
    source: Optional[str] = typer.Option(
        None,
        "--source",
        help=(
            "Restrict the reset to a single source.  Matches sources.name "
            "or sources.source_type, case-insensitive.  Examples: 'gta', "
            "'Global Trade Alert', 'opensanctions', 'eur-lex', "
            "'federal_register'.  Without this flag the reset clears every "
            "RiskEvent across all sources.  Useful when one ingester's "
            "attribution logic changed (e.g. the GTA structural classifier "
            "from 2026-05-12) and you want to re-ingest only that source's "
            "events without re-running every other ingester."
        ),
    ),
) -> None:
    """Truncate the risk_events + junction tables in FK-safe order.

    Use this when:
      * Attribution logic changed (e.g. Tier 1.x audits, GTA Scope 2 fixes,
        EUR-Lex material-junction wiring) and the existing rows reflect
        the OLD logic — re-ingest is the only way to apply the new rules
        to historical events.
      * You want a clean slate before partner / customer review.

    Does NOT touch:
      * Reference data (materials, hs_code_material_mappings, regulations,
        regulation_material_scope, regulation_geography_scope, facilities,
        material_production_shares, commodity_prices, companies, etc.)
      * Trade flows — the confidence weighting from the 2026-05-09 fix
        applies at query time via JOIN, so existing TradeFlow rows do
        not need re-ingest.
      * Scoring outputs (run rescore-* commands separately after re-ingest).

    Order matters: junction tables are truncated before risk_events to
    avoid FK violations.  RiskEventGeography, RiskEventCompany,
    RiskEventMaterial, RiskEventHsMapping, RiskEventRegulation, then
    risk_events.

    Idempotency notes for re-ingest after a reset:
      * GTA          — title/summary/event_date come straight from CSV
                       columns; Scope 2 added metadata only, content_hash
                       inputs unchanged.  Safe.
      * IEA          — content_hash(title, countries_raw, year).  Safe.
      * Federal Reg  — content_hash(document_number, publication_date). Safe.
      * SEC EDGAR    — dedup via source_document_id existence check. Safe.
      * EUR-Lex      — dedup via RiskEventRegulation existence check. Safe.
      * OpenSanctions geo events — title contains entity COUNT; post-
                       Tier-1.2 the count drops sharply (topic filter),
                       so re-ingest produces a different content_hash
                       from any pre-existing rows.  Cleared rows mean
                       no duplicates; do NOT re-run a second time
                       without clearing again.
      * trade_signal_builder — title contains the percentage; Tier 1.4
                       confidence weighting changes the percentage.
                       Same rule: cleared rows mean no duplicates;
                       don't re-run on top.
      * Census trade — pipeline.py._add_risk_event currently does NOT
                       set content_hash and has NO dedup check.  Every
                       re-run inserts duplicates regardless of clears.
                       Separate fix needed; tracked.
    """
    from sqlalchemy import text

    # Order is FK-safe: child junctions first, then risk_events.
    # We use raw DELETE rather than TRUNCATE because (a) TRUNCATE in
    # Postgres requires the privilege and (b) on SQLite (used in tests)
    # TRUNCATE doesn't exist.  Postgres can re-use the same physical
    # pages after the DELETE so the storage cost is the same.
    junction_tables = [
        "risk_event_companies",
        "risk_event_materials",
        "risk_event_hs_mappings",
        "risk_event_geographies",
        "risk_event_regulations",
    ]
    tables_in_order = junction_tables + ["risk_events"]

    s = _session()
    try:
        # ── Resolve --source to a source_id (when supplied) ───────────────
        # Matches either sources.name (e.g. "Global Trade Alert") or
        # sources.source_type (e.g. "gta") case-insensitively so callers
        # don't need to know which one is canonical.  Returns a single
        # row; an ambiguous match (very unlikely — name and source_type
        # are both stored as distinct strings) takes the first hit.
        source_id: Optional[int] = None
        source_name: Optional[str] = None
        if source is not None:
            src_row = s.execute(
                text(
                    """
                    SELECT id, name FROM sources
                    WHERE LOWER(name) = LOWER(:s)
                       OR LOWER(source_type) = LOWER(:s)
                    LIMIT 1
                    """
                ),
                {"s": source.strip()},
            ).first()
            if src_row is None:
                typer.echo(
                    json.dumps(
                        {
                            "ok": False,
                            "error": (
                                f"No source matched '{source}'.  Check the "
                                "sources table — match is case-insensitive "
                                "against name OR source_type."
                            ),
                        }
                    ),
                    err=True,
                )
                raise typer.Exit(code=1)
            source_id, source_name = src_row[0], src_row[1]

        # Filter clauses for the count/delete queries.  When source_id is
        # None we touch every row (legacy behavior).  When set, we use a
        # subquery on source_documents → risk_events so junction deletes
        # only hit rows belonging to the named source.  The subquery
        # approach works on both Postgres and SQLite without array params.
        if source_id is None:
            risk_events_where = ""
            junction_where = ""
            params: dict = {}
        else:
            risk_events_where = (
                "WHERE source_document_id IN "
                "(SELECT id FROM source_documents WHERE source_id = :sid)"
            )
            junction_where = (
                "WHERE risk_event_id IN ("
                "SELECT id FROM risk_events "
                "WHERE source_document_id IN "
                "(SELECT id FROM source_documents WHERE source_id = :sid)"
                ")"
            )
            params = {"sid": source_id}

        # Snapshot row counts up-front so the dry-run print and the
        # actual-run print look identical.
        counts: dict[str, int] = {}
        for tbl in junction_tables:
            counts[tbl] = s.execute(
                text(f"SELECT COUNT(*) FROM {tbl} {junction_where}"),
                params,
            ).scalar_one()
        counts["risk_events"] = s.execute(
            text(f"SELECT COUNT(*) FROM risk_events {risk_events_where}"),
            params,
        ).scalar_one()

        scope = (
            f"source='{source_name}' (id={source_id})"
            if source_id is not None
            else "all sources"
        )

        if not confirm:
            typer.echo(json.dumps({
                "ok": True,
                "dry_run": True,
                "scope": scope,
                "would_delete": counts,
                "hint": "re-run with --yes to actually delete",
            }, indent=2))
            return

        # Execute the deletes in order — junctions first, then events.
        deleted: dict[str, int] = {}
        for tbl in junction_tables:
            result = s.execute(text(f"DELETE FROM {tbl} {junction_where}"), params)
            deleted[tbl] = result.rowcount or 0
        result = s.execute(
            text(f"DELETE FROM risk_events {risk_events_where}"),
            params,
        )
        deleted["risk_events"] = result.rowcount or 0
        s.commit()

        # Optionally also purge source_documents (NOT recommended).
        # When --source is set we restrict the purge to that source so a
        # scoped reset can also clear its cached payloads if requested.
        sd_deleted: Optional[int] = None
        if not keep_source_documents:
            if source_id is None:
                sd_result = s.execute(text("DELETE FROM source_documents"))
            else:
                sd_result = s.execute(
                    text("DELETE FROM source_documents WHERE source_id = :sid"),
                    {"sid": source_id},
                )
            sd_deleted = sd_result.rowcount or 0
            s.commit()

        typer.echo(json.dumps({
            "ok": True,
            "dry_run": False,
            "scope": scope,
            "deleted": deleted,
            "source_documents_deleted": sd_deleted,
        }, indent=2))
    except Exception as exc:
        s.rollback()
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("reingest-all-events")
def reingest_all_events_cmd(
    reset: bool = typer.Option(
        False,
        "--reset",
        help=(
            "Run `reset-events --yes` first to clear all RiskEvent rows + "
            "junctions before re-ingesting.  Source documents are preserved "
            "so ingesters reuse cached payloads where possible."
        ),
    ),
    gta_file: Optional[str] = typer.Option(
        None,
        "--gta-file",
        help=(
            "Path to a locally-downloaded GTA CSV file.  GTA requires "
            "registration for bulk access, so the file path must be passed "
            "explicitly.  If omitted (and --gta-use-api is not set), the "
            "GTA step is skipped with a warning."
        ),
    ),
    gta_use_api: bool = typer.Option(
        False,
        "--gta-use-api",
        help=(
            "Run the GTA step via the REST API (ingest-gta --use-api) "
            "instead of a local CSV.  Requires GTA_API_KEY in the "
            "environment.  Takes precedence over --gta-file when both are "
            "set.  Server-side HS-prefix + date filtering — equivalent to "
            "the HS-filtered bulk path, NOT the --skip-hs-filter curated-"
            "export path."
        ),
    ),
    gta_since_year: int = typer.Option(
        2018,
        "--gta-since-year",
        help="since-year passed to ingest-gta in API mode.  Default 2018.",
    ),
    iea_file: Optional[str] = typer.Option(
        None,
        "--iea-file",
        help=(
            "Path to the downloaded IEA Critical Minerals Policy Tracker "
            "CSV/XLSX.  If omitted, the IEA step is skipped with a warning."
        ),
    ),
    federal_register_since: Optional[str] = typer.Option(
        None,
        "--fr-since",
        help=(
            "Federal Register --since date (YYYY-MM-DD).  Default keeps the "
            "ingester's own default (90 days).  Pass '2020-01-01' for a "
            "full historical backfill."
        ),
    ),
    skip: Optional[str] = typer.Option(
        None,
        "--skip",
        help=(
            "Comma-separated step names to skip.  Valid names: "
            "eurlex, gta, iea, opensanctions, federal-register, sec-edgar, "
            "trade-signals."
        ),
    ),
    continue_on_error: bool = typer.Option(
        False,
        "--continue-on-error",
        help=(
            "Continue running subsequent steps if one fails.  Default "
            "aborts on first failure (matching `full-score` semantics)."
        ),
    ),
) -> None:
    """Re-ingest every risk-event-producing source in dependency order.

    Use this after a logic change that invalidates existing RiskEvent rows
    (e.g., Tier 1.x audits, GTA Scope 2 fix, EUR-Lex material-junction
    wiring, content_hash format changes).  Combine with --reset to start
    from a clean slate.

    \b
    Execution order:
      1.  reset-events           (only when --reset is set)
      2.  ingest-eurlex          regulatory standing obligations
      3.  ingest-gta             trade interventions (requires --gta-file)
      4.  ingest-iea-policy-tracker
                                 positive-policy signals (requires --iea-file)
      5.  ingest-opensanctions   sanctions matches (--force used to bypass
                                 the 6-day interval gate)
      6.  ingest-federal-register
                                 US export-control / tariff / UFLPA notices
      7.  ingest-sec-edgar       10-K / 10-Q / 8-K material discussion
      8.  build-trade-signals    derives TRADE_CONCENTRATION / EXPORT_DROP
                                 from trade_flows (does not re-fetch
                                 Comtrade itself)

    \b
    Deliberately NOT included:
      ingest-comtrade            confidence-weighting applies at query time
                                 via JOIN, so existing TradeFlow rows do
                                 not need re-fetching.  Re-run separately
                                 if you actually need fresh trade volume
                                 data — costs API quota.

    \b
    Examples:
      # Clean reset + full re-ingest (typical scoring-engine fix workflow):
      bdi-ingest reingest-all-events --reset --gta-file ./data/gta.csv \\
          --iea-file ./data/iea_policy_tracker.csv

      # Re-run only what doesn't need local files:
      bdi-ingest reingest-all-events --skip gta,iea

      # Refresh after a Federal Register parser change, skip the rest:
      bdi-ingest reingest-all-events \\
          --skip eurlex,gta,iea,opensanctions,sec-edgar,trade-signals

    After completion, run `bdi-ingest full-score` so the new events flow
    through to material / chemistry / company scores.
    """
    import sys
    import subprocess

    skip_set = {s.strip().lower() for s in (skip or "").split(",") if s.strip()}
    failures: list[str] = []
    completed: list[str] = []
    skipped: list[str] = []

    def _run(step_name: str, args: list[str]) -> bool:
        """Run a sub-command. Returns True on success, False on failure.

        Honors --continue-on-error: when set, failures are recorded but
        execution continues to the next step.  When unset, the first
        failure aborts the whole orchestrator with the sub-command's
        exit code (mirroring full-score semantics).
        """
        typer.echo(f"\n{'='*60}")
        typer.echo(f"  reingest-all-events  ▶  {step_name}")
        typer.echo(f"{'='*60}")
        result = subprocess.run(
            [sys.executable, "-m", "app.cli"] + args,
            check=False,
        )
        if result.returncode != 0:
            msg = f"'{step_name}' exited with code {result.returncode}"
            failures.append(msg)
            if continue_on_error:
                typer.echo(
                    f"\n⚠  {msg} — continuing because --continue-on-error is set.",
                    err=True,
                )
                return False
            typer.echo(
                f"\n✗  reingest-all-events aborted: {msg}.",
                err=True,
            )
            raise typer.Exit(code=result.returncode)
        completed.append(step_name)
        return True

    def _maybe_skip(name: str, reason: Optional[str] = None) -> bool:
        """Return True if this step should be skipped."""
        if name in skip_set:
            typer.echo(f"\n⊘  Skipping {name} (--skip)")
            skipped.append(f"{name} (--skip)")
            return True
        if reason:
            typer.echo(f"\n⊘  Skipping {name}: {reason}")
            skipped.append(f"{name} ({reason})")
            return True
        return False

    # ── 1. Optional reset ────────────────────────────────────────────────
    if reset:
        _run("reset-events --yes", ["reset-events", "--yes"])
    else:
        typer.echo(
            "\nℹ  --reset not set; existing RiskEvent rows preserved. "
            "Re-ingest dedups via content_hash so this is safe but means "
            "rows produced under old logic stay in the table.",
        )

    # ── 2. EUR-Lex ───────────────────────────────────────────────────────
    if not _maybe_skip("eurlex"):
        _run("ingest-eurlex", ["ingest-eurlex"])

    # ── 3. GTA (requires --gta-use-api or --gta-file) ────────────────────
    if not _maybe_skip(
        "gta",
        reason=(
            "--gta-file / --gta-use-api not provided"
            if not (gta_file or gta_use_api) else None
        ),
    ):
        if gta_use_api:
            _run("ingest-gta", [
                "ingest-gta", "--use-api",
                "--since-year", str(gta_since_year),
            ])
        else:
            _run("ingest-gta", ["ingest-gta", "--local-file", gta_file])

    # ── 4. IEA Policy Tracker (requires --iea-file) ──────────────────────
    if not _maybe_skip(
        "iea",
        reason="--iea-file not provided" if not iea_file else None,
    ):
        _run(
            "ingest-iea-policy-tracker",
            ["ingest-iea-policy-tracker", "--file-path", iea_file],
        )

    # ── 5. OpenSanctions (--force bypasses the 6-day interval gate) ──────
    if not _maybe_skip("opensanctions"):
        _run("ingest-opensanctions", ["ingest-opensanctions", "--force"])

    # ── 6. Federal Register ──────────────────────────────────────────────
    if not _maybe_skip("federal-register"):
        fr_args = ["ingest-federal-register"]
        if federal_register_since:
            fr_args.extend(["--since", federal_register_since])
        _run("ingest-federal-register", fr_args)

    # ── 7. SEC EDGAR ─────────────────────────────────────────────────────
    if not _maybe_skip("sec-edgar"):
        _run("ingest-sec-edgar", ["ingest-sec-edgar"])

    # ── 8. Trade signals (NOT a Comtrade re-fetch — only derives events) ─
    if not _maybe_skip("trade-signals"):
        _run("build-trade-signals", ["build-trade-signals"])

    # ── Summary ──────────────────────────────────────────────────────────
    typer.echo(f"\n{'='*60}")
    typer.echo("  reingest-all-events  ▶  summary")
    typer.echo(f"{'='*60}")
    typer.echo(json.dumps({
        "ok": len(failures) == 0,
        "reset": reset,
        "completed": completed,
        "skipped": skipped,
        "failures": failures,
        "next_step": (
            "Run `bdi-ingest full-score` to push the new events through "
            "to material / chemistry / company scoring."
        ),
    }, indent=2))


@app.command("ingest-wmd")
def ingest_wmd_cmd(
    data_dir: str = typer.Option(
        "data/world-mining", "--dir",
        help="Folder containing the WMD xlsx files (6.4 required; 6.3c/6.3d optional).",
    ),
    edition: int = typer.Option(
        2026, "--edition", help="WMD edition year (2026 edition = 2020-2024 data).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Load World Mining Data xlsx files (production + stability/bloc groups).

    All-or-nothing on validation failures (Total mismatch, unmapped country,
    unknown sheet layout).  Idempotent upserts by (commodity, country, year);
    re-running a newer edition revises prior years in place.
    WMD is the CROSS-CHECK layer — it never feeds scoring (USGS canonical).
    """
    import json

    from app.services.ingestion.wmd import ingest_wmd

    s = _session()
    try:
        report = ingest_wmd(s, data_dir, edition=edition, dry_run=dry_run)
        typer.echo(json.dumps(report.to_dict(), indent=2))
        if report.errors:
            raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("wmd-crosscheck")
def wmd_crosscheck_cmd(
    year: int = typer.Option(2024, "--year", help="WMD reference year to compare against."),
    threshold_pp: float = typer.Option(
        2.0, "--threshold-pp",
        help="Ignore share differences below this many percentage points.",
    ),
    out: str = typer.Option(
        "data/world-mining/wmd_usgs_crosscheck.html", "--out",
        help="HTML report path (readable outside the terminal).",
    ),
) -> None:
    """USGS vs WMD production-share crosscheck -> HTML report.

    WMD's 2020-2024 series is the vintage control for the ~1-year-newer USGS
    estimate: gaps on the trajectory = timing (ignored); gaps off the whole
    series = definitional (flagged INVESTIGATE). WMD never feeds scoring.
    Run after every USGS or WMD load.
    """
    import datetime as _dt
    from pathlib import Path

    from app.services.ingestion.wmd_crosscheck import render_html, run_crosscheck

    s = _session()
    try:
        generated = _dt.date.today().isoformat()
        reports, skipped = run_crosscheck(
            s, year=year, threshold_pp=threshold_pp, generated=generated,
        )
        if not reports:
            typer.echo("No WMD data loaded (run `ingest-wmd` first) or no mapped materials.")
            raise typer.Exit(code=1)
        html = render_html(reports, year=year, threshold_pp=threshold_pp,
                           generated=generated, skipped=skipped)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(html)
        total_unexpl = sum(m.counts().get("unexplained", 0) for m in reports)
        typer.echo(f"crosscheck: {len(reports)} materials, {total_unexpl} INVESTIGATE flags")
        for m in sorted(reports, key=lambda x: -x.counts().get("unexplained", 0)):
            n = m.counts().get("unexplained", 0)
            if n:
                typer.echo(f"  {m.material}: {n} INVESTIGATE")
        typer.echo(f"\nreport written: {out}")
    finally:
        s.close()



@app.command("ingest-operational-news")
def ingest_operational_news_cmd(
    lookback_days: int = typer.Option(
        14,
        "--lookback-days",
        help="Only keep items published within this many days. Weekly cadence "
             "+ 14-day lookback deliberately overlaps; dedupe makes re-seen "
             "items no-ops.",
    ),
    feeds: str = typer.Option(
        "edgar,asx,google_news",
        "--feeds",
        help="Comma-separated subset of: edgar, asx, google_news.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Fetch + classify + count, but write nothing.",
    ),
) -> None:
    """Poll operational news feeds against the curated facility watchlist.

    Lands DISPLAY-ONLY candidate events (event_type=operational_news_candidate,
    primary_category NULL, severity NULL) for partner triage. Nothing this
    command creates can affect scores until a human promotes the event
    (sets primary_category='operational' + severity + subtype).

    \b
    Feeds:
      edgar        — SEC 8-K/6-K for watchlist operators with a CIK (official)
      asx          — UNOFFICIAL asx.com.au announcements JSON (fail-soft)
      google_news  — Google News RSS query per watchlist facility

    \b
      bdi-ingest ingest-operational-news --dry-run
      bdi-ingest ingest-operational-news --feeds edgar --lookback-days 30
    """
    from app.services.ingestion.ingest_operational_news import (
        ingest_operational_news,
    )

    feed_list = [f.strip() for f in feeds.split(",") if f.strip()]
    s = _session()
    try:
        result = ingest_operational_news(
            s, lookback_days=lookback_days, feeds=feed_list, dry_run=dry_run,
        )
        if not dry_run:
            s.commit()
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        s.rollback()
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("list-operational-candidates")
def list_operational_candidates_cmd(
    limit: int = typer.Option(50, "--limit", help="Max candidates to show."),
    check_duplicates: bool = typer.Option(
        True,
        "--check-duplicates/--no-check-duplicates",
        help="Warn when a candidate resembles an event already in the DB.",
    ),
) -> None:
    """Show the operational news triage queue (pending candidates only).

    Promoted and rejected events are excluded. Each row shows the keyword
    subtype SUGGESTION (metadata only — nothing scores off it) and any
    facility links or candidates for attachment.

    By default each row is also checked against every canonical event
    sharing a material/facility/company anchor, and near-matches print as
    "POSSIBLE DUPLICATE". Read the status tag before acting: [SCORING]
    means promoting this row would double-count the story in the
    operational pillar; [rejected] means a human already called the same
    story noise; [pending] means two feeds landed it and only one should
    be promoted. The hints are advisory — nothing is filtered out — and
    "no similar events found" prints distinctly from "SKIPPED", which
    means the candidate has no entity links to compare on.
    """
    from app.services.ingestion.operational_triage import list_pending_candidates

    s = _session()
    try:
        rows = list_pending_candidates(
            s, limit=limit, check_duplicates=check_duplicates
        )
        if not rows:
            typer.echo("Triage queue is empty.")
            return
        flagged = 0
        for r in rows:
            typer.echo(f"#{r['id']}  [{r['date'] or '?'}]  ({r['feed']})  {r['title']}")
            if r["suggested_subtype"]:
                typer.echo(f"      suggested: {r['suggested_subtype']}")
            if r["linked_facilities"]:
                typer.echo(f"      facilities: {', '.join(r['linked_facilities'])}")
            elif r["candidate_facilities"]:
                typer.echo(
                    f"      attach candidates: {', '.join(r['candidate_facilities'])}"
                )
            if r["url"]:
                typer.echo(f"      {r['url']}")
            dh = r.get("duplicate_hints")
            if dh is not None:
                if not dh["checked"]:
                    typer.echo(f"      dup check: SKIPPED ({dh['reason']})")
                elif not dh["hints"]:
                    typer.echo("      dup check: no similar events found")
                else:
                    flagged += 1
                    typer.echo("      POSSIBLE DUPLICATE:")
                    for h in dh["hints"]:
                        tag = "SCORING" if h["scores_already"] else h["status"]
                        typer.echo(
                            f"        #{h['event_id']} [{tag}] sim={h['similarity']} "
                            f"({h['source']}, {h['date'] or '?'}) {h['title'][:90]}"
                        )
                        typer.echo(f"          shared: {', '.join(h['shared'])}")
        if flagged:
            typer.echo(
                f"\n{flagged} candidate(s) resemble existing events — check those "
                "before promoting, or reject with a reason."
            )
        typer.echo(f"\n{len(rows)} pending. Promote with:")
        typer.echo(
            "  bdi-ingest promote-operational-event --event-id N --subtype "
            "facility_shutdown [--severity 0.8] [--facility 'Name']"
        )
    finally:
        s.close()


@app.command("promote-operational-event")
def promote_operational_event_cmd(
    event_id: int = typer.Option(..., "--event-id", help="risk_events.id of the candidate."),
    subtype: str = typer.Option(
        ...,
        "--subtype",
        help=(
            "Operational subtype. Taxonomy subtypes get a default severity "
            "(see constants.OPERATIONAL_SUBTYPE_DEFAULT_SEVERITY); free-form "
            "subtypes require --severity."
        ),
    ),
    severity: Optional[float] = typer.Option(
        None, "--severity",
        help="Override the taxonomy default (0-1). Required for free-form subtypes.",
    ),
    facility: Optional[str] = typer.Option(
        None, "--facility",
        help="Curated facility name to attach (adds facility/geo/material/operator links).",
    ),
    note: Optional[str] = typer.Option(None, "--note", help="Audit note."),
) -> None:
    """Promote an operational news candidate into scoring.

    Sets event_subtype + severity (taxonomy default unless overridden),
    primary_category='operational', verified=True, and records the triage
    audit trail in metadata. The event participates in the operational
    pillar at the NEXT rescore of the affected material×geo pairs.
    """
    from app.services.ingestion.operational_triage import (
        TriageError,
        promote_candidate,
    )

    s = _session()
    try:
        result = promote_candidate(
            s, event_id, subtype=subtype, severity=severity,
            facility_name=facility, note=note,
        )
        s.commit()
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except TriageError as exc:
        s.rollback()
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("reject-operational-event")
def reject_operational_event_cmd(
    event_id: int = typer.Option(..., "--event-id", help="risk_events.id of the candidate."),
    reason: str = typer.Option(..., "--reason", help="Why it's rejected (audit trail)."),
) -> None:
    """Reject an operational news candidate (removes it from the queue).

    The row is kept (audit trail + dedupe anchor so re-ingestion can't
    resurrect it) but it never scores and stops appearing in
    list-operational-candidates.
    """
    from app.services.ingestion.operational_triage import (
        TriageError,
        reject_candidate,
    )

    s = _session()
    try:
        result = reject_candidate(s, event_id, reason=reason)
        s.commit()
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except TriageError as exc:
        s.rollback()
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
