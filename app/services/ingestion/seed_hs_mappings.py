"""HS code to material mapping seed data and upsert logic.

HS 2022 nomenclature.  Each entry is a 7-tuple:
  (hs_prefix, canonical_name, description, confidence, supply_chain_stage, digit_count, market_scope)

Confidence tiers:
  1.0 — unambiguous single-material prefix
  0.7–0.9 — primary material; prefix covers 2–3 commodities
  0.5–0.6 — partial attribution; prefix covers many commodities

Confidence reflects prefix specificity, NOT data quality.  A 0.6 for
"2615 → Vanadium" means that 2615 covers Nb/Ta/Va/Zr and cannot be cleanly
attributed to one material at the 4-digit level.

Supply chain stages:
  ore           Raw mined ore or concentrate before processing
  concentrate   Processed concentrate before smelting
  intermediate  Intermediate metallurgical product (mattes, sinters, ferroalloys)
  refined       Unwrought refined metal or unprocessed chemical form
  battery_grade Battery-grade compound (hydroxide, carbonate, sulfate, etc.)
  fabricated    Finished/semi-finished product (future use)
  scrap         Recycled/secondary material (future use)

Sources:
  - USGS MCS 2025 (per-commodity HS code tables in PDF annexes)
  - UN Comtrade HS 2022 nomenclature (comtradeapi.un.org/data/v1/getDA/HS)
  - EU CRM Act 2023 Annex II (official HS codes per strategic raw material)

Re-run with --force when:
  - WCO updates the HS nomenclature (every 5 years)
  - A new material is added and its HS codes are known
  - Stage assignments or confidence values need correction
  - Keyword definitions in `_HS_KEYWORDS_BY_MAPPING` change

Keywords (`hs_code_material_mappings.keywords`, migration 028) are seeded by
`_upsert_keywords_for_mappings()` defined below, which is called from
`upsert_hs_mappings` after the mapping rows are written.  Co-located in this
module so a single seed run handles both.  See the dict's docstring for the
keyword selection rule.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.supply import HsCodeMaterialMapping, Material

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Stage sequence lookup — derived from supply_chain_stage, not stored in tuple
# ---------------------------------------------------------------------------

_STAGE_SEQUENCE: dict[str, int] = {
    "ore": 1,
    "concentrate": 2,
    "intermediate": 3,
    "refined": 4,
    "battery_grade": 5,
    "fabricated": 6,
    "scrap": 7,
}

# ---------------------------------------------------------------------------
# Static reference data
# Tuple shape: (hs_prefix, canonical_name, description, confidence,
#               supply_chain_stage, digit_count, market_scope)
#
# canonical_name must match materials.canonical_name exactly.
# Rows where the material is absent from the DB are silently skipped.
# ---------------------------------------------------------------------------

_MAPPINGS: list[tuple[str, str, str, float, str, int, str]] = [
    # ── Natural Graphite ──────────────────────────────────────────────────
    ("2504", "Natural Graphite",
     "Natural graphite — all forms", 1.0, "ore", 4, "global"),

    # ── Phosphate ─────────────────────────────────────────────────────────
    ("2510", "Phosphate (Battery Grade)",
     "Natural calcium phosphates and phosphatic chalk", 1.0, "ore", 4, "global"),
    ("2809", "Phosphate (Battery Grade)",
     "Diphosphorus pentaoxide; phosphoric acid (processed phosphate)",
     0.9, "battery_grade", 4, "global"),

    # ── Fluorspar ─────────────────────────────────────────────────────────
    ("2529", "Fluorspar",
     "Fluorspar (fluorite); feldspar; leucite (fluorspar is primary battery-relevant component)",
     0.8, "ore", 4, "global"),

    # ── Magnesium ─────────────────────────────────────────────────────────
    ("2519", "Magnesium",
     "Natural magnesium carbonate (magnesite); fused magnesia", 0.7, "ore", 4, "global"),
    ("8104", "Magnesium",
     "Magnesium and articles thereof — unwrought metal", 1.0, "refined", 4, "global"),

    # ── Iron Ore (LFP Grade) ──────────────────────────────────────────────
    ("2601", "Iron Ore (LFP Grade)",
     "Iron ores and concentrates, including roasted iron pyrites", 1.0, "ore", 4, "global"),

    # ── Manganese ─────────────────────────────────────────────────────────
    ("2602", "Manganese",
     "Manganese ores and concentrates", 1.0, "ore", 4, "global"),
    ("8111", "Manganese",
     "Manganese and articles thereof — unwrought metal", 1.0, "refined", 4, "global"),

    # ── Copper ────────────────────────────────────────────────────────────
    ("2603", "Copper",
     "Copper ores and concentrates", 1.0, "ore", 4, "global"),
    ("7402", "Copper",
     "Copper (unrefined); copper anodes for electrolytic refining",
     0.9, "intermediate", 4, "global"),
    ("7403", "Copper",
     "Refined copper and copper alloys — unwrought", 1.0, "refined", 4, "global"),

    # ── Nickel ────────────────────────────────────────────────────────────
    ("2604", "Nickel",
     "Nickel ores and concentrates", 1.0, "ore", 4, "global"),
    ("7501", "Nickel",
     "Nickel mattes; nickel oxide sinters and other intermediate products",
     1.0, "intermediate", 4, "global"),
    ("7502", "Nickel",
     "Unwrought nickel", 1.0, "refined", 4, "global"),

    # ── Cobalt ────────────────────────────────────────────────────────────
    ("2605", "Cobalt",
     "Cobalt ores and concentrates", 1.0, "ore", 4, "global"),
    ("8105", "Cobalt",
     "Cobalt mattes and other intermediate products of cobalt metallurgy; unwrought cobalt",
     # 8105.20 = mattes/intermediates (dominant traded form); 8105.90 = articles.
     # Classified as intermediate since mattes dominate Comtrade flows at this prefix.
     1.0, "intermediate", 4, "global"),

    # ── Aluminum ──────────────────────────────────────────────────────────
    ("2606", "Aluminum",
     "Aluminium ores and concentrates (bauxite)", 1.0, "ore", 4, "global"),
    ("7601", "Aluminum",
     "Unwrought aluminium", 1.0, "refined", 4, "global"),

    # ── Tin ───────────────────────────────────────────────────────────────
    ("2609", "Tin",
     "Tin ores and concentrates", 1.0, "ore", 4, "global"),
    ("8001", "Tin",
     "Unwrought tin", 1.0, "refined", 4, "global"),

    # ── Chromium ──────────────────────────────────────────────────────────
    ("2610", "Chromium",
     "Chromium ores and concentrates (chromite)", 1.0, "ore", 4, "global"),
    ("8112", "Chromium",
     "Chapter 81 minor metals — chromium is primary battery-relevant component at 8112.21",
     # 8112 also covers Ge, Ga, In, Nb, V — lower confidence at 4-digit level
     0.7, "refined", 4, "global"),

    # ── Tungsten ──────────────────────────────────────────────────────────
    ("2611", "Tungsten",
     "Tungsten ores and concentrates", 1.0, "ore", 4, "global"),
    ("8101", "Tungsten",
     "Tungsten and articles thereof — unwrought metal", 1.0, "refined", 4, "global"),

    # ── Molybdenum ────────────────────────────────────────────────────────
    ("2613", "Molybdenum",
     "Molybdenum ores and concentrates", 1.0, "ore", 4, "global"),
    ("8102", "Molybdenum",
     "Molybdenum and articles thereof — unwrought metal", 1.0, "refined", 4, "global"),
    ("7202", "Molybdenum",
     "Ferroalloys — ferromolybdenum (7202.70) is the primary traded form",
     # 7202 covers all ferroalloys; molybdenum is one of many
     0.6, "intermediate", 4, "global"),

    # ── Titanium ──────────────────────────────────────────────────────────
    ("2614", "Titanium",
     "Titanium ores and concentrates (ilmenite, rutile)", 1.0, "ore", 4, "global"),
    ("8108", "Titanium",
     "Titanium and articles thereof — unwrought metal, sponge", 1.0, "refined", 4, "global"),
    ("7202", "Titanium",
     "Ferroalloys — ferrotitanium (7202.91) is a traded intermediate form",
     # shared prefix with all ferroalloys; low confidence at 4-digit
     0.5, "intermediate", 4, "global"),

    # ── Vanadium ──────────────────────────────────────────────────────────
    ("2615", "Vanadium",
     "Niobium, tantalum, vanadium and zirconium ores — vanadium is primary by trade volume",
     # shared with Nb, Ta, Zr ores
     0.6, "ore", 4, "global"),
    ("7202", "Vanadium",
     "Ferroalloys — ferrovanadium (7202.92) is the primary traded form",
     # shared prefix; 7202.92 is ferrovanadium specifically
     0.7, "intermediate", 4, "global"),

    # ── Niobium ───────────────────────────────────────────────────────────
    ("2615", "Niobium",
     "Niobium, tantalum, vanadium and zirconium ores — niobium-bearing (columbite-tantalite)",
     # shared with Va, Ta, Zr
     0.6, "ore", 4, "global"),
    ("8112", "Niobium",
     "Chapter 81 minor metals — niobium (columbium) unwrought at 8112.92",
     # shared with Ge, Ga, In, Cr, V
     0.6, "refined", 4, "global"),

    # ── Tantalum ──────────────────────────────────────────────────────────
    ("2615", "Tantalum",
     "Niobium, tantalum, vanadium and zirconium ores — tantalite component",
     # shared with Nb, Va, Zr
     0.6, "ore", 4, "global"),
    ("8103", "Tantalum",
     "Tantalum and articles thereof — unwrought metal, powders", 1.0, "refined", 4, "global"),

    # ── Zirconium ─────────────────────────────────────────────────────────
    ("2615", "Zirconium",
     "Niobium, tantalum, vanadium and zirconium ores — zircon sands",
     0.6, "ore", 4, "global"),
    ("8109", "Zirconium",
     "Zirconium and articles thereof — unwrought metal, sponge", 1.0, "refined", 4, "global"),

    # ── Silicon (Anode Grade) ─────────────────────────────────────────────
    ("2804", "Silicon (Anode Grade)",
     "Hydrogen; rare gases; other non-metals — silicon at 2804.61/2804.69",
     # 2804 also covers H, He, N, O, Se, Te at 4-digit level
     0.7, "refined", 4, "global"),

    # ── Tellurium ─────────────────────────────────────────────────────────
    ("2804", "Tellurium",
     "Non-metals — tellurium at 2804.50; shares prefix with Si, Se, S",
     0.6, "refined", 4, "global"),

    # ── Gallium ───────────────────────────────────────────────────────────
    ("8112", "Gallium",
     "Chapter 81 minor metals — gallium unwrought at 8112.21/8112.29",
     # shared with Ge, Cr, In, Nb, V at 4-digit level
     0.7, "refined", 4, "global"),

    # ── Germanium ─────────────────────────────────────────────────────────
    ("8112", "Germanium",
     "Chapter 81 minor metals — germanium at 8112.31/8112.39",
     0.7, "refined", 4, "global"),

    # ── Indium ────────────────────────────────────────────────────────────
    ("8112", "Indium",
     "Chapter 81 minor metals — indium at 8112.61/8112.69",
     0.7, "refined", 4, "global"),

    # ── Antimony ──────────────────────────────────────────────────────────
    ("2617", "Antimony",
     "Other ores and concentrates — antimony ores at 2617.10",
     # 2617.90 (other ores) reduces confidence slightly at 4-digit
     0.8, "ore", 4, "global"),
    ("8110", "Antimony",
     "Antimony and articles thereof — unwrought metal", 1.0, "refined", 4, "global"),

    # ── Lithium ───────────────────────────────────────────────────────────
    ("2825", "Lithium",
     "Lithium oxide and hydroxide — primary processed form for battery electrolyte",
     # 2825.20 is lithium hydroxide specifically; dominant battery-grade Li trade
     1.0, "battery_grade", 4, "global"),
    ("2836", "Lithium",
     "Carbonates — lithium carbonate at 2836.91; primary cathode precursor form",
     # 2836 covers all carbonates; lithium carbonate is dominant battery Li form
     0.9, "battery_grade", 4, "global"),
    ("2805", "Lithium",
     "Alkali metals — lithium metal at 2805.12; also covers Na, K, Ca, Sr, Ba, Ra",
     # 2805 also contains rare earth metals at 2805.30
     0.7, "refined", 4, "global"),

    # ── Rare Earth Elements ───────────────────────────────────────────────
    ("2846", "Rare Earth Elements",
     "Compounds of rare earth metals, yttrium, scandium — primary processed REE trade form",
     1.0, "battery_grade", 4, "global"),
    ("2805", "Rare Earth Elements",
     "Rare earth metals, scandium, yttrium — unwrought at 2805.30; shares prefix with Li",
     0.8, "refined", 4, "global"),
    ("2617", "Rare Earth Elements",
     "Other ores — some REE-bearing ores (bastnasite, monazite) trade under 2617.90",
     # 2617.90 is a catch-all; low confidence
     0.5, "ore", 4, "global"),

    # ── Silver ────────────────────────────────────────────────────────────
    ("2616", "Silver",
     "Precious metal ores and concentrates — silver ores at 2616.10", 0.9, "ore", 4, "global"),
    ("7106", "Silver",
     "Silver (including silver plated with gold or platinum) — unwrought", 1.0, "refined", 4, "global"),

    # ── Platinum-Group Metals ─────────────────────────────────────────────
    ("7110", "Platinum-Group Metals",
     "Platinum; palladium; rhodium; iridium; osmium; ruthenium — all PGM forms",
     1.0, "refined", 4, "global"),
    ("2616", "Platinum-Group Metals",
     "Precious metal ores — PGM-bearing ores at 2616.90", 0.8, "ore", 4, "global"),

    # ── Zinc ──────────────────────────────────────────────────────────────
    ("2608", "Zinc",
     "Zinc ores and concentrates", 1.0, "ore", 4, "global"),
    ("7901", "Zinc",
     "Unwrought zinc", 1.0, "refined", 4, "global"),

    # ── Boron ─────────────────────────────────────────────────────────────
    ("2528", "Boron",
     "Natural borates and concentrates; natural boric acid", 0.9, "ore", 4, "global"),
    ("2810", "Boron",
     "Oxides of boron; boric acids (processed boron compounds)", 0.9, "intermediate", 4, "global"),

    # ── Selenium ──────────────────────────────────────────────────────────
    ("2804", "Selenium",
     "Non-metals — selenium at 2804.50; shares prefix with Si, S, Te",
     0.6, "refined", 4, "global"),

    # ── Individual motor REEs ─────────────────────────────────────────────
    ("2805", "Neodymium",
     "Rare earth metals — neodymium metal unwrought at 2805.30; shares prefix with Li and other REEs",
     0.6, "refined", 4, "global"),
    ("2846", "Neodymium",
     "Compounds of rare earth metals — neodymium compounds including NdFeB precursors",
     0.7, "battery_grade", 4, "global"),
    ("2805", "Praseodymium",
     "Rare earth metals — praseodymium metal unwrought at 2805.30",
     0.6, "refined", 4, "global"),
    ("2846", "Praseodymium",
     "Compounds of rare earth metals — praseodymium compounds",
     0.7, "battery_grade", 4, "global"),
    ("2805", "Dysprosium",
     "Rare earth metals — dysprosium metal unwrought at 2805.30",
     0.6, "refined", 4, "global"),
    ("2846", "Dysprosium",
     "Compounds of rare earth metals — dysprosium compounds",
     0.7, "battery_grade", 4, "global"),
    ("2805", "Terbium",
     "Rare earth metals — terbium metal unwrought at 2805.30",
     0.6, "refined", 4, "global"),
    ("2846", "Terbium",
     "Compounds of rare earth metals — terbium compounds",
     0.7, "battery_grade", 4, "global"),

    # ── Sodium (Na-ion) ───────────────────────────────────────────────────
    ("2836", "Sodium",
     "Carbonates — sodium carbonate (soda ash) at 2836.20; primary Na-ion cathode precursor",
     0.8, "battery_grade", 4, "global"),
    ("2827", "Sodium",
     "Chlorides and chloride oxides — sodium chloride at 2827.10",
     0.7, "intermediate", 4, "global"),

    # =========================================================================
    # 6-DIGIT GLOBAL ROWS (stage-specific; derived from MCS 2026 tariff tables)
    # Sourced by reading MCS 2026 PDF tariff sections for each commodity.
    # These rows backfill supply_chain_stage on rows the PDF parser creates with
    # stage=NULL.  Run `seed-hs-mappings --force` after `ingest-mcs-pdf` to
    # apply.
    # Confidence reflects 6-digit prefix specificity (not data quality).
    # =========================================================================

    # ── Natural Graphite (6-digit) ────────────────────────────────────────
    ("250410", "Natural Graphite",
     "Crystalline flake graphite and powder", 1.0, "ore", 6, "global"),
    ("250490", "Natural Graphite",
     "Other natural graphite (amorphous, lump)", 1.0, "ore", 6, "global"),

    # ── Phosphate (Battery Grade) (6-digit) ──────────────────────────────
    ("251010", "Phosphate (Battery Grade)",
     "Natural calcium phosphates, unground", 1.0, "ore", 6, "global"),
    ("251020", "Phosphate (Battery Grade)",
     "Natural calcium phosphates, ground", 1.0, "ore", 6, "global"),
    ("280920", "Phosphate (Battery Grade)",
     "Phosphoric acid and polyphosphoric acids (battery-grade H3PO4 precursor)",
     0.9, "battery_grade", 6, "global"),

    # ── Fluorspar (6-digit) ───────────────────────────────────────────────
    ("252921", "Fluorspar",
     "Fluorspar, metallurgical grade (≤97% CaF2)", 1.0, "ore", 6, "global"),
    ("252922", "Fluorspar",
     "Fluorspar, acid grade (>97% CaF2)", 1.0, "concentrate", 6, "global"),
    ("253090", "Fluorspar",
     "Natural cryolite and chiolite (fluorine-bearing minerals)",
     0.8, "ore", 6, "global"),
    ("281111", "Fluorspar",
     "Hydrogen fluoride (hydrofluoric acid) — processed fluorspar derivative",
     1.0, "intermediate", 6, "global"),
    ("282612", "Fluorspar",
     "Aluminum fluoride (AlF3)", 1.0, "intermediate", 6, "global"),
    ("282630", "Fluorspar",
     "Sodium hexafluoroaluminate (synthetic cryolite)", 1.0, "intermediate", 6, "global"),

    # ── Magnesium (6-digit) ───────────────────────────────────────────────
    ("251910", "Magnesium",
     "Crude magnesite", 1.0, "ore", 6, "global"),
    ("251990", "Magnesium",
     "Dead-burned, fused, and caustic-calcined magnesia (MgO)",
     1.0, "intermediate", 6, "global"),
    ("253020", "Magnesium",
     "Kieserite and Epsom salts (natural magnesium sulfate minerals)",
     0.9, "ore", 6, "global"),
    ("281610", "Magnesium",
     "Magnesium hydroxide and peroxide", 0.9, "intermediate", 6, "global"),
    ("282731", "Magnesium",
     "Magnesium chloride", 0.9, "intermediate", 6, "global"),
    ("283321", "Magnesium",
     "Magnesium sulfate (synthetic)", 0.9, "intermediate", 6, "global"),
    ("810411", "Magnesium",
     "Magnesium, unwrought (not alloyed)", 1.0, "refined", 6, "global"),
    ("810419", "Magnesium",
     "Magnesium alloys, unwrought", 1.0, "refined", 6, "global"),
    ("810420", "Magnesium",
     "Magnesium waste and scrap", 1.0, "scrap", 6, "global"),
    ("810430", "Magnesium",
     "Magnesium powders and granules", 1.0, "refined", 6, "global"),
    ("810490", "Magnesium",
     "Wrought magnesium and magnesium articles", 1.0, "fabricated", 6, "global"),

    # ── Iron Ore (LFP Grade) (6-digit) ───────────────────────────────────
    ("260111", "Iron Ore (LFP Grade)",
     "Iron ores and concentrates, non-agglomerated", 1.0, "ore", 6, "global"),
    ("260112", "Iron Ore (LFP Grade)",
     "Iron ores and concentrates, agglomerated (pellets, sinter feed)",
     1.0, "ore", 6, "global"),
    ("260120", "Iron Ore (LFP Grade)",
     "Roasted iron pyrites (iron sinter)", 0.9, "concentrate", 6, "global"),

    # ── Manganese (6-digit) ───────────────────────────────────────────────
    ("260200", "Manganese",
     "Manganese ores and concentrates (all grades)", 1.0, "ore", 6, "global"),
    ("282010", "Manganese",
     "Manganese dioxide (EMD/CMD — direct battery cathode input)",
     1.0, "battery_grade", 6, "global"),
    ("720211", "Manganese",
     "Ferromanganese, containing >4% carbon (high-carbon)", 1.0, "intermediate", 6, "global"),
    ("720219", "Manganese",
     "Ferromanganese, containing ≤4% carbon (medium/low-carbon)",
     1.0, "intermediate", 6, "global"),
    ("720230", "Manganese",
     "Ferrosilicon manganese (silicomanganese)", 1.0, "intermediate", 6, "global"),
    ("811100", "Manganese",
     "Manganese metal, unwrought; waste and scrap; articles",
     1.0, "refined", 6, "global"),

    # ── Copper (6-digit) ──────────────────────────────────────────────────
    ("260300", "Copper",
     "Copper ores and concentrates (copper content)", 1.0, "ore", 6, "global"),
    ("740200", "Copper",
     "Unrefined copper; copper anodes for electrolytic refining",
     1.0, "intermediate", 6, "global"),
    ("740300", "Copper",
     "Refined copper and copper alloys, unwrought", 1.0, "refined", 6, "global"),
    ("740400", "Copper",
     "Copper waste and scrap", 1.0, "scrap", 6, "global"),
    ("740811", "Copper",
     "Wire rod of refined copper (continuous cast rod)", 1.0, "fabricated", 6, "global"),

    # ── Nickel (6-digit) ──────────────────────────────────────────────────
    ("260400", "Nickel",
     "Nickel ores and concentrates (nickel content)", 1.0, "ore", 6, "global"),
    ("720260", "Nickel",
     "Ferronickel", 1.0, "intermediate", 6, "global"),
    ("750210", "Nickel",
     "Unwrought nickel, not alloyed", 1.0, "refined", 6, "global"),
    ("750300", "Nickel",
     "Nickel waste and scrap", 1.0, "scrap", 6, "global"),

    # ── Cobalt (6-digit) ──────────────────────────────────────────────────
    ("260500", "Cobalt",
     "Cobalt ores and concentrates", 1.0, "ore", 6, "global"),
    ("282200", "Cobalt",
     "Cobalt oxides and hydroxides (Co3O4, Co(OH)2 — cathode precursors)",
     1.0, "intermediate", 6, "global"),
    ("283329", "Cobalt",
     "Cobalt sulfate (CoSO4 — battery-grade NMC precursor input)",
     # 283329 covers several sulfates; cobalt sulfate is the dominant battery form
     0.8, "battery_grade", 6, "global"),
    ("283699", "Cobalt",
     "Cobalt carbonate (CoCO3 — chemical precursor)",
     # 283699 covers many carbonates; cobalt carbonate is a precursor form
     0.7, "intermediate", 6, "global"),
    ("291529", "Cobalt",
     "Cobalt acetates (Co(CH3COO)2 — chemical precursor)", 0.9, "intermediate", 6, "global"),
    ("810520", "Cobalt",
     "Unwrought cobalt and cobalt alloys (refined metal)", 1.0, "refined", 6, "global"),
    ("810530", "Cobalt",
     "Cobalt mattes and other intermediate products of cobalt metallurgy; powders",
     1.0, "intermediate", 6, "global"),
    ("810590", "Cobalt",
     "Cobalt waste and scrap", 1.0, "scrap", 6, "global"),

    # ── Aluminum (6-digit) ────────────────────────────────────────────────
    ("760110", "Aluminum",
     "Unwrought aluminum, not alloyed (primary or secondary)",
     1.0, "refined", 6, "global"),
    ("760120", "Aluminum",
     "Aluminum alloys, unwrought (billet, T-bar)", 1.0, "refined", 6, "global"),
    ("760200", "Aluminum",
     "Aluminum waste and scrap", 1.0, "scrap", 6, "global"),

    # ── Tin (6-digit) ─────────────────────────────────────────────────────
    ("800110", "Tin",
     "Unwrought tin, not alloyed", 1.0, "refined", 6, "global"),
    ("800120", "Tin",
     "Tin alloys, unwrought", 1.0, "refined", 6, "global"),
    ("800200", "Tin",
     "Tin waste and scrap", 1.0, "scrap", 6, "global"),

    # ── Chromium (6-digit) ────────────────────────────────────────────────
    ("261000", "Chromium",
     "Chromium ores and concentrates (chromite)", 1.0, "ore", 6, "global"),
    ("720241", "Chromium",
     "Ferrochromium, containing >4% carbon (high-carbon)", 1.0, "intermediate", 6, "global"),
    ("720249", "Chromium",
     "Ferrochromium, containing ≤4% carbon (charge/low-carbon)",
     1.0, "intermediate", 6, "global"),
    ("811221", "Chromium",
     "Chromium metal, unwrought (including powders)", 1.0, "refined", 6, "global"),
    ("811222", "Chromium",
     "Chromium waste and scrap", 1.0, "scrap", 6, "global"),
    ("811229", "Chromium",
     "Other chromium: wrought, articles", 0.9, "fabricated", 6, "global"),

    # ── Tungsten (6-digit) ────────────────────────────────────────────────
    ("261100", "Tungsten",
     "Tungsten ores and concentrates (scheelite, wolframite)",
     1.0, "ore", 6, "global"),
    ("282590", "Tungsten",
     "Tungsten oxides (WO3, tungstic acid) — primary chemical intermediate",
     # 282590 covers many metal oxides; tungsten oxide is primary traded form
     0.8, "intermediate", 6, "global"),
    ("284180", "Tungsten",
     "Ammonium paratungstate (APT) — key tungsten chemical intermediate",
     1.0, "intermediate", 6, "global"),
    ("284990", "Tungsten",
     "Tungsten carbides (WC — cemented carbide precursor)", 1.0, "intermediate", 6, "global"),
    ("720280", "Tungsten",
     "Ferrotungsten and ferrosilicon tungsten", 1.0, "intermediate", 6, "global"),
    ("810110", "Tungsten",
     "Tungsten powders (W metal)", 1.0, "refined", 6, "global"),
    ("810197", "Tungsten",
     "Tungsten waste and scrap", 1.0, "scrap", 6, "global"),

    # ── Molybdenum (6-digit) ──────────────────────────────────────────────
    ("261310", "Molybdenum",
     "Molybdenum ores and concentrates, roasted (molybdic oxide calcine)",
     1.0, "concentrate", 6, "global"),
    ("261390", "Molybdenum",
     "Molybdenum ores and concentrates, other (unroasted)", 1.0, "ore", 6, "global"),
    ("282570", "Molybdenum",
     "Molybdenum oxides and hydroxides (MoO3)", 1.0, "intermediate", 6, "global"),
    ("284170", "Molybdenum",
     "Molybdates (ammonium molybdate, sodium molybdate)", 0.9, "intermediate", 6, "global"),
    ("320620", "Molybdenum",
     "Molybdenum-based pigments and preparations", 0.8, "fabricated", 6, "global"),
    ("720270", "Molybdenum",
     "Ferromolybdenum", 1.0, "intermediate", 6, "global"),
    ("810210", "Molybdenum",
     "Molybdenum powders", 1.0, "refined", 6, "global"),
    ("810294", "Molybdenum",
     "Molybdenum, unwrought (not alloyed)", 1.0, "refined", 6, "global"),
    ("810295", "Molybdenum",
     "Molybdenum bars, rods, profiles, and wire", 1.0, "fabricated", 6, "global"),
    ("810296", "Molybdenum",
     "Molybdenum wire", 1.0, "fabricated", 6, "global"),
    ("810297", "Molybdenum",
     "Molybdenum waste and scrap", 1.0, "scrap", 6, "global"),
    ("810299", "Molybdenum",
     "Other molybdenum articles (wrought)", 1.0, "fabricated", 6, "global"),

    # ── Titanium (6-digit) ────────────────────────────────────────────────
    ("261400", "Titanium",
     "Titanium mineral concentrates (ilmenite, rutile, leucoxene)",
     1.0, "ore", 6, "global"),
    ("262099", "Titanium",
     "Titanium slag (upgraded ilmenite — primary TiO2 feedstock)",
     # 262099 covers various slags/residues; titanium slag is dominant form
     0.8, "intermediate", 6, "global"),
    ("282300", "Titanium",
     "Titanium oxides (unfinished TiO2 pigment feedstock)", 1.0, "intermediate", 6, "global"),
    ("320611", "Titanium",
     "TiO2 pigments, ≥80% TiO2 by weight", 1.0, "fabricated", 6, "global"),
    ("320619", "Titanium",
     "TiO2 pigments, other", 1.0, "fabricated", 6, "global"),
    ("720291", "Titanium",
     "Ferrotitanium and ferrosilicon titanium", 1.0, "intermediate", 6, "global"),
    ("810820", "Titanium",
     "Unwrought titanium metal (sponge, ingot, billet)", 1.0, "refined", 6, "global"),
    ("810830", "Titanium",
     "Titanium waste and scrap", 1.0, "scrap", 6, "global"),
    ("810890", "Titanium",
     "Other titanium articles (wrought, plates, sheets, tubes)",
     0.9, "fabricated", 6, "global"),

    # ── Vanadium (6-digit) ────────────────────────────────────────────────
    ("261590", "Vanadium",
     "Vanadium ores and concentrates (patronite, roscoelite); shares with Nb, Ta, Zr",
     0.6, "ore", 6, "global"),
    ("262040", "Vanadium",
     "Vanadium-bearing slag, ash, and residues (steel slag — secondary V source)",
     0.9, "intermediate", 6, "global"),
    ("262099", "Vanadium",
     "Vanadium-bearing slag, other residues",
     # 262099 shared with titanium slag entries
     0.7, "intermediate", 6, "global"),
    ("282530", "Vanadium",
     "Vanadium pentoxide (V2O5) and vanadium oxides/hydroxides",
     1.0, "intermediate", 6, "global"),
    ("720292", "Vanadium",
     "Ferrovanadium", 1.0, "intermediate", 6, "global"),
    ("811292", "Vanadium",
     "Vanadium metal, unwrought; shares 6-digit prefix with Ga, Ge, In, Nb",
     0.6, "refined", 6, "global"),
    ("811299", "Vanadium",
     "Other vanadium articles; shares 6-digit with Ge, Nb",
     0.6, "fabricated", 6, "global"),

    # ── Niobium (6-digit) ─────────────────────────────────────────────────
    ("261590", "Niobium",
     "Niobium (columbite-tantalite) ores and concentrates; shares with V, Ta, Zr",
     0.6, "ore", 6, "global"),
    ("282590", "Niobium",
     "Niobium oxide (Nb2O5); shares prefix with other metal oxides",
     0.8, "intermediate", 6, "global"),
    ("720293", "Niobium",
     "Ferroniobium (standard-grade FeNb65)", 1.0, "intermediate", 6, "global"),
    ("811292", "Niobium",
     "Niobium metal, unwrought; shares 6-digit with Ga, Ge, In, V",
     0.6, "refined", 6, "global"),
    ("811299", "Niobium",
     "Other niobium articles; shares 6-digit with Ge, V",
     0.6, "fabricated", 6, "global"),

    # ── Tantalum (6-digit) ────────────────────────────────────────────────
    ("261590", "Tantalum",
     "Tantalum ores and concentrates (tantalite, synthetic Ta-Nb concentrates);"
     " shares with V, Nb, Zr",
     0.6, "ore", 6, "global"),
    ("282590", "Tantalum",
     "Tantalum oxide (Ta2O5); shares prefix with other metal oxides",
     0.8, "intermediate", 6, "global"),
    ("282690", "Tantalum",
     "Potassium fluorotantalate (K2TaF7) — primary refining intermediate",
     0.9, "intermediate", 6, "global"),
    ("810320", "Tantalum",
     "Tantalum, unwrought (metal and powders)", 1.0, "refined", 6, "global"),
    ("810330", "Tantalum",
     "Tantalum alloys, unwrought", 0.9, "refined", 6, "global"),
    ("810391", "Tantalum",
     "Tantalum waste and scrap", 1.0, "scrap", 6, "global"),
    ("810399", "Tantalum",
     "Tantalum, wrought; capacitor-grade articles", 0.9, "fabricated", 6, "global"),

    # ── Zirconium (6-digit) ───────────────────────────────────────────────
    ("261510", "Zirconium",
     "Zirconium ores and concentrates (zircon sands)", 1.0, "ore", 6, "global"),
    ("282560", "Zirconium",
     "Zirconium dioxide (ZrO2, baddeleyite); shares with Ge, Hf oxides",
     0.8, "intermediate", 6, "global"),
    ("283699", "Zirconium",
     "Zirconium carbonate; shares 6-digit with cobalt and other carbonates",
     0.7, "intermediate", 6, "global"),
    ("720299", "Zirconium",
     "Ferrozirconium; shares with other minor ferroalloys", 0.8, "intermediate", 6, "global"),
    ("810921", "Zirconium",
     "Zirconium, unwrought, not alloyed (nuclear-reactor grade)", 1.0, "refined", 6, "global"),
    ("810929", "Zirconium",
     "Zirconium, unwrought, other (commercial grade, powder)", 1.0, "refined", 6, "global"),
    ("810931", "Zirconium",
     "Zirconium waste and scrap, not alloyed", 1.0, "scrap", 6, "global"),
    ("810939", "Zirconium",
     "Zirconium waste and scrap, other", 1.0, "scrap", 6, "global"),
    ("810991", "Zirconium",
     "Other zirconium articles, not alloyed (tubes, rods for nuclear use)",
     1.0, "fabricated", 6, "global"),
    ("810999", "Zirconium",
     "Other zirconium articles, other", 1.0, "fabricated", 6, "global"),

    # ── Silicon — Anode Grade (6-digit) ───────────────────────────────────
    # MCS 2026 has no tariff section for silicon (US net exporter).
    # Codes sourced from WCO HS 2022 nomenclature.
    ("280461", "Silicon (Anode Grade)",
     "Silicon, containing ≥99.99% silicon by weight (solar/semiconductor/anode grade)",
     1.0, "battery_grade", 6, "global"),
    ("280469", "Silicon (Anode Grade)",
     "Silicon, other (metallurgical grade, <99.99%)", 0.9, "refined", 6, "global"),

    # ── Gallium (6-digit) ─────────────────────────────────────────────────
    ("285390", "Gallium",
     "Gallium arsenide wafers, undoped", 1.0, "fabricated", 6, "global"),
    ("381800", "Gallium",
     "Gallium arsenide wafers, doped", 1.0, "fabricated", 6, "global"),
    ("811292", "Gallium",
     "Gallium metal, unwrought (including powders); shares 6-digit with Ge, In, Nb, V",
     0.7, "refined", 6, "global"),

    # ── Germanium (6-digit) ───────────────────────────────────────────────
    ("282560", "Germanium",
     "Germanium dioxide (GeO2); shares 6-digit with Zr, Hf oxides",
     0.8, "intermediate", 6, "global"),
    ("282739", "Germanium",
     "Germanium tetrachloride (GeCl4) — primary refining intermediate; "
     "shares with other unspecified chlorides",
     0.7, "intermediate", 6, "global"),
    ("811292", "Germanium",
     "Germanium metal, unwrought; shares 6-digit with Ga, In, Nb, V",
     0.7, "refined", 6, "global"),
    ("811299", "Germanium",
     "Germanium metal, powder; shares 6-digit with Nb, V",
     0.7, "refined", 6, "global"),

    # ── Indium (6-digit) ──────────────────────────────────────────────────
    ("811292", "Indium",
     "Unwrought indium including powders; shares 6-digit with Ga, Ge, Nb, V",
     0.7, "refined", 6, "global"),

    # ── Antimony (6-digit) ────────────────────────────────────────────────
    ("261710", "Antimony",
     "Antimony ores and concentrates", 1.0, "ore", 6, "global"),
    ("282580", "Antimony",
     "Antimony oxide (Sb2O3) — primary chemical form", 1.0, "intermediate", 6, "global"),
    ("811010", "Antimony",
     "Unwrought antimony and antimony powders", 1.0, "refined", 6, "global"),
    ("811020", "Antimony",
     "Antimony waste and scrap", 1.0, "scrap", 6, "global"),
    ("811090", "Antimony",
     "Other antimony articles (wrought)", 0.9, "fabricated", 6, "global"),

    # ── Lithium (6-digit) ─────────────────────────────────────────────────
    ("282520", "Lithium",
     "Lithium oxide and lithium hydroxide (LiOH·H2O — high-Ni cathode grade)",
     1.0, "battery_grade", 6, "global"),
    ("283691", "Lithium",
     "Lithium carbonate (Li2CO3 — primary cathode precursor and electrolyte salt)",
     1.0, "battery_grade", 6, "global"),

    # ── Rare Earth Elements (6-digit) ─────────────────────────────────────
    ("280530", "Rare Earth Elements",
     "Rare-earth metals, unwrought (mixed or individual, not alloyed/alloyed)",
     0.8, "refined", 6, "global"),
    ("284610", "Rare Earth Elements",
     "Cerium compounds (CeO2, CeCl3)", 0.9, "intermediate", 6, "global"),
    ("284690", "Rare Earth Elements",
     "Other rare-earth compounds (oxides, chlorides, carbonates of La, Nd, Pr, Dy, Tb…)",
     0.8, "intermediate", 6, "global"),

    # Individual motor REEs — narrowed from Rare Earths codes above
    ("280530", "Neodymium",
     "Rare-earth metals — neodymium metal unwrought; shares with all REEs",
     0.6, "refined", 6, "global"),
    ("284690", "Neodymium",
     "Other rare-earth compounds — neodymium compounds (Nd2O3, NdCl3)",
     0.6, "intermediate", 6, "global"),
    ("280530", "Praseodymium",
     "Rare-earth metals — praseodymium metal unwrought; shares with all REEs",
     0.6, "refined", 6, "global"),
    ("284690", "Praseodymium",
     "Other rare-earth compounds — praseodymium compounds",
     0.6, "intermediate", 6, "global"),
    ("280530", "Dysprosium",
     "Rare-earth metals — dysprosium metal unwrought; shares with all REEs",
     0.6, "refined", 6, "global"),
    ("284690", "Dysprosium",
     "Other rare-earth compounds — dysprosium compounds (Dy2O3)",
     0.6, "intermediate", 6, "global"),
    ("280530", "Terbium",
     "Rare-earth metals — terbium metal unwrought; shares with all REEs",
     0.6, "refined", 6, "global"),
    ("284690", "Terbium",
     "Other rare-earth compounds — terbium compounds (Tb4O7)",
     0.6, "intermediate", 6, "global"),

    # ── Boron (6-digit) ───────────────────────────────────────────────────
    ("252800", "Boron",
     "Natural borates (ulexite, colemanite, kernite) and natural boric acid",
     1.0, "ore", 6, "global"),
    ("281000", "Boron",
     "Oxides of boron; boric acids (B2O3, H3BO3) — processed boron intermediate",
     0.9, "intermediate", 6, "global"),
    ("284011", "Boron",
     "Disodium tetraborate (refined borax, anhydrous)", 0.9, "refined", 6, "global"),
    ("284019", "Boron",
     "Other borates (sodium perborate, other refined borates)", 0.8, "refined", 6, "global"),

    # ── Zinc (6-digit) ────────────────────────────────────────────────────
    ("260800", "Zinc",
     "Zinc ores and concentrates", 1.0, "ore", 6, "global"),
    ("281700", "Zinc",
     "Zinc oxide; zinc peroxide (ZnO — intermediate chemical form)",
     1.0, "intermediate", 6, "global"),
    ("283329", "Zinc",
     "Zinc sulfate (ZnSO4); shares 6-digit with cobalt and other sulfates",
     0.7, "intermediate", 6, "global"),
]


# ---------------------------------------------------------------------------
# Stage-specific keywords for `hs_code_material_mappings.keywords` (mig 028)
# ---------------------------------------------------------------------------
#
# Keys are ``(hs_code_prefix, canonical_material_name)`` — the same identity
# as a row in ``_MAPPINGS`` (with implicit ``market_scope='global'``).  Every
# key MUST correspond to an entry in ``_MAPPINGS`` for the named material;
# otherwise the upsert silently does nothing for that key (logged at debug).
# Verified against the present ``_MAPPINGS`` contents at the time of the
# 2026-05 audit.  When you add a new ``_MAPPINGS`` row, add a keyword entry
# here in the same commit.
#
# Keyword selection rule
# ----------------------
# Each keyword must **unambiguously identify the specific HS node**, not the
# parent material.  "Lithium" is NOT a keyword for ``282520`` (lithium
# hydroxide) because it matches the broad material; the canonical-name path
# in ``MaterialCache`` already handles that case at relevance 0.85.
# "Lithium hydroxide" IS a keyword for ``282520`` because it can only mean
# that specific compound at that specific stage.
#
# Why this lives here, not in ``_MAPPINGS``
# -----------------------------------------
# Most ``_MAPPINGS`` rows do not need keywords.  Adding an 8th tuple element
# would require ``None`` on every row that has none — ~700 changes for ~30
# meaningful additions.  Co-locating in the same module keeps the data near
# the schema it modifies; the parallel-dict shape just minimises the diff
# blast radius.
#
# Detection downstream
# --------------------
# ``MaterialCache.build()`` in ``app/services/ingestion/normalizers/material_resolver.py``
# loads these into a keyword → ``(material_id, hs_mapping_id, relevance=0.90)``
# lookup.  Federal Register, EUR-Lex, news, IEA — anything that calls
# ``MaterialCache.detect()`` on free text — gets stage attribution from these
# entries.  Without them, those ingesters fall back to canonical-name match
# at relevance 0.85 with no ``hs_mapping_id``, so no
# ``risk_event_hs_mappings`` row is ever written from text-based sources.
# That's the gap closed by populating this dict.
# ---------------------------------------------------------------------------

_HS_KEYWORDS_BY_MAPPING: dict[tuple[str, str], list[str]] = {
    # ── Lithium ──────────────────────────────────────────────────────────
    # 4-digit ore prefix is not yet in `_MAPPINGS` for Lithium (HS 2530
    # spans many materials and isn't seeded under Lithium today).  Lithium
    # ore detection (spodumene, brines) therefore relies on the canonical-name
    # path until that gap is closed.  6-digit chemistry forms below.
    ("282520", "Lithium"): [
        "lithium hydroxide",
        "LiOH",
        "lithium hydrate",
        "lithium hydroxide monohydrate",
        "LiOH·H2O",
    ],
    ("283691", "Lithium"): [
        "lithium carbonate",
        "Li2CO3",
        "battery-grade carbonate",
        "battery grade lithium carbonate",
    ],

    # ── Cobalt ───────────────────────────────────────────────────────────
    ("2605", "Cobalt"): [
        "cobalt ore",
        "cobalt concentrate",
    ],
    ("260500", "Cobalt"): [
        "cobalt ores",
        "cobalt concentrate",
    ],
    ("8105", "Cobalt"): [
        "cobalt metal",
        "unwrought cobalt",
    ],
    ("810520", "Cobalt"): [
        "cobalt mattes",
        "cobalt matte",
        "unwrought cobalt",
    ],
    ("282200", "Cobalt"): [
        "cobalt hydroxide",
        "cobaltous oxide",
        "cobalt(II) hydroxide",
        "Co(OH)2",
    ],
    ("283329", "Cobalt"): [
        "cobalt sulfate",
        "cobaltous sulfate",
        "CoSO4",
    ],

    # ── Nickel ───────────────────────────────────────────────────────────
    ("2604", "Nickel"): [
        "nickel ore",
        "nickel concentrate",
    ],
    ("260400", "Nickel"): [
        "nickel ores",
        "nickel concentrate",
        "laterite ore",
        "saprolite ore",
        "limonite ore",
    ],
    ("7501", "Nickel"): [
        "nickel matte",
        "nickel oxide sinter",
    ],
    ("7502", "Nickel"): [
        "unwrought nickel",
    ],
    ("750210", "Nickel"): [
        "unwrought nickel, not alloyed",
        "class 1 nickel",
        "Ni briquette",
    ],

    # ── Manganese ────────────────────────────────────────────────────────
    ("2602", "Manganese"): [
        "manganese ore",
    ],
    ("260200", "Manganese"): [
        "manganese ores",
        "manganese concentrate",
    ],
    ("8111", "Manganese"): [
        "unwrought manganese",
    ],
    ("811100", "Manganese"): [
        "unwrought manganese",
        "manganese metal",
    ],

    # ── Natural Graphite ─────────────────────────────────────────────────
    ("2504", "Natural Graphite"): [
        "natural graphite",
        "flake graphite",
    ],
    ("250410", "Natural Graphite"): [
        "natural graphite, powder",
        "amorphous graphite",
        "spherical graphite",
        "spheroidized graphite",
    ],
    ("250490", "Natural Graphite"): [
        "natural graphite, other forms",
    ],

    # ── Copper ───────────────────────────────────────────────────────────
    ("2603", "Copper"): [
        "copper ore",
        "copper concentrate",
    ],
    ("260300", "Copper"): [
        "copper ores",
        "copper concentrate",
    ],
    ("7402", "Copper"): [
        "blister copper",
        "copper anode",
        "anode copper",
    ],
    ("7403", "Copper"): [
        "refined copper",
        "copper cathode",
        "cathode copper",
    ],

    # ── Aluminum ─────────────────────────────────────────────────────────
    ("2606", "Aluminum"): [
        "bauxite",
    ],
    ("7601", "Aluminum"): [
        "unwrought aluminum",
        "primary aluminum",
        "aluminum ingot",
    ],
    ("760110", "Aluminum"): [
        "unwrought aluminum, not alloyed",
        "primary aluminum",
        "P1020",
    ],
    ("760120", "Aluminum"): [
        "unwrought aluminum, alloyed",
    ],

    # ── Iron Ore (LFP Grade) ─────────────────────────────────────────────
    ("2601", "Iron Ore (LFP Grade)"): [
        "iron ore",
        "iron pyrites",
    ],
    ("260111", "Iron Ore (LFP Grade)"): [
        "iron ore, non-agglomerated",
    ],
    ("260112", "Iron Ore (LFP Grade)"): [
        "iron ore, agglomerated",
    ],

    # ── Rare Earth Elements ──────────────────────────────────────────────
    # 2805 4-digit is shared with Lithium in `_MAPPINGS`; we attach REE
    # keywords only to the 6-digit `280530` row (REE-only) so there's no
    # cross-contamination.  The 4-digit `2617` is REE-only in seed.
    ("2617", "Rare Earth Elements"): [
        "rare earth ore",
        "monazite",
        "bastnasite",
    ],
    ("280530", "Rare Earth Elements"): [
        "rare earth metal",
        "rare earth metals, scandium and yttrium",
        "neodymium metal",
        "dysprosium metal",
        "terbium metal",
        "praseodymium metal",
        "samarium metal",
    ],
    ("284610", "Rare Earth Elements"): [
        "cerium oxide",
        "cerium compound",
        "lanthanum cerium",
    ],
    ("284690", "Rare Earth Elements"): [
        "rare earth oxide",
        "neodymium oxide",
        "Nd2O3",
        "dysprosium oxide",
        "Dy2O3",
        "terbium oxide",
        "rare earth carbonate",
    ],

    # ── Titanium ─────────────────────────────────────────────────────────
    ("2614", "Titanium"): [
        "titanium ore",
        "ilmenite",
        "rutile",
    ],
    ("261400", "Titanium"): [
        "titanium ores",
        "ilmenite",
        "rutile",
    ],
    ("8108", "Titanium"): [
        "titanium metal",
        "titanium sponge",
    ],
    ("810820", "Titanium"): [
        "unwrought titanium",
        "titanium sponge",
    ],

    # ── Tungsten ─────────────────────────────────────────────────────────
    ("2611", "Tungsten"): [
        "tungsten ore",
        "scheelite",
        "wolframite",
    ],
    ("261100", "Tungsten"): [
        "tungsten ores",
        "scheelite",
        "wolframite",
    ],
    ("8101", "Tungsten"): [
        "tungsten metal",
        "unwrought tungsten",
    ],
    ("810197", "Tungsten"): [
        "unwrought tungsten",
    ],
    ("284180", "Tungsten"): [
        "ammonium paratungstate",
        "APT",
    ],

    # ── Silicon (Anode Grade) ────────────────────────────────────────────
    # NOTE: 280461 covers metallurgical silicon broadly. Battery-grade silicon
    # (anode material) is a small slice of this prefix's actual trade. Detection
    # here will pick up general silicon mentions; whether a specific event is
    # really about battery-grade vs metallurgical needs human review at the
    # event level. Tagged needs_review in any partner audit.
    ("280461", "Silicon (Anode Grade)"): [
        "silicon metal",
        "metallurgical silicon",
        "polysilicon",
        "battery-grade silicon",
    ],
    ("280469", "Silicon (Anode Grade)"): [
        "silicon, other",
    ],
}


def _upsert_keywords_for_mappings(
    session: Session,
    *,
    mat_map: dict[str, int],
    force: bool,
) -> dict[str, int]:
    """Apply ``_HS_KEYWORDS_BY_MAPPING`` to ``hs_code_material_mappings.keywords``.

    Called from ``upsert_hs_mappings`` after mapping rows are inserted/updated.
    Resolves each ``(prefix, canonical_name)`` key to a specific row scoped to
    ``market_scope='global'`` and writes the keyword list when:

      * ``force=True``                          → always overwrite
      * the existing ``keywords`` column is NULL or empty → set fresh
      * existing keywords already populated AND ``force=False`` → skip

    Args:
        session:  Active SQLAlchemy session.  Caller owns the commit.
        mat_map:  Pre-built ``canonical_name → material_id`` map (passed in
                  to avoid a duplicate query).
        force:    See above.

    Returns:
        ``{"keywords_set", "keywords_overwritten",
           "keywords_skipped_existing", "keywords_skipped_no_row"}``
    """
    set_count = 0
    overwritten = 0
    skipped_existing = 0
    skipped_no_row = 0

    for (hs_prefix, canonical_name), keywords in _HS_KEYWORDS_BY_MAPPING.items():
        if not keywords:
            continue

        mat_id = mat_map.get(canonical_name)
        if mat_id is None:
            log.debug(
                "seed_hs_mappings.keywords.material_not_seeded",
                canonical_name=canonical_name,
                hs_prefix=hs_prefix,
            )
            skipped_no_row += 1
            continue

        # Only touch global-scope rows.  US-scope rows from the MCS PDF
        # parser don't need stage-attribution keywords for Federal Register
        # / EUR-Lex / news detection — those events are global by nature.
        rows = list(session.scalars(
            select(HsCodeMaterialMapping).where(
                HsCodeMaterialMapping.hs_code_prefix == hs_prefix,
                HsCodeMaterialMapping.material_id == mat_id,
                HsCodeMaterialMapping.market_scope == "global",
            )
        ).all())

        if not rows:
            log.debug(
                "seed_hs_mappings.keywords.no_matching_row",
                hs_prefix=hs_prefix,
                canonical_name=canonical_name,
                note=(
                    "(prefix, material) pair has no global mapping row; "
                    "verify the prefix exists in `_MAPPINGS` for this material."
                ),
            )
            skipped_no_row += 1
            continue

        for row in rows:
            existing = row.keywords or []
            if existing and not force:
                skipped_existing += 1
                continue
            row.keywords = list(keywords)
            if existing:
                overwritten += 1
            else:
                set_count += 1

    return {
        "keywords_set": set_count,
        "keywords_overwritten": overwritten,
        "keywords_skipped_existing": skipped_existing,
        "keywords_skipped_no_row": skipped_no_row,
    }


def upsert_hs_mappings(session: Session, force: bool = False) -> dict[str, int]:
    """Upsert all HS code material mappings. Idempotent.

    Resolves material_id by canonical_name for each entry. Rows whose
    canonical_name is not found in the materials table are skipped with a
    warning — this is expected for materials not yet seeded.

    Normal run (force=False):
        Inserts only new (hs_code_prefix, material_id, market_scope) combos.
        Existing rows are skipped — their values in the DB are authoritative.

    Force run (force=True):
        Updates existing rows with the current tuple values: description,
        confidence, supply_chain_stage, stage_sequence, digit_count.
        Use this after migration 022 to backfill stage metadata on existing rows,
        or after correcting stage assignments in _MAPPINGS.
        Does NOT change hs_mapping_id or material_id on existing rows.
        Also passed through to ``_upsert_keywords_for_mappings`` so existing
        non-empty ``keywords`` arrays are overwritten with the current
        ``_HS_KEYWORDS_BY_MAPPING`` definitions.

    Returns:
        {"inserted": int, "updated": int, "skipped_existing": int,
         "skipped_unknown_material": int,
         "keywords_set": int, "keywords_overwritten": int,
         "keywords_skipped_existing": int, "keywords_skipped_no_row": int}
    """
    inserted = 0
    updated = 0
    skipped_existing = 0
    skipped_unknown = 0

    # Build material name → id cache in one query.
    mat_map: dict[str, int] = {
        m.canonical_name: m.id
        for m in session.scalars(select(Material)).all()
    }

    for hs_prefix, canonical_name, description, confidence, stage, digit_count, market_scope in _MAPPINGS:
        mat_id = mat_map.get(canonical_name)
        if mat_id is None:
            log.warning(
                "seed_hs_mappings.unknown_material",
                canonical_name=canonical_name,
                hs_prefix=hs_prefix,
            )
            skipped_unknown += 1
            continue

        stage_sequence = _STAGE_SEQUENCE.get(stage)

        existing = session.scalar(
            select(HsCodeMaterialMapping).where(
                HsCodeMaterialMapping.hs_code_prefix == hs_prefix,
                HsCodeMaterialMapping.material_id == mat_id,
                HsCodeMaterialMapping.market_scope == market_scope,
            )
        )

        if existing is not None:
            if force:
                existing.description = description
                existing.confidence = confidence
                existing.supply_chain_stage = stage
                existing.stage_sequence = stage_sequence
                existing.digit_count = digit_count
                updated += 1
            else:
                skipped_existing += 1
            continue

        session.add(HsCodeMaterialMapping(
            hs_code_prefix=hs_prefix,
            material_id=mat_id,
            description=description,
            confidence=confidence,
            supply_chain_stage=stage,
            stage_sequence=stage_sequence,
            digit_count=digit_count,
            market_scope=market_scope,
        ))
        inserted += 1

    if inserted or updated:
        session.flush()

    # Apply stage-specific keywords for the rows we just upserted.  Runs in
    # the same transaction as the mapping inserts so a partial seed never
    # leaves keywords on rows that didn't get committed.
    keyword_stats = _upsert_keywords_for_mappings(
        session, mat_map=mat_map, force=force,
    )

    session.commit()

    log.info(
        "seed_hs_mappings.done",
        inserted=inserted,
        updated=updated,
        skipped_existing=skipped_existing,
        skipped_unknown_material=skipped_unknown,
        keywords_set=keyword_stats["keywords_set"],
        keywords_overwritten=keyword_stats["keywords_overwritten"],
        keywords_skipped_existing=keyword_stats["keywords_skipped_existing"],
        keywords_skipped_no_row=keyword_stats["keywords_skipped_no_row"],
        force=force,
    )
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped_existing": skipped_existing,
        "skipped_unknown_material": skipped_unknown,
        **keyword_stats,
    }
