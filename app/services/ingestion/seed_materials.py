"""Seed non-USGS materials and battery chemistry junction data.

Two responsibilities:

1. **Non-USGS materials** — insert 5 material rows that are not present in the
   USGS MCS CSV (individual motor-relevant REEs and Sodium). These are absent
   because USGS publishes only a "Rare earths" aggregate row.  Uses
   ``INSERT ... ON CONFLICT (canonical_name) DO NOTHING`` — never overwrites
   any USGS-owned value.

2. **Battery chemistry junction** — insert ``battery_chemistry_materials`` rows
   linking each chemistry to its constituent materials by looking up material IDs
   from the ``materials`` table by ``canonical_name``.  This step must run *after*
   ``ingest-usgs`` has populated the materials table.

   Idempotent: uses ``ON CONFLICT ... DO NOTHING`` on the
   ``(battery_chemistry_id, material_id, role, valid_from)`` unique constraint.

Usage
-----
``uv run bdi-ingest seed-materials``
"""

from __future__ import annotations

import datetime
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.battery_chemistry import BatteryChemistry, BatteryChemistryMaterial
from app.models.supply import Material

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# 5 materials not in the USGS CSV
# ---------------------------------------------------------------------------

# Junction valid_from used for all v1 seed rows — represents NMC 622 era.
_JUNCTION_VALID_FROM = datetime.date(2024, 1, 1)

_NON_USGS_MATERIALS: list[dict] = [
    {
        "canonical_name": "Neodymium",
        "category": "magnet",
        "symbol_or_code": "Nd",
        "hs_codes": ["2805.30", "2846.90"],
        "criticality_score": 0.87,   # highly concentrated in CN
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,   # no PATSTAT coverage yet
        "data_availability": "limited",
        "notes": (
            "NdFeB permanent magnets used in EV traction motors. "
            "Co-extracted with praseodymium; China accounts for ~90% of global "
            "separation capacity. Not tracked as a separate row in USGS MCS — "
            "USGS publishes only a 'Rare earths' aggregate. "
            "criticality_score is a manual estimate based on known concentration."
        ),
    },
    {
        "canonical_name": "Praseodymium",
        "category": "magnet",
        "symbol_or_code": "Pr",
        "hs_codes": ["2805.30", "2846.90"],
        "criticality_score": 0.87,
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,
        "data_availability": "limited",
        "notes": (
            "Co-extracted with neodymium in NdPr mixed oxide. Used in NdFeB "
            "permanent magnets for EV motors. Same supply chain concentration "
            "risk as neodymium. Not tracked separately in USGS MCS."
        ),
    },
    {
        "canonical_name": "Dysprosium",
        "category": "magnet",
        "symbol_or_code": "Dy",
        "hs_codes": ["2805.30", "2846.90"],
        "criticality_score": 0.91,   # more concentrated than NdPr
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,
        "data_availability": "limited",
        "notes": (
            "Heavy rare earth used to improve high-temperature performance of "
            "NdFeB magnets in EV traction motors. ~90% sourced from China. "
            "Not substitutable for high-temperature EV motor applications. "
            "Not tracked separately in USGS MCS."
        ),
    },
    {
        "canonical_name": "Terbium",
        "category": "magnet",
        "symbol_or_code": "Tb",
        "hs_codes": ["2805.30", "2846.90"],
        "criticality_score": 0.95,
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,
        "data_availability": "no_benchmark",   # no public price benchmark
        "notes": (
            "Heavy rare earth; co-added with dysprosium in high-performance NdFeB "
            "magnets. Estimated ~100% sourced from China. No established public "
            "price benchmark — pricing is opaque and bilateral. Not tracked "
            "separately in USGS MCS."
        ),
    },
    {
        "canonical_name": "Sodium",
        "category": "cathode_active",
        "symbol_or_code": "Na",
        "hs_codes": ["2827.10", "2836.20"],
        "criticality_score": 0.05,   # geographically distributed; low concentration risk
        "price_unit": "per_mt",
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": False,
        "patent_occurrence_trend": None,
        "data_availability": "commercial",
        "notes": (
            "Key differentiating material in sodium-ion batteries. "
            "Sodium carbonate (soda ash) is globally abundant and geographically "
            "distributed — low concentration risk. Used as a supply chain "
            "signal anchor for Na-ion chemistry tracking. Not tracked by USGS MCS "
            "as a battery material."
        ),
    },
]

# ---------------------------------------------------------------------------
# Battery chemistry material compositions
#
# Intensities reflect approximate 2024 material requirements per chemistry.
# NMC: NMC 622 midpoint (Ni 0.60, Mn 0.20, Co 0.20).
#   NMC 811 (Ni 0.80) is the current industry direction — insert a new row
#   with valid_from when that shift is formally tracked.
# is_substitutable=True means another material can perform the same function
#   in a different generation of the chemistry.
# ---------------------------------------------------------------------------

# Each tuple: (chemistry_slug, canonical_name, role, intensity, is_substitutable)
_JUNCTION_ROWS: list[tuple[str, str, str, float, bool]] = [
    # ------------------------------------------------------------------
    # NMC (NMC 622 midpoint)
    # ------------------------------------------------------------------
    ("nmc", "Lithium",          "cathode_active",    0.90, False),
    ("nmc", "Nickel",           "cathode_active",    0.60, False),
    ("nmc", "Manganese",        "cathode_active",    0.20, False),
    ("nmc", "Cobalt",           "cathode_active",    0.20, False),
    ("nmc", "Natural Graphite", "anode",             0.85, True),   # can use synthetic
    ("nmc", "Copper",           "current_collector", 0.30, True),
    ("nmc", "Aluminum",         "current_collector", 0.30, True),
    ("nmc", "Fluorspar",        "electrolyte",       0.20, False),
    # Motor magnet materials (traction motor, not cell — tracked at chemistry level
    # because NMC cells go into NMC packs with NdFeB motors)
    ("nmc", "Neodymium",        "other",             0.50, False),
    ("nmc", "Dysprosium",       "other",             0.40, False),

    # ------------------------------------------------------------------
    # LFP (Lithium Iron Phosphate)
    # ------------------------------------------------------------------
    ("lfp", "Lithium",              "cathode_active",    0.90, False),
    ("lfp", "Iron Ore (LFP Grade)", "cathode_active",    0.90, False),
    ("lfp", "Phosphate (Battery Grade)", "cathode_active", 0.90, False),
    ("lfp", "Natural Graphite",     "anode",             0.85, True),
    ("lfp", "Copper",               "current_collector", 0.30, True),
    ("lfp", "Aluminum",             "current_collector", 0.30, True),
    ("lfp", "Fluorspar",            "electrolyte",       0.20, False),

    # ------------------------------------------------------------------
    # NCA (Lithium Nickel Cobalt Aluminum Oxide)
    # ------------------------------------------------------------------
    ("nca", "Lithium",          "cathode_active",    0.90, False),
    ("nca", "Nickel",           "cathode_active",    0.80, False),
    ("nca", "Cobalt",           "cathode_active",    0.15, False),
    ("nca", "Aluminum",         "cathode_active",    0.05, False),  # structural doping
    ("nca", "Natural Graphite", "anode",             0.85, True),
    ("nca", "Copper",           "current_collector", 0.30, True),
    ("nca", "Fluorspar",        "electrolyte",       0.20, False),

    # ------------------------------------------------------------------
    # LFMP (Lithium Iron Manganese Phosphate)
    # ------------------------------------------------------------------
    ("lfmp", "Lithium",              "cathode_active",    0.90, False),
    ("lfmp", "Iron Ore (LFP Grade)", "cathode_active",    0.70, False),
    ("lfmp", "Manganese",            "cathode_active",    0.30, False),
    ("lfmp", "Phosphate (Battery Grade)", "cathode_active", 0.90, False),
    ("lfmp", "Natural Graphite",     "anode",             0.85, True),
    ("lfmp", "Copper",               "current_collector", 0.30, True),
    ("lfmp", "Aluminum",             "current_collector", 0.30, True),
    ("lfmp", "Fluorspar",            "electrolyte",       0.20, False),

    # ------------------------------------------------------------------
    # Sodium-Ion (NIB)
    # Primary near-term disruption vector — does not use lithium.
    # Materials more geographically distributed, reducing concentration risk.
    # ------------------------------------------------------------------
    ("sodium_ion", "Sodium",              "cathode_active",    0.90, False),
    ("sodium_ion", "Iron Ore (LFP Grade)","cathode_active",    0.70, True),   # NFPP variant
    ("sodium_ion", "Manganese",           "cathode_active",    0.50, True),   # layered oxide
    ("sodium_ion", "Natural Graphite",    "anode",             0.50, True),   # hard carbon better
    ("sodium_ion", "Aluminum",            "current_collector", 0.50, True),   # can replace Cu
    ("sodium_ion", "Copper",              "current_collector", 0.20, True),
    ("sodium_ion", "Fluorspar",           "electrolyte",       0.20, False),

    # ------------------------------------------------------------------
    # Solid-State Lithium (SSL)
    # Pre-commercial at scale. New material dependencies vs liquid-electrolyte.
    # Sulfide electrolytes → Germanium/Silicon; Oxide electrolytes → Tantalum.
    # ------------------------------------------------------------------
    ("solid_state", "Lithium",          "cathode_active",    0.90, False),
    ("solid_state", "Nickel",           "cathode_active",    0.50, False),  # NMC-like cathode
    ("solid_state", "Cobalt",           "cathode_active",    0.30, False),
    ("solid_state", "Lithium",          "anode",             0.90, False),  # Li metal anode
    ("solid_state", "Germanium",        "electrolyte",       0.60, True),   # sulfide electrolyte
    ("solid_state", "Silicon (Anode Grade)", "electrolyte",  0.40, True),   # LGPS-type
    ("solid_state", "Tantalum",         "electrolyte",       0.30, True),   # oxide electrolyte
    ("solid_state", "Copper",           "current_collector", 0.30, True),
    ("solid_state", "Aluminum",         "current_collector", 0.30, True),
]


def seed_non_usgs_materials(session: Session) -> int:
    """Insert the 5 materials not in the USGS CSV.

    Uses ON CONFLICT DO NOTHING — never overwrites USGS-derived values.
    Returns count of rows inserted.
    """
    inserted = 0
    for m in _NON_USGS_MATERIALS:
        existing = session.scalar(
            select(Material).where(Material.canonical_name == m["canonical_name"])
        )
        if existing is None:
            session.add(Material(**m))
            inserted += 1
            log.info("seed_materials.material_inserted", name=m["canonical_name"])
        else:
            log.debug("seed_materials.material_exists", name=m["canonical_name"])
    if inserted:
        session.flush()
    return inserted


def seed_battery_chemistry_junctions(session: Session) -> dict[str, int]:
    """Insert battery_chemistry_materials rows.

    Requires that both battery_chemistries and materials tables have been
    populated (run ingest-usgs and seed-materials first).

    Idempotent: skips rows whose (chemistry_id, material_id, role, valid_from)
    already exist.

    Returns {"inserted": int, "skipped_missing_material": int, "skipped_existing": int}.
    """
    inserted = 0
    skipped_missing = 0
    skipped_existing = 0

    # Cache chemistry slug → id
    chem_rows = session.scalars(select(BatteryChemistry)).all()
    chem_map: dict[str, int] = {c.slug: c.id for c in chem_rows}

    # Cache canonical_name → id for materials
    mat_rows = session.scalars(select(Material)).all()
    mat_map: dict[str, int] = {m.canonical_name: m.id for m in mat_rows}

    for slug, canonical_name, role, intensity, is_sub in _JUNCTION_ROWS:
        chem_id = chem_map.get(slug)
        mat_id = mat_map.get(canonical_name)

        if chem_id is None:
            log.warning("seed_junctions.missing_chemistry", slug=slug)
            skipped_missing += 1
            continue

        if mat_id is None:
            log.warning(
                "seed_junctions.missing_material",
                chemistry=slug,
                material=canonical_name,
            )
            skipped_missing += 1
            continue

        # Idempotency check
        existing = session.scalar(
            select(BatteryChemistryMaterial).where(
                BatteryChemistryMaterial.battery_chemistry_id == chem_id,
                BatteryChemistryMaterial.material_id == mat_id,
                BatteryChemistryMaterial.role == role,
                BatteryChemistryMaterial.valid_from == _JUNCTION_VALID_FROM,
            )
        )
        if existing is not None:
            skipped_existing += 1
            continue

        session.add(
            BatteryChemistryMaterial(
                battery_chemistry_id=chem_id,
                material_id=mat_id,
                role=role,
                intensity=intensity,
                is_substitutable=is_sub,
                valid_from=_JUNCTION_VALID_FROM,
                valid_to=None,
            )
        )
        inserted += 1

    if inserted:
        session.flush()

    log.info(
        "seed_junctions.done",
        inserted=inserted,
        skipped_missing=skipped_missing,
        skipped_existing=skipped_existing,
    )
    return {
        "inserted": inserted,
        "skipped_missing_material": skipped_missing,
        "skipped_existing": skipped_existing,
    }


def run_seed(session: Session) -> dict[str, int]:
    """Run both seed steps. Commits at the end."""
    mat_inserted = seed_non_usgs_materials(session)
    junc_stats = seed_battery_chemistry_junctions(session)
    session.commit()
    return {
        "materials_inserted": mat_inserted,
        **{f"junctions_{k}": v for k, v in junc_stats.items()},
    }
