# Operations Runbook

> **Last updated: April 2026**

This is the single reference for running, refreshing, and maintaining the BDI data pipeline.  For data model details see [database_architecture.md](database_architecture.md).  For scoring formulas and weights see [scoring.md](scoring.md).

---

## Quick reference: the two orchestrator commands

For most situations you only need two commands.

```bash
# First time (or full rebuild):
bdi-ingest setup-all --usgs-file path/to/MCS2025_World_Data.csv

# After ingesting new data (annual or ad-hoc):
bdi-ingest full-score
```

Both commands print each step as it runs and stop immediately on failure so the error is visible.

---

## Phase 1 — Initial environment setup (run once)

Use `setup-all` to bootstrap a new environment.  Every step inside it is idempotent so it is safe to re-run if something fails midway.

```bash
bdi-ingest setup-all --usgs-file ~/Downloads/MCS2025_World_Data.csv
```

Optional flags:

| Flag | Purpose |
|------|---------|
| `--mcs-year 2025` | Override the MCS publication year (default: 2025) |
| `--skip-gleif` | Skip GLEIF LEI enrichment (useful when offline) |
| `--skip-mrds` | Skip USGS MRDS mine data download (~300 MB) |

**What `setup-all` runs, in order:**

1. `seed-countries` — country reference table: ISO2/ISO3 codes, Comtrade reporter codes, producer/consumer flags, common-name aliases used by GTA and Comtrade ingesters
2. `seed` — source registry, reference aliases
3. `ingest-usgs <file>` — USGS MCS world production data; derives `criticality_score` and `primary_producing_countries` per material
4. `seed-materials` — non-USGS minerals (individual motor REEs, Sodium) + battery chemistry junction data
5. `seed-hs-mappings` — curated HS code → material lookup table (used by Comtrade and GTA ingesters at both ingest and scoring time)
6. `seed-companies` — curated battery supply chain company list
7. `seed-facilities` — known physical facilities per company (cell factories, pack plants, recyclers)
8. `ingest-mrds` — USGS MRDS mine and processing facility data (~300k records); feeds operational scoring pillar
9. `seed-supply-relationships` — upstream/downstream supplier graph
10. `seed-material-exposures` — company × material exposure weights; unblocks Material and Geopolitical scoring pillars
11. `seed-regulations` — curated regulatory seed rows; unblocks Regulatory pillar
12. `ingest-gleif` — LEI enrichment for company entities via GLEIF public API

After `setup-all` completes, run the periodic ingestion commands (Phase 2) then `full-score`.

---

## Phase 2 — Periodic data ingestion

These commands refresh market intelligence data.  Run them before `full-score`.

### Annual (typically January–February after USGS publishes)

```bash
# USGS Mineral Commodity Summaries — download CSV first from pubs.usgs.gov/publication/mcs2025
bdi-ingest ingest-usgs path/to/MCS2025_World_Data.csv --force --mcs-year 2025

# IEA Critical Minerals reports (PDFs downloaded automatically)
bdi-ingest ingest-iea-reports

# Comtrade export flows — previous calendar year
bdi-ingest ingest-comtrade --flow-code X --years 2024

# Comtrade import flows — previous calendar year
bdi-ingest ingest-comtrade --flow-code M --years 2024

# IEA Policy Tracker (requires CSV export from IEA website)
bdi-ingest ingest-iea-policy-tracker path/to/iea_policies.xlsx
```

### Monthly

```bash
# World Bank Pink Sheet commodity prices (auto-gated: skips months already ingested)
bdi-ingest ingest-worldbank
```

### Weekly

```bash
# OpenSanctions — sanctions and PEP entity data
bdi-ingest ingest-opensanctions

# US Federal Register — regulatory notices referencing battery materials
bdi-ingest ingest-federal-register
```

### Quarterly

```bash
# SEC EDGAR — 10-K/10-Q filings for tracked companies
bdi-ingest ingest-sec-edgar
```

### As-needed

```bash
# EUR-Lex — EU regulations and directives
bdi-ingest ingest-eurlex

# GTA — Global Trade Alert (requires a locally downloaded file from globaltradeAlert.org)
bdi-ingest ingest-gta --file path/to/gta_export.csv

# VPIC — NHTSA vehicle production data
bdi-ingest ingest-vpic
```

---

## Phase 3 — Scoring

After ingestion, run `full-score` to recompute all scores.

```bash
bdi-ingest full-score
```

Optional flags:

| Flag | Purpose |
|------|---------|
| `--as-of YYYY-MM-DD` | Score against a historical date (default: today) |
| `--skip-trade-signals` | Skip `build-trade-signals` when trade_flows has not changed |

**What `full-score` runs, in order:**

1. `build-trade-signals` — derives `TRADE_CONCENTRATION` and `EXPORT_DROP` / `IMPORT_DISRUPTION` risk events from `trade_flows`; these feed the Geopolitical pillar
2. `rescore-market` — computes `material_geography_risk_scores` (material × country) from criticality signals, events, price volatility, and trade data
3. `rescore-global-rollups` — rolls up geo scores into `material_global_risk_scores` weighted by production share
4. `rescore-chemistry` — computes `chemistry_risk_scores` weighted by material composition intensity
5. `rescore-all` pass 1 — six-pillar company scores; propagation pillar is null on first pass
6. `rescore-all` pass 2 — re-scores with upstream propagation scores now available

The two-pass design for `rescore-all` is intentional: the supply-chain propagation pillar (15% weight) depends on upstream company scores, which cannot exist until pass 1 has run.

### Scoring a single company (ad-hoc)

```bash
bdi-ingest rescore-company <company_id>
# or via API:
curl -X POST /api/v1/companies/<id>/rescore
```

---

## Cadence summary

| Command | Cadence | Notes |
|---------|---------|-------|
| `ingest-usgs` | Annual (Jan–Feb) | Requires manual CSV download from USGS |
| `ingest-iea-reports` | Annual | PDFs downloaded automatically |
| `ingest-iea-policy-tracker` | Annual | Requires CSV export from IEA website |
| `ingest-comtrade` (X + M) | Annual | Run for previous calendar year |
| `ingest-worldbank` | Monthly | Auto-gated; skips already-ingested months |
| `ingest-opensanctions` | Weekly | |
| `ingest-federal-register` | Weekly | |
| `ingest-sec-edgar` | Quarterly | |
| `ingest-eurlex` | As needed | |
| `ingest-gta` | As needed | Requires local file |
| `ingest-vpic` | As needed | |
| `ingest-mrds` | As needed | Re-run when USGS publishes MRDS updates |
| `full-score` | After any ingestion | Run after each ingestion batch |
| `rescore-market` / `rescore-global-rollups` / `rescore-chemistry` | Scheduled (Inngest) | Mondays 02–04 UTC; also triggered by `full-score` |

---

## Maintenance commands

### Seed staleness review

Identifies seed rows that may be stale. Read-only — no data is modified.

```bash
bdi-ingest review-seeds
bdi-ingest review-seeds --seed-type regulation,company --since 2025-01-01
bdi-ingest review-seeds --dry-run --format both
```

Results are written to `seed_review_runs` and `seed_review_findings` for analyst triage.

### Re-seed countries (after adding a new reporter/consumer country)

```bash
bdi-ingest seed-countries          # idempotent: upserts on iso2 PK
```

Run this whenever you add a new producing or consuming country to the `_COUNTRIES` list in `app/services/ingestion/seed_countries.py`.  After re-seeding, the next `ingest-comtrade` and `ingest-gta` runs will automatically pick up the new country — no code changes needed in those ingesters because they query the `countries` table at runtime.

### Re-seed HS mappings (after adding new materials or HS nomenclature update)

```bash
bdi-ingest seed-hs-mappings          # idempotent: skips existing pairs
bdi-ingest seed-hs-mappings --force  # drops all rows and re-inserts from scratch
```

WCO updates the HS nomenclature every 5 years. Re-seed after any material additions regardless.

### Federal Register event backfill

```bash
bdi-ingest patch-fr-events     # backfill missing severity/material tags on existing FR events
bdi-ingest backfill-fr-links   # link FR events to companies via entity resolution
```

### Inspect current scores

```bash
bdi-ingest show-scores                         # all companies
bdi-ingest show-scores --company-id <id>       # single company
bdi-ingest show-scores --format json           # machine-readable
```

---

## Scheduling (Inngest)

The following scoring jobs run automatically via Inngest:

| Job | Schedule | Function |
|-----|----------|----------|
| Market pair scores | Monday 02:00 UTC | `score_all_active_materials` |
| Material global rollups | Monday 03:00 UTC | `score_all_material_global_rollups` |
| Chemistry rollups | Monday 04:00 UTC | `score_all_chemistries` |

Job functions live in `app/tasks/scoring_jobs.py`.  The scheduler is registered via `/api/inngest`.

Company scoring is **not scheduled** — it is on-demand only via `rescore-all` or `POST /api/v1/companies/{id}/rescore`.

Local dev Inngest setup:

```bash
# Terminal 1
uvicorn app.main:app --reload

# Terminal 2
npx --ignore-scripts=false inngest-cli@latest dev \
  -u http://127.0.0.1:8000/api/inngest --no-discovery
```

---

## Pipeline architecture notes

### Two ingestion paths

1. **Pipeline-based** (`IngestionPipeline`) — event-oriented sources that produce `risk_events` and `source_documents`: Federal Register, Census Trade, SEC EDGAR, news stub. Triggered via `bdi-ingest ingest <alias>` or `POST /api/v1/sources/{id}/ingest`.

2. **CLI-based** — reference data and sources not wired into the pipeline: USGS, World Bank, OpenSanctions, Comtrade, EUR-Lex, GTA, seeds.

### HS code resolution

The `hs_code_material_mappings` table is the single source of truth for HS → material mapping at both ingest time (Comtrade, GTA) and scoring time (`chemistry_risk._geo_concentration`).  The `materials.hs_codes` JSONB column is display-only.

Resolution order at ingest time:
1. Exact 6-digit match — if a single winner, use it; if tied at equal confidence, return null
2. 4-digit prefix fallback — if no 6-digit match, find all mappings whose prefix matches the leading 4 digits; apply same confidence-winner logic

### Company-event linking gate

After inserting a `risk_events` row the pipeline can link companies via entity resolution.  This is controlled by:

```python
# app/services/ingestion/feature_flags.py
LINK_EVENTS_TO_COMPANIES = False  # default
```

With the flag off, market scoring functions normally (it uses material/geography tags).  Company scoring loses the direct event-relevance multiplier signal but still runs.

---

## Related docs

- [Data sources](data-sources.md)
- [Scoring](scoring.md)
- [Overview](overview.md)
- [Database architecture](database_architecture.md)
