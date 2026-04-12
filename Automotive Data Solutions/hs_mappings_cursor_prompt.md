Create an HS code to material mapping seed for the battery-data-intelligence-engine project.

## Context

The `hs_code_material_mappings` table exists in the schema but is empty. It maps 4-digit HS code prefixes to `materials.id` via FK, with a `confidence` score (0–1) reflecting how cleanly a given HS prefix maps to a single material (1.0 = unambiguous single commodity; lower = prefix covers multiple commodities and attribution is partial).

These mappings are what allow the `chemistry_risk.py` scorer to pull geographic concentration data from `trade_flows` for each material. Without this table populated, geographic concentration scoring silently returns zero for all materials.

This is **reference data, not ingested data** — HS nomenclature changes on a 5-year WCO cycle. The right implementation is a migration seed (runs once, establishes baseline) plus an idempotent CLI command for re-seeding if mappings are updated.

The `material_criticality_signals` and `battery_chemistry_materials` tables from migration `002_battery_chemistry.py` will have already expanded the `materials` table to 36+ minerals. This migration depends on those materials existing.

## What to build

### 1. Alembic migration `alembic/versions/003_hs_mappings.py`

Create this migration. It seeds only — no DDL changes (the `hs_code_material_mappings` table already exists from `001_baseline.py`).

#### Migration metadata

```python
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None
```

Docstring:
```
Seed hs_code_material_mappings with 4-digit HS prefix → material mappings.

Covers all 36 critical minerals across three confidence tiers:
  1.0 — unambiguous: HS prefix maps to exactly one material
  0.7–0.9 — moderate: prefix maps to 2–3 materials; this material is primary
  0.5–0.6 — low: prefix covers many commodities; attribution is partial

Sources: USGS MCS 2025, UN Comtrade HS 2022 nomenclature, EU CRM Act 2023 Annex II.

Each INSERT uses a subquery against materials.canonical_name so that material_id
FKs are resolved at migration time without hardcoding integer IDs (which vary
across environments). ON CONFLICT DO NOTHING makes the migration idempotent.
"""
```

#### Mapping data structure

Define a module-level list of tuples **inside the migration file**:

```python
_MAPPINGS: list[tuple[str, str, str, float]] = [
    # (hs_prefix, canonical_name, description, confidence)
    ...
]
```

Then in `upgrade()`, iterate and execute:

```python
for hs_prefix, canonical_name, description, confidence in _MAPPINGS:
    op.execute(sa.text("""
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
    ))
```

In `downgrade()`:
```python
op.execute(sa.text(
    "DELETE FROM hs_code_material_mappings WHERE source IS NULL OR source = 'migration_003'"
))
```

Actually: add a `source VARCHAR(64) DEFAULT 'migration_003'` column value approach is too complex. Instead, `downgrade()` simply truncates the table:
```python
op.execute(sa.text("TRUNCATE TABLE hs_code_material_mappings"))
```

#### Full `_MAPPINGS` list

Use exactly these entries. Notes are in comments; do not include them in the actual tuple values.

```python
_MAPPINGS = [
    # ── Natural Graphite ────────────────────────────────────────────────
    ("2504", "Natural Graphite",
     "Natural graphite — all forms", 1.0),

    # ── Phosphate ───────────────────────────────────────────────────────
    ("2510", "Phosphate",
     "Natural calcium phosphates and phosphatic chalk", 1.0),
    ("2809", "Phosphate",
     "Diphosphorus pentaoxide; phosphoric acid (processed phosphate)", 0.9),

    # ── Fluorite ────────────────────────────────────────────────────────
    ("2529", "Fluorite",
     "Fluorspar (fluorite); feldspar; leucite (fluorite is primary battery-relevant component)", 0.8),

    # ── Magnesium ───────────────────────────────────────────────────────
    ("2519", "Magnesium",
     "Natural magnesium carbonate (magnesite); fused magnesia", 0.7),
    ("8104", "Magnesium",
     "Magnesium and articles thereof — unwrought metal", 1.0),

    # ── Iron ────────────────────────────────────────────────────────────
    ("2601", "Iron",
     "Iron ores and concentrates, including roasted iron pyrites", 1.0),

    # ── Manganese ───────────────────────────────────────────────────────
    ("2602", "Manganese",
     "Manganese ores and concentrates", 1.0),
    ("8111", "Manganese",
     "Manganese and articles thereof — unwrought metal", 1.0),

    # ── Copper ──────────────────────────────────────────────────────────
    ("2603", "Copper",
     "Copper ores and concentrates", 1.0),
    ("7402", "Copper",
     "Copper (unrefined); copper anodes for electrolytic refining", 0.9),
    ("7403", "Copper",
     "Refined copper and copper alloys — unwrought", 1.0),

    # ── Nickel ──────────────────────────────────────────────────────────
    ("2604", "Nickel",
     "Nickel ores and concentrates", 1.0),
    ("7501", "Nickel",
     "Nickel mattes; nickel oxide sinters and other intermediate products", 1.0),
    ("7502", "Nickel",
     "Unwrought nickel", 1.0),

    # ── Cobalt ──────────────────────────────────────────────────────────
    ("2605", "Cobalt",
     "Cobalt ores and concentrates", 1.0),
    ("8105", "Cobalt",
     "Cobalt mattes and other intermediate products of cobalt metallurgy; unwrought cobalt", 1.0),

    # ── Aluminum ────────────────────────────────────────────────────────
    ("2606", "Aluminum",
     "Aluminium ores and concentrates (bauxite)", 1.0),
    ("7601", "Aluminum",
     "Unwrought aluminium", 1.0),

    # ── Tin ─────────────────────────────────────────────────────────────
    ("2609", "Tin",
     "Tin ores and concentrates", 1.0),
    ("8001", "Tin",
     "Unwrought tin", 1.0),

    # ── Chromium ────────────────────────────────────────────────────────
    ("2610", "Chromium",
     "Chromium ores and concentrates (chromite)", 1.0),
    ("8112", "Chromium",
     "Chapter 81 minor metals — chromium is primary battery-relevant component at 8112.21",
     0.7),  # 8112 also covers Ge, Ga, In, Nb, V — lower confidence at 4-digit level

    # ── Tungsten ────────────────────────────────────────────────────────
    ("2611", "Tungsten",
     "Tungsten ores and concentrates", 1.0),
    ("8101", "Tungsten",
     "Tungsten and articles thereof — unwrought metal", 1.0),

    # ── Molybdenum ──────────────────────────────────────────────────────
    ("2613", "Molybdenum",
     "Molybdenum ores and concentrates", 1.0),
    ("8102", "Molybdenum",
     "Molybdenum and articles thereof — unwrought metal", 1.0),
    ("7202", "Molybdenum",
     "Ferroalloys — ferromolybdenum (7202.70) is the primary traded form",
     0.6),  # 7202 covers all ferroalloys; molybdenum is one of many

    # ── Titanium ────────────────────────────────────────────────────────
    ("2614", "Titanium",
     "Titanium ores and concentrates (ilmenite, rutile)", 1.0),
    ("8108", "Titanium",
     "Titanium and articles thereof — unwrought metal, sponge", 1.0),
    ("7202", "Titanium",
     "Ferroalloys — ferrotitanium (7202.91) is a traded intermediate form",
     0.5),  # shared prefix with all ferroalloys; low confidence at 4-digit

    # ── Vanadium ────────────────────────────────────────────────────────
    ("2615", "Vanadium",
     "Niobium, tantalum, vanadium and zirconium ores — vanadium is primary by trade volume",
     0.6),  # shared with Nb, Ta, Zr ores
    ("7202", "Vanadium",
     "Ferroalloys — ferrovanadium (7202.92) is the primary traded form",
     0.7),  # shared prefix; 7202.92 is ferrovanadium specifically

    # ── Niobium ─────────────────────────────────────────────────────────
    ("2615", "Niobium",
     "Niobium, tantalum, vanadium and zirconium ores — niobium-bearing (columbite-tantalite)",
     0.6),  # shared with Va, Ta, Zr
    ("8112", "Niobium",
     "Chapter 81 minor metals — niobium (columbium) unwrought at 8112.92",
     0.6),  # shared with Ge, Ga, In, Cr, V

    # ── Tantalum ────────────────────────────────────────────────────────
    ("2615", "Tantalum",
     "Niobium, tantalum, vanadium and zirconium ores — tantalite component",
     0.6),  # shared with Nb, Va, Zr
    ("8103", "Tantalum",
     "Tantalum and articles thereof — unwrought metal, powders", 1.0),

    # ── Zirconium ───────────────────────────────────────────────────────
    ("2615", "Zirconium",
     "Niobium, tantalum, vanadium and zirconium ores — zircon sands",
     0.6),  # shared
    ("8109", "Zirconium",
     "Zirconium and articles thereof — unwrought metal, sponge", 1.0),

    # ── Silicon ─────────────────────────────────────────────────────────
    ("2804", "Silicon",
     "Hydrogen; rare gases; other non-metals — silicon at 2804.61/2804.69",
     0.7),  # 2804 also covers H, He, N, O, Se, Te at 4-digit level

    # ── Tellurium ───────────────────────────────────────────────────────
    ("2804", "Tellurium",
     "Non-metals — tellurium at 2804.50; shares prefix with Si, Se, S",
     0.6),  # shared chapter with silicon and other non-metals

    # ── Gallium ─────────────────────────────────────────────────────────
    ("8112", "Gallium",
     "Chapter 81 minor metals — gallium unwrought at 8112.21/8112.29",
     0.7),  # shared with Ge, Cr, In, Nb, V at 4-digit level

    # ── Germanium ───────────────────────────────────────────────────────
    ("8112", "Germanium",
     "Chapter 81 minor metals — germanium at 8112.31/8112.39",
     0.7),  # shared

    # ── Indium ──────────────────────────────────────────────────────────
    ("8112", "Indium",
     "Chapter 81 minor metals — indium at 8112.61/8112.69",
     0.7),  # shared

    # ── Antimony ────────────────────────────────────────────────────────
    ("2617", "Antimony",
     "Other ores and concentrates — antimony ores at 2617.10",
     0.8),  # 2617.90 (other ores) reduces confidence slightly at 4-digit
    ("8110", "Antimony",
     "Antimony and articles thereof — unwrought metal", 1.0),

    # ── Lithium ─────────────────────────────────────────────────────────
    ("2825", "Lithium",
     "Lithium oxide and hydroxide — primary processed form for battery electrolyte",
     1.0),  # 2825.20 is lithium hydroxide specifically
    ("2836", "Lithium",
     "Carbonates — lithium carbonate at 2836.91; primary cathode precursor form",
     0.9),  # 2836 covers all carbonates; lithium carbonate is dominant battery trade form
    ("2805", "Lithium",
     "Alkali metals — lithium metal at 2805.12; also covers Na, K, Ca, Sr, Ba, Ra",
     0.7),  # 2805 also contains rare earth metals at 2805.30

    # ── Rare Earth Elements ─────────────────────────────────────────────
    ("2846", "Rare Earth Elements",
     "Compounds of rare earth metals, yttrium, scandium — primary processed REE trade form",
     1.0),
    ("2805", "Rare Earth Elements",
     "Rare earth metals, scandium, yttrium — unwrought at 2805.30; shares prefix with Li",
     0.8),
    ("2617", "Rare Earth Elements",
     "Other ores — some REE-bearing ores (bastnäsite, monazite) trade under 2617.90",
     0.5),  # 2617.90 is a catch-all; low confidence

    # ── Silver ──────────────────────────────────────────────────────────
    ("2616", "Silver",
     "Precious metal ores and concentrates — silver ores at 2616.10", 0.9),
    ("7106", "Silver",
     "Silver (including silver plated with gold or platinum) — unwrought", 1.0),

    # ── Platinum Group Metals ───────────────────────────────────────────
    ("7110", "Platinum Group Metals",
     "Platinum; palladium; rhodium; iridium; osmium; ruthenium — all PGM forms",
     1.0),
    ("2616", "Platinum Group Metals",
     "Precious metal ores — PGM-bearing ores at 2616.90", 0.8),

    # ── Lead ────────────────────────────────────────────────────────────
    ("2607", "Lead",
     "Lead ores and concentrates", 1.0),
    ("7801", "Lead",
     "Unwrought lead", 1.0),

    # ── Zinc ────────────────────────────────────────────────────────────
    ("2608", "Zinc",
     "Zinc ores and concentrates", 1.0),
    ("7901", "Zinc",
     "Unwrought zinc", 1.0),

    # ── Boron ───────────────────────────────────────────────────────────
    ("2528", "Boron",
     "Natural borates and concentrates; natural boric acid", 0.9),
    ("2810", "Boron",
     "Oxides of boron; boric acids (processed boron compounds)", 0.9),

    # ── Cadmium ─────────────────────────────────────────────────────────
    ("8107", "Cadmium",
     "Cadmium and articles thereof — unwrought metal", 1.0),

    # ── Selenium ────────────────────────────────────────────────────────
    ("2804", "Selenium",
     "Non-metals — selenium at 2804.50; shares prefix with Si, S, Te",
     0.6),  # shared

    # ── Arsenic ─────────────────────────────────────────────────────────
    ("2811", "Arsenic",
     "Other inorganic acids and inorganic oxygen compounds — arsenic trioxide at 2811.29",
     0.6),  # 2811 covers many inorganic acids; arsenic is one component
]
```

### 2. `app/services/ingestion/seed_hs_mappings.py` — seed service module

Create this file so the seed logic is importable by both the migration and the CLI command.

```python
"""HS code to material mapping seed data and upsert logic.

HS 2022 nomenclature. Confidence tiers:
  1.0 — unambiguous single-material prefix
  0.7–0.9 — primary material, prefix covers 2–3 commodities
  0.5–0.6 — partial attribution; prefix covers many commodities

Sources:
  - USGS MCS 2025 (per-commodity HS code tables in PDF annexes)
  - UN Comtrade HS 2022 nomenclature (comtradeapi.un.org/data/v1/getDA/HS)
  - EU CRM Act 2023 Annex II (official HS codes per strategic raw material)

Re-run when: WCO updates the HS nomenclature (every 5 years), or when a
new material is added to the materials table and its HS codes are known.
"""
```

Implement:

```python
def upsert_hs_mappings(session: Session) -> dict[str, int]:
    """Upsert all HS code material mappings. Idempotent.

    Returns {"inserted": int, "skipped_existing": int, "skipped_unknown_material": int}
    """
```

Use the same `_MAPPINGS` list (import or copy it here — duplication is acceptable since this is static reference data). For each mapping:
1. Query `materials.id` by `canonical_name`. If not found, increment `skipped_unknown_material` and log a warning.
2. Check if `(hs_code_prefix, material_id)` already exists in `hs_code_material_mappings`. If yes, increment `skipped_existing`.
3. If new, insert and increment `inserted`.

Commit at the end.

### 3. `app/cli.py` — add `seed-hs-mappings` command

```python
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
    from app.services.ingestion.seed_hs_mappings import upsert_hs_mappings
    from app.models.supply import HsCodeMaterialMapping

    s = _session()
    try:
        if force:
            s.query(HsCodeMaterialMapping).delete()
            s.commit()
            typer.echo("Cleared existing HS mappings.")

        result = upsert_hs_mappings(s)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()
```

### 4. `pyproject.toml` — add hatch scripts

```toml
seed-hs-mappings = "bdi-ingest seed-hs-mappings"
seed-hs-mappings-force = "bdi-ingest seed-hs-mappings --force"
```

### 5. Tests — `tests/test_seed_hs_mappings.py`

Create this file.

**Test `upsert_hs_mappings`:**
- Mock a session with 3 materials in the DB: "Natural Graphite", "Nickel", "Cobalt"
- Call `upsert_hs_mappings(session)`
- Verify rows were inserted for the 3 known materials
- Verify `skipped_unknown_material` > 0 (materials in `_MAPPINGS` not in the mock DB)
- Call again — verify `skipped_existing` equals the prior `inserted` count (idempotency)

**Test confidence values:**
- Verify no mapping in `_MAPPINGS` has confidence outside [0.0, 1.0]
- Verify no mapping has a blank description
- Verify all `hs_code_prefix` values are 4-character strings matching `\d{4}`

## Constraints

- The `_MAPPINGS` list is **static reference data** — do not make it configurable or read it from a file. It lives in the source code and is versioned alongside the schema.
- Do not add any new dependencies
- The migration must be idempotent (`ON CONFLICT DO NOTHING`) so it is safe to re-run
- The migration must not fail if a `canonical_name` doesn't exist in `materials` yet — use a `WHERE canonical_name = :name` subquery, which simply returns zero rows and inserts nothing if the material is absent
- Multi-material HS prefixes (e.g. `2615` maps to Vanadium, Niobium, Tantalum, Zirconium) are correct — multiple rows with different `material_id`s for the same `hs_code_prefix` are expected and valid
- Use `structlog` for all logging in the service module; use `typer.echo` only in the CLI command
- Do not update existing rows on re-run without `--force` — the confidence and description values in the DB are authoritative and may have been manually corrected

## Reference: existing ORM model

```python
# app/models/supply.py
class HsCodeMaterialMapping(Base):
    __tablename__ = "hs_code_material_mappings"
    __table_args__ = (
        UniqueConstraint("hs_code_prefix", "material_id", name="uq_hs_material"),
    )
    id: Mapped[int]
    hs_code_prefix: Mapped[str]      # 4-digit string e.g. "2604"
    material_id: Mapped[int]          # FK → materials.id
    description: Mapped[Optional[str]]
    confidence: Mapped[float]         # 0.0–1.0
    created_at: Mapped[datetime]
```

## Important notes (add as comments in the migration docstring)

- **Confidence is not a data quality score** — it reflects the specificity of the HS prefix, not the reliability of the mapping. A 0.6 confidence mapping for `2615 → Vanadium` is correct; it means that trade_flows data under prefix 2615 captures Vanadium + Niobium + Tantalum + Zirconium and cannot be cleanly attributed to any one material at the 4-digit level.
- **When using these mappings in concentration scoring**, filter by `confidence >= 0.8` for high-precision signals. Use all mappings (confidence >= 0.5) only for broad trend analysis where false-positives are acceptable.
- **These are 4-digit prefixes by design** — 6-digit HS codes are more precise but Comtrade data in `trade_flows` is ingested at the 4-digit level (that is what the `ingest-comtrade` command queries). Adding 6-digit codes would give false precision against the actual data in the DB.
- **Chapter 81 (8112) aggregation**: Gallium, Germanium, Indium, Niobium (metal), Chromium, and Vanadium all trade under HS 8112 at the 4-digit level. The individual 6-digit codes are specific (8112.21 = Chromium, 8112.31 = Germanium, 8112.61 = Indium, etc.) but the Comtrade ingestion queries at 4-digit. This is a known limitation — until the Comtrade ingestion is updated to query 6-digit codes for Chapter 81, these mappings will have inherent attribution uncertainty.
