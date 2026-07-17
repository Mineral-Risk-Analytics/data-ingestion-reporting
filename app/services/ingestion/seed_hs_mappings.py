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
    ("2510", "Phosphate",
     "Natural calcium phosphates and phosphatic chalk", 1.0, "ore", 4, "global"),
    ("2809", "Phosphate",
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

    # ── Iron Ore ──────────────────────────────────────────────
    ("2601", "Iron Ore",
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
     0.33, "refined", 4, "global"),

    # ── Tellurium ─────────────────────────────────────────────────────────
    ("2804", "Tellurium",
     "Non-metals — tellurium at 2804.50; shares prefix with Si, Se, S",
     0.33, "refined", 4, "global"),

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
     0.5, "ore", 4, "global"),
    ("8110", "Antimony",
     "Antimony and articles thereof — unwrought metal", 1.0, "refined", 4, "global"),

    # ── Lithium ───────────────────────────────────────────────────────────
    # Ore-stage entry added 2026-05-09.  HS 2530 ("Mineral substances NES")
    # is where spodumene concentrate trades internationally — primarily
    # Australian hard-rock production (~32% of world Li supply).  Confidence
    # 0.7 reflects honest stage heterogeneity in MCS "World Mine Production":
    # Australian spodumene IS ore-stage at HS 2530.90, but Chilean brine
    # output (~19%) is actually post-evaporation and trades as carbonate
    # (HS 2836.91, separately seeded as battery_grade). Routing all of MCS
    # mine production here is directionally correct for Australia / China
    # hard-rock; over-attributes brine countries to ore stage.  Partner
    # research can refine per-country attribution later.
    ("2530", "Lithium",
     "Mineral substances NES — spodumene concentrate at 2530.90 "
     "(multi-material 4-digit; also covers magnesium, fluorspar minerals)",
     0.5, "ore", 4, "global"),
    ("2825", "Lithium",
     "Lithium oxide and hydroxide — primary processed form for battery electrolyte",
     # 2825.20 is lithium hydroxide specifically; dominant battery-grade Li trade
     0.5, "battery_grade", 4, "global"),
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
     0.33, "refined", 4, "global"),
    # Selenium dioxide (SeO2) is an intermediate inorganic oxide.  Added
    # 2026-05-09 to clear NULL stage on rows the MCS PDF parser created
    # from the US HTS tariff table (HS 2811.29.20.00 → derived 6-digit).
    ("2811", "Selenium",
     "Other inorganic oxides / acids — selenium dioxide at 2811.29",
     0.5, "intermediate", 4, "global"),
    ("281129", "Selenium",
     "Selenium dioxide (SeO2 — intermediate inorganic oxide)",
     0.3, "intermediate", 6, "global"),

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

    # ── Sodium (Na-ion battery scope) ───────────────────────────────────
    # Reverted from "Sodium Carbonate (Battery Grade)" (May 2026).  Partner's
    # HS code review showed Na-ion battery supply chain spans multiple
    # sodium chemistries — see additional 4-digit entries in the May 2026
    # coverage block below.  HS 2827 (sodium chloride / salt) intentionally
    # NOT included — partner says NaCl isn't a battery feedstock.
    ("2836", "Sodium",
     "Carbonates — sodium carbonate (soda ash) at 283620; primary Na-ion cathode precursor",
     0.8, "battery_grade", 4, "global"),

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
    # 11.2 audit easy-add #3 (2026-06): synthetic graphite is a direct
    # substitute for natural-flake anode material — ~50% of global anode-
    # active-material production. Mapping to 'Natural Graphite' is pragmatic
    # for Phase 1; Phase 1.5 may split into a separate canonical 'Synthetic
    # Graphite' material since upstream supply dynamics (petroleum coke
    # feedstock + graphitisation furnaces) differ from natural flake.
    # Confidence 0.7 reflects this methodological compromise.
    ("380110", "Natural Graphite",
     "Artificial / synthetic graphite (anode-grade for Li-ion batteries).",
     0.7, "battery_grade", 6, "global"),
    # 11.2 audit easy-add #4 (2026-06): smaller volume than 380110 but
    # directly battery-cell-bound. Same Phase 1.5 split question.
    ("380130", "Natural Graphite",
     "Carbonaceous pastes for electrodes (anode-side cell input).",
     0.6, "battery_grade", 6, "global"),

    # ── Phosphate (6-digit) ──────────────────────────────
    ("251010", "Phosphate",
     "Natural calcium phosphates, unground", 1.0, "ore", 6, "global"),
    ("251020", "Phosphate",
     "Natural calcium phosphates, ground", 1.0, "ore", 6, "global"),
    ("280920", "Phosphate",
     "Phosphoric acid and polyphosphoric acids (battery-grade H3PO4 precursor)",
     0.9, "battery_grade", 6, "global"),

    # ── Fluorspar (6-digit) ───────────────────────────────────────────────
    ("252921", "Fluorspar",
     "Fluorspar, metallurgical grade (≤97% CaF2)", 1.0, "ore", 6, "global"),
    ("252922", "Fluorspar",
     "Fluorspar, acid grade (>97% CaF2)", 1.0, "concentrate", 6, "global"),
    ("253090", "Fluorspar",
     "Natural cryolite and chiolite (fluorine-bearing minerals)",
     0.5, "ore", 6, "global"),
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

    # ── Iron Ore (6-digit) ───────────────────────────────────
    ("260111", "Iron Ore",
     "Iron ores and concentrates, non-agglomerated", 1.0, "ore", 6, "global"),
    ("260112", "Iron Ore",
     "Iron ores and concentrates, agglomerated (pellets, sinter feed)",
     1.0, "ore", 6, "global"),
    ("260120", "Iron Ore",
     "Roasted iron pyrites (iron sinter)", 0.9, "concentrate", 6, "global"),

    # ── Manganese (6-digit) ───────────────────────────────────────────────
    ("260200", "Manganese",
     "Manganese ores and concentrates (all grades)", 1.0, "ore", 6, "global"),
    ("282010", "Manganese",
     "Manganese dioxide (EMD/CMD — direct battery cathode input)",
     1.0, "battery_grade", 6, "global"),
    # 11.2 audit easy-add #2 (2026-06): HS 283329 is the residual "other
    # sulphates" bucket and also carries the Cobalt mapping (cobalt sulphate
    # also classified here). Confidence 0.5 reflects that ~half the trade
    # at this code is cobalt-not-manganese. Phase 1.5 partner refinement
    # may set per-(country, year) shares if needed.
    ("283329", "Manganese",
     "Manganese sulphate (MnSO4·H2O, HPMSM — NCM cathode precursor).",
     0.2, "battery_grade", 6, "global"),
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
    # 2026-06-11 ERROR FIX: removed "740300" → Copper refined row.  HS heading
    # 7403 requires a 6-digit sub-classification.  Specific subheadings
    # already in seed below: 740311 (cathodes) + 740319 (other unwrought refined
    # Cu) + alloy variants 7403.21-29.  No info lost by removal.
    ("740400", "Copper",
     "Copper waste and scrap", 1.0, "scrap", 6, "global"),
    ("740811", "Copper",
     "Wire rod of refined copper (continuous cast rod)", 1.0, "fabricated", 6, "global"),
    # 11.2 audit easy-add #5 (2026-06): battery copper foil is a ~$5B+/yr
    # market dominated by Korea (SK Nexilis, Iljin) and Japan (Furukawa,
    # Nippon Mining). Confidence 0.7 reflects that HS 741011 also covers
    # non-battery rolled foils (industrial, decorative); battery-grade is
    # a large but not exclusive slice. China is also rapidly expanding
    # battery-foil capacity.
    ("741011", "Copper",
     "Rolled copper foil, not backed, <0.15mm (battery current collector).",
     0.7, "battery_grade", 6, "global"),

    # ── Nickel (6-digit) ──────────────────────────────────────────────────
    ("260400", "Nickel",
     "Nickel ores and concentrates (nickel content)", 1.0, "ore", 6, "global"),
    # 11.2 audit easy-add #1 (2026-06): HS 283324 is specifically nickel
    # sulphates within the 2833 sulphate family — clean single-material
    # attribution. China dominates battery-grade NiSO4 refining for the
    # NCM cathode supply chain.
    ("283324", "Nickel",
     "Nickel sulfates (NiSO4·6H2O — battery-grade NCM precursor input).",
     1.0, "battery_grade", 6, "global"),
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
     0.25, "battery_grade", 6, "global"),
    ("283699", "Cobalt",
     "Cobalt carbonate (CoCO3 — chemical precursor)",
     # 283699 covers many carbonates; cobalt carbonate is a precursor form
     0.5, "intermediate", 6, "global"),
    ("291529", "Cobalt",
     "Cobalt acetates (Co(CH3COO)2 — chemical precursor)", 0.3, "intermediate", 6, "global"),
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
    # 11.2 audit easy-add #6 (2026-06): battery aluminum foil is dominated
    # by China (~70%+ of global capacity) with Korea + Japan as secondary
    # suppliers. Confidence 0.7 reflects that HS 760711 also covers non-
    # battery rolled foils (packaging, industrial); battery-grade is a
    # meaningful but not exclusive slice.
    ("760711", "Aluminum",
     "Rolled aluminum foil, not backed, <0.2mm (battery current collector).",
     0.7, "battery_grade", 6, "global"),

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
     0.33, "intermediate", 6, "global"),
    ("284180", "Tungsten",
     "Ammonium paratungstate (APT) — key tungsten chemical intermediate",
     1.0, "intermediate", 6, "global"),
    ("284990", "Tungsten",
     "Tungsten carbides (WC — cemented carbide precursor)", 0.5, "intermediate", 6, "global"),
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
     0.5, "intermediate", 6, "global"),
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
     0.5, "intermediate", 6, "global"),
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
     0.33, "fabricated", 6, "global"),

    # ── Niobium (6-digit) ─────────────────────────────────────────────────
    ("261590", "Niobium",
     "Niobium (columbite-tantalite) ores and concentrates; shares with V, Ta, Zr",
     0.6, "ore", 6, "global"),
    ("282590", "Niobium",
     "Niobium oxide (Nb2O5); shares prefix with other metal oxides",
     0.33, "intermediate", 6, "global"),
    ("720293", "Niobium",
     "Ferroniobium (standard-grade FeNb65)", 1.0, "intermediate", 6, "global"),
    ("811292", "Niobium",
     "Niobium metal, unwrought; shares 6-digit with Ga, Ge, In, V",
     0.6, "refined", 6, "global"),
    ("811299", "Niobium",
     "Other niobium articles; shares 6-digit with Ge, V",
     0.33, "fabricated", 6, "global"),

    # ── Tantalum (6-digit) ────────────────────────────────────────────────
    ("261590", "Tantalum",
     "Tantalum ores and concentrates (tantalite, synthetic Ta-Nb concentrates);"
     " shares with V, Nb, Zr",
     0.6, "ore", 6, "global"),
    ("282590", "Tantalum",
     "Tantalum oxide (Ta2O5); shares prefix with other metal oxides",
     0.33, "intermediate", 6, "global"),
    ("282690", "Tantalum",
     "Potassium fluorotantalate (K2TaF7) — primary refining intermediate",
     0.3, "intermediate", 6, "global"),
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
     0.5, "intermediate", 6, "global"),
    ("720299", "Zirconium",
     "Ferrozirconium; shares with other minor ferroalloys", 0.5, "intermediate", 6, "global"),
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
    # Stage downgraded from battery_grade → refined (May 2026, partner
    # decision).  280461 covers high-purity silicon metal generally;
    # bulk trade flows on this prefix go to semiconductor / solar, not
    # battery anode specifically.  Sub-segment battery-grade silicon
    # would need its own 8/10-digit code if/when one exists.
    ("280461", "Silicon (Anode Grade)",
     "Silicon, containing ≥99.99% silicon by weight (solar/semiconductor/anode grade)",
     1.0, "refined", 6, "global"),
    ("280469", "Silicon (Anode Grade)",
     "Silicon, other (metallurgical grade, <99.99%)", 0.9, "refined", 6, "global"),

    # ── Gallium (6-digit) ─────────────────────────────────────────────────
    ("285390", "Gallium",
     "Gallium arsenide wafers, undoped", 0.3, "fabricated", 6, "global"),
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
     0.5, "intermediate", 6, "global"),
    ("811292", "Germanium",
     "Germanium metal, unwrought; shares 6-digit with Ga, In, Nb, V",
     0.7, "refined", 6, "global"),
    ("811299", "Germanium",
     "Germanium metal, powder; shares 6-digit with Nb, V",
     0.33, "refined", 6, "global"),

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
    ("253090", "Lithium",
     "Mineral substances NES — spodumene concentrate (Australian hard-rock Li)",
     # See parent ("2530", "Lithium") for confidence rationale.
     0.5, "ore", 6, "global"),
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
    # Added 2026-05-09 to clear NULL stage on a row inserted by the MCS PDF
    # parser: ferrocerium is a refined mischmetal alloy (~70% Ce + La/Nd/Pr)
    # used industrially as flint material — refined-stage REE product.
    ("360690", "Rare Earth Elements",
     "Ferrocerium and other pyrophoric alloys — refined REE alloy form",
     0.7, "refined", 6, "global"),
    # 11.2 audit easy-add #7 (2026-06): NdFeB magnets are the highest-
    # leverage downstream form for the CRMA-critical magnet REEs (Nd, Pr,
    # Dy, Tb).  China dominates ~90% of global sintered-magnet production.
    # Confidence 0.8 reflects that 8505.11 also covers non-NdFeB sintered
    # magnets (SmCo, ferrite) but NdFeB is the dominant slice by both
    # value and battery-supply-chain relevance.  Conceptual note: EV motor
    # materials are not strictly battery components, but partner direction
    # (11.2 action item #7) is that battery-supply-chain scoring should
    # include EV powertrain magnet exposure given the inseparable demand
    # link.
    ("850511", "Rare Earth Elements",
     "Sintered NdFeB permanent magnets (EV motor + wind-turbine generator).",
     0.8, "battery_grade", 6, "global"),

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
    # Refined borates upgraded from 'refined' → 'battery_grade' (May 2026)
    # per partner annotation: "Battery materials (established – electrolytes)"
    ("284011", "Boron",
     "Disodium tetraborate (refined borax, anhydrous) — battery electrolyte chemical",
     0.9, "battery_grade", 6, "global"),
    ("284019", "Boron",
     "Other borates (sodium perborate, other refined borates) — battery electrolyte chemicals",
     0.8, "battery_grade", 6, "global"),

    # ── Zinc (6-digit) ────────────────────────────────────────────────────
    ("260800", "Zinc",
     "Zinc ores and concentrates", 1.0, "ore", 6, "global"),
    ("281700", "Zinc",
     "Zinc oxide; zinc peroxide (ZnO) — Zn-air battery active material",
     1.0, "battery_grade", 6, "global"),
    ("283329", "Zinc",
     "Zinc sulfate (ZnSO4) — Zn-ion electrolyte / aqueous battery; shares 6-digit with cobalt sulfate",
     0.1, "battery_grade", 6, "global"),

    # =========================================================================
    # COVERAGE ADDITIONS — May 2026 (partner CSV review)
    # =========================================================================
    # Added based on partner's "Comprehensive HS_HTS codes" review.  Closes 60
    # 4-digit prefix gaps from her curated CSV:
    #   - 14 P0 (direct battery materials): cobalt salts, MnO2, V2O5, ZnO,
    #     fluorides for electrolyte, MgCl2, TiO2 (LTO), boron borax, etc.
    #   - 7 P1 (structural EV / scrap streams): ferroalloys, recycling
    #   - Sodium expansion: NaOH (2815), Na phosphate (2835), peroxometallates (2841)
    #   - Bismuth + Rhenium minimal coverage
    # P2 (R&D / niche) and P3 (non-battery) deferred per partner annotation.
    # =========================================================================

    # ── P0 — Cobalt cathode precursors (multi-material 4-digit, mid confidence) ─
    # 2026-07-15: stage battery_grade → intermediate.  The 4-digit parent
    # was staged battery_grade while its 6-digit child 282200 is
    # intermediate — same code family, contradictory stages.  Hydroxide is
    # refinery FEEDSTOCK (DRC crude Co(OH)2 / Indonesian MHP), not a
    # battery-grade output; intermediate matches the child and reality.
    ("2822", "Cobalt",
     "Cobalt oxides and hydroxides — Co(OH)2 cathode precursor at 282200",
     1.0, "intermediate", 4, "global"),
    ("2833", "Cobalt",
     "Sulphates — cobalt sulfate (CoSO4) at 283329; cathode precursor (multi-material 4-digit)",
     0.4, "battery_grade", 4, "global"),
    ("2836", "Cobalt",
     "Carbonates — cobalt carbonate at 283699; cathode precursor (multi-material 4-digit)",
     0.4, "battery_grade", 4, "global"),

    # ── P0 — Manganese dioxide (cathode active form) ──────────────────────
    ("2820", "Manganese",
     "Manganese oxides — MnO2 cathode active material",
     1.0, "battery_grade", 4, "global"),

    # ── P0 — Vanadium pentoxide (VRFB active material) ───────────────────
    ("2825", "Vanadium",
     "Hydrazine and metal oxides — vanadium pentoxide (V2O5) at 282530; VRFB active material (multi-material 4-digit)",
     0.4, "battery_grade", 4, "global"),

    # ── P0 — Zinc battery materials ───────────────────────────────────────
    ("2817", "Zinc",
     "Zinc oxide; zinc peroxide — Zn-air battery active material",
     1.0, "battery_grade", 4, "global"),

    # ── P0 — Titanium dioxide (LTO anode) ────────────────────────────────
    ("2823", "Titanium",
     "Titanium oxides — TiO2 used in lithium-titanate (LTO) anodes; mostly pigments by volume",
     0.8, "battery_grade", 4, "global"),

    # ── P0 — Boron refined borates (electrolyte) ─────────────────────────
    ("2840", "Boron",
     "Refined borates and borax — battery electrolyte chemicals",
     0.8, "battery_grade", 4, "global"),

    # ── P0 — Fluorspar derivatives (electrolyte chemicals) ───────────────
    ("2811", "Fluorspar",
     "Hydrofluoric acid (HF) — battery electrolyte precursor at 281111",
     0.5, "intermediate", 4, "global"),
    ("2826", "Fluorspar",
     "Fluorides — AlF3 (282612), synthetic cryolite (282630) for battery electrolytes",
     0.7, "intermediate", 4, "global"),
    ("2530", "Fluorspar",
     "Mineral substances NEC — natural cryolite at 253090 (multi-material 4-digit)",
     0.4, "ore", 4, "global"),

    # ── P0 — Magnesium chloride (battery electrolyte) ────────────────────
    ("2827", "Magnesium",
     "Chlorides — magnesium chloride (MgCl2) electrolyte at 282731 (multi-material 4-digit)",
     0.4, "intermediate", 4, "global"),

    # ── P0 — Nickel scrap (recycling) ────────────────────────────────────
    ("7503", "Nickel",
     "Nickel waste and scrap — recycling stream",
     1.0, "scrap", 4, "global"),

    # ── P1 — Aluminum scrap (structural EV / battery pack recycling) ──────
    ("7602", "Aluminum",
     "Aluminum waste and scrap — recycling stream",
     1.0, "scrap", 4, "global"),

    # ── P1 — Copper scrap and wire (battery wiring / current collectors) ─
    ("7404", "Copper",
     "Copper waste and scrap — recycling stream",
     1.0, "scrap", 4, "global"),
    ("7408", "Copper",
     "Copper wire — fabricated form for battery wiring and current collectors",
     1.0, "fabricated", 4, "global"),

    # ── P1 — Steel-related ferroalloys (structural EV) ───────────────────
    # 7202 is the multi-material ferroalloys chapter; existing seed already
    # has Titanium, Vanadium, Molybdenum entries.  These complete coverage
    # for the structural EV chassis material chain.
    ("7202", "Chromium",
     "Ferroalloys — ferrochromium for stainless steel; structural EV chassis",
     0.7, "intermediate", 4, "global"),
    ("7204", "Chromium",
     "Iron and steel scrap — stainless component recovers chromium",
     0.5, "scrap", 4, "global"),
    ("7202", "Manganese",
     "Ferroalloys — ferromanganese (720211/720219), silicomanganese (720230)",
     0.7, "intermediate", 4, "global"),
    ("7202", "Niobium",
     "Ferroalloys — ferroniobium (720293) for high-strength steel",
     0.7, "intermediate", 4, "global"),
    ("7202", "Nickel",
     "Ferroalloys — ferronickel (720260) for stainless steel",
     0.6, "intermediate", 4, "global"),
    ("7202", "Silicon (Anode Grade)",
     "Ferroalloys — ferrosilicon (720221 >55% Si, 720229 ≤55% Si); a cost-effective alternative to graphite anodes that enables up to ~10x silicon energy storage while alleviating volume expansion, enhancing battery capacity and faster charging in advanced Li-ion cells (also serves as metallurgical precursor to higher-purity silicon)",
     0.6, "battery_grade", 4, "global"),

    # ── P1 — Zinc scrap ──────────────────────────────────────────────────
    ("7902", "Zinc",
     "Zinc waste and scrap — recycling",
     1.0, "scrap", 4, "global"),

    # ── Sodium expansion (Option B — single material, multi-prefix scope) ─
    # Existing 2836 entry above is kept as the primary battery-grade
    # precursor.  Below add NaOH precursor, Na phosphate (NIB cathode), and
    # peroxometallates per partner's CSV annotations.  HS 2827 (sodium
    # chloride / salt), 2805 (sodium metal), and 2833 (Na2SO4) are
    # intentionally excluded — partner tagged each as not battery-relevant.
    ("2815", "Sodium",
     "Sodium hydroxide (caustic soda) at 281511 — chemical precursor for Na-ion cathode synthesis",
     0.7, "intermediate", 4, "global"),
    ("2835", "Sodium",
     "Phosphates — trisodium phosphate (Na3PO4) at 283526; polyanionic NIB cathode precursor (multi-material 4-digit)",
     0.5, "battery_grade", 4, "global"),
    ("2841", "Sodium",
     "Salts of peroxometallic acids — sodium-containing TM oxide precursors at 284169 (multi-material 4-digit)",
     0.4, "battery_grade", 4, "global"),

    # ── Bismuth (battery R&D — minimal coverage) ──────────────────────────
    # Partner tags as "Battery R&D / future (electrolytes)".  Single-material
    # 4-digit means high confidence on attribution.
    ("8106", "Bismuth",
     "Bismuth and articles thereof — battery R&D (electrolyte additives)",
     1.0, "refined", 4, "global"),

    # ── Rhenium (non-battery — minimal coverage so material has any HS path) ─
    # Partner does not tag rhenium as battery-relevant; entries kept minimal
    # so the material isn't an orphan in materials table without any HS
    # mapping at all.  Both prefixes are multi-material — low confidence.
    ("2841", "Rhenium",
     "Salts of peroxometallic acids — ammonium perrhenate at 284190 (multi-material 4-digit)",
     0.4, "intermediate", 4, "global"),
    ("8112", "Rhenium",
     "Beryllium / chromium / minor metals chapter — rhenium unwrought at 811241 (multi-material 4-digit)",
     0.4, "refined", 4, "global"),

    # ── 6-digit additions for the prefixes above ─────────────────────────
    # Only entries NOT already present in the existing 6-digit blocks above.
    # Confirmed via post-edit duplicate check (2026-05).  720421 (stainless
    # scrap) is the only new ferro-/scrap 6-digit entry needed; the other
    # ferroalloy 6-digit codes (720211/19/41/49/60/93, 740400, 740811,
    # 750300, 760200, 790200) already existed under their respective
    # material sections.  Same logic for fluoride / titanium / vanadium /
    # magnesium 6-digit codes.
    ("720421", "Chromium",
     "Stainless steel scrap — chromium recovery stream",
     1.0, "scrap", 6, "global"),
    ("790200", "Zinc",
     "Zinc waste and scrap — recycling",
     1.0, "scrap", 6, "global"),
    ("720221", "Silicon (Anode Grade)",
     "Ferrosilicon, >55% Si by weight — graphite-alternative anode material for advanced Li-ion cells; enables ~10x silicon energy storage while alleviating volume expansion (faster charging, higher capacity)",
     1.0, "battery_grade", 6, "global"),
    ("720229", "Silicon (Anode Grade)",
     "Ferrosilicon, ≤55% Si by weight — graphite-alternative anode material for advanced Li-ion cells; enables ~10x silicon energy storage while alleviating volume expansion (faster charging, higher capacity)",
     1.0, "battery_grade", 6, "global"),

    # Sodium 6-digit (NaOH, Na phosphate, soda ash, peroxometallates)
    ("281511", "Sodium",
     "Sodium hydroxide (caustic soda), solid — chemical precursor",
     1.0, "intermediate", 6, "global"),
    ("281512", "Sodium",
     "Sodium hydroxide (caustic soda), aqueous — chemical precursor",
     1.0, "intermediate", 6, "global"),
    ("283620", "Sodium",
     "Disodium carbonate (Na2CO3 / soda ash) — Na-ion cathode precursor",
     1.0, "battery_grade", 6, "global"),
    ("284169", "Manganese",
     "Salts of oxometallic acids; manganates/permanganates — includes Li manganate (LMO cathode chemistry)",
     0.3, "battery_grade", 6, "global"),

    # Bismuth 6-digit
    ("810610", "Bismuth",
     "Bismuth ≥99.99% (high purity) — battery R&D",
     1.0, "refined", 6, "global"),
    ("810690", "Bismuth",
     "Bismuth, other forms — battery R&D",
     0.8, "refined", 6, "global"),

    # Rhenium 6-digit
    ("811241", "Rhenium",
     "Rhenium unwrought (incl. powders) — partner non-battery; minimal coverage",
     1.0, "refined", 6, "global"),
    ("284190", "Rhenium",
     "Ammonium perrhenate — Re precursor",
     0.3, "intermediate", 6, "global"),

    # ── Migrated from mcs_pdf_parser._HS_6DIGIT_STAGE_OVERRIDE (May 2026) ──
    # 20 entries that the PDF parser carried as overrides but were
    # missing from this seed.  Migrating them here lets us delete the
    # override dict and have a single source of truth for stage
    # assignments.
    ("260600", "Aluminum",
     "Aluminium ores and concentrates — bauxite (sub-prefix of 2606)",
     1.0, "ore", 6, "global"),
    ("281820", "Aluminum",
     "Aluminium oxide (alumina) — refined intermediate from bauxite",
     1.0, "intermediate", 6, "global"),
    # 260111 and 260112 (Iron Ore non/agglomerated) are ALREADY seeded
    # earlier in this list — entries 92/93.  Removed redundant duplicates
    # added during the May 2026 PDF-override migration pass.
    # 2026-06-11 AUDIT FIX: "261500" does not exist in HS2022.  Heading 2615
    # splits at 6-digit into 261510 (Zirconium-specific, already in seed at
    # line ~717) and 261590 (Nb/Ta/V shared, already in seed at lines ~658,
    # 681, 697 with confidence 0.6 each).  No replacement rows added — the
    # canonical entries already provide coverage.
    ("261790", "Rare Earth Elements",
     "Rare-earth ores and concentrates (other) — bastnäsite / monazite mixed",
     0.3, "ore", 6, "global"),
    # 283324 Nickel + 283329 Manganese moved to their canonical material
    # sections (Nickel 6-digit + Manganese 6-digit) in the 11.2 audit
    # easy-adds.  Removed duplicates here.  Note: the Manganese mapping
    # was previously at confidence 1.0; the canonical entry uses
    # confidence 0.5 to reflect that HS 283329 ("other sulphates") also
    # carries the Cobalt mapping (cobalt sulphate, confidence 0.8) — the
    # code is genuinely multi-material at the 6-digit level.
    ("740311", "Copper",
     "Copper cathodes (refined) — battery current collector input",
     1.0, "refined", 6, "global"),
    ("740319", "Copper",
     "Refined copper, other unwrought forms",
     1.0, "refined", 6, "global"),
    # 2026-06-11 AUDIT FIX: "750100" does not exist in HS2022.  Heading 7501
    # splits at 6-digit into 750110 (mattes) and 750120 (oxide sinters and
    # other intermediate products).  Both are intermediate-stage in our taxonomy.
    ("750110", "Nickel",
     "Nickel mattes — intermediate sulfide-flow product",
     1.0, "intermediate", 6, "global"),
    ("750120", "Nickel",
     "Nickel oxide sinters and other intermediate products of nickel metallurgy",
     1.0, "intermediate", 6, "global"),
    ("750220", "Nickel",
     "Nickel alloys, unwrought — refined product, partial battery use",
     0.6, "refined", 6, "global"),
    ("810194", "Tungsten",
     "Tungsten powders / unwrought — intermediate metallurgical form",
     0.7, "intermediate", 6, "global"),
    ("810199", "Tungsten",
     "Tungsten, other (refined / wrought)",
     0.7, "refined", 6, "global"),
    # 2026-06-11 AUDIT FIX: "810292" was removed in HS2022 reshuffle of Ch.81.
    # Closest replacement is 810294 (Mo unwrought including sintered bars) —
    # already in seed at line ~622 as refined-stage @ confidence 1.0.  No new
    # row added; the canonical entry provides the coverage.
    # 2026-06-11 AUDIT FIX: HS2022 collapsed 2528 to a single 6-digit
    # subheading 252800.  Previous 252810 (sodium borates) and 252890 (other)
    # don't exist in HS2022 — both removed.  Canonical 252800 row already in
    # seed at line ~864 at confidence 1.0; no replacement row added here.
    ("282739", "Lithium",
     "Other chlorides — includes lithium chloride (LiCl) intermediate",
     0.5, "intermediate", 6, "global"),
    # 2026-06-11 ERROR FIX: removed prior "280512" → Lithium row.  HS 2805.12
    # is "Calcium" (not lithium); lithium metal lives at 2805.19 ("other
    # alkaline-earth metals, n.e.s.").  The 4-digit 2805 umbrella row above
    # already provides Lithium fallback coverage at confidence 0.7.

    # ── Comtrade unresolved-prefix coverage (2026-05-11) ─────────────────
    # Targeted additions for the top-volume HS prefixes that were leaving
    # TradeFlow rows with material_id IS NULL.  Survey at 2026-05-10 over
    # ~1,619 trade_flows rows showed 274 unresolved (17%), concentrated in
    # 7202 / 2818 / 2620 (~$85B of ~$125B unresolved value).  See
    # docs/scoring-audit-2026-05-addendum.md for the dollar-volume table.
    #
    # Each addition is the standard HS 2022 subheading mapping.  No
    # confidences below 1.0 here — these are single-material 6-digit
    # codes per the WCO nomenclature, so the resolver's exact-match
    # Pass 1 handles them cleanly without fighting the existing 4-digit
    # catch-alls.

    # 7202.50 — Ferrosilicochromium.  The only HS 2022 subheading under
    # chapter 7202 not previously seeded; 720211/19/21/29/30/41/49/60/70/
    # 80/91/92/93/99 were all already covered.  Chromium is the primary
    # metal recovered from FeSiCr.
    ("720250", "Chromium",
     "Ferrosilicochromium (FeSiCr) — stainless and tool-steel intermediate",
     1.0, "intermediate", 6, "global"),

    # 2818 — Aluminium oxide / hydroxide / corundum.  281820 (calcined
    # alumina) was already seeded; 281810 and 281830 are added here so
    # the full 2818 chapter resolves.  Also adding a 4-digit catch-all so
    # any rows reported at the aggregated 2818 level resolve cleanly via
    # the resolver's Pass 2 (4-digit fallback) — important because Comtrade
    # occasionally returns 4-digit aggregates when reporters don't disclose
    # below the heading level.
    ("281810", "Aluminum",
     "Artificial corundum, whether or not chemically defined — abrasive/refractory aluminum form",
     1.0, "intermediate", 6, "global"),
    ("281830", "Aluminum",
     "Aluminium hydroxide — alumina precursor / aluminium chemical intermediate",
     1.0, "intermediate", 6, "global"),
    ("2818", "Aluminum",
     "Aluminium oxide (including artificial corundum); aluminium hydroxide — 4-digit catch-all "
     "for aggregated trade flows that don't disclose the 6-digit subheading",
     # All 281810/20/30 subheadings unambiguously trace to Aluminum supply
     # chain, so the 4-digit roll-up is genuinely a single-material prefix.
     0.9, "intermediate", 4, "global"),

    # 2620 — Slag, ash and residues containing metals.  Battery recycling
    # pathway.  Existing seed has 262040 → Vanadium @ 0.9 (steel slag
    # interpretation) and 262099 → Titanium/Vanadium.  Added below are
    # the two unambiguous standard mappings (262030 copper-bearing slag,
    # 262040 aluminium-bearing slag per WCO HS 2022).
    #
    # CONFLICT FLAG — 262040: existing seed maps this to Vanadium at 0.9
    # with the comment "steel slag — secondary V source".  Per WCO HS 2022
    # the subheading is officially "Aluminium-bearing slag and residues."
    # Adding 262040 → Aluminum at 1.0 below means it will win the exact-
    # match path (top-confidence) over the existing Vanadium entry.  If
    # the original Vanadium classification was deliberate (capturing
    # vanadium recovery from V-rich steel slag), the resolver behaviour
    # changes: rows previously attributed to Vanadium will now go to
    # Aluminum.  Decide with partner; if Vanadium attribution should be
    # preserved, remove the Aluminum entry below or downgrade its
    # confidence to 0.8 so the existing 0.9 Vanadium wins.
    #
    # 262099 (Li-ion black mass) is deliberately NOT added here — black-
    # mass material attribution is an open partner-data-decision question.
    # Existing 262099 → Titanium/Vanadium entries left alone.  See ticket.
    ("262030", "Copper",
     "Copper-bearing slag, ash and residues — secondary copper / battery recycling stream",
     1.0, "scrap", 6, "global"),
    ("262040", "Aluminum",
     "Aluminium-bearing slag, ash and residues (WCO HS 2022) — secondary aluminum / battery "
     "recycling stream.  CONFLICTS with existing 262040 → Vanadium @ 0.9 (steel-slag "
     "interpretation); this 1.0 entry wins exact-match resolution.  Reconcile with partner.",
     1.0, "scrap", 6, "global"),

    # ─────────────────────────────────────────────────────────────────────
    # 2026-06-11 HS coverage expansion — Tier 1 (high-leverage gaps)
    # ─────────────────────────────────────────────────────────────────────
    # Audit found several launch-mineral × stage cells missing entirely.
    # Most consequential: Iron Ore had no battery_grade or
    # intermediate stage at all — the LFP cathode precursor (FeSO4) was
    # invisible to scoring.  Nickel was missing the MHP/MHC battery_grade
    # input.  Phosphate had no intermediate stage.  Silicon had no ore
    # stage.  Natural Graphite had no intermediate/refined coverage.
    #
    # All additions use 6-digit codes for precise stage attribution.

    # ── Iron Ore — battery_grade + intermediate gaps ──────────
    ("283329", "Iron Ore",
     "Iron(II) sulfate (FeSO4) — LFP cathode precursor (heptahydrate FeSO4·7H2O)",
     0.4, "battery_grade", 6, "global"),
    ("284290", "Iron Ore",
     "Other phosphate salts incl. iron(III) phosphate (FePO4) — LFP intermediate",
     0.3, "intermediate", 6, "global"),

    # ── Synthetic Graphite (restored 2026-07-13; authored 2026-07-10 —
    #    the device commit never landed; reconstructed from live DB) ──
    ("271312", "Synthetic Graphite",
     "Calcined petroleum coke incl. needle coke - synthetic graphite feedstock. "
     "Code dominated by fuel/aluminium-grade CPC; needle coke has no dedicated "
     "HS line, hence low confidence.",
     0.4, "intermediate", 6, "global"),
    ("380110", "Synthetic Graphite",
     "Artificial graphite - synthetic anode material. DUAL-MAPPED with Natural "
     "Graphite (natural AAM also ships under this code in trade practice, e.g. "
     "Vidalia); HS definition is artificial.",
     0.7, "battery_grade", 6, "global"),

    # ── Nickel — Ni(OH)2 / MHP battery_grade ──────────────────────────────
    ("282540", "Nickel",
     "Nickel oxides and hydroxides (NiO, Ni(OH)2) — MHP/MHC battery-grade precursor",
     1.0, "battery_grade", 6, "global"),

    # ── Phosphate — intermediate + battery_grade gaps ────
    ("283531", "Phosphate",
     "Sodium triphosphate (Na5P3O10) — phosphate intermediate",
     0.8, "intermediate", 6, "global"),
    ("283539", "Phosphate",
     "Other polyphosphates — phosphate intermediate (residual; covers K/NH4 polyphosphates too)",
     0.5, "intermediate", 6, "global"),
    ("283525", "Phosphate",
     "Calcium hydrogenorthophosphate (dicalcium phosphate) — feed/battery precursor",
     0.7, "battery_grade", 6, "global"),

    # ── Silicon (Anode Grade) — ore stage entirely missing ────────────────
    ("250510", "Silicon (Anode Grade)",
     "Silica sands and quartz sands — silicon metallurgical raw input (ore stage)",
     0.8, "ore", 6, "global"),
    ("281122", "Silicon (Anode Grade)",
     "Silicon dioxide (SiO2) — high-purity quartz feedstock",
     0.7, "ore", 6, "global"),

    # ── Natural Graphite — intermediate / refined gaps ────────────────────
    ("380120", "Natural Graphite",
     "Colloidal / semi-colloidal graphite preparations — intermediate processing",
     0.6, "intermediate", 6, "global"),
    ("380190", "Natural Graphite",
     "Other graphite/carbon preparations — refined graphite forms (residual; carbon-paste mix)",
     0.4, "refined", 6, "global"),

    # ─────────────────────────────────────────────────────────────────────
    # 2026-06-11 HS coverage expansion — Tier 2 (lower-impact additions)
    # ─────────────────────────────────────────────────────────────────────

    # ── Aluminum — other Al-foil battery_grade variants ───────────────────
    # 2026-06-11 AUDIT FIX: removed "760712" — not in HS2022.  Canonical
    # 760711 (rolled Al foil <0.2mm) already in seed at line ~559.  760719
    # (other Al foil) below is a valid HS2022 code, keep it.
    ("760719", "Aluminum",
     "Aluminum foil, other, <0.2mm — battery-cell foil variants",
     0.6, "battery_grade", 6, "global"),

    # ── Copper — fabricated wire forms ────────────────────────────────────
    # 2026-06-11 AUDIT FIX: "740812" not in HS2022 — corrected to 740819
    # (refined Cu wire, cross-section ≤6mm).  Note 740811 (>6mm) already in
    # seed at conf 1.0 as the wire-rod "fabricated" entry.
    ("740819", "Copper",
     "Refined copper wire, cross-section ≤6mm — battery-pack and motor wiring",
     0.8, "fabricated", 6, "global"),

    # ── Manganese — additional refined-oxide coverage ─────────────────────
    # 2026-06-11 AUDIT FIX: removed "281641" → Manganese refined row.  The
    # code does not exist in HS2022 (2816 only has .10 = Mg and .40 = Sr/Ba).
    # Mn(OH)2 has no clean 6-digit code; trade volume would report under
    # 282090 ("other Mn oxides").  Documented gap, no replacement row added.

    # ── Rare Earth Elements — bonded magnets + ferro-REE alloys ───────────
    ("850519", "Rare Earth Elements",
     "Other permanent magnets (bonded NdFeB, SmCo, ferrite) — EV motor magnets",
     0.3, "battery_grade", 6, "global"),
    ("720299", "Rare Earth Elements",
     "Other ferro-alloys incl. ferro-rare-earth — REE magnet-feedstock alloys",
     0.5, "refined", 6, "global"),
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
        # 2026-06-14: price-source vocabulary added so the hs_resolver
        # can map USGS / Pink Sheet / future commercial price descriptors
        # back to this HS row.  LME and U.S. spot cobalt are quoted on
        # refined cobalt cathode metal, which is 810520 (unwrought
        # refined cobalt) at the HS-6 level.
        "LME cobalt",
        "London Metal Exchange",
        "U.S. spot",                # 2026-06-14 fix: shorter form matches
        "U.S. spot cobalt",          # actual USGS phrasing
        "cobalt cathode",
        "electrolytic cobalt",
        "refined cobalt",
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
    # 11.2 audit easy-adds (2026-06): keyword entries for the new
    # battery-grade HS codes seeded in this pass.
    ("283324", "Nickel"): [
        "nickel sulfate",
        "nickel sulphate",
        "NiSO4",
        "battery-grade nickel sulphate",
    ],
    ("283329", "Manganese"): [
        "manganese sulfate",
        "manganese sulphate",
        "MnSO4",
        "high-purity manganese sulphate",
        "HPMSM",
    ],
    # 2026-07-13: the two LFP-iron tuples (added to _MAPPINGS under the
    # original LFP-grade scoping) only started resolving after the Iron
    # Ore identity fix — they landed in the DB keywordless, breaking the
    # zero-keywordless invariant from the 07-10 backfill.  Iron-specific
    # vocabulary only; the sulfate-family overlap across 283329's four
    # materials (Co/Mn/Zn/Fe) is handled by IDF downweighting.
    # Deliberately NO "lithium iron phosphate"/"LFP" tokens — that is the
    # cathode PRODUCT, not the precursor salt these nodes carry.
    ("283329", "Iron Ore"): [
        "iron sulfate",
        "iron sulphate",
        "ferrous sulfate",
        "ferrous sulphate",
        "FeSO4",
        "iron(II) sulfate",
        "copperas",
        "battery-grade iron sulphate",
    ],
    ("284290", "Iron Ore"): [
        "iron phosphate",
        "ferric phosphate",
        "iron(III) phosphate",
        "FePO4",
        "ferric orthophosphate",
    ],
    ("380110", "Natural Graphite"): [
        "natural graphite anode material",
        "spherical graphite",
        "spheronized graphite",
        "purified spherical graphite",
        "coated spherical purified graphite",
        "CSPG",
        "natural AAM",
        "anode-grade graphite",
    ],
    ("380130", "Natural Graphite"): [
        "electrode paste",
        "carbonaceous paste",
        "anode electrode paste",
    ],
    ("741011", "Copper"): [
        "battery copper foil",
        "rolled copper foil",
        "copper current collector",
        "anode copper foil",
        "cathode copper foil",
    ],
    ("760711", "Aluminum"): [
        "battery aluminum foil",
        "aluminium foil",
        "cathode aluminum foil",
        "rolled aluminum foil",
        "aluminum current collector",
    ],
    ("850511", "Rare Earth Elements"): [
        "NdFeB",
        "neodymium magnet",
        "neodymium-iron-boron",
        "permanent magnet",
        "sintered magnet",
        "EV motor magnet",
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
        "ni briquette",
        "primary nickel",
        "lme nickel",
        "london metal exchange",
        "lme cash",
        "nickel cathode",
        "refined nickel",
        "electrolytic nickel",
        "nickel",
        "ni",
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

    # ── Phosphate ────────────────────────────────────────
    # 2026-06-14: Added so the hs_resolver can map USGS / Pink Sheet
    # phosphate-rock benchmarks back to specific HS rows.  USGS Salient
    # "Price, average value, f.o.b. mine" applies to phosphate rock at
    # the mine — which is HS 2510.10 (unground) at the canonical stage.
    # 251020 (ground) covers the post-beneficiation form when downstream
    # processors quote it.  Partner should adjust if convention shifts.
    ("251010", "Phosphate"): [
        "phosphate rock",
        "natural calcium phosphate",
        "unground phosphate rock",
        "f.o.b. mine",
        "FOB mine",
        "phosphate rock F.O.B.",
        "rock phosphate",
    ],
    ("251020", "Phosphate"): [
        "ground phosphate rock",
        "beneficiated phosphate rock",
        "phosphate concentrate",
        "natural calcium phosphates ground",
    ],

    # ── Fluorspar ────────────────────────────────────────────────────────
    # 2026-06-14: Added for fluorspar price benchmarks.  Most US
    # commercial fluorspar trade is acid-grade (252922) used as HF / EV
    # battery electrolyte precursor; metallurgical-grade (252921) is for
    # steel-making flux.  USGS "Price, average unit value of imports" is
    # weighted by total imports, where acid-grade dominates by value, so
    # the c.i.f. / imports benchmarks route to 252922 by default.
    # Met-grade quotes ("metallurgical fluorspar") route to 252921.
    ("252921", "Fluorspar"): [
        "metallurgical fluorspar",
        "metallurgical grade fluorspar",
        "met-grade fluorspar",
        "fluorspar metallurgical",
    ],
    ("252922", "Fluorspar"): [
        "acid-grade fluorspar",
        "acid grade fluorspar",
        "fluorspar acid grade",
        "acidspar",
        "fluorspar imports",
        "cost insurance and freight",
        "average unit value of imports",
    ],

    # ── Natural Graphite ─────────────────────────────────────────────────
    ("2504", "Natural Graphite"): [
        "natural graphite",
        "flake graphite",
        # 2026-05-12: added bare "graphite" so IEA / Federal Register /
        # GTA text scans catch policies that use the unmodified word
        # (cross-cutting critical-minerals lists do this constantly —
        # "cobalt, nickel, lithium, copper, and graphite" patterns
        # previously matched every other co-listed material but skipped
        # graphite).  If a Synthetic Graphite material is added later,
        # register "graphite" under both — the inverse-frequency weight
        # in MaterialCache.build() automatically damps the keyword's
        # relevance to handle the ambiguity, and the more-specific
        # "natural graphite" / "synthetic graphite" keywords still take
        # precedence when present.
        "graphite",
    ],
    ("250410", "Natural Graphite"): [
        "natural graphite, powder",
        "amorphous graphite",
        "spherical graphite",
        "spheroidized graphite",
        # 2026-06-14: price-source vocabulary so the hs_resolver maps
        # the USGS "average unit value of imports" benchmark here —
        # natural-graphite powder / flake is the dominant imported
        # form for the US battery supply chain.
        "natural flake graphite",
        "graphite flake",
        "graphite powder",
        "average unit value of imports, dollars per metric ton",
        "natural graphite imports",
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
        "copper content of ore and concentrate",
        "copper ore and concentrate",
        "copper",
        "cu",
        "ore and concentrates",
        "copper ore and concentrates",
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
        # 2026-06-14: price-source vocabulary so the hs_resolver can
        # map USGS / Pink Sheet / commercial price descriptors back
        # here.  LME and U.S. market spot Al prices both quote
        # unalloyed primary aluminum ingot (HS 7601.10).
        "LME aluminum",
        "London Metal Exchange",
        "ingot",                    # 2026-06-14 fix: standalone "ingot"
        "aluminum ingot",            # matches USGS "Price, ingot, average..."
        "aluminum sow",
        "U.S. market spot",
        "U.S. spot aluminum",
    ],
    ("760120", "Aluminum"): [
        "unwrought aluminum, alloyed",
    ],

    # ── Iron Ore ─────────────────────────────────────────────
    ("2601", "Iron Ore"): [
        "iron ore",
        "iron pyrites",
        "iron ores and concentrates",
        "hematite",
        "magnetite",
        "itabirite",
    ],
    ("260111", "Iron Ore"): [
        "iron ore, non-agglomerated",
        "iron ore fines",
        "Tianjin iron ore",
        "average unit value reported by mines",
        "mine-value iron ore",
        "CFR iron ore",
        "SGX iron ore",
        "iron ore lump",
        "direct shipping ore",
        "DSO",
    ],
    ("260112", "Iron Ore"): [
        "iron ore, agglomerated",
        "iron ore pellets",
        "pellet feed",
        "sinter feed",
        "DR-grade pellets",
    ],

    # ── Rare Earth Elements ──────────────────────────────────────────────
    # 2805 4-digit is shared with Lithium in `_MAPPINGS`; we attach REE
    # keywords only to the 6-digit `280530` row (REE-only) so there's no
    # cross-contamination.  The 4-digit `2617` is REE-only in seed.
    ("2617", "Rare Earth Elements"): [
        "rare earth ore",
        "monazite",
        "bastnasite",
        # 2026-05-12: bare-form keywords — same shape as the Graphite fix.
        # Critical-minerals lists routinely write "rare earth" / "rare earths"
        # alongside other minerals without the full "rare earth elements"
        # phrase.  Probe found 15 unattributed IEA events using these forms.
        # Highly specific to the minerals domain — no false-positive risk
        # from common English.
        "rare earth",
        "rare earths",
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

    # ── Platinum-Group Metals ────────────────────────────────────────────
    # 2026-05-12: added per the qualifier-canonical sweep.  The canonical
    # name "Platinum-Group Metals" gets registered via MaterialCache's
    # canonical-name path at relevance 0.85, but its hyphenated form means
    # source text using the unhyphenated phrasing ("platinum group metals",
    # "platinum group elements", "pgms") doesn't match.  Probe found 5
    # unattributed IEA events using these forms.
    ("7110", "Platinum-Group Metals"): [
        "platinum group metals",
        "platinum group elements",
        "platinum group minerals",
        "pgms",
    ],
    ("2616", "Platinum-Group Metals"): [
        "platinum group ore",
        "platinum group bearing ore",
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
        "titanium sponge metal",
        "titanium metal",
        "titanium",
        "ti",
        "titanium, unwrought",
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
    # Ferrosilicon — graphite-alternative anode material for advanced Li-ion
    # cells (per partner input).  USGS uses the misspelling "Ferosilicon" in
    # the MCS CSV; included here so free-text matching catches both forms
    # plus the FeSi shorthand commonly used in trade reporting.
    ("7202", "Silicon (Anode Grade)"): [
        "ferrosilicon",
        "FeSi",
        "Ferosilicon",   # USGS misspelling (one r) — matches MCS CSV TYPE strings
    ],

    # ════════════════════════════════════════════════════════════════════════
    # MCS 2026 Import Sources sub-type aliases (May 2026 batch)
    # ════════════════════════════════════════════════════════════════════════
    # Below this line: keywords derived directly from the MCS 2026
    # Import Sources Statistics_detail values, so the parser's sub-type
    # resolution can route each sub-type to its specific HS prefix
    # instead of collapsing all sub-types onto material.hs_codes[0].
    #
    # Coverage rationale: each chapter publishes 2-7 distinct sub-types
    # (e.g. ANTIMONY: oxide / unwrought metal and powder / ore and
    # concentrates / total).  Without these keywords, the CLI dedup
    # pass keeps one row and discards the rest.  WITH these keywords,
    # each sub-type lands on its own HS prefix and partner sees per-
    # stage country distributions.
    #
    # "Total"-style sub-types (e.g. "Combined total", "Total imports",
    # "Total metal and oxide") are intentionally NOT keyworded — the
    # CLI dedup pass already prefers them when keywords don't resolve,
    # so they fall through correctly without explicit aliases.

    # ── Antimony ─────────────────────────────────────────────────────────
    ("261710", "Antimony"): [
        "ore and concentrates",
        "antimony ore",
        "antimony concentrate",
    ],
    ("282580", "Antimony"): [
        "oxide",
        "antimony oxide",
        "Sb2O3",
    ],
    ("811010", "Antimony"): [
        "unwrought metal and powder",
        "unwrought antimony",
        "antimony metal",
        "antimony powder",
        "metal and powder",
    ],

    # ── Aluminum (BAUXITE AND ALUMINA chapter sub-types) ─────────────────
    # 'Bauxite' keyword already lives on (2606, Aluminum).  Add 6-digit
    # form + alumina alias on the intermediate prefix.
    ("260600", "Aluminum"): [
        "bauxite",
        "aluminium ore",
        "aluminum ore",
    ],
    ("281820", "Aluminum"): [
        "alumina",
        "aluminium oxide",
        "aluminum oxide",
        "Al2O3",
    ],

    # ── Chromium ─────────────────────────────────────────────────────────
    # 'Chromium-containing chemicals' and 'Stainless steel' sub-types
    # have no precise HS code in seed today — left unmapped, fall through
    # to dedup. Partner can revisit once chemical-specific HS rows are added.
    ("261000", "Chromium"): [
        "chromite",
        "chromite (ores and concentrates)",
        "chromium ore",
        "chromium concentrate",
    ],
    ("720241", "Chromium"): [
        "ferrochromium",                          # default (high-C is dominant trade form)
        "ferrochromium, high-carbon",
        "high-carbon ferrochromium",
        "charge ferrochromium",
    ],
    ("720249", "Chromium"): [
        "ferrochromium, low-carbon",
        "low-carbon ferrochromium",
    ],
    ("811221", "Chromium"): [
        "chromium metal",
        "unwrought chromium",
    ],
    ("720421", "Chromium"): [
        "chromium-containing scrap",
        "stainless steel scrap",
    ],

    # ── Copper ───────────────────────────────────────────────────────────
    ("740200", "Copper"): [
        "copper content of blister and anodes",
        "blister copper",
        "copper anodes",
        "copper anode",
        "anode copper",
        "unrefined copper",
        "copper content of matte, ash, and precipitate",
        "copper matte",
        "matte, ash, and precipitate",
    ],
    ("740311", "Copper"): [
        "refined copper",
        "copper cathodes",
        "copper cathode",
        "cathode copper",
        # 2026-06-14: price-source vocabulary so the hs_resolver can
        # map USGS / Pink Sheet / commercial price descriptors back to
        # this HS row.  All three benchmarks (LME / COMEX / U.S. producer)
        # quote refined Grade-A copper cathodes (HS 7403.11).
        "LME copper",
        "London Metal Exchange",
        "COMEX",
        "COMEX high-grade",
        "U.S. producer",            # 2026-06-14 fix: shorter form matches
        "U.S. producer cathode",     # the actual USGS phrasing
        "U.S. producer copper",
        "high-grade copper",
    ],
    ("740400", "Copper"): [
        "copper content of scrap",
        "copper scrap",
        "copper waste and scrap",
    ],
    # 260300 already has 'copper ores', 'copper concentrate' — extend with
    # the MCS-specific phrasing.  Note: this overrides the existing entry,
    # so we re-list the prior keywords too.

    # ── Germanium ────────────────────────────────────────────────────────
    ("282560", "Germanium"): [
        "germanium dioxide",
        "germanium dioxide (Ge content)",
        "GeO2",
        "germanium oxide",
    ],
    ("811292", "Germanium"): [
        "germanium metal",
        "unwrought germanium",
    ],

    # ── Magnesium (METAL chapter) ────────────────────────────────────────
    ("810411", "Magnesium"): [
        "magnesium metal",
        "magnesium metal (99.8% purity)",
        "unwrought magnesium",
        "pure magnesium",
    ],
    ("810419", "Magnesium"): [
        "magnesium alloys",
        "magnesium alloy",
        "magnesium alloys (magnesium content)",
    ],
    ("810420", "Magnesium"): [
        "scrap",
        "magnesium scrap",
        "magnesium waste and scrap",
    ],
    ("810430", "Magnesium"): [
        "magnesium powders",
        "magnesium powder",
    ],
    ("810490", "Magnesium"): [
        "sheet, powder, and other",
        "magnesium sheet",
        "wrought magnesium",
        "magnesium articles",
    ],

    # ── Manganese ────────────────────────────────────────────────────────
    ("720211", "Manganese"): [
        "ferromanganese",                           # default to high-C as dominant
        "ferromanganese, high-carbon",
    ],
    ("720219", "Manganese"): [
        "ferromanganese, low-carbon",
        "ferromanganese, medium-carbon",
    ],
    ("720230", "Manganese"): [
        "silicomanganese",
        "ferrosilicon manganese",
        "silicon manganese",
    ],

    # ── Molybdenum ───────────────────────────────────────────────────────
    ("261310", "Molybdenum"): [
        "molybdenum ore and concentrates, roasted",
        "molybdic oxide calcine",
    ],
    ("261390", "Molybdenum"): [
        "molybdenum ore",
        "molybdenum concentrate",
        "molybdenum ore and concentrates",     # default (unroasted is dominant)
    ],
    ("720270", "Molybdenum"): [
        "ferromolybdenum",
    ],

    # ── Nickel ───────────────────────────────────────────────────────────
    # 750210 already has keywords; extend with 'primary nickel' MCS phrasing
    # plus 2026-06-14 price-source vocabulary so the hs_resolver maps
    # LME nickel cash to refined unalloyed nickel (the LME-deliverable form).
    ("750300", "Nickel"): [
        "nickel-containing scrap",
        "nickel scrap",
        "nickel-containing scrap, including nickel content of stainless-steel scrap",
        "stainless steel scrap (nickel content)",
    ],

    # ── Niobium ──────────────────────────────────────────────────────────
    ("261590", "Niobium"): [
        "niobium and tantalum ores and concentrates",
        "columbite-tantalite",
        "columbite",
        "niobium ore",
        "niobium concentrate",
    ],
    ("282590", "Niobium"): [
        "niobium oxide",
        "Nb2O5",
        "columbium oxide",
    ],
    ("720293", "Niobium"): [
        "ferroniobium",
        "ferroniobium and niobium metal",     # combined sub-type → routes to dominant ferro form
        "ferro-columbium",
    ],

    # ── Rhenium ──────────────────────────────────────────────────────────
    ("284190", "Rhenium"): [
        "ammonium perrhenate",
    ],
    ("811241", "Rhenium"): [
        "rhenium metal",
        "unwrought rhenium",
    ],

    # ── Tantalum ─────────────────────────────────────────────────────────
    # 261590 also serves Tantalum (mixed-material 6-digit prefix).
    ("261590", "Tantalum"): [
        "tantalum ores and concentrates",
        "tantalite",
        "tantalum ore",
        "tantalum concentrate",
    ],
    ("810320", "Tantalum"): [
        "tantalum metal and powder",
        "tantalum metal",
        "tantalum powder",
        "unwrought tantalum",
    ],
    ("810391", "Tantalum"): [
        "tantalum waste and scrap",
        "tantalum scrap",
    ],

    # ── Tin ──────────────────────────────────────────────────────────────
    ("800110", "Tin"): [
        "refined tin",
        "unwrought tin",
        "pure tin",
        "tin, not alloyed",
    ],
    ("800200", "Tin"): [
        "waste and scrap",
        "tin waste and scrap",
        "tin scrap",
    ],

    # ── Titanium ─────────────────────────────────────────────────────────
    ("320611", "Titanium"): [
        "TiO2 pigment",
        "titanium dioxide pigment",
        "TiO2 ≥80%",
    ],
    # 810820 already has 'unwrought titanium', 'titanium sponge'; extend
    # with the MCS phrasing variant.

    # ── Vanadium ─────────────────────────────────────────────────────────
    ("282530", "Vanadium"): [
        "vanadium pentoxide",
        "V2O5",
        "vanadium oxide",
    ],
    ("720292", "Vanadium"): [
        "ferrovanadium",
    ],

    # ── Zinc ─────────────────────────────────────────────────────────────
    ("260800", "Zinc"): [
        "ores and concentrates",
        "zinc ore",
        "zinc concentrate",
        "zinc ores and concentrates",
    ],
    # 7901 (4-digit) is the only "refined zinc" prefix in seed today.
    # Routing 'refined metal' here lets the parser resolve at the 4-digit
    # level until partner adds 6-digit zinc refined entries.
    ("7901", "Zinc"): [
        "refined metal",
        "refined zinc",
        "unwrought zinc",
        "zinc metal",
    ],
    ("790200", "Zinc"): [
        "waste and scrap",
        "zinc waste and scrap",
        "zinc scrap",
        "waste and scrap (gross weight)",
    ],

    # ── Zirconium (and Hafnium sub-types) ────────────────────────────────
    # MCS publishes Hafnium sub-types under the same chapter but seed has
    # no Hf-specific HS rows (Hf shares 6-digit prefixes with Zr).  Hf
    # sub-types fall through to dedup until partner adds Hf entries.
    ("261510", "Zirconium"): [
        "zirconium ores and concentrates",
        "zircon",
        "zircon sand",
        "zircon sands",
    ],
    ("282560", "Zirconium"): [
        "zirconium, compounds",
        "zirconium oxide",
        "zirconium dioxide",
        "ZrO2",
        "baddeleyite",
    ],
    ("810929", "Zirconium"): [
        "zirconium, unwrought",
        "unwrought zirconium",
        "zirconium powder",
    ],
    ("810999", "Zirconium"): [
        "zirconium, wrought",
        "wrought zirconium",
        "zirconium articles",
    ],

    # ── Restored 2026-07-13 from the live DB: the 2026-07-10 keyword
    #    backfill (36 nodes) + Synthetic Graphite entries were dual-
    #    written to the DB but the device commit of this file never
    #    landed.  Includes entries for PDF-derived nodes that only
    #    exist after ingest-mcs-pdf — harmless no-ops until then. ──
    ("250510", "Silicon (Anode Grade)"): [
        "silica sand",
        "quartz sand",
        "industrial sand",
    ],
    ("2530", "Lithium"): [
        "spodumene",
        "spodumene concentrate",
        "lithium concentrate",
        "SC6",
        "lepidolite concentrate",
        "petalite",
        "lithium ore",
    ],
    ("253090", "Lithium"): [
        "spodumene",
        "spodumene concentrate",
        "lithium concentrate",
        "SC6",
        "6% spodumene",
    ],
    ("261610", "Silver"): [
        "silver ores",
        "silver concentrate",
        "argentiferous concentrate",
    ],
    ("262030", "Copper"): [
        "copper slag",
        "copper dross",
        "copper residues",
        "secondary copper",
    ],
    ("262040", "Aluminum"): [
        "aluminium dross",
        "aluminum dross",
        "salt slag",
        "aluminium residues",
        "secondary aluminium",
    ],
    ("271312", "Synthetic Graphite"): [
        "calcined petroleum coke",
        "needle coke",
        "petroleum needle coke",
        "CPC",
        "coal tar pitch coke",
        "graphitization feedstock",
    ],
    ("280450", "Tellurium"): [
        "tellurium",
        "tellurium metal",
        "refined tellurium",
    ],
    ("280490", "Selenium"): [
        "selenium",
        "selenium metal",
        "refined selenium",
    ],
    ("2811", "Selenium"): [
        "selenium dioxide",
        "SeO2",
        "selenious acid",
    ],
    ("281122", "Silicon (Anode Grade)"): [
        "silicon dioxide",
        "SiO2",
        "high purity quartz",
        "fumed silica",
        "precipitated silica",
    ],
    ("281129", "Selenium"): [
        "selenium dioxide",
        "SeO2",
    ],
    ("2818", "Aluminum"): [
        "alumina",
        "aluminium oxide",
        "aluminum oxide",
        "Al2O3",
        "smelter grade alumina",
        "calcined alumina",
        "aluminium hydroxide",
    ],
    ("281810", "Aluminum"): [
        "artificial corundum",
        "fused alumina",
        "fused aluminium oxide",
        "corundum",
    ],
    ("281830", "Aluminum"): [
        "aluminium hydroxide",
        "aluminum hydroxide",
        "alumina trihydrate",
        "Al(OH)3",
        "aluminium hydrate",
    ],
    ("282540", "Nickel"): [
        "nickel hydroxide",
        "nickel oxide",
        "Ni(OH)2",
        "NiO",
        "battery grade nickel hydroxide",
    ],
    ("283525", "Phosphate"): [
        "dicalcium phosphate",
        "calcium hydrogenorthophosphate",
        "DCP",
        "calcium hydrogen phosphate",
    ],
    ("283526", "Sodium"): [
        "sodium",
        "na",
        "battery grade",
        "battery-grade",
        "high purity",
        "sodium phosphates, tribasic",
        "trisodium phosphate",
    ],
    ("283531", "Phosphate"): [
        "sodium triphosphate",
        "sodium tripolyphosphate",
        "STPP",
        "Na5P3O10",
    ],
    ("283539", "Phosphate"): [
        "polyphosphates",
        "sodium polyphosphate",
        "phosphate salts",
    ],
    ("284169", "Manganese"): [
        "manganate",
        "permanganate",
        "potassium permanganate",
        "manganese salts of oxometallic acids",
    ],
    ("360690", "Rare Earth Elements"): [
        "ferrocerium",
        "mischmetal",
        "pyrophoric alloys",
        "cerium alloy",
    ],
    ("380110", "Synthetic Graphite"): [
        "synthetic graphite",
        "artificial graphite",
        "synthetic anode material",
        "graphitized anode material",
        "graphitization",
        "synthetic AAM",
    ],
    ("380120", "Natural Graphite"): [
        "colloidal graphite",
        "semi-colloidal graphite",
        "graphite dispersion",
        "graphite preparations colloidal",
    ],
    ("380190", "Natural Graphite"): [
        "graphite preparations",
        "purified graphite",
        "micronized graphite",
        "expandable graphite",
        "graphite paste",
    ],
    ("710691", "Silver"): [
        "silver bullion",
        "silver unwrought",
        "fine silver",
        "silver metal",
    ],
    ("720250", "Chromium"): [
        "ferrosilicochromium",
        "ferro-silico-chromium",
        "FeSiCr",
        "silicochromium",
    ],
    ("720299", "Rare Earth Elements"): [
        "ferro rare earth",
        "rare earth ferroalloy",
        "mischmetal ferroalloy",
        "neodymium iron alloy",
        "NdFe alloy",
    ],
    ("740819", "Copper"): [
        "copper wire",
        "refined copper wire",
        "winding wire",
        "magnet wire",
    ],
    ("750110", "Nickel"): [
        "nickel matte",
        "nickel mattes",
        "high grade nickel matte",
        "low grade nickel matte",
    ],
    ("750120", "Nickel"): [
        "nickel oxide sinter",
        "nickel oxide sinters",
        "intermediate products of nickel metallurgy",
        "nickel intermediates",
        "mixed hydroxide precipitate",
        "MHP",
        "mixed hydroxide cake",
        "MHC",
        "mixed sulphide precipitate",
        "MSP",
    ],
    ("760719", "Aluminum"): [
        "aluminium foil",
        "aluminum foil",
        "battery foil",
        "cathode foil",
        "current collector foil",
    ],
    ("790111", "Zinc"): [
        "special high grade zinc",
        "SHG zinc",
        "zinc 99.99",
        "refined zinc unwrought",
    ],
    ("790112", "Zinc"): [
        "casting grade zinc",
        "high grade zinc",
        "zinc unwrought not alloyed",
    ],
    ("790120", "Zinc"): [
        "zinc alloys",
        "zamak",
        "zinc die casting alloy",
    ],
    ("811231", "Zirconium"): [
        "hafnium",
        "hafnium unwrought",
        "hafnium powder",
    ],
    ("811239", "Zirconium"): [
        "hafnium wrought",
        "hafnium other",
    ],
    ("811249", "Rhenium"): [
        "rhenium",
        "rhenium metal",
        "rhenium wrought",
    ],
    ("850519", "Rare Earth Elements"): [
        "bonded NdFeB",
        "bonded neodymium magnet",
        "ferrite magnet",
        "SmCo magnet",
        "samarium cobalt magnet",
        "permanent magnets other",
    ],
}


def _merged_keywords_for(
    hs_prefix: str, canonical_name: str
) -> list[str]:
    """Return the merged keyword list for a (prefix, canonical) pair.

    Two sources, merged with curated-first ordering so the partner-
    curated keywords win on lookup priority:

      Tier 1  ``_HS_KEYWORDS_BY_MAPPING``      hand-curated, in this file
      Tiers 2-4 ``_HS_KEYWORDS_AUTO``           auto-derived from canonical
                                                name + symbol + stage +
                                                partner CSV descriptions
                                                (see ``seed_hs_keywords_auto.py``)

    Dedup is case-insensitive; first-seen wins.  Returns lowercased
    keywords ready for ``hs_code_material_mappings.keywords`` (a JSONB
    array — case is preserved at the storage layer but the resolver
    lowercases for matching).
    """
    from app.services.ingestion.seed_hs_keywords_auto import _HS_KEYWORDS_AUTO

    seen: set[str] = set()
    out: list[str] = []
    for src in (_HS_KEYWORDS_BY_MAPPING, _HS_KEYWORDS_AUTO):
        for k in src.get((hs_prefix, canonical_name), []) or []:
            kl = (k or "").strip().lower()
            if kl and kl not in seen:
                seen.add(kl)
                out.append(kl)
    return out


def _all_keyword_keys() -> set[tuple[str, str]]:
    """Union of (prefix, canonical) keys present in either keyword source."""
    from app.services.ingestion.seed_hs_keywords_auto import _HS_KEYWORDS_AUTO

    return set(_HS_KEYWORDS_BY_MAPPING.keys()) | set(_HS_KEYWORDS_AUTO.keys())


def _upsert_keywords_for_mappings(
    session: Session,
    *,
    mat_map: dict[str, int],
    force: bool,
) -> dict[str, int]:
    """Apply merged curated+auto keywords to ``hs_code_material_mappings.keywords``.

    Called from ``upsert_hs_mappings`` after mapping rows are inserted/updated.
    Resolves each ``(prefix, canonical_name)`` key to a specific row scoped to
    ``market_scope='global'`` and writes the merged keyword list when:

      * ``force=True``                          → always overwrite
      * the existing ``keywords`` column is NULL or empty → set fresh
      * existing keywords already populated AND ``force=False`` → skip

    Source merge order (see ``_merged_keywords_for``):
      1. ``_HS_KEYWORDS_BY_MAPPING``  — hand-curated (highest priority)
      2. ``_HS_KEYWORDS_AUTO``        — derived from canonical/symbol/stage/CSV

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

    for (hs_prefix, canonical_name) in _all_keyword_keys():
        keywords = _merged_keywords_for(hs_prefix, canonical_name)
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
