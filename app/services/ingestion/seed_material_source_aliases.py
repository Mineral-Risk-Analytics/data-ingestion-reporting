"""Seed the material_source_aliases reference table.

Single source of truth for "what does external source X call canonical
material Y?".  Replaces four scattered Python dicts:

    mcs2026_parser._CHAPTER_TO_MATERIAL          → source_system='mcs_2026_csv'
    mcs2026_fig10_parser._PRICE_NAME_TO_MATERIAL → source_system='fig10_prices'
    mcs_pdf_parser._MCS_COMMODITY_MAP            → source_system='mcs_pdf'
    usgs_mcs_parser._COMMODITY_CONFIG            → source_system='mcs_2025_csv'

Format
------
Each row in ``_ALIASES`` is one of:

    ("source_system", "source_name", "canonical")
        — alias resolves to a tracked material; primary chapter
          (writes_material_signals=true)

    ("source_system", "source_name", None, "skip_reason")
        — explicit "we considered this and chose not to ingest" decision

    ("source_system", "source_name", "canonical", "secondary", "notes")
        — alias resolves to a tracked material but is a non-primary
          chapter sharing the canonical with a sibling primary chapter
          (e.g. BAUXITE AND ALUMINA → Aluminum, where ALUMINUM is the
          primary).  CLI writes only per-HS-prefix shares for these,
          not material-level signals.  ``writes_material_signals=False``.

The resolver normalizes lookups via ``lower(btrim(source_name))`` so
USGS's frequent trailing-whitespace quirks (``'Boron '``, ``'Iron Ore  '``)
are tolerated without duplicate rows.

When adding a new source
------------------------
1. Pick a stable ``source_system`` slug (snake_case, e.g.
   ``'t4_export_controls'``, ``'comtrade'``).
2. Add one row per commodity name the source publishes — even ones we
   intentionally skip; the explicit-skip rows preserve the audit trail.
3. Re-run ``seed-material-aliases``.

Run ordering on a fresh DB
--------------------------
    alembic upgrade head
    seed-materials              ← seed_materials.py first (creates rows)
    seed-material-aliases       ← this file (FK-references those rows)
    seed-hs-mappings
    ingest-usgs / ingest-mcs-prices / ingest-mcs-pdf
"""

from __future__ import annotations

from typing import Optional, Union

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.supply import Material, MaterialSourceAlias

log = structlog.get_logger(__name__)


# Three forms (see module docstring):
#   3-tuple: primary alias resolving to canonical
#   4-tuple with None canonical: explicit skip with reason
#   5-tuple with non-None canonical: secondary chapter sharing a canonical
#     (writes_material_signals=False — CLI writes only per-HS-prefix shares)
_AliasRow = Union[
    tuple[str, str, str],
    tuple[str, str, None, str],
    tuple[str, str, str, str, str],
]

# ---------------------------------------------------------------------------
# Source: USGS MCS 2026 long-format CSV (mcs_2026_csv)
#
# Chapter heading (uppercase, exact match) → canonical material.
# Ported from mcs2026_parser._CHAPTER_TO_MATERIAL (May 2026).
# ---------------------------------------------------------------------------

_MCS_2026_CSV: list[_AliasRow] = [
    ("mcs_2026_csv", "ALUMINUM",                       "Aluminum"),
    ("mcs_2026_csv", "ANTIMONY",                       "Antimony"),
    # BAUXITE AND ALUMINA chapter routes to the same Aluminum canonical
    # as the ALUMINUM chapter (which is the primary).  The MCS publishes
    # bauxite ore production per country (Australia/Guinea/China dominate)
    # — different geography mix from refined-aluminum production (China
    # dominates).  We capture the bauxite ore data via per-HS-prefix
    # shares on prefix 2606 (already in seed_hs_mappings as Aluminum
    # ore-stage) and skip material-level signal upserts to avoid
    # overwriting the ALUMINUM chapter's signals.
    ("mcs_2026_csv", "BAUXITE AND ALUMINA",            "Aluminum", "secondary",
     "Bauxite is the ore stage of Aluminum (HS 2606). 4 tons of bauxite "
     "→ 1 ton of aluminum.  ALUMINUM chapter is the primary; this chapter "
     "feeds only per-HS-prefix ore-stage country shares."),
    ("mcs_2026_csv", "BISMUTH",                        "Bismuth"),
    ("mcs_2026_csv", "BORON",                          "Boron"),
    ("mcs_2026_csv", "CHROMIUM",                       "Chromium"),
    ("mcs_2026_csv", "COBALT",                         "Cobalt"),
    ("mcs_2026_csv", "COPPER",                         "Copper"),
    ("mcs_2026_csv", "FLUORSPAR",                      "Fluorspar"),
    ("mcs_2026_csv", "GALLIUM",                        "Gallium"),
    ("mcs_2026_csv", "GERMANIUM",                      "Germanium"),
    ("mcs_2026_csv", "GRAPHITE (NATURAL)",             "Natural Graphite"),
    ("mcs_2026_csv", "INDIUM",                         "Indium"),
    ("mcs_2026_csv", "IRON ORE",                       "Iron Ore (LFP Grade)"),
    ("mcs_2026_csv", "LITHIUM",                        "Lithium"),
    ("mcs_2026_csv", "MAGNESIUM COMPOUNDS",            None, "Partner: use MAGNESIUM METAL chapter for the canonical."),
    ("mcs_2026_csv", "MAGNESIUM METAL",                "Magnesium"),
    ("mcs_2026_csv", "MANGANESE",                      "Manganese"),
    ("mcs_2026_csv", "MOLYBDENUM",                     "Molybdenum"),
    ("mcs_2026_csv", "NICKEL",                         "Nickel"),
    ("mcs_2026_csv", "NIOBIUM (COLUMBIUM)",            "Niobium"),
    ("mcs_2026_csv", "PHOSPHATE ROCK",                 "Phosphate (Battery Grade)"),
    ("mcs_2026_csv", "PLATINUM-GROUP METALS",          "Platinum-Group Metals"),
    ("mcs_2026_csv", "RARE EARTHS",                    "Rare Earth Elements"),
    ("mcs_2026_csv", "RHENIUM",                        "Rhenium"),
    ("mcs_2026_csv", "SELENIUM",                       "Selenium"),
    ("mcs_2026_csv", "SILICON",                        "Silicon (Anode Grade)"),
    ("mcs_2026_csv", "SILVER",                         "Silver"),
    ("mcs_2026_csv", "SODA ASH",                       "Sodium"),
    ("mcs_2026_csv", "TANTALUM",                       "Tantalum"),
    ("mcs_2026_csv", "TELLURIUM",                      "Tellurium"),
    ("mcs_2026_csv", "TIN",                            "Tin"),
    ("mcs_2026_csv", "TITANIUM AND TITANIUM DIOXIDE",  "Titanium"),
    ("mcs_2026_csv", "TITANIUM MINERAL CONCENTRATES",  None, "Out of battery scope — covered by TITANIUM AND TITANIUM DIOXIDE chapter."),
    ("mcs_2026_csv", "TUNGSTEN",                       "Tungsten"),
    ("mcs_2026_csv", "VANADIUM",                       "Vanadium"),
    ("mcs_2026_csv", "ZINC",                           "Zinc"),
    ("mcs_2026_csv", "ZIRCONIUM AND HAFNIUM",          "Zirconium"),

    # ── Explicit skips (non-battery-relevant chapters published in the
    #    same MCS file).  Listed so the resolver returns "skipped" rather
    #    than "unknown", and so partner has a visible audit trail of
    #    everything we considered and chose not to ingest.
    ("mcs_2026_csv", "ABRASIVES (MANUFACTURED)",  None, "Industrial abrasives — not battery-relevant."),
    ("mcs_2026_csv", "ARSENIC",                   None, "Toxic semi-metal — not battery-relevant."),
    ("mcs_2026_csv", "ASBESTOS",                  None, "Mineral fibre — not battery-relevant."),
    ("mcs_2026_csv", "BARITE",                    None, "Drilling-mud weighting agent — not battery-relevant."),
    ("mcs_2026_csv", "BERYLLIUM",                 None, "Aerospace alloy — not battery-relevant; no canonical in register."),
    ("mcs_2026_csv", "BROMINE",                   None, "Brominated flame retardants / pharma — not battery-relevant."),
    ("mcs_2026_csv", "CADMIUM",                   None, "NiCd batteries are out of our EV/Li-ion scope."),
    ("mcs_2026_csv", "CEMENT",                    None, "Construction material — not battery-relevant."),
    ("mcs_2026_csv", "CESIUM",                    None, "Atomic clocks / vacuum-tube cathodes — not battery-relevant."),
    ("mcs_2026_csv", "CLAYS",                     None, "Industrial clays — not battery-relevant."),
    ("mcs_2026_csv", "DIAMOND (INDUSTRIAL)",      None, "Cutting/abrasive — not battery-relevant."),
    ("mcs_2026_csv", "DIATOMITE",                 None, "Filtration/insulation — not battery-relevant."),
    ("mcs_2026_csv", "FELDSPAR AND NEPHELINE SYENITE", None, "Glass/ceramics — not battery-relevant."),
    ("mcs_2026_csv", "GARNET (INDUSTRIAL)",       None, "Abrasive — not battery-relevant."),
    ("mcs_2026_csv", "GEMSTONES",                 None, "Jewellery — not battery-relevant."),
    ("mcs_2026_csv", "GOLD",                      None, "Precious metal — minor electronics use; not battery-relevant in our scope."),
    ("mcs_2026_csv", "GYPSUM",                    None, "Wallboard / cement additive — not battery-relevant."),
    ("mcs_2026_csv", "HELIUM AND RARE GASES",     None, "Inert gases — not battery-relevant."),
    ("mcs_2026_csv", "IODINE",                    None, "Pharma / X-ray contrast — not battery-relevant."),
    ("mcs_2026_csv", "IRON AND STEEL",            None, "Bulk steel — Iron Ore (LFP Grade) is the battery-relevant canonical, fed from IRON ORE chapter."),
    ("mcs_2026_csv", "IRON AND STEEL SCRAP",      None, "Bulk recycle — not in scope; battery scrap is tracked separately."),
    ("mcs_2026_csv", "IRON AND STEEL SLAG",       None, "Construction aggregate — not battery-relevant."),
    ("mcs_2026_csv", "IRON OXIDE PIGMENTS",       None, "Coatings/pigments — not battery-relevant."),
    ("mcs_2026_csv", "KYANITE AND RELATED MINERALS", None, "Refractory ceramics — not battery-relevant."),
    ("mcs_2026_csv", "LEAD",                      None, "Lead-acid is out of our EV/Li-ion scope."),
    ("mcs_2026_csv", "LIME",                      None, "Construction / desulfurisation — not battery-relevant."),
    ("mcs_2026_csv", "MERCURY",                   None, "Toxic, banned in batteries — not in scope."),
    ("mcs_2026_csv", "MICA (NATURAL)",            None, "Insulation / cosmetics — not battery-relevant."),
    ("mcs_2026_csv", "NITROGEN (FIXED)—AMMONIA",  None, "Fertilizer / chemical feedstock — not battery-relevant."),
    ("mcs_2026_csv", "PEAT",                      None, "Fuel / horticulture — not battery-relevant."),
    ("mcs_2026_csv", "PERLITE",                   None, "Insulation — not battery-relevant."),
    ("mcs_2026_csv", "POTASH",                    None, "Fertilizer — not battery-relevant."),
    ("mcs_2026_csv", "PUMICE AND PUMICITE",       None, "Construction — not battery-relevant."),
    ("mcs_2026_csv", "QUARTZ (High-purity and Industrial Cultured CRYSTAL)", None, "Optics/electronics — not battery-relevant."),
    ("mcs_2026_csv", "RARE EARTHS (Heavy)",       None, "Sub-category of RARE EARTHS — covered by the bundled chapter alias."),
    ("mcs_2026_csv", "RUBIDIUM",                  None, "Atomic-clock / pharma — not battery-relevant."),
    ("mcs_2026_csv", "SALT",                      None, "Bulk industrial — not battery-relevant."),
    ("mcs_2026_csv", "SAND AND GRAVEL (CONSTRUCTION)", None, "Construction aggregate — not battery-relevant."),
    ("mcs_2026_csv", "SAND AND GRAVEL (INDUSTRIAL)",   None, "Industrial silica — not battery-relevant in this canonical (Si tracked under SILICON)."),
    ("mcs_2026_csv", "SCANDIUM",                  None, "Aerospace alloy — not battery-relevant; no canonical in register."),
    ("mcs_2026_csv", "STONE (CRUSHED)",           None, "Construction — not battery-relevant."),
    ("mcs_2026_csv", "STONE (DIMENSION)",         None, "Construction — not battery-relevant."),
    ("mcs_2026_csv", "STRONTIUM",                 None, "CRT/glass-additive — not battery-relevant in our scope."),
    ("mcs_2026_csv", "SULFUR",                    None, "Bulk industrial — not battery-relevant in this canonical (Li-S batteries out of scope)."),
    ("mcs_2026_csv", "TALC AND PYROPHYLLITE",     None, "Cosmetics / ceramics — not battery-relevant."),
    ("mcs_2026_csv", "THALLIUM",                  None, "Toxic; minor electronics — not battery-relevant."),
    ("mcs_2026_csv", "THORIUM",                   None, "Radioactive; nuclear — not battery-relevant."),
    ("mcs_2026_csv", "VERMICULITE",               None, "Insulation — not battery-relevant."),
    ("mcs_2026_csv", "WOLLASTONITE",              None, "Ceramics / friction — not battery-relevant."),
    ("mcs_2026_csv", "YTTRIUM",                   None, "Tracked via Fig 10 'Yttrium, oxide' which routes to Rare Earth Elements; main-chapter ingest skipped to avoid double-counting."),
    ("mcs_2026_csv", "ZEOLITES (NATURAL)",        None, "Filtration / detergent — not battery-relevant."),
]


# ---------------------------------------------------------------------------
# Source: USGS MCS 2025 wide-format CSV (mcs_2025_csv)
#
# Different commodity-name conventions than 2026.  Ported from
# usgs_mcs_parser._COMMODITY_CONFIG.  USGS's trailing-whitespace quirks
# are kept verbatim (``'Boron '``, ``'Iron Ore  '``) — the resolver
# normalizes them; storing the raw form lets partner cross-check the CSV.
# Note ``'Gemanium'`` (sic) is a USGS typo we have to match.
# ---------------------------------------------------------------------------

_MCS_2025_CSV: list[_AliasRow] = [
    ("mcs_2025_csv", "Aluminum",                       "Aluminum"),
    ("mcs_2025_csv", "Antimony",                       "Antimony"),
    ("mcs_2025_csv", "Bismuth",                        "Bismuth"),
    ("mcs_2025_csv", "Boron ",                         "Boron"),
    ("mcs_2025_csv", "Chromium",                       "Chromium"),
    ("mcs_2025_csv", "Cobalt",                         "Cobalt"),
    ("mcs_2025_csv", "Copper ",                        "Copper"),
    ("mcs_2025_csv", "Fluorspar",                      "Fluorspar"),
    ("mcs_2025_csv", "Gallium ",                       "Gallium"),
    ("mcs_2025_csv", "Gemanium",                       "Germanium"),  # USGS typo, sic
    ("mcs_2025_csv", "Graphite",                       "Natural Graphite"),
    ("mcs_2025_csv", "Indium",                         "Indium"),
    ("mcs_2025_csv", "Iron Ore  ",                     "Iron Ore (LFP Grade)"),
    ("mcs_2025_csv", "Lithium ",                       "Lithium"),
    ("mcs_2025_csv", "Magnesium Compounds",            "Magnesium"),
    ("mcs_2025_csv", "Manganese",                      "Manganese"),
    ("mcs_2025_csv", "Molybdenum ",                    "Molybdenum"),
    ("mcs_2025_csv", "Nickel",                         "Nickel"),
    ("mcs_2025_csv", "Niobium",                        "Niobium"),
    ("mcs_2025_csv", "Phosphate rock ",                "Phosphate (Battery Grade)"),
    ("mcs_2025_csv", "Platinum-Group metals",          "Platinum-Group Metals"),
    ("mcs_2025_csv", "Rare earths",                    "Rare Earth Elements"),
    ("mcs_2025_csv", "Rhenium",                        "Rhenium"),
    ("mcs_2025_csv", "Selenium",                       "Selenium"),
    ("mcs_2025_csv", "Silicon",                        "Silicon (Anode Grade)"),
    ("mcs_2025_csv", "Silver",                         "Silver"),
    ("mcs_2025_csv", "Tantalum",                       "Tantalum"),
    ("mcs_2025_csv", "Tellurium",                      "Tellurium"),
    ("mcs_2025_csv", "Tin",                            "Tin"),
    ("mcs_2025_csv", "Titanium Mineral Concentrates",  "Titanium"),
    ("mcs_2025_csv", "Tungsten ",                      "Tungsten"),
    ("mcs_2025_csv", "Vanadium",                       "Vanadium"),
    ("mcs_2025_csv", "Zinc",                           "Zinc"),
    ("mcs_2025_csv", "Zirconium and Hafnium",          "Zirconium"),
]


# ---------------------------------------------------------------------------
# Source: USGS MCS PDF chapter heading (mcs_pdf)
#
# Same commodities as the 2026 CSV but with the headings as they appear
# in the PDF text.  Some chapters that the CSV breaks out (e.g. BISMUTH,
# RHENIUM, SELENIUM) historically didn't appear in the PDF parser's
# scope; they're added here so a future PDF run can resolve them.
# ---------------------------------------------------------------------------

_MCS_PDF: list[_AliasRow] = [
    ("mcs_pdf", "ALUMINUM",                       "Aluminum"),
    ("mcs_pdf", "ANTIMONY",                       "Antimony"),
    ("mcs_pdf", "BISMUTH",                        "Bismuth"),
    ("mcs_pdf", "BORON",                          "Boron"),
    ("mcs_pdf", "CHROMIUM",                       "Chromium"),
    ("mcs_pdf", "COBALT",                         "Cobalt"),
    ("mcs_pdf", "COPPER",                         "Copper"),
    ("mcs_pdf", "FLUORSPAR",                      "Fluorspar"),
    ("mcs_pdf", "GALLIUM",                        "Gallium"),
    ("mcs_pdf", "GERMANIUM",                      "Germanium"),
    ("mcs_pdf", "GRAPHITE (NATURAL)",             "Natural Graphite"),
    ("mcs_pdf", "INDIUM",                         "Indium"),
    ("mcs_pdf", "IRON ORE",                       "Iron Ore (LFP Grade)"),
    ("mcs_pdf", "LITHIUM",                        "Lithium"),
    ("mcs_pdf", "MAGNESIUM",                      "Magnesium"),
    ("mcs_pdf", "MANGANESE",                      "Manganese"),
    ("mcs_pdf", "MOLYBDENUM",                     "Molybdenum"),
    ("mcs_pdf", "NICKEL",                         "Nickel"),
    ("mcs_pdf", "NIOBIUM",                        "Niobium"),
    ("mcs_pdf", "PHOSPHATE ROCK",                 "Phosphate (Battery Grade)"),
    ("mcs_pdf", "PLATINUM-GROUP METALS",          "Platinum-Group Metals"),
    ("mcs_pdf", "RARE EARTHS",                    "Rare Earth Elements"),
    ("mcs_pdf", "RHENIUM",                        "Rhenium"),
    ("mcs_pdf", "SELENIUM",                       "Selenium"),
    ("mcs_pdf", "SILICON",                        "Silicon (Anode Grade)"),
    ("mcs_pdf", "SILVER",                         "Silver"),
    ("mcs_pdf", "SODA ASH",                       "Sodium"),
    ("mcs_pdf", "TANTALUM",                       "Tantalum"),
    ("mcs_pdf", "TELLURIUM",                      "Tellurium"),
    ("mcs_pdf", "TIN",                            "Tin"),
    ("mcs_pdf", "TITANIUM",                       "Titanium"),
    ("mcs_pdf", "TUNGSTEN",                       "Tungsten"),
    ("mcs_pdf", "VANADIUM",                       "Vanadium"),
    ("mcs_pdf", "ZINC",                           "Zinc"),
    ("mcs_pdf", "ZIRCONIUM AND HAFNIUM",          "Zirconium"),
]


# ---------------------------------------------------------------------------
# Source: USGS MCS 2026 Fig 10 — Price Growth Rates (fig10_prices)
#
# Sub-typed commodity names ("Aluminum, bauxite", "Lithium, battery-grade
# lithium carbonate") aggregated to the canonical material — multiple
# rows for the same canonical end up averaged in the parser.  Skipped
# rows preserve the explicit "we decided not to ingest" decision.
# Ported from mcs2026_fig10_parser._PRICE_NAME_TO_MATERIAL.
# ---------------------------------------------------------------------------

_FIG10_PRICES: list[_AliasRow] = [
    # ── Battery-relevant materials ────────────────────────────────────────
    ("fig10_prices", "Aluminum, bauxite",                          "Aluminum"),
    ("fig10_prices", "Antimony, metal",                            "Antimony"),
    ("fig10_prices", "Bismuth",                                    "Bismuth"),
    ("fig10_prices", "Boron",                                      "Boron"),
    ("fig10_prices", "Chromium, chromite ore",                     "Chromium"),
    ("fig10_prices", "Cobalt (U.S. spot cathode)",                 "Cobalt"),
    ("fig10_prices", "Copper (LME)",                               "Copper"),
    ("fig10_prices", "Fluorspar, acid grade",                      "Fluorspar"),
    ("fig10_prices", "Fluorspar, metallurgical grade",             "Fluorspar"),
    ("fig10_prices", "Gallium",                                    "Gallium"),
    ("fig10_prices", "Germanium, metal",                           "Germanium"),
    ("fig10_prices", "Graphite, natural, flake",                   "Natural Graphite"),
    ("fig10_prices", "Indium (Rotterdam)",                         "Indium"),
    ("fig10_prices", "Lithium, battery-grade lithium carbonate",   "Lithium"),
    ("fig10_prices", "Magnesium, metal (U.S. spot Western)",       "Magnesium"),
    ("fig10_prices", "Manganese",                                  "Manganese"),
    ("fig10_prices", "Nickel",                                     "Nickel"),
    ("fig10_prices", "Niobium, ferroniobium",                      "Niobium"),
    ("fig10_prices", "Phosphate",                                  "Phosphate (Battery Grade)"),
    ("fig10_prices", "Rhenium, metal",                             "Rhenium"),
    ("fig10_prices", "Silicon, metal",                             "Silicon (Anode Grade)"),
    ("fig10_prices", "Silver",                                     "Silver"),
    ("fig10_prices", "Tantalum",                                   "Tantalum"),
    ("fig10_prices", "Tellurium (U.S.)",                           "Tellurium"),
    ("fig10_prices", "Tin (New York dealer)",                      "Tin"),
    ("fig10_prices", "Titanium, sponge",                           "Titanium"),
    ("fig10_prices", "Tungsten, concentrate",                      "Tungsten"),
    ("fig10_prices", "Vanadium, vanadium pentoxide",               "Vanadium"),
    ("fig10_prices", "Zinc (LME)",                                 "Zinc"),
    ("fig10_prices", "Zirconium, sponge",                          "Zirconium"),

    # ── Rare Earth Elements ──────────────────────────────────────────────
    # Individual REE oxide rows.  Routes to specific REE when one exists
    # in our register (Nd / Pr / Dy / Tb); other REEs aggregate into the
    # bundled "Rare Earth Elements" canonical so the partner can still
    # see a group-level price signal.
    ("fig10_prices", "Cerium, oxide",        "Rare Earth Elements"),
    ("fig10_prices", "Dysprosium, oxide",    "Dysprosium"),
    ("fig10_prices", "Erbium, oxide",        "Rare Earth Elements"),
    ("fig10_prices", "Europium, oxide",      "Rare Earth Elements"),
    ("fig10_prices", "Gadolinium, oxide",    "Rare Earth Elements"),
    ("fig10_prices", "Holmium, oxide",       "Rare Earth Elements"),
    ("fig10_prices", "Lanthanum, oxide",     "Rare Earth Elements"),
    ("fig10_prices", "Lutetium, oxide",      "Rare Earth Elements"),
    ("fig10_prices", "Neodymium, oxide",     "Neodymium"),
    ("fig10_prices", "Praseodymium, oxide",  "Praseodymium"),
    ("fig10_prices", "Samarium, oxide",      "Rare Earth Elements"),
    ("fig10_prices", "Terbium, oxide",       "Terbium"),
    ("fig10_prices", "Yttrium, oxide",       "Rare Earth Elements"),
    ("fig10_prices", "Ytterbium, oxide",     "Rare Earth Elements"),

    # ── Explicit skips ────────────────────────────────────────────────────
    ("fig10_prices", "Arsenic, metal",   None, "Not battery-relevant in our scope."),
    ("fig10_prices", "Asbestos",         None, "Not battery-relevant."),
    ("fig10_prices", "Barite",           None, "Not battery-relevant in our scope."),
    ("fig10_prices", "Beryllium",        None, "Not battery-relevant; no canonical in register."),
    ("fig10_prices", "Cesium",           None, "Not battery-relevant in our scope."),
    ("fig10_prices", "Iridium",          None, "Individual PGM; bundled canonical 'Platinum-Group Metals' already too noisy to average individual-PGM prices into."),
    ("fig10_prices", "Lead",             None, "Not battery-relevant in our scope."),
    ("fig10_prices", "Palladium",        None, "Individual PGM (see Iridium reasoning)."),
    ("fig10_prices", "Platinum",         None, "Individual PGM (see Iridium reasoning)."),
    ("fig10_prices", "Potash, all products", None, "Not battery-relevant."),
    ("fig10_prices", "Rhodium",          None, "Individual PGM (see Iridium reasoning)."),
    ("fig10_prices", "Ruthenium",        None, "Individual PGM (see Iridium reasoning)."),
    ("fig10_prices", "Scandium, ingot",  None, "Not battery-relevant; aerospace alloy use-case only."),
]


# ---------------------------------------------------------------------------
# Source: World Bank Pink Sheet monthly prices (worldbank_pinksheet)
#
# Pink Sheet Excel column headers → canonical material.  The header is
# also used verbatim as ``commodity_prices.price_form`` so partner-side
# UI can show "LME copper Grade A cathode" vs "spodumene 6% Li2O" etc.
#
# Per-header HS-prefix attribution lives in
# ``worldbank_pinksheet._HEADER_TO_HS_PREFIX`` (price-specific metadata
# the alias table doesn't model).  A header without an entry there gets
# ``hs_mapping_id=NULL`` — the honest representation when Pink Sheet
# doesn't disclose the form.
# ---------------------------------------------------------------------------

_WORLDBANK_PINKSHEET: list[_AliasRow] = [
    # ── Battery cathode / anode metals ──────────────────────────────────
    ("worldbank_pinksheet", "Cobalt",                            "Cobalt"),
    ("worldbank_pinksheet", "Copper",                            "Copper"),
    ("worldbank_pinksheet", "Nickel",                            "Nickel"),
    ("worldbank_pinksheet", "Aluminum",                          "Aluminum"),
    ("worldbank_pinksheet", "Aluminium",                         "Aluminum"),  # British spelling in some editions
    ("worldbank_pinksheet", "Lithium carbonate, battery grade",  "Lithium"),
    ("worldbank_pinksheet", "Lithium",                           "Lithium"),
    ("worldbank_pinksheet", "Manganese ore",                     "Manganese"),
    ("worldbank_pinksheet", "Manganese",                         "Manganese"),
    ("worldbank_pinksheet", "Graphite",                          "Natural Graphite"),
    ("worldbank_pinksheet", "Natural graphite",                  "Natural Graphite"),

    # ── Industrial metals (battery / EV components, solder) ─────────────
    ("worldbank_pinksheet", "Tin",       "Tin"),
    ("worldbank_pinksheet", "Tin, LME",  "Tin"),  # alternate header in some editions
    ("worldbank_pinksheet", "Zinc",      "Zinc"),

    # Platinum used as the representative benchmark for the PGM bundle.
    # Palladium / Rhodium / Iridium / Ruthenium are skipped below — they
    # share the canonical "Platinum-Group Metals" material_id and the
    # price unique constraint would force one to silently overwrite
    # another (until per-PGM canonicals are added to the materials
    # roster).
    ("worldbank_pinksheet", "Platinum",  "Platinum-Group Metals"),

    # ── Explicit skips ──────────────────────────────────────────────────
    # Partner-curated audit trail of Pink Sheet columns we considered and
    # chose not to ingest.  Listing them here (rather than letting them
    # silently fall through) preserves visibility into what we know we
    # are NOT capturing.
    ("worldbank_pinksheet", "Palladium",        None, "Bundled into Platinum-Group Metals; would collide with Platinum on the unique constraint."),
    ("worldbank_pinksheet", "Rhodium",          None, "Bundled into Platinum-Group Metals (see Palladium reasoning)."),
    ("worldbank_pinksheet", "Lead",             None, "Not battery-relevant in our scope."),
    ("worldbank_pinksheet", "Iron ore, cfr spot", None, "Iron ore is tracked at LFP-grade only via material 'Iron Ore (LFP Grade)'; Pink Sheet's 62% Fe spot is the wrong basis for LFP signal."),
    ("worldbank_pinksheet", "Tungsten",         None, "No reliable Pink Sheet benchmark; stays material-level via USGS only."),
]


# ---------------------------------------------------------------------------
# Source: USGS Mineral Resources Data System CSV (mrds)
#
# Per-row commodity columns ``commod1`` / ``commod2`` / ``commod3``
# in the MRDS dataset.  MRDS publishes commodity values primarily as
# Title-Case full English names ("Lithium", "Copper", "Bauxite"), with
# some filtered subsets / older exports using chemical-symbol
# abbreviations ("Li", "Cu") instead.  Both forms are seeded.
#
# Storage form below uses Title Case for full names + chemical-symbol
# capitalisation for short codes; the resolver normalises via
# ``lower(btrim(source_name))`` so casing/whitespace differences in the
# source CSV don't matter at lookup time.
#
# Ore-stage aliases sharing a canonical (BAUXITE → Aluminum, CHROMITE
# → Chromium, ZIRCON → Zirconium, etc.) are kept as 3-tuple primaries
# rather than 4-tuple secondaries: MRDS writes Facility rows, not
# material-level price/production signals, so the secondary-suppression
# behaviour the MCS chapters use here doesn't apply.
#
# Ported from the legacy ``mrds.MRDS_COMMODITY_MAP`` dict (2026-05-06).
# The dict still exists as a fallback in ``_resolve_commodities`` for
# environments that haven't been re-seeded yet; it can be removed once
# this seed has been applied to all deployments.
# ---------------------------------------------------------------------------

_MRDS: list[_AliasRow] = [
    # ── Lithium ──────────────────────────────────────────────────────────
    ("mrds", "Lithium",                "Lithium"),
    ("mrds", "Lithium carbonate",      "Lithium"),
    ("mrds", "Lithium brine",          "Lithium"),
    ("mrds", "Li",                     "Lithium"),
    ("mrds", "Lith",                   "Lithium"),

    # ── Cobalt ───────────────────────────────────────────────────────────
    ("mrds", "Cobalt",                 "Cobalt"),
    ("mrds", "Co",                     "Cobalt"),
    ("mrds", "Cob",                    "Cobalt"),

    # ── Nickel ───────────────────────────────────────────────────────────
    ("mrds", "Nickel",                 "Nickel"),
    ("mrds", "Ni",                     "Nickel"),
    ("mrds", "Nick",                   "Nickel"),

    # ── Manganese ────────────────────────────────────────────────────────
    ("mrds", "Manganese",              "Manganese"),
    ("mrds", "Mn",                     "Manganese"),
    ("mrds", "Mang",                   "Manganese"),

    # ── Natural Graphite ─────────────────────────────────────────────────
    ("mrds", "Graphite",               "Natural Graphite"),
    ("mrds", "Natural graphite",       "Natural Graphite"),
    ("mrds", "Graphite, natural",      "Natural Graphite"),
    ("mrds", "Carbon, graphite",       "Natural Graphite"),
    ("mrds", "Gr",                     "Natural Graphite"),
    ("mrds", "Graph",                  "Natural Graphite"),

    # ── Copper ───────────────────────────────────────────────────────────
    ("mrds", "Copper",                 "Copper"),
    ("mrds", "Cu",                     "Copper"),
    ("mrds", "Copp",                   "Copper"),

    # ── Aluminum (incl. ore-stage aliases) ───────────────────────────────
    ("mrds", "Aluminum",               "Aluminum"),
    ("mrds", "Aluminium",              "Aluminum"),
    ("mrds", "Bauxite",                "Aluminum"),
    ("mrds", "Alumina",                "Aluminum"),
    ("mrds", "Al",                     "Aluminum"),

    # ── Rare Earth Elements (aggregate canonical) ────────────────────────
    ("mrds", "Rare earths",            "Rare Earth Elements"),
    ("mrds", "Rare-earth elements",    "Rare Earth Elements"),
    ("mrds", "Rare earth elements",    "Rare Earth Elements"),
    ("mrds", "Rare earth metals",      "Rare Earth Elements"),
    ("mrds", "Rare earth",             "Rare Earth Elements"),
    ("mrds", "REE",                    "Rare Earth Elements"),
    ("mrds", "REO",                    "Rare Earth Elements"),
    # Light + heavy REEs without individual canonical rows — bundled.
    ("mrds", "Lanthanum",              "Rare Earth Elements"),
    ("mrds", "Cerium",                 "Rare Earth Elements"),
    ("mrds", "Yttrium",                "Rare Earth Elements"),
    ("mrds", "Europium",               "Rare Earth Elements"),
    ("mrds", "Gadolinium",             "Rare Earth Elements"),
    ("mrds", "Holmium",                "Rare Earth Elements"),
    ("mrds", "Erbium",                 "Rare Earth Elements"),
    ("mrds", "Ytterbium",              "Rare Earth Elements"),
    ("mrds", "Lutetium",               "Rare Earth Elements"),
    ("mrds", "Scandium",               "Rare Earth Elements"),
    ("mrds", "Samarium",               "Rare Earth Elements"),
    ("mrds", "La",                     "Rare Earth Elements"),
    ("mrds", "Ce",                     "Rare Earth Elements"),
    ("mrds", "Y",                      "Rare Earth Elements"),

    # ── Individual magnet REEs (have own canonical rows) ─────────────────
    ("mrds", "Neodymium",              "Neodymium"),
    ("mrds", "Nd",                     "Neodymium"),
    ("mrds", "Praseodymium",           "Praseodymium"),
    ("mrds", "Pr",                     "Praseodymium"),
    ("mrds", "Dysprosium",             "Dysprosium"),
    ("mrds", "Dy",                     "Dysprosium"),
    ("mrds", "Terbium",                "Terbium"),
    ("mrds", "Tb",                     "Terbium"),

    # ── Vanadium ─────────────────────────────────────────────────────────
    ("mrds", "Vanadium",               "Vanadium"),
    ("mrds", "V",                      "Vanadium"),

    # ── Silicon (Anode Grade) ────────────────────────────────────────────
    ("mrds", "Silicon",                "Silicon (Anode Grade)"),
    ("mrds", "Silicon metal",          "Silicon (Anode Grade)"),
    ("mrds", "Si",                     "Silicon (Anode Grade)"),

    # ── Phosphate (Battery Grade) ────────────────────────────────────────
    ("mrds", "Phosphate",              "Phosphate (Battery Grade)"),
    ("mrds", "Phosphorite",            "Phosphate (Battery Grade)"),
    ("mrds", "Phosphate rock",         "Phosphate (Battery Grade)"),
    ("mrds", "P",                      "Phosphate (Battery Grade)"),

    # ── Chromium (incl. chromite ore alias) ──────────────────────────────
    ("mrds", "Chromium",               "Chromium"),
    ("mrds", "Chromite",               "Chromium"),
    ("mrds", "Cr",                     "Chromium"),

    # ── Molybdenum ───────────────────────────────────────────────────────
    ("mrds", "Molybdenum",             "Molybdenum"),
    ("mrds", "Mo",                     "Molybdenum"),

    # ── Niobium (incl. historical "Columbium" US name) ───────────────────
    ("mrds", "Niobium",                "Niobium"),
    ("mrds", "Columbium",              "Niobium"),
    ("mrds", "Niobium (columbium)",    "Niobium"),
    ("mrds", "Nb",                     "Niobium"),

    # ── Tantalum ─────────────────────────────────────────────────────────
    ("mrds", "Tantalum",               "Tantalum"),
    ("mrds", "Ta",                     "Tantalum"),

    # ── Titanium (incl. ilmenite/rutile ore aliases) ─────────────────────
    ("mrds", "Titanium",               "Titanium"),
    ("mrds", "Ilmenite",               "Titanium"),
    ("mrds", "Rutile",                 "Titanium"),
    ("mrds", "Ti",                     "Titanium"),

    # ── Zirconium (incl. zircon ore alias) ───────────────────────────────
    ("mrds", "Zirconium",              "Zirconium"),
    ("mrds", "Zircon",                 "Zirconium"),
    ("mrds", "Zr",                     "Zirconium"),

    # ── Iron Ore (LFP Grade) ─────────────────────────────────────────────
    ("mrds", "Iron",                   "Iron Ore (LFP Grade)"),
    ("mrds", "Iron ore",               "Iron Ore (LFP Grade)"),
    ("mrds", "Fe",                     "Iron Ore (LFP Grade)"),

    # ── Magnesium (incl. magnesite ore alias) ────────────────────────────
    ("mrds", "Magnesium",              "Magnesium"),
    ("mrds", "Magnesite",              "Magnesium"),
    ("mrds", "Mg",                     "Magnesium"),

    # ── Platinum-Group Metals (bundled canonical) ────────────────────────
    ("mrds", "Platinum",               "Platinum-Group Metals"),
    ("mrds", "Palladium",              "Platinum-Group Metals"),
    ("mrds", "Platinum-group metals",  "Platinum-Group Metals"),
    ("mrds", "Platinum group metals",  "Platinum-Group Metals"),
    ("mrds", "PGM",                    "Platinum-Group Metals"),
    ("mrds", "Rhodium",                "Platinum-Group Metals"),
    ("mrds", "Iridium",                "Platinum-Group Metals"),
    ("mrds", "Osmium",                 "Platinum-Group Metals"),
    ("mrds", "Ruthenium",              "Platinum-Group Metals"),
    ("mrds", "Pt",                     "Platinum-Group Metals"),
    ("mrds", "Pd",                     "Platinum-Group Metals"),

    # ── Tungsten (incl. wolframite/scheelite ore aliases) ────────────────
    ("mrds", "Tungsten",               "Tungsten"),
    ("mrds", "Wolframite",             "Tungsten"),
    ("mrds", "Scheelite",              "Tungsten"),
    ("mrds", "W",                      "Tungsten"),

    # ── Tin ──────────────────────────────────────────────────────────────
    ("mrds", "Tin",                    "Tin"),
    ("mrds", "Sn",                     "Tin"),

    # ── Silver ───────────────────────────────────────────────────────────
    ("mrds", "Silver",                 "Silver"),
    ("mrds", "Ag",                     "Silver"),

    # ── Fluorspar ────────────────────────────────────────────────────────
    ("mrds", "Fluorspar",              "Fluorspar"),
    ("mrds", "Fluorite",               "Fluorspar"),
    ("mrds", "Fluorine",               "Fluorspar"),
    ("mrds", "F",                      "Fluorspar"),

    # ── Antimony ─────────────────────────────────────────────────────────
    ("mrds", "Antimony",               "Antimony"),
    ("mrds", "Sb",                     "Antimony"),

    # ── Zinc ─────────────────────────────────────────────────────────────
    ("mrds", "Zinc",                   "Zinc"),
    ("mrds", "Zn",                     "Zinc"),

    # ── Rhenium ──────────────────────────────────────────────────────────
    ("mrds", "Rhenium",                "Rhenium"),

    # ── Gallium ──────────────────────────────────────────────────────────
    ("mrds", "Gallium",                "Gallium"),
    ("mrds", "Ga",                     "Gallium"),

    # ── Germanium ────────────────────────────────────────────────────────
    ("mrds", "Germanium",              "Germanium"),
    ("mrds", "Ge",                     "Germanium"),

    # ── Tellurium ────────────────────────────────────────────────────────
    ("mrds", "Tellurium",              "Tellurium"),
    ("mrds", "Te",                     "Tellurium"),

    # ── Indium ───────────────────────────────────────────────────────────
    ("mrds", "Indium",                 "Indium"),
    ("mrds", "In",                     "Indium"),

    # ── Boron ────────────────────────────────────────────────────────────
    ("mrds", "Boron",                  "Boron"),
    ("mrds", "Borax",                  "Boron"),
    ("mrds", "Borate",                 "Boron"),
    ("mrds", "B",                      "Boron"),

    # ── Selenium ─────────────────────────────────────────────────────────
    ("mrds", "Selenium",               "Selenium"),
    ("mrds", "Se",                     "Selenium"),

    # ── Bismuth ──────────────────────────────────────────────────────────
    ("mrds", "Bismuth",                "Bismuth"),
    ("mrds", "Bi",                     "Bismuth"),

    # ── Explicit skips — flagged for partner review ───────────────────────
    # "Re" was mapped to Rare Earth Elements in the legacy dict, but Re is
    # the chemical symbol for Rhenium.  Marking it as a skip here forces the
    # row through partner review rather than silently routing every "Re"-
    # tagged MRDS row to REE.  To fix: change to ("mrds", "Re", "Rhenium")
    # if MRDS uses bare "Re" for Rhenium, or keep skipped if MRDS uses the
    # full word "Rhenium" exclusively (which is the assumption — no MRDS
    # rows have surfaced bare "Re" so far).
    ("mrds", "Re", None,
     "Ambiguous: Re is the chemical symbol for Rhenium but the legacy MRDS "
     "dict mapped it to Rare Earth Elements. Skipping pending partner review."),
]


# Aggregate of all alias rows.  Order of definition above doesn't matter
# at write time but matters for readability when editing this file.
_ALIASES: list[_AliasRow] = [
    *_MCS_2026_CSV,
    *_MCS_2025_CSV,
    *_MCS_PDF,
    *_FIG10_PRICES,
    *_WORLDBANK_PINKSHEET,
    *_MRDS,
]


# ---------------------------------------------------------------------------
# Seed function — UPSERT semantics on (source_system, lower(btrim(source_name)))
# ---------------------------------------------------------------------------

def seed_material_source_aliases(
    session: Session, *, force_update: bool = False
) -> dict[str, int]:
    """Upsert all alias rows.  Idempotent.

    Behavior:
      - INSERT when (source_system, normalised source_name) is new.
      - SKIP when the row exists and force_update=False.
      - UPDATE the canonical_material_id / is_skipped / skip_reason / notes
        when force_update=True.

    ``force_update`` is the right flag to pass after partner edits to
    the alias lists in this module.

    Returns counts {"inserted", "updated", "skipped_existing",
    "skipped_unknown_canonical"}.
    """
    # Build canonical_name → material_id lookup once.
    mat_rows = session.scalars(select(Material)).all()
    mat_id_by_name: dict[str, int] = {m.canonical_name: m.id for m in mat_rows}

    # Pre-cache existing aliases keyed by (source_system, normalized_name)
    # so each row is a constant-time dict hit instead of a per-row query.
    existing_rows = session.scalars(select(MaterialSourceAlias)).all()
    existing_by_key: dict[tuple[str, str], MaterialSourceAlias] = {
        (r.source_system, r.source_name.strip().lower()): r
        for r in existing_rows
    }

    inserted = updated = skipped_existing = skipped_unknown = 0

    for row in _ALIASES:
        skip_reason: Optional[str] = None
        notes: Optional[str] = None
        writes_material_signals = True

        if len(row) == 3:
            source_system, source_name, canonical_name = row
        elif len(row) == 4:
            # Skip with reason: canonical_name is None.
            source_system, source_name, canonical_name, skip_reason = row
        elif len(row) == 5:
            # Secondary chapter: canonical_name is non-None, role='secondary'.
            source_system, source_name, canonical_name, role, notes = row
            if role != "secondary":
                log.warning("seed_aliases.unknown_role", role=role,
                            source_system=source_system, source_name=source_name)
            writes_material_signals = False
        else:
            log.warning("seed_aliases.malformed_row", row=row)
            continue

        is_skipped = canonical_name is None
        canonical_id: Optional[int] = None
        if not is_skipped:
            canonical_id = mat_id_by_name.get(canonical_name)
            if canonical_id is None:
                log.warning(
                    "seed_aliases.unknown_canonical",
                    source_system=source_system,
                    source_name=source_name,
                    canonical=canonical_name,
                )
                skipped_unknown += 1
                continue

        # Match the migration's unique expression index: lower(btrim(source_name)).
        lookup_key = (source_system, source_name.strip().lower())
        existing = existing_by_key.get(lookup_key)

        if existing is None:
            session.add(
                MaterialSourceAlias(
                    source_system=source_system,
                    source_name=source_name,
                    canonical_material_id=canonical_id,
                    is_skipped=is_skipped,
                    skip_reason=skip_reason,
                    writes_material_signals=writes_material_signals,
                    notes=notes,
                )
            )
            inserted += 1
        elif force_update:
            existing.canonical_material_id = canonical_id
            existing.is_skipped = is_skipped
            existing.skip_reason = skip_reason
            existing.writes_material_signals = writes_material_signals
            existing.notes = notes
            updated += 1
        else:
            skipped_existing += 1

    if inserted or updated:
        session.flush()

    log.info(
        "seed_aliases.done",
        inserted=inserted, updated=updated,
        skipped_existing=skipped_existing,
        skipped_unknown_canonical=skipped_unknown,
    )
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped_existing": skipped_existing,
        "skipped_unknown_canonical": skipped_unknown,
    }


def run_seed(session: Session, *, force_update: bool = False) -> dict[str, int]:
    """Run the seed and commit."""
    stats = seed_material_source_aliases(session, force_update=force_update)
    session.commit()
    return stats


__all__ = [
    "seed_material_source_aliases",
    "run_seed",
    "_ALIASES",
]
