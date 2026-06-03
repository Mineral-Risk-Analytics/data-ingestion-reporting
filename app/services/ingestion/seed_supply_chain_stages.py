"""Seed the ``supply_chain_stages`` reference table (Axis B / activity).

Canonical ACTIVITY-stage taxonomy referenced by:
  * ``companies.primary_activity_stage_fk``
  * ``company_activity_stages.stage_code``
  * (deferred migration) ``company_material_exposures.supply_chain_stage_fk``

The codebase carries TWO orthogonal "stage" axes (see migration 044
docstring): Axis A = product form (ore/refined/battery_grade/…), Axis B
= activity (mining/refining/cell_making/…).  This table normalises
Axis B only.  ``typical_output_forms`` per row captures the advisory
many-to-many mapping from activity to typical product forms.

Canonical source of truth is the **"Supply Chain Stages" tab** of
``company_seed_template.xlsx`` (partner-curated).  This Python seed
mirrors that tab so a fresh DB has the same vocabulary before any
partner-curated CSV/Excel is loaded.  When the partner updates the
template, re-export the tab and update this file (or build a loader
that reads the .xlsx directly — deferred).

Bottleneck weights
------------------
Four values are methodology-anchored per the saved risk-scoring
methodology handbook:

    mining                    0.80
    refining                  1.20
    cathode_active_material   1.30
    cell_making               1.40

The remaining stages are extrapolated and flagged in the ``notes``
column for partner calibration.  Treat the non-anchor weights as
**preliminary** — they will move once the methodology document covers
them explicitly.

Run via:
    bdi-ingest seed-supply-chain-stages

Idempotent: INSERT ... ON CONFLICT (stage_code) DO UPDATE refreshes
every column except ``created_at``.
"""

from __future__ import annotations

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.supply_chain_context import SupplyChainStage

log = structlog.get_logger(__name__)


# Each row mirrors a row in the company_seed_template.xlsx "Supply Chain
# Stages" tab.  Keys map 1:1 to supply_chain_stages columns.
_STAGES: list[dict] = [
    {
        "stage_code": "mining",
        "display_name": "Mining (Extraction)",
        "description": (
            "Extraction of raw ore, brine, or concentrate from the ground. "
            "Includes hard-rock spodumene mining, brine pumping, laterite "
            "open-pit, and deep underground operations."
        ),
        "typical_facility_types": ["mine"],
        "typical_output_forms": ["ore"],
        "bottleneck_weight": 0.80,
        "hs_chapter_hint": "25, 26",
        "sort_order": 10,
        "notes": (
            "Methodology-anchored weight.  Mining is geographically "
            "diversifiable on long horizons; capacity bottlenecks happen "
            "mostly downstream."
        ),
    },
    {
        "stage_code": "beneficiation",
        "display_name": "Beneficiation / Concentration",
        "description": (
            "Physical processing of ore into concentrate (crushing, "
            "flotation, magnetic separation).  Concentrators producing "
            "spodumene concentrate, copper concentrate, REE concentrate.  "
            "Often co-located with mining but distinct activity."
        ),
        "typical_facility_types": ["concentrator", "smelter"],
        "typical_output_forms": ["concentrate"],
        "bottleneck_weight": 1.00,
        "hs_chapter_hint": "25, 26",
        "sort_order": 20,
        "notes": (
            "PRELIMINARY weight (not in methodology handbook).  Often "
            "bundled with mining when on-site.  Use 'mining' for combined "
            "ops; reserve this code for standalone concentrators."
        ),
    },
    {
        "stage_code": "refining",
        "display_name": "Refining (Chemical Conversion)",
        "description": (
            "Chemical conversion of concentrate to refined metal or "
            "battery-grade chemical: lithium hydroxide/carbonate, nickel "
            "sulfate, cobalt sulfate, copper cathode.  The classic supply-"
            "chain bottleneck stage."
        ),
        "typical_facility_types": ["refinery", "processing"],
        "typical_output_forms": ["intermediate", "refined", "battery_grade"],
        "bottleneck_weight": 1.20,
        "hs_chapter_hint": "28, 29",
        "sort_order": 30,
        "notes": (
            "Methodology-anchored weight.  Highest bottleneck pressure for "
            "battery supply chain — China dominates ~70%+ of Li, Ni, Co "
            "refining capacity."
        ),
    },
    {
        "stage_code": "precursor_production",
        "display_name": "Precursor (pCAM) Production",
        "description": (
            "Production of cathode active material precursors (NCM-pCAM, "
            "NCA-pCAM, LFP precursor).  One of the most concentrated "
            "stages in the chain — ~85% in China."
        ),
        "typical_facility_types": ["processing"],
        "typical_output_forms": ["fabricated"],
        "bottleneck_weight": 1.30,
        "hs_chapter_hint": "28",
        "sort_order": 40,
        "notes": (
            "PRELIMINARY weight (extrapolated from CAM anchor).  Often "
            "integrated into the same facility as CAM; sometimes a "
            "distinct earlier stage."
        ),
    },
    {
        "stage_code": "cathode_active_material",
        "display_name": "Cathode Active Material (CAM)",
        "description": (
            "Synthesis of finished cathode powder ready for cell electrode "
            "coating: NMC, NCA, LFP, LMFP cathode production."
        ),
        "typical_facility_types": ["processing"],
        "typical_output_forms": ["fabricated"],
        "bottleneck_weight": 1.30,
        "hs_chapter_hint": "28, 29",
        "sort_order": 50,
        "notes": (
            "Methodology-anchored weight.  Chinese share is significant; "
            "Korea + Japan + EU have growing capacity."
        ),
    },
    {
        "stage_code": "anode_active_material",
        "display_name": "Anode Active Material",
        "description": (
            "Synthesis of anode materials: synthetic graphite, natural "
            "graphite spheroidization + coating, silicon-carbon "
            "composites, lithium titanate."
        ),
        "typical_facility_types": ["processing"],
        "typical_output_forms": ["fabricated"],
        "bottleneck_weight": 1.30,
        "hs_chapter_hint": "38, 28",
        "sort_order": 60,
        "notes": (
            "PRELIMINARY weight (extrapolated from CAM anchor).  Synthetic "
            "graphite is ~95% Chinese; natural graphite shaping similar "
            "concentration."
        ),
    },
    {
        "stage_code": "separator_production",
        "display_name": "Separator Production",
        "description": (
            "Manufacture of microporous polymer films (PE, PP) that "
            "separate anode and cathode in a cell."
        ),
        "typical_facility_types": ["processing", "other"],
        "typical_output_forms": ["fabricated"],
        "bottleneck_weight": 1.10,
        "hs_chapter_hint": "39, 85",
        "sort_order": 70,
        "notes": (
            "PRELIMINARY weight.  Specialty polymer processing; "
            "concentrated in Japan, Korea, China."
        ),
    },
    {
        "stage_code": "electrolyte_production",
        "display_name": "Electrolyte Production",
        "description": (
            "Manufacture of lithium-salt electrolyte solutions (LiPF6 + "
            "solvents + additives)."
        ),
        "typical_facility_types": ["processing", "other"],
        "typical_output_forms": ["refined", "fabricated"],
        "bottleneck_weight": 1.10,
        "hs_chapter_hint": "28, 29, 38",
        "sort_order": 80,
        "notes": "PRELIMINARY weight.  LiPF6 production is highly concentrated.",
    },
    {
        "stage_code": "cell_making",
        "display_name": "Cell Manufacturing",
        "description": (
            "Assembly of finished battery cells: electrode coating, "
            "calendering, slitting, cell winding/stacking, electrolyte "
            "fill, formation, ageing.  Highest bottleneck weight."
        ),
        "typical_facility_types": ["cell_factory"],
        "typical_output_forms": ["fabricated"],
        "bottleneck_weight": 1.40,
        "hs_chapter_hint": "85",
        "sort_order": 90,
        "notes": (
            "Methodology-anchored weight.  Most capital-intensive, most "
            "concentrated globally (CATL + BYD ~50% market).  Gigafactory "
            "scale."
        ),
    },
    {
        "stage_code": "module_assembly",
        "display_name": "Module Assembly",
        "description": (
            "Assembly of cells into modules with thermal management + "
            "BMS-lite."
        ),
        "typical_facility_types": ["pack_plant"],
        "typical_output_forms": ["fabricated"],
        "bottleneck_weight": 1.10,
        "hs_chapter_hint": "85",
        "sort_order": 100,
        "notes": (
            "PRELIMINARY weight.  Lower barrier than cell manufacturing; "
            "capacity is more distributed."
        ),
    },
    {
        "stage_code": "pack_assembly",
        "display_name": "Pack Assembly",
        "description": (
            "Final assembly of modules into vehicle-ready packs with BMS, "
            "cooling, and high-voltage connections."
        ),
        "typical_facility_types": ["pack_plant"],
        "typical_output_forms": ["fabricated"],
        "bottleneck_weight": 1.05,
        "hs_chapter_hint": "85",
        "sort_order": 110,
        "notes": (
            "PRELIMINARY weight.  OEMs often do this in-house; capacity "
            "is well-distributed."
        ),
    },
    {
        "stage_code": "vehicle_assembly",
        "display_name": "Vehicle Assembly",
        "description": (
            "Final EV assembly — chassis, drivetrain, body, pack "
            "integration."
        ),
        "typical_facility_types": ["other"],
        "typical_output_forms": ["fabricated"],
        "bottleneck_weight": 1.00,
        "hs_chapter_hint": "87",
        "sort_order": 120,
        "notes": (
            "PRELIMINARY weight.  OEM core activity.  Capacity is the most "
            "distributed of any stage."
        ),
    },
    {
        "stage_code": "recycling",
        "display_name": "Recycling",
        "description": (
            "Battery recycling — black mass production, hydrometallurgical "
            "recovery, pyrometallurgical recovery.  Closes the loop back "
            "to refined metals."
        ),
        "typical_facility_types": ["recycling"],
        "typical_output_forms": ["intermediate", "refined"],
        "bottleneck_weight": 1.15,
        "hs_chapter_hint": "26, 28, 38",
        "sort_order": 130,
        "notes": (
            "PRELIMINARY weight.  Emerging stage.  Weighted higher than "
            "mining because recycled supply alleviates upstream "
            "concentration risk."
        ),
    },
    {
        "stage_code": "trading",
        "display_name": "Commodity Trading",
        "description": (
            "Physical commodity trading without owned production "
            "conversion.  Mercantile traders that move material between "
            "counterparties."
        ),
        "typical_facility_types": ["hq", "other"],
        "typical_output_forms": None,
        "bottleneck_weight": 1.00,
        "hs_chapter_hint": None,
        "sort_order": 140,
        "notes": (
            "PRELIMINARY weight.  Trafigura, IXM, Mercuria.  Doesn't add "
            "physical conversion but concentrates market power and "
            "information."
        ),
    },
    {
        "stage_code": "financial",
        "display_name": "Financial Sponsor / Lender",
        "description": (
            "Private equity, sovereign wealth funds, project lenders "
            "without direct operations.  Risk linkage is via portfolio "
            "exposure."
        ),
        "typical_facility_types": ["hq", "other"],
        "typical_output_forms": None,
        "bottleneck_weight": 0.50,
        "hs_chapter_hint": None,
        "sort_order": 150,
        "notes": (
            "PRELIMINARY weight.  Lower weight because risk is one step "
            "removed (mediated by portfolio companies)."
        ),
    },
    {
        "stage_code": "integrated",
        "display_name": "Integrated (Multi-Stage)",
        "description": (
            "META: Use when a company operates across multiple stages and "
            "no single stage dominates.  Prefer the bottleneck stage if "
            "one is clearly dominant."
        ),
        "typical_facility_types": ["varies"],
        "typical_output_forms": None,
        "bottleneck_weight": 1.30,
        "hs_chapter_hint": None,
        "sort_order": 160,
        "notes": (
            "Fallback only — operates_at_stages should always be filled "
            "in to capture the actual stage list."
        ),
    },
]


def seed_supply_chain_stages(session: Session) -> dict[str, int]:
    """Upsert the canonical supply_chain_stages reference rows.

    Returns ``{"upserted": N}`` for CLI reporting.  Caller commits.
    """
    table = SupplyChainStage.__table__
    upserted = 0
    for row in _STAGES:
        stmt = pg_insert(table).values(**row)
        update_cols = {
            c.name: stmt.excluded[c.name]
            for c in table.columns
            if c.name not in {"stage_code", "created_at"}
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["stage_code"],
            set_=update_cols,
        )
        session.execute(stmt)
        upserted += 1
    session.flush()
    log.info("seed_supply_chain_stages.done", upserted=upserted)
    return {"upserted": upserted}
