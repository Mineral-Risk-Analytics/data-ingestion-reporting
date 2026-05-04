# Battery Supply Chain Risk Scoring — Reingest Playbook
**Database Version:** Migration chain 001 → 034  
**Created:** 2026-05-02  
**Purpose:** Clear all ingested data (except trade_flows) and rebuild from scratch.

---

## Migration Chain

**Fully applied chain:**
```
001_baseline → 002_battery_chemistry → 003_hs_mappings → 004_supply_relationship_volume
→ 005_seed_review_tables → 006_supply_chain_rollup → 007_company_vehicle_models
→ 008_verified_flags → 009_event_review_status → 010_company_facilities_junction
→ 011_mat_geo_risk_scores → 012_insight_posts → 013_facility_material_links
→ 014_material_production_shares → 015_material_global_risk_scores
→ 016_chem_five_pillars → 017_reg_geo_weights → 018_chem_risk_upsert_key
→ 019_criticality_supply_metrics → 020_countries → 021_drop_legacy_scores
→ 022_hs_expand → 023_hs_production_shares → 024_hs_geo_scores
→ 025_geo_rollup_annotations → 027_trade_flows_hs_mapping
→ 028_hs_event_attribution → 029_facility_material_links_stage
→ 030_commodity_prices_form → 031_country_detection_patterns
→ 034_hs_mapping_stage_sequence_check
```

**Notes:**
- Migration 026 appears to exist in file listing but is not in the chain (skipped/deleted)
- Migration 032 and 033 referenced in design docs are renumbered as 029 and 030
- All 34 migration files are present and should be up-to-date

---

## Foreign Key Dependency Graph

### Safe Delete Order (Children Before Parents)

Tables are sorted by dependency depth: leaf nodes (no outbound FKs) first, then parents.

```
DEPTH 0 (Leaf tables — safe to truncate first):
  - raw_api_payloads           (FK to source_documents)
  - document_chunks            (FK to source_documents)
  - usage_events               (FK to users)
  - analyst_notes              (no outbound FKs in ORM)
  - seed_review_runs           (no outbound FKs in ORM)
  - seed_review_findings       (FK to seed_review_runs)
  - ingestion_runs             (FK to sources)

DEPTH 1 (Junction tables & derived scores — depend on Depth 0):
  - company_vehicle_models     (FK to companies)
  - company_aliases            (FK to companies)
  - company_supply_relationships (FK to companies[buyer/supplier])
  - company_material_exposures (FK to companies, materials)
  - company_facilities         (FK to companies, facilities)
  - company_regulation_exposure (FK to companies, regulations)
  - company_scores             (FK to companies)
  
  - facility_material_links    (FK to facilities, materials, hs_code_material_mappings)
  - risk_event_companies       (FK to risk_events, companies)
  - risk_event_materials       (FK to risk_events, materials)
  - risk_event_regulations     (FK to risk_events, regulations)
  - risk_event_geographies     (FK to risk_events)
  - risk_event_facilities      (FK to risk_events, facilities)
  - risk_event_hs_mappings     (FK to risk_events, hs_code_material_mappings)

DEPTH 2 (Reference data & intermediate mappings):
  - source_documents           (FK to sources, regulations) — KEEP IF PRESERVING regulatory lineage
  - regulation_material_scope  (FK to regulations, materials)
  - regulation_geography_scope (FK to regulations)
  
  - material_criticality_signals (FK to materials)
  - material_production_shares (FK to materials)
  - hs_code_production_shares  (FK to hs_code_material_mappings)
  - commodity_prices           (FK to materials, hs_code_material_mappings)
  - battery_chemistry_materials (FK to battery_chemistries, materials)

DEPTH 3 (Scored output tables — computed from events/materials):
  - hs_code_geography_risk_scores (FK to hs_code_material_mappings)
  - material_geography_risk_scores (FK to materials)
  - material_global_risk_scores    (FK to materials)
  - chemistry_risk_scores          (FK to battery_chemistries)
  - insight_posts                  (FK to materials)
  - report_runs                    (no outbound FK in ORM definition)
  - report_insights                (no outbound FK in ORM definition)
  - supply_chain_contexts          (FK to companies)

DEPTH 4 (Core entity tables — primary keys for all above):
  - risk_events                (FK to source_documents)
  - regulations                (FK to source_documents)
  - hs_code_material_mappings  (FK to materials)
  - facilities                 (no direct outbound FKs to data tables)
  - companies                  (self-referential: parent_company_id)

DEPTH 5 (Reference & identity tables — must survive):
  - materials                  (core dimension)
  - battery_chemistries        (core dimension)
  - countries                  (reference — IRA, CRMA, etc.)
  - sources                    (reference — ingest configuration)

PRESERVED (Do NOT delete):
  - trade_flows                (user requirement — preserve all rows)
```

---

## Seed Scripts (Reference Data)

| Command | Table(s) Populated | Purpose |
|---------|------------------|---------|
| `seed` | countries, materials, battery_chemistries, battery_chemistry_materials, sources, company_aliases (seeded), regulations (EUR-Lex manifest), etc. | Load static reference data if empty |
| `seed-countries` | countries | ISO2 codes, Comtrade codes, major producer/consumer flags, common name aliases, detection patterns |
| `seed-materials` | materials | Li, Co, Ni, Mn, Cu, Mo, etc. with category, HS codes, IRA/CRMA criticality flags |
| `seed-hs-mappings` | hs_code_material_mappings | Map HS prefixes (4/6/8/10 digit) to materials by supply stage and market scope |
| `seed-companies` | companies, company_aliases | OEM & tier-1 supply base (seeded manually, not ingested) |
| `seed-facilities` | facilities | Cell factories, pack plants, recycling (seeded; mining/refining via MRDS) |
| `seed-supply-relationships` | company_supply_relationships | Buyer-supplier pairs (seeded from filings) |
| `seed-material-exposures` | company_material_exposures | Company material exposure estimates (seeded from filings) |
| `seed-regulations` | regulations, regulation_material_scope, regulation_geometry_scope | EUR-Lex manifest (EU Battery Reg, CRMA, etc.) |

---

## Ingest Commands (Data Population)

| Command | Table(s) Populated | Data Source | Notes |
|---------|------------------|------------|-------|
| `ingest-usgs` | material_production_shares, material_criticality_signals, materials (upsert) | MCS2025_World_Data.csv | Mine production shares & HHI. Run annually. Populates denormalized criticality_score cache. |
| `ingest-mrds` | facilities, facility_material_links | GEM Global Mine Tracker | Mining & processing site capacity. Sets mrds_dep_id for dedup. Stage attribution via facility_type. |
| `ingest-comtrade` | trade_flows, hs_code_production_shares (market_scope=global) | UN Comtrade API | 6-digit HS trade data. Queries major producers/consumers. Resolves to material_id & hs_mapping_id at ingest time. |
| `ingest-mcs-pdf` | hs_code_production_shares (both global & us scopes) | USGS MCS PDF text | HS-stage-level production shares & US import source splits. Populates hhi_score cache. |
| `ingest-sec-edgar` | company_material_exposures (upsert), company_supply_relationships (upsert) | SEC EDGAR 10-K/10-Q | Material exposure estimates from supply chain disclosure. |
| `ingest-vpic` | company_vehicle_models, companies (upsert) | NHTSA VPIC | OEM-vehicle mappings for demand signal. |
| `ingest-gleif` | companies (upsert) | GLEIF LEI registry | Legal entity names & LEI numbers. |
| `ingest-worldbank` | (none — reference lookup only) | World Bank Trademap | Used for country validation, not persisted. |
| `build-trade-signals` | risk_events, risk_event_materials, risk_event_hs_mappings, risk_event_companies | internal trade flow analysis | Derives synthetic risk events from extreme trade volatility (spikes, crashes, reversals). |
| `ingest-federal-register` | risk_events, risk_event_materials, risk_event_hs_mappings, risk_event_geographies, risk_event_regulations | Federal Register API | Tariff, trade remedy, and export control events. HS attribution via RiskEventHsMapping. |
| `patch-fr-events` | risk_events (update), risk_event_materials, risk_event_hs_mappings | Federal Register manual curation | Re-parse specific Federal Register documents with corrected HS attribution. |
| `backfill-fr-links` | risk_event_companies (insert) | risk_events + company data | Derive company relevance from event geographies and material exposures. |
| `ingest-gta` | risk_events, risk_event_materials, risk_event_hs_mappings, risk_event_geographies | Global Trade Alert API | Trade distortion events. Country/HS resolution via DB lookups (no hardcoded maps). |
| `ingest-opensanctions` | risk_events, risk_event_companies, risk_event_facilities, risk_event_geographies | OpenSanctions data | Sanctions & restricted entity events. Company/facility entity linking. |
| `ingest-eurlex` | risk_events, risk_event_regulations, risk_event_materials, risk_event_geographies | EUR-Lex API | EU regulatory events (IRA impact, CRMA obligations). |
| `ingest-iea-policy-tracker` | risk_events, risk_event_geographies, risk_event_materials | IEA Policies & Measures DB | Clean energy policy events (subsidies, carbon pricing, etc.). |
| `ingest-iea-reports` | risk_events, risk_event_materials | IEA Technology Reports | Market outlook & supply chain trend events. |

---

## Scoring Commands (Pipeline Order)

**Four-tier scoring stack** (must run in dependency order):

| Tier | Command | Inngest Job ID | Input Table(s) | Output Table | Schedule (UTC) | Notes |
|------|---------|----------------|----------------|---------------|----------------|-------|
| **0** | `rescore-hs-nodes` | `rescore-hs-nodes` | hs_code_production_shares | hs_code_geography_risk_scores | Mon 01:00 | Level-0 node scoring. Must complete before Tier 1. |
| **1** | `rescore-market` | `rescore-market-scores` | materials, risk_events, hs_code_geography_risk_scores, material_production_shares, material_criticality_signals | material_geography_risk_scores | Mon 02:00 | Geo-level risk (0–100). Per-material Inngest steps. Must complete before Tier 2. |
| **2** | `rescore-global-rollups` | `rescore-global-rollups` | material_geography_risk_scores, trade_flows | material_global_risk_scores | Mon 03:00 | Trade-flow-weighted rollup per material. Must complete before Tier 3. |
| **3** | `rescore-chemistry` | `rescore-all-chemistries` | material_global_risk_scores, battery_chemistry_materials | chemistry_risk_scores | Mon 04:00 | Intensity-weighted final score per chemistry. |

**CLI equivalents (ad-hoc / manual runs):**
```bash
bdi-ingest rescore-hs-nodes
bdi-ingest rescore-market --as-of 2026-05-02
bdi-ingest rescore-global-rollups --as-of 2026-05-02
bdi-ingest rescore-chemistry --as-of 2026-05-02
```

**Combined orchestrator (preferred for full rebuild):**
```bash
bdi-ingest full-score --as-of 2026-05-02
# Runs: rescore-hs-nodes → rescore-market → rescore-global-rollups → rescore-chemistry
# Then: rescore-all (pass 1) → rescore-all (pass 2)  [company six-pillar scores]
```

**Note:** `rescore-all` is the **company scoring** command (six-pillar company scores, Level 4).
It is NOT a pipeline alias — do not use it as a substitute for `full-score`.

---

## Playbook Steps

### STEP 1 — Ensure migrations are current
```bash
alembic upgrade head
```
**What it does:** Applies any pending migrations (028 through 034 are likely unapplied on a freshly seeded DB).  
**Affected tables:** All schema changes from migration 001 through 034.

---

### STEP 2 — Clear ingested data (preserve trade_flows & reference data)

**FK deletion order** (children first, parents last):

```sql
-- Depth 0: Leaf tables
TRUNCATE TABLE raw_api_payloads CASCADE;
TRUNCATE TABLE document_chunks CASCADE;
TRUNCATE TABLE usage_events CASCADE;
TRUNCATE TABLE seed_review_findings CASCADE;
TRUNCATE TABLE seed_review_runs CASCADE;
TRUNCATE TABLE ingestion_runs CASCADE;

-- Depth 1: Junction & derived tables
TRUNCATE TABLE company_vehicle_models CASCADE;
TRUNCATE TABLE company_aliases CASCADE;
TRUNCATE TABLE company_supply_relationships CASCADE;
TRUNCATE TABLE company_material_exposures CASCADE;
TRUNCATE TABLE company_facilities CASCADE;
TRUNCATE TABLE company_regulation_exposure CASCADE;
TRUNCATE TABLE company_scores CASCADE;

TRUNCATE TABLE facility_material_links CASCADE;
TRUNCATE TABLE risk_event_companies CASCADE;
TRUNCATE TABLE risk_event_materials CASCADE;
TRUNCATE TABLE risk_event_regulations CASCADE;
TRUNCATE TABLE risk_event_geographies CASCADE;
TRUNCATE TABLE risk_event_facilities CASCADE;
TRUNCATE TABLE risk_event_hs_mappings CASCADE;

-- Depth 2: Reference & intermediate mappings

-- ⚠️  DO NOT use TRUNCATE on source_documents — it would cascade into trade_flows.
--     trade_flows.source_document_id is a non-nullable FK to source_documents (ondelete=CASCADE).
--     Instead, delete only the non-trade-data rows via targeted DELETEs below.

TRUNCATE TABLE regulation_material_scope CASCADE;
TRUNCATE TABLE regulation_geometry_scope CASCADE;

TRUNCATE TABLE material_criticality_signals CASCADE;
TRUNCATE TABLE material_production_shares CASCADE;
TRUNCATE TABLE hs_code_production_shares CASCADE;
TRUNCATE TABLE commodity_prices CASCADE;
TRUNCATE TABLE battery_chemistry_materials CASCADE;

-- Depth 3: Scored output tables
TRUNCATE TABLE hs_code_geography_risk_scores CASCADE;
TRUNCATE TABLE material_geography_risk_scores CASCADE;
TRUNCATE TABLE material_global_risk_scores CASCADE;
TRUNCATE TABLE chemistry_risk_scores CASCADE;
TRUNCATE TABLE insight_posts CASCADE;
TRUNCATE TABLE report_runs CASCADE;
TRUNCATE TABLE report_insights CASCADE;
TRUNCATE TABLE supply_chain_contexts CASCADE;

-- Depth 4: Core event & entity tables
-- risk_events and regulations FK to source_documents; delete them before clearing source docs.
TRUNCATE TABLE risk_events CASCADE;
TRUNCATE TABLE regulations CASCADE;
TRUNCATE TABLE hs_code_material_mappings CASCADE;
TRUNCATE TABLE facilities CASCADE;
TRUNCATE TABLE companies CASCADE;

-- Clear non-trade source documents LAST (after risk_events and regulations are gone)
-- document_type = 'trade_data' rows are written by ingest-comtrade and back trade_flows — keep them.
DELETE FROM source_documents WHERE document_type != 'trade_data';
-- document_chunks are already gone (TRUNCATE at Depth 0), but in case of partial runs:
DELETE FROM document_chunks
WHERE document_id NOT IN (SELECT id FROM source_documents);

-- Depth 5: Reference tables (KEEP)
-- DO NOT TRUNCATE: materials, battery_chemistries, countries, sources, trade_flows, source_documents (trade_data rows)
```

**Alternative (if using CASCADE in Python):**
```python
from app.db.session import get_session_factory
from sqlalchemy import text

session = get_session_factory()()
try:
    for table in [
        "raw_api_payloads", "document_chunks", "usage_events",
        "seed_review_findings", "seed_review_runs", "ingestion_runs",
        "company_vehicle_models", "company_aliases", "company_supply_relationships",
        "company_material_exposures", "company_facilities", "company_regulation_exposure",
        "company_scores",
        "facility_material_links", "risk_event_companies", "risk_event_materials",
        "risk_event_regulations", "risk_event_geographies", "risk_event_facilities",
        "risk_event_hs_mappings",
        "source_documents",  # Comment out if preserving lineage
        "regulation_material_scope", "regulation_geometry_scope",
        "material_criticality_signals", "material_production_shares",
        "hs_code_production_shares", "commodity_prices", "battery_chemistry_materials",
        "hs_code_geography_risk_scores", "material_geography_risk_scores",
        "material_global_risk_scores", "chemistry_risk_scores", "insight_posts",
        "report_runs", "report_insights", "supply_chain_contexts",
        "risk_events", "regulations", "hs_code_material_mappings",
        "facilities", "companies",
    ]:
        session.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
    session.commit()
finally:
    session.close()
```

---

### STEP 3 — Seed reference data

```bash
bdi-ingest seed-countries
bdi-ingest seed-materials
bdi-ingest seed
bdi-ingest seed-hs-mappings
bdi-ingest seed-companies
bdi-ingest seed-facilities
bdi-ingest seed-supply-relationships
bdi-ingest seed-material-exposures
bdi-ingest seed-regulations
```

**What each does:**
- `seed-countries`: ISO2 codes, Comtrade codes, major producer/consumer flags, aliases, detection patterns.
- `seed-materials`: Li, Co, Ni, Mn, Cu, Mo, W, etc. with HS codes and IRA/CRMA flags. Populates denormalized criticality_score (set to NULL initially — will be refreshed by ingest-usgs).
- `seed` (if needed): Loads full reference seed once if empty (countries, materials, sources, regulation manifest). Idempotent.
- `seed-hs-mappings`: HS code prefixes (4/6/8/10 digit) mapped to materials by supply chain stage and market scope.
- `seed-companies`: OEM & tier-1 supply base (manually maintained).
- `seed-facilities`: Cell factories, pack plants, recycling facilities (manually maintained; mining/refining come from MRDS).
- `seed-supply-relationships`: Buyer-supplier pairs (extracted from SEC filings during previous runs; seeded after manual curation).
- `seed-material-exposures`: Company material exposure estimates (extracted from SEC filings; seeded after curation).
- `seed-regulations`: EUR-Lex manifest (EU Battery Reg 2023/1670, CRMA list, etc.).

---

### STEP 4 — Ingest supply chain data

```bash
# A. Material production data (annual, from USGS MCS CSV)
bdi-ingest ingest-usgs --filepath ./path/to/MCS2025_World_Data.csv --force --mcs-year 2025
# Populates: material_production_shares, material_criticality_signals
# Sets denormalized materials.criticality_score cache

# B. Mining & processing facilities (from GEM Global Mine Tracker)
bdi-ingest ingest-mrds
# Populates: facilities (mining, refining), facility_material_links with supply_chain_stage attribution
# Sets mrds_dep_id for dedup on re-ingest

# C. Trade data (from UN Comtrade API)
bdi-ingest ingest-comtrade --months 12 --as-of 2026-04-30
# Populates: trade_flows (6-digit HS trade data), hs_code_production_shares (market_scope=global)
# Resolves material_id & hs_mapping_id at ingest time

# D. HS-stage production shares from MCS PDF (global & US)
bdi-ingest ingest-mcs-pdf --filepath ./path/to/MCS2025.pdf
# Populates: hs_code_production_shares (both global and us scopes)
# Sets hhi_score cache on hs_code_material_mappings

# E. Company material exposures & relationships (from SEC 10-K/10-Q)
bdi-ingest ingest-sec-edgar --years 2024,2025
# Populates: company_material_exposures, company_supply_relationships

# F. OEM-vehicle mappings (from NHTSA VPIC)
bdi-ingest ingest-vpic --year 2025
# Populates: company_vehicle_models

# G. Company LEI data (from GLEIF registry)
bdi-ingest ingest-gleif
# Populates: companies (LEI numbers & legal names)
```

**Dependency order:** A → B → C → D → E → F → G (can run F & G in parallel with others).

---

### STEP 5 — Ingest risk events

```bash
# Trade signals (synthetic — from trade_flows extremes)
bdi-ingest build-trade-signals

# US Federal Register tariffs, trade remedies, export controls
bdi-ingest ingest-federal-register --days 365
# If curation is needed, apply corrections:
bdi-ingest patch-fr-events --start-date 2025-05-01 --end-date 2026-05-02

# Backfill company relevance from event/material/geography overlaps
bdi-ingest backfill-fr-links

# Global Trade Alert (trade distortion events)
bdi-ingest ingest-gta

# OpenSanctions (sanctions & restricted entities)
bdi-ingest ingest-opensanctions

# EU regulations (IRA impact, CRMA obligations)
bdi-ingest ingest-eurlex

# IEA policy events & market outlooks
bdi-ingest ingest-iea-policy-tracker
bdi-ingest ingest-iea-reports
```

**Dependency order:** build-trade-signals → {federal-register, gta, opensanctions, eurlex, iea-*} (can run in parallel).

---

### STEP 6 — Run scoring pipeline

**Full scoring run (all four tiers in order):**
```bash
bdi-ingest full-score --as-of 2026-05-02
```

**Or run each tier manually (for debugging/monitoring):**
```bash
# Tier 0: HS node scores (Level-0 nodes for Material Concentration rollup)
bdi-ingest rescore-hs-nodes --as-of 2026-05-02
# Output: hs_code_geography_risk_scores

# Tier 1: Geo-level market scores (5 pillars)
bdi-ingest rescore-market --as-of 2026-05-02
# Output: material_geography_risk_scores

# Tier 2: Global material rollups (trade-flow weighted)
bdi-ingest rescore-global-rollups --as-of 2026-05-02
# Output: material_global_risk_scores

# Tier 3: Chemistry scores (intensity-weighted)
bdi-ingest rescore-chemistry --as-of 2026-05-02
# Output: chemistry_risk_scores
```

**Automatic weekly job schedule (Inngest):**
- Monday 01:00 UTC: `rescore-hs-nodes-job`
- Monday 02:00 UTC: `rescore-market-scores-job`
- Monday 03:00 UTC: `rescore-global-rollups-job`
- Monday 04:00 UTC: `rescore-chemistries-job`

---

## Summary Table

| Phase | Step | Commands | Input | Output | Duration | Notes |
|-------|------|----------|-------|--------|----------|-------|
| **Setup** | 1 | `alembic upgrade head` | schema | migrations 001–034 applied | < 1 min | Apply pending migrations first |
| **Clear** | 2 | SQL TRUNCATE or Python loop | all ingested tables | Clean slate (trade_flows & refs preserved) | 1–2 min | Run in DB transaction; roll back if error |
| **Seed** | 3 | 9 seed commands | fixture files | Reference data loaded | 1–2 min | Countries, materials, chemistries, regulations, etc. |
| **Ingest: Supply** | 4 | 7 ingest commands | USGS CSV, GEM API, Comtrade API, MCS PDF, SEC EDGAR, NHTSA VPIC, GLEIF | 7 core tables: trade_flows, facility_material_links, hs_code_production_shares, company_material_exposures, company_supply_relationships, company_vehicle_models, companies (updates) | 20–30 min | Most time spent on Comtrade (1000s of queries). Can parallelize 4,5,6,7. |
| **Ingest: Events** | 5 | 7 ingest commands | Federal Register, GTA, OpenSanctions, EUR-Lex, IEA, trade flow analysis | risk_events, risk_event_* junctions (6 tables) | 10–20 min | Fastest: build-trade-signals. Slowest: federal-register. backfill-fr-links derives company links. |
| **Score** | 6 | `full-score` or 4 tier commands | All ingest outputs + criticality signals | hs_code_geography_risk_scores, material_geography_risk_scores, material_global_risk_scores, chemistry_risk_scores | 15–30 min | Inngest per-material steps allow recovery on failure. full-score is sequential wrapper. |
| **Total** | — | 23 commands | — | — | ~60–90 min | Excludes API rate-limit delays. Trade_flows untouched; lineage preserved. |

---

## Validation Checklist

After completing the playbook:

1. **Trade flows untouched:**
   ```sql
   SELECT COUNT(*) FROM trade_flows;  -- Should match pre-ingest count
   ```

2. **Reference data seeded:**
   ```sql
   SELECT COUNT(*) FROM countries;      -- Should be ~200+
   SELECT COUNT(*) FROM materials;      -- Should be ~30–50
   SELECT COUNT(*) FROM battery_chemistries;  -- Should be ~6–8
   SELECT COUNT(*) FROM sources;        -- Should be ~6–8
   ```

3. **Core entities populated:**
   ```sql
   SELECT COUNT(*) FROM companies;      -- Should be 100s after SEC EDGAR + seed
   SELECT COUNT(*) FROM facilities;     -- Should be 100s after MRDS
   SELECT COUNT(*) FROM risk_events;    -- Should be 1000s after all ingesters
   ```

4. **Scores computed:**
   ```sql
   SELECT COUNT(*) FROM material_geography_risk_scores WHERE as_of_date = '2026-05-02';
   SELECT COUNT(*) FROM chemistry_risk_scores WHERE as_of_date = '2026-05-02';
   -- Both should have rows for each material/chemistry
   ```

5. **Schema valid:**
   ```sql
   SELECT constraint_name FROM information_schema.table_constraints
   WHERE constraint_type = 'FOREIGN KEY' AND table_schema = 'public'
   LIMIT 5;  -- Verify FKs exist
   ```

---

## Rollback & Troubleshooting

**If TRUNCATE fails on FK constraints:**
- Ensure `CASCADE` keyword is present or use `ON DELETE CASCADE` in migration.
- Check for missing migrations (028–034 must be applied).
- Verify no active locks: `SELECT * FROM pg_locks;`

**If ingest commands fail:**
- Check API credentials (USGS, Comtrade, SEC EDGAR, etc.).
- Verify fixture file paths (USGS CSV, MCS PDF).
- Review logs: `journalctl -u bdi-ingest` or app logs.
- Retry individual command; do not skip steps.

**If scoring fails on missing data:**
- Confirm all ingest steps 4–5 completed successfully.
- Check `material_production_shares` has rows (ingest-usgs).
- Check `hs_code_production_shares` has rows (ingest-mcs-pdf or Comtrade).
- Check `risk_events` has rows (all event ingesters).

**To preserve lineage while clearing:**
- Comment out `TRUNCATE TABLE source_documents CASCADE;` in Step 2.
- This preserves document references for EU regulations and other structured sources.
- Event data will still be cleared, but the source_document table persists.

