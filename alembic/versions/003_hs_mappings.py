"""Seed hs_code_material_mappings with 4-digit HS prefix → material mappings.

Covers all 36+ critical minerals across three confidence tiers:
  1.0 — unambiguous: HS prefix maps to exactly one material
  0.7–0.9 — moderate: prefix maps to 2–3 materials; this material is primary
  0.5–0.6 — low: prefix covers many commodities; attribution is partial

Sources: USGS MCS 2025, UN Comtrade HS 2022 nomenclature, EU CRM Act 2023 Annex II.

Each INSERT uses a subquery against materials.canonical_name so that material_id
FKs are resolved at migration time without hardcoding integer IDs (which vary
across environments). ON CONFLICT DO NOTHING makes the migration idempotent.

IMPORTANT NOTES
---------------
- Confidence is NOT a data quality score. It reflects the specificity of the
  HS prefix, not the reliability of the mapping. A 0.6 confidence mapping for
  "2615 → Vanadium" is correct — it means trade_flows data under prefix 2615
  captures Vanadium + Niobium + Tantalum + Zirconium and cannot be cleanly
  attributed to one material at the 4-digit level.
- When using these mappings in concentration scoring, filter by confidence >= 0.8
  for high-precision signals. Use confidence >= 0.5 only for broad trend analysis.
- These are 4-digit prefixes by design. Comtrade data in trade_flows is ingested
  at the 4-digit level (what ingest-comtrade queries). Adding 6-digit codes would
  give false precision against the actual data in the DB.
- Chapter 81 (8112) aggregation: Gallium, Germanium, Indium, Niobium (metal),
  Chromium, and Vanadium all trade under HS 8112 at the 4-digit level. Individual
  6-digit codes are specific (8112.21 = Chromium, 8112.31 = Germanium, 8112.61 =
  Indium, etc.) but Comtrade ingestion queries at 4-digit. Until ingest-comtrade
  is updated to query 6-digit codes for Chapter 81, these mappings have inherent
  attribution uncertainty.
- Multi-material HS prefixes (e.g. 2615 → Vanadium, Niobium, Tantalum, Zirconium)
  are correct and expected. Multiple rows with different material_ids for the same
  hs_code_prefix are valid by design.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_hs_mappings"
down_revision: Union[str, None] = "002_battery_chemistry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Mapping data: (hs_prefix, canonical_name, description, confidence)
#
# canonical_name must exactly match materials.canonical_name in the DB.
# Rows where canonical_name doesn't exist in materials are silently skipped
# (the subquery returns zero rows; nothing is inserted).
# ---------------------------------------------------------------------------

_MAPPINGS: list[tuple[str, str, str, float]] = [
    # ── Natural Graphite ──────────────────────────────────────────────────
    ("2504", "Natural Graphite",
     "Natural graphite — all forms", 1.0),

    # ── Phosphate ─────────────────────────────────────────────────────────
    ("2510", "Phosphate (Battery Grade)",
     "Natural calcium phosphates and phosphatic chalk", 1.0),
    ("2809", "Phosphate (Battery Grade)",
     "Diphosphorus pentaoxide; phosphoric acid (processed phosphate)", 0.9),

    # ── Fluorspar ─────────────────────────────────────────────────────────
    ("2529", "Fluorspar",
     "Fluorspar (fluorite); feldspar; leucite (fluorspar is primary battery-relevant component)", 0.8),

    # ── Magnesium ─────────────────────────────────────────────────────────
    ("2519", "Magnesium",
     "Natural magnesium carbonate (magnesite); fused magnesia", 0.7),
    ("8104", "Magnesium",
     "Magnesium and articles thereof — unwrought metal", 1.0),

    # ── Iron Ore (LFP Grade) ──────────────────────────────────────────────
    ("2601", "Iron Ore (LFP Grade)",
     "Iron ores and concentrates, including roasted iron pyrites", 1.0),

    # ── Manganese ─────────────────────────────────────────────────────────
    ("2602", "Manganese",
     "Manganese ores and concentrates", 1.0),
    ("8111", "Manganese",
     "Manganese and articles thereof — unwrought metal", 1.0),

    # ── Copper ────────────────────────────────────────────────────────────
    ("2603", "Copper",
     "Copper ores and concentrates", 1.0),
    ("7402", "Copper",
     "Copper (unrefined); copper anodes for electrolytic refining", 0.9),
    ("7403", "Copper",
     "Refined copper and copper alloys — unwrought", 1.0),

    # ── Nickel ────────────────────────────────────────────────────────────
    ("2604", "Nickel",
     "Nickel ores and concentrates", 1.0),
    ("7501", "Nickel",
     "Nickel mattes; nickel oxide sinters and other intermediate products", 1.0),
    ("7502", "Nickel",
     "Unwrought nickel", 1.0),

    # ── Cobalt ────────────────────────────────────────────────────────────
    ("2605", "Cobalt",
     "Cobalt ores and concentrates", 1.0),
    ("8105", "Cobalt",
     "Cobalt mattes and other intermediate products of cobalt metallurgy; unwrought cobalt", 1.0),

    # ── Aluminum ──────────────────────────────────────────────────────────
    ("2606", "Aluminum",
     "Aluminium ores and concentrates (bauxite)", 1.0),
    ("7601", "Aluminum",
     "Unwrought aluminium", 1.0),

    # ── Tin ───────────────────────────────────────────────────────────────
    ("2609", "Tin",
     "Tin ores and concentrates", 1.0),
    ("8001", "Tin",
     "Unwrought tin", 1.0),

    # ── Chromium ──────────────────────────────────────────────────────────
    ("2610", "Chromium",
     "Chromium ores and concentrates (chromite)", 1.0),
    ("8112", "Chromium",
     "Chapter 81 minor metals — chromium is primary battery-relevant component at 8112.21",
     0.7),  # 8112 also covers Ge, Ga, In, Nb, V — lower confidence at 4-digit level

    # ── Tungsten ──────────────────────────────────────────────────────────
    ("2611", "Tungsten",
     "Tungsten ores and concentrates", 1.0),
    ("8101", "Tungsten",
     "Tungsten and articles thereof — unwrought metal", 1.0),

    # ── Molybdenum ────────────────────────────────────────────────────────
    ("2613", "Molybdenum",
     "Molybdenum ores and concentrates", 1.0),
    ("8102", "Molybdenum",
     "Molybdenum and articles thereof — unwrought metal", 1.0),
    ("7202", "Molybdenum",
     "Ferroalloys — ferromolybdenum (7202.70) is the primary traded form",
     0.6),  # 7202 covers all ferroalloys; molybdenum is one of many

    # ── Titanium ──────────────────────────────────────────────────────────
    ("2614", "Titanium",
     "Titanium ores and concentrates (ilmenite, rutile)", 1.0),
    ("8108", "Titanium",
     "Titanium and articles thereof — unwrought metal, sponge", 1.0),
    ("7202", "Titanium",
     "Ferroalloys — ferrotitanium (7202.91) is a traded intermediate form",
     0.5),  # shared prefix with all ferroalloys; low confidence at 4-digit

    # ── Vanadium ──────────────────────────────────────────────────────────
    ("2615", "Vanadium",
     "Niobium, tantalum, vanadium and zirconium ores — vanadium is primary by trade volume",
     0.6),  # shared with Nb, Ta, Zr ores
    ("7202", "Vanadium",
     "Ferroalloys — ferrovanadium (7202.92) is the primary traded form",
     0.7),  # shared prefix; 7202.92 is ferrovanadium specifically

    # ── Niobium ───────────────────────────────────────────────────────────
    ("2615", "Niobium",
     "Niobium, tantalum, vanadium and zirconium ores — niobium-bearing (columbite-tantalite)",
     0.6),  # shared with Va, Ta, Zr
    ("8112", "Niobium",
     "Chapter 81 minor metals — niobium (columbium) unwrought at 8112.92",
     0.6),  # shared with Ge, Ga, In, Cr, V

    # ── Tantalum ──────────────────────────────────────────────────────────
    ("2615", "Tantalum",
     "Niobium, tantalum, vanadium and zirconium ores — tantalite component",
     0.6),  # shared with Nb, Va, Zr
    ("8103", "Tantalum",
     "Tantalum and articles thereof — unwrought metal, powders", 1.0),

    # ── Zirconium ─────────────────────────────────────────────────────────
    ("2615", "Zirconium",
     "Niobium, tantalum, vanadium and zirconium ores — zircon sands",
     0.6),  # shared
    ("8109", "Zirconium",
     "Zirconium and articles thereof — unwrought metal, sponge", 1.0),

    # ── Silicon (Anode Grade) ─────────────────────────────────────────────
    ("2804", "Silicon (Anode Grade)",
     "Hydrogen; rare gases; other non-metals — silicon at 2804.61/2804.69",
     0.7),  # 2804 also covers H, He, N, O, Se, Te at 4-digit level

    # ── Tellurium ─────────────────────────────────────────────────────────
    ("2804", "Tellurium",
     "Non-metals — tellurium at 2804.50; shares prefix with Si, Se, S",
     0.6),  # shared chapter with silicon and other non-metals

    # ── Gallium ───────────────────────────────────────────────────────────
    ("8112", "Gallium",
     "Chapter 81 minor metals — gallium unwrought at 8112.21/8112.29",
     0.7),  # shared with Ge, Cr, In, Nb, V at 4-digit level

    # ── Germanium ─────────────────────────────────────────────────────────
    ("8112", "Germanium",
     "Chapter 81 minor metals — germanium at 8112.31/8112.39",
     0.7),  # shared

    # ── Indium ────────────────────────────────────────────────────────────
    ("8112", "Indium",
     "Chapter 81 minor metals — indium at 8112.61/8112.69",
     0.7),  # shared

    # ── Antimony ──────────────────────────────────────────────────────────
    ("2617", "Antimony",
     "Other ores and concentrates — antimony ores at 2617.10",
     0.8),  # 2617.90 (other ores) reduces confidence slightly at 4-digit
    ("8110", "Antimony",
     "Antimony and articles thereof — unwrought metal", 1.0),

    # ── Lithium ───────────────────────────────────────────────────────────
    ("2825", "Lithium",
     "Lithium oxide and hydroxide — primary processed form for battery electrolyte",
     1.0),  # 2825.20 is lithium hydroxide specifically
    ("2836", "Lithium",
     "Carbonates — lithium carbonate at 2836.91; primary cathode precursor form",
     0.9),  # 2836 covers all carbonates; lithium carbonate is dominant battery trade form
    ("2805", "Lithium",
     "Alkali metals — lithium metal at 2805.12; also covers Na, K, Ca, Sr, Ba, Ra",
     0.7),  # 2805 also contains rare earth metals at 2805.30

    # ── Rare Earth Elements ───────────────────────────────────────────────
    ("2846", "Rare Earth Elements",
     "Compounds of rare earth metals, yttrium, scandium — primary processed REE trade form",
     1.0),
    ("2805", "Rare Earth Elements",
     "Rare earth metals, scandium, yttrium — unwrought at 2805.30; shares prefix with Li",
     0.8),
    ("2617", "Rare Earth Elements",
     "Other ores — some REE-bearing ores (bastnasite, monazite) trade under 2617.90",
     0.5),  # 2617.90 is a catch-all; low confidence

    # ── Silver ────────────────────────────────────────────────────────────
    ("2616", "Silver",
     "Precious metal ores and concentrates — silver ores at 2616.10", 0.9),
    ("7106", "Silver",
     "Silver (including silver plated with gold or platinum) — unwrought", 1.0),

    # ── Platinum-Group Metals ─────────────────────────────────────────────
    ("7110", "Platinum-Group Metals",
     "Platinum; palladium; rhodium; iridium; osmium; ruthenium — all PGM forms",
     1.0),
    ("2616", "Platinum-Group Metals",
     "Precious metal ores — PGM-bearing ores at 2616.90", 0.8),

    # ── Zinc ──────────────────────────────────────────────────────────────
    ("2608", "Zinc",
     "Zinc ores and concentrates", 1.0),
    ("7901", "Zinc",
     "Unwrought zinc", 1.0),

    # ── Boron ─────────────────────────────────────────────────────────────
    ("2528", "Boron",
     "Natural borates and concentrates; natural boric acid", 0.9),
    ("2810", "Boron",
     "Oxides of boron; boric acids (processed boron compounds)", 0.9),

    # ── Selenium ──────────────────────────────────────────────────────────
    ("2804", "Selenium",
     "Non-metals — selenium at 2804.50; shares prefix with Si, S, Te",
     0.6),  # shared

    # ── Rhenium ───────────────────────────────────────────────────────────
    # Rhenium has no dedicated HS heading; it trades as a by-product of molybdenum
    # roasting and is typically reported under mixed headings. No mapping added.

    # ── Individual motor REEs (from seed_materials.py) ────────────────────
    ("2805", "Neodymium",
     "Rare earth metals — neodymium metal unwrought at 2805.30; shares prefix with Li and other REEs",
     0.6),
    ("2846", "Neodymium",
     "Compounds of rare earth metals — neodymium compounds including NdFeB precursors",
     0.7),
    ("2805", "Praseodymium",
     "Rare earth metals — praseodymium metal unwrought at 2805.30",
     0.6),
    ("2846", "Praseodymium",
     "Compounds of rare earth metals — praseodymium compounds",
     0.7),
    ("2805", "Dysprosium",
     "Rare earth metals — dysprosium metal unwrought at 2805.30",
     0.6),
    ("2846", "Dysprosium",
     "Compounds of rare earth metals — dysprosium compounds",
     0.7),
    ("2805", "Terbium",
     "Rare earth metals — terbium metal unwrought at 2805.30",
     0.6),
    ("2846", "Terbium",
     "Compounds of rare earth metals — terbium compounds",
     0.7),

    # ── Sodium (Na-ion) ───────────────────────────────────────────────────
    ("2836", "Sodium",
     "Carbonates — sodium carbonate (soda ash) at 2836.20; primary Na-ion cathode precursor",
     0.8),
    ("2827", "Sodium",
     "Chlorides and chloride oxides — sodium chloride at 2827.10",
     0.7),
]


def upgrade() -> None:
    for hs_prefix, canonical_name, description, confidence in _MAPPINGS:
        op.execute(
            sa.text("""
                INSERT INTO hs_code_material_mappings
                    (hs_code_prefix, material_id, description, confidence)
                SELECT :hs, id, :desc, :conf
                FROM materials
                WHERE canonical_name = :name
                ON CONFLICT (hs_code_prefix, material_id) DO NOTHING
            """).bindparams(
                hs=hs_prefix,
                name=canonical_name,
                desc=description,
                conf=confidence,
            )
        )


def downgrade() -> None:
    op.execute(sa.text("TRUNCATE TABLE hs_code_material_mappings"))
