"""Seed the materials register and battery-chemistry junction data.

Two responsibilities:

1. **Material register** — insert *all* 39 canonical material rows that the
   platform tracks.  This is the single source of truth for the
   genuinely-static facts about each material: ``canonical_name``,
   ``category``, ``symbol_or_code``, ``hs_codes``, and IRA/CRMA flags.

   Three columns deliberately NOT seeded for the 35 USGS-tracked
   materials (audit, May 2026):

     - ``price_unit`` — derived from USGS Salient Price Statistics_detail
       string at ingest time (see ``mcs2026_parser._extract_price_unit_from_salient``).
     - ``data_availability`` — partner-curated qualitative judgment
       ("commercial" / "limited" / "no_benchmark") that drives chemistry
       score confidence.  Left NULL until partner reviews each material.
     - ``patent_occurrence_trend`` — denormalized cache of
       ``material_criticality_signals.trend_direction``.  Source is
       PATSTAT (future phase).  Left NULL until that pipeline lands.

   The 4 individual REEs (Nd/Pr/Dy/Tb) and Sodium DO carry these three
   columns because they came with partner-curated reasoning attached
   when first added to the codebase.

   Other dynamic signals (HHI, criticality_score, YoY changes, prices)
   are written by ``ingest-usgs`` / ``ingest-mcs-prices`` into
   ``material_criticality_signals``; the
   ``materials.criticality_score`` column is a denormalized cache that
   ``ingest-usgs`` back-syncs from the latest signal row.

   Why all 39 here (not split across seed + parser):  Materials are
   partner-curated business reference data, not parser-derived.  A
   fresh DB should populate the register without depending on a CSV
   download.  This also gives partner one file to PR-review when adding
   or revising a material — previously the same metadata lived in
   ``_MATERIAL_META`` inside ``mcs2026_parser.py`` plus the older
   ``_NON_USGS_MATERIALS`` here, with drift risk.

   Idempotent: ``UPSERT`` by ``canonical_name``.  Adding a material is
   pure-additive; renaming a canonical requires a manual SQL migration
   because foreign keys reference ``materials.id``.

2. **Battery chemistry junction** — insert ``battery_chemistry_materials``
   rows linking each chemistry to its constituent materials by looking
   up material IDs from the ``materials`` table by ``canonical_name``.

   Idempotent: skips rows whose
   ``(battery_chemistry_id, material_id, role, valid_from)`` already exist.

Usage
-----
``uv run bdi-ingest seed-materials``

Run ordering on a fresh DB:
    alembic upgrade head
    seed-materials              ← this file (creates materials)
    seed-material-aliases       ← seed_material_source_aliases.py
    seed-hs-mappings
    ingest-usgs                 ← writes signals only, no longer creates materials
    ingest-mcs-prices
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
# Material register — all 39 canonical materials the platform tracks
# ---------------------------------------------------------------------------
#
# Static facts only.  Dynamic signals (criticality_score, HHI, production
# YoY, price YoY) are written by ingest-usgs / ingest-mcs-prices into
# material_criticality_signals; the cache columns on ``materials`` are
# back-synced.  ``criticality_score`` here is set ONLY when the value is
# a partner-curated estimate that USGS doesn't compute (the four
# individual REEs and Sodium).  All other entries leave it None until
# ingest-usgs runs.
#
# When updating an entry: the partner reviews this file via PR.  When
# adding a new material: also add the source aliases that feed it
# (mcs_2026_csv chapter, fig10_prices commodity, mcs_pdf chapter, etc.)
# to ``seed_material_source_aliases.py``.

# Junction valid_from used for all v1 seed rows — represents NMC 622 era.
_JUNCTION_VALID_FROM = datetime.date(2024, 1, 1)

_MATERIALS: list[dict] = [
    # ── USGS-tracked materials (35) ───────────────────────────────────────
    # Ported from _MATERIAL_META in mcs2026_parser.py (May 2026).  All
    # 4-digit HS prefixes here match seed_hs_mappings.py partner curation.
    {
        "canonical_name": "Aluminum",
        "category": "structural",
        "symbol_or_code": "Al",
        # 2606 = bauxite (the ore — 4 tons of bauxite → 1 ton of aluminum;
        #        primary global ore source, dominated by Australia / Guinea / China)
        # 7601 = aluminum unwrought (primary refined metal — China dominates)
        # 7602 = aluminum waste/scrap (recycling stage)
        "hs_codes": ["2606", "7601", "7602"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": (
            "Cell can / module casings / busbars / battery housings. "
            "Tracked at three stages: bauxite ore (HS 2606, dominated by "
            "AU/GN/CN), primary refined aluminum (HS 7601, dominated by "
            "CN), and aluminum scrap (HS 7602, recycling stream). The MCS "
            "BAUXITE AND ALUMINA chapter feeds ore-stage country shares; "
            "the ALUMINUM chapter feeds material-level signals and refined-"
            "stage shares."
        ),
    },
    {
        "canonical_name": "Antimony",
        "category": "component",
        "symbol_or_code": "Sb",
        "hs_codes": ["2617", "2825", "8110"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Lead-acid grid alloy. Battery role limited but supply highly concentrated in China.",
    },
    {
        "canonical_name": "Bismuth",
        "category": "component",
        "symbol_or_code": "Bi",
        "hs_codes": ["8106"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Soft-magnet and solder applications. Tracked for upstream supply-shock risk.",
    },
    {
        "canonical_name": "Boron",
        "category": "electrolyte",
        "symbol_or_code": "B",
        "hs_codes": ["2528", "2810", "2840"],
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": True,
        "notes": "Borate electrolyte additives (LiBOB, LiBF4). Geographically concentrated in Turkey/USA.",
    },
    {
        "canonical_name": "Bromine",
        "category": "component",
        "symbol_or_code": "Br",
        "hs_codes": ["2801", "2811"],
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": False,
        "notes": "Bromine + brominated compounds. Not battery-primary but tracked because Albemarle "
                 "Specialties segment (Arkansas brine + Dead Sea via Jordan Bromine Company JV) uses it as "
                 "core input for fire-safety chemicals + specialty compounds. Also used in lithium-metal "
                 "battery R&D. Supply concentrated in USA (Arkansas), Dead Sea basin (Israel + Jordan), "
                 "and China.",
    },
    {
        "canonical_name": "Chromium",
        "category": "component",
        "symbol_or_code": "Cr",
        "hs_codes": ["2610", "7202", "7204", "8112"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Stainless current collectors, structural housings, ferro-alloy precursor.",
    },
    {
        "canonical_name": "Cobalt",
        "category": "cathode_active",
        "symbol_or_code": "Co",
        "hs_codes": ["2605", "2822", "2833", "2836", "8105"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "NMC/NCA cathode active. ~70% mined in DRC; concentrated refining in China.",
    },
    {
        "canonical_name": "Copper",
        "category": "current_collector",
        "symbol_or_code": "Cu",
        "hs_codes": ["2603", "7402", "7403", "7404", "7408"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Anode current collector, busbar, motor wiring. Mine vs refinery splits tracked separately.",
    },
    {
        "canonical_name": "Fluorspar",
        "category": "electrolyte",
        "symbol_or_code": "F",
        "hs_codes": ["2529", "2530", "2811", "2826"],
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": True,
        "notes": "HF / LiPF6 electrolyte salt precursor. EU CRMA strategic material.",
    },
    {
        "canonical_name": "Gallium",
        "category": "component",
        "symbol_or_code": "Ga",
        "hs_codes": ["285390", "381800", "8112"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "China dominates supply (~98%). Used in BMS power electronics rather than cell chemistry.",
    },
    {
        "canonical_name": "Germanium",
        "category": "component",
        "symbol_or_code": "Ge",
        "hs_codes": ["282560", "282739", "8112"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Sulfide-electrolyte solid-state battery candidate (LGPS). Co-product with zinc refining.",
    },
    {
        "canonical_name": "Indium",
        "category": "component",
        "symbol_or_code": "In",
        "hs_codes": ["8112"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "BMS / power-electronics indium-tin-oxide. Limited direct cell role.",
    },
    {
        "canonical_name": "Iron Ore",
        "category": "cathode_active",
        "symbol_or_code": "Fe",
        "hs_codes": ["2601"],
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": False,
        "notes": "LFP/LMFP cathode active. Battery-grade iron requires low-impurity refinement; commodity Fe HHI is not the right risk signal.",
    },
    {
        "canonical_name": "Lithium",
        "category": "cathode_active",
        "symbol_or_code": "Li",
        "hs_codes": ["2825", "2836", "2805", "283691"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Universal cathode-active across Li-ion chemistries. First HS prefix (2825) is the partner-curated fallback for unspecified import sub-types.",
    },
    {
        "canonical_name": "Magnesium",
        "category": "structural",
        "symbol_or_code": "Mg",
        "hs_codes": ["2519", "2816", "2827", "2833", "8104"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Pack structural castings. China holds ~85% of primary metal supply.",
    },
    {
        "canonical_name": "Manganese",
        "category": "cathode_active",
        "symbol_or_code": "Mn",
        "hs_codes": ["2602", "2820", "7202", "8111"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "NMC, LMFP, LMO cathode active. Battery-grade Mn sulfate is the constraint, not ore.",
    },
    {
        "canonical_name": "Molybdenum",
        "category": "component",
        "symbol_or_code": "Mo",
        "hs_codes": ["2613", "7202", "8102"],
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": False,
        "notes": "High-strength steel alloy in pack frames. Indirect battery role.",
    },
    {
        "canonical_name": "Natural Graphite",
        "category": "anode_active",
        "symbol_or_code": "C",
        "hs_codes": ["2504"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Default anode active. China holds ~70% of mining and >95% of spheronization capacity.",
    },
    {
        "canonical_name": "Nickel",
        "category": "cathode_active",
        "symbol_or_code": "Ni",
        "hs_codes": ["2604", "7501", "7502", "7503"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Class-1 nickel sulfate is the battery-relevant input. Indonesian HPAL is the dominant new capacity.",
    },
    {
        "canonical_name": "Niobium",
        "category": "component",
        "symbol_or_code": "Nb",
        "hs_codes": ["2615", "8112"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "NbTi-O fast-charge anode candidate. Brazilian CBMM holds ~85% of primary supply.",
    },
    {
        "canonical_name": "Phosphate",
        "category": "cathode_active",
        "symbol_or_code": "P",
        "hs_codes": ["2510"],
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": False,
        "notes": "LFP/LMFP cathode precursor. Battery-grade phosphoric acid is the constraint, not commodity phosphate rock.",
    },
    {
        "canonical_name": "Platinum-Group Metals",
        "category": "component",
        "symbol_or_code": "PGM",
        "hs_codes": ["7110"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Hydrogen fuel-cell catalysts (not Li-ion). Bundled canonical because individual PGMs vary by 3x in price and use.",
    },
    {
        "canonical_name": "Rare Earth Elements",
        "category": "component",
        "symbol_or_code": "REE",
        "hs_codes": ["2805", "2846", "2617"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Bundled canonical for REEs that don't have their own row (Cerium, Lanthanum, Yttrium, etc.). NdPr/Dy/Tb are tracked individually below.",
    },
    {
        "canonical_name": "Rhenium",
        "category": "component",
        "symbol_or_code": "Re",
        "hs_codes": ["2841", "8112"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": False,
        "notes": "Indirect battery-adjacent role; Mo-Re alloys used in some BMS components.",
    },
    {
        "canonical_name": "Selenium",
        "category": "component",
        "symbol_or_code": "Se",
        "hs_codes": ["2804", "2811"],
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": False,
        "notes": "Cu-refining co-product. Limited direct battery role; tracked for completeness.",
    },
    {
        "canonical_name": "Silicon (Anode Grade)",
        "category": "anode_active",
        "symbol_or_code": "Si",
        "hs_codes": ["2804", "7202"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": False,
        "notes": "Silicon-rich (5-100%) anode active. Ferrosilicon (7202.21) is the cost-effective graphite alternative; silicon metal (2804.61) is the higher-purity input. Both tracked as separate HS nodes.",
    },
    {
        "canonical_name": "Silver",
        "category": "component",
        "symbol_or_code": "Ag",
        "hs_codes": ["2616", "7106"],
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": False,
        "notes": "Conductive paste (limited use). Solid-state cell candidates explored Ag-rich anodes; not at scale.",
    },
    {
        # Briefly renamed to "Sodium Carbonate (Battery Grade)" then reverted
        # back to "Sodium" after partner review.  The narrower name didn't
        # fit the actual scoring scope — partner's curated HS code list
        # spans multiple sodium-related chemistries (NaOH, Na phosphates,
        # Na2CO3, sodium peroxometallates) all relevant to Na-ion battery
        # supply chains.  Single canonical with multiple HS-prefix
        # attributions is more honest about that scope.  USGS MCS still
        # tracks the soda ash form under "SODA ASH" chapter.
        "canonical_name": "Sodium",
        "category": "cathode_active",
        "symbol_or_code": "Na",
        "hs_codes": ["2815.11", "2835.26", "2836.20", "2841.69"],
        "criticality_score": 0.05,   # geographically distributed; low concentration risk
        "price_unit": "per_mt",
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": False,
        "patent_occurrence_trend": None,
        "data_availability": "commercial",
        "notes": (
            "Key differentiating material in sodium-ion batteries.  Tracked "
            "across multiple HS prefixes covering the Na-ion supply chain: "
            "2836.20 (sodium carbonate / soda ash — primary cathode "
            "precursor), 2835.26 (trisodium phosphate — polyanionic NIB "
            "cathode precursor), 2815.11 (caustic soda — chemical "
            "precursor), 2841.69 (sodium peroxometallates — Na-containing "
            "TM oxide precursors).  USGS MCS tracks the soda ash form."
        ),
    },
    {
        "canonical_name": "Tantalum",
        "category": "component",
        "symbol_or_code": "Ta",
        "hs_codes": ["2615", "2825", "2826", "8103"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Solid-state oxide-electrolyte candidate (LLZO with Ta dopant). Conflict-mineral provenance concerns.",
    },
    {
        "canonical_name": "Tellurium",
        "category": "component",
        "symbol_or_code": "Te",
        "hs_codes": ["2804"],
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": False,
        "notes": "Cu-refining co-product. Power-electronics (CdTe) rather than cells.",
    },
    {
        "canonical_name": "Tin",
        "category": "component",
        "symbol_or_code": "Sn",
        "hs_codes": ["8001", "8002"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Sn-Si and Sn-Sb anode candidates. Solder for module assembly.",
    },
    {
        "canonical_name": "Titanium",
        "category": "anode_active",
        "symbol_or_code": "Ti",
        "hs_codes": ["2614", "2620", "2823", "3206", "7202", "8108"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "LTO anodes (long-cycle, fast-charge). NbTi-O hybrid anodes also use Ti.",
    },
    {
        "canonical_name": "Tungsten",
        "category": "component",
        "symbol_or_code": "W",
        "hs_codes": ["2611", "2825", "2841", "2849", "7202", "8101"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Cathode dopant for high-Ni NMC. China holds ~80% of mining and refining.",
    },
    {
        "canonical_name": "Vanadium",
        "category": "component",
        "symbol_or_code": "V",
        "hs_codes": ["2615", "2620", "2825", "7202", "8112"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "Vanadium redox flow batteries (grid storage, not EV). Tracked for adjacency.",
    },
    {
        "canonical_name": "Zinc",
        "category": "component",
        "symbol_or_code": "Zn",
        "hs_codes": ["2608", "2817", "2833", "7901", "7902"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": False,
        "notes": "Anode in Zn-air and Ni-Zn cells. Also Cu/Pb refining context.",
    },
    {
        "canonical_name": "Zirconium",
        "category": "component",
        "symbol_or_code": "Zr",
        "hs_codes": ["2615", "2825", "2836", "7202", "8109", "8112"],
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "notes": "LLZO solid electrolyte (Li7La3Zr2O12) — solid-state battery candidate.",
    },

    # ── Materials NOT tracked as separate USGS MCS chapters (4) ───────────
    # USGS publishes only the 'Rare earths' aggregate; we track the four
    # most magnet-relevant individual REEs separately.  ``criticality_score``
    # is a partner-curated estimate because no USGS HHI exists for these.
    {
        "canonical_name": "Neodymium",
        "category": "magnet",
        "symbol_or_code": "Nd",
        "hs_codes": ["2805.30", "2846.90"],
        "criticality_score": 0.87,   # highly concentrated in CN
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,
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
    ("lfp", "Iron Ore", "cathode_active",    0.90, False),
    ("lfp", "Phosphate", "cathode_active", 0.90, False),
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
    ("lfmp", "Iron Ore", "cathode_active",    0.70, False),
    ("lfmp", "Manganese",            "cathode_active",    0.30, False),
    ("lfmp", "Phosphate", "cathode_active", 0.90, False),
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
    ("sodium_ion", "Iron Ore","cathode_active",    0.70, True),   # NFPP variant
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


def seed_materials_register(
    session: Session, *, force_update: bool = False
) -> dict[str, int]:
    """Upsert the full 39-material register.

    Behavior:
      - When canonical_name does NOT exist: insert all fields from the seed dict.
      - When it exists and force_update=False (default): skip — preserves any
        dynamic fields (criticality_score back-synced by ingest-usgs).
      - When it exists and force_update=True: overwrite the *static* metadata
        columns (category, symbol_or_code, hs_codes, IRA/CRMA flags,
        data_availability, price_unit, notes) but never the dynamic
        criticality_score / patent_occurrence_trend columns.

    The split between static and dynamic preservation matters: partner
    edits to seed_materials.py should propagate (force_update), but
    re-running the seed must never accidentally overwrite the latest
    USGS-derived criticality_score that ingest-usgs computed.

    Returns counts {"inserted", "updated", "skipped_existing"}.
    """
    # Truly-static fields that ``force_update`` rewrites on existing rows.
    # ``price_unit`` is excluded — it's parser-derived from USGS Salient
    # Price rows.  ``data_availability`` and ``patent_occurrence_trend``
    # are excluded for the 35 USGS-tracked materials (left NULL until
    # partner review / PATSTAT lands), but for the 4 REEs + Sodium they
    # ARE in the seed dict and the ``if k in m`` guard below handles
    # both shapes — those rows DO get force-updated.
    _STATIC_FIELDS = (
        "category", "symbol_or_code", "hs_codes",
        "is_ira_critical_mineral", "is_eu_crma_critical",
        "notes",
        # The next three are only force-updated when present in the
        # seed dict (manual REEs + Sodium).
        "data_availability", "patent_occurrence_trend",
    )
    inserted = updated = skipped = 0
    for m in _MATERIALS:
        existing = session.scalar(
            select(Material).where(Material.canonical_name == m["canonical_name"])
        )
        if existing is None:
            session.add(Material(**m))
            inserted += 1
            log.info("seed_materials.material_inserted", name=m["canonical_name"])
        elif force_update:
            for k in _STATIC_FIELDS:
                if k in m:
                    setattr(existing, k, m[k])
            # Only set criticality_score on insert, never overwrite (dynamic).
            updated += 1
            log.info("seed_materials.material_updated", name=m["canonical_name"])
        else:
            skipped += 1
            log.debug("seed_materials.material_exists", name=m["canonical_name"])

    if inserted or updated:
        session.flush()
    return {"inserted": inserted, "updated": updated, "skipped_existing": skipped}


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


def run_seed(session: Session, *, force_update: bool = False) -> dict[str, int]:
    """Run both seed steps. Commits at the end.

    ``force_update`` is forwarded to seed_materials_register — set True
    after partner edits to propagate static-metadata changes onto
    existing rows (without touching the dynamic criticality_score cache).
    """
    mat_stats = seed_materials_register(session, force_update=force_update)
    junc_stats = seed_battery_chemistry_junctions(session)
    session.commit()
    return {
        **{f"materials_{k}": v for k, v in mat_stats.items()},
        **{f"junctions_{k}": v for k, v in junc_stats.items()},
    }
