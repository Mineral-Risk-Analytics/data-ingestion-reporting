# Data Sources → Scoring Inputs (EV battery domain)

> **Last updated: May 2026**

This page is the canonical map of **(data source → tables/fields → scoring inputs → pillars → scoring layers)**.

If you only remember one thing: **data sources do not “feed a pillar” directly** — they feed *specific* scoring inputs that are combined by pillar math in [`app/services/scoring/`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/).

## Materials register & alias resolution (May 2026 refactor)

Canonical material names are now seeded register data, not parser-derived. All ingest parsers emit raw `source_name` records and the CLI resolves them to canonical materials via the `material_source_aliases` table. Three concepts that follow from this:

- **Material register** is the 39-row list in `seed_materials.py` populated into the `materials` table by `seed-materials`. Static facts live here (canonical_name, category, symbol_or_code, hs_codes, IRA/CRMA flags). Dynamic signals (criticality_score, HHI, price trends) are written by ingest, not seeded.
- **Source aliases** in `material_source_aliases` translate external commodity names to canonical materials per source (`mcs_2026_csv`, `mcs_2025_csv`, `mcs_pdf`, `fig10_prices`). 215 rows seeded by `seed-material-aliases`. Includes explicit `is_skipped=True` rows for non-battery commodities (LEAD, ASBESTOS, individual PGM prices) so partner sees an audit trail of considered-and-rejected names.
- **Secondary chapters** (`writes_material_signals=False`) are aliases that share a canonical with a sibling primary chapter — currently only `BAUXITE AND ALUMINA → Aluminum`. The CLI writes their per-HS-prefix country shares (bauxite ore at HS 2606) but skips material-level upserts that would collide with the primary `ALUMINUM` chapter's signals.

Run order on a fresh DB: `alembic upgrade head` → `seed-materials` → `seed-material-aliases` → `seed-hs-mappings` → `ingest-usgs` → `ingest-mcs-prices` → `ingest-mcs-pdf`.

---

## Scheduled ingestion jobs (Inngest)

In addition to manual CLI/API ingestion, the backend registers cron-triggered ingestion jobs in
[`app/tasks/ingestion_jobs.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/tasks/ingestion_jobs.py).

### Weekly (Sunday night UTC)

| Function (fn_id) | Cron (UTC) | What it ingests |
|---|---|---|
| `ingest-opensanctions-weekly` | `0 22 * * SUN` | OpenSanctions aliases/sanctions entities |
| `ingest-federal-register-weekly` | `30 22 * * SUN` | Federal Register documents → regulations + regulatory events |
| `ingest-worldbank-weekly` | `0 23 * * SUN` | World Bank Pink Sheet commodity prices |

Notes:
- OpenSanctions and Pink Sheet ingestors have **min-interval gates** to avoid redundant downloads on every weekly run.

### Quarterly (1st of Jan/Apr/Jul/Oct)

| Function (fn_id) | Cron (UTC) | What it ingests |
|---|---|---|
| `ingest-comtrade-quarterly` | `0 0 1 1,4,7,10 *` | UN Comtrade trade flows |
| `ingest-eurlex-quarterly` | `30 0 1 1,4,7,10 *` | EUR-Lex regulation updates + summary backfill |
| `ingest-sec-edgar-quarterly` | `0 1 1 1,4,7,10 *` | SEC EDGAR filings (via pipeline source) |

### Quarterly reminder (MRDS)

| Function (fn_id) | Cron (UTC) | What it does |
|---|---|---|
| `mrds-refresh-reminder-quarterly` | `0 9 15 1,4,7,10 *` | Logs a structured reminder to re-run `bdi-ingest ingest-mrds` |

---

## Scoring layers (where inputs are consumed)

The platform computes risk at four levels, each consuming a different subset of inputs:

1. **Market pair score**: **material × geography** → five pillars → overall  
   Output: `material_geography_risk_scores` (scored by `market_aggregator.py`).
2. **Material rollup score**: **material × {geographies}** → roll up five pillars → overall  
   Output: `material_global_risk_scores` (scored by `global_rollup.py`).
3. **Chemistry rollup score**: **chemistry × {materials}** → intensity-weighted five pillars → composite  
   Output: `chemistry_risk_scores` (methodology `2.0` in `chemistry_risk.py`).
4. **Company score**: **company** → six pillars → overall (propagation optional)  
   Output: `company_scores` (scored by `orchestrator.py` + `supplier_risk.py`).

See [Scoring](scoring.md) for formulas/weights; this page focuses on **where the inputs come from**.

---

## Pillar input map (what each pillar needs, and what sources supply it)

### Material Concentration pillar inputs

The pure scorer is `score_material_exposure(criticality, concentration, trade_volatility)` in
[`app/services/scoring/material_risk.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/material_risk.py).

| Input | What it means | Primary tables / fields | Primary sources |
|---|---|---|---|
| `criticality` | intrinsic supply risk of the material | `material_criticality_signals.criticality_score` (preferred) else `materials.criticality_score` fallback | USGS MCS (live), EU CRMA (planned), IEA (planned), manual seeds |
| `concentration` | concentration of supply across countries/suppliers | market: derived from HCG + criticality signal; company: derived from `company_material_exposures` (+ chemistry reweight) | USGS MCS (live), seeded exposures (live) |
| `trade_volatility` | volatility proxy from prices and/or trade/event evidence | market: price volatility + events; company: evidence aggregator + trade signals | World Bank Pink Sheet (live), Census trade (live), Comtrade (live), GTA (live) |
| `reserve_hhi_score` | forward-looking concentration over country reserves (15% of stage rollup) | `material_criticality_signals.reserve_hhi_score` | USGS MCS (live, 2026 main) |
| `reserve_life_index` | scarcity proxy = world reserves / world annual production (years; 30% of scarcity sub-score) | `material_criticality_signals.reserve_life_index` | USGS MCS (live, 2026 main) |
| `production_yoy_pct` | supply contraction signal (YoY change in world production) | `material_criticality_signals.production_yoy_pct` | USGS MCS (live, 2026 main) |
| `capacity_utilization` | tight-market signal (production / capacity, 10% of stage rollup) | `material_criticality_signals.capacity_utilization` | USGS MCS Salient table; **not published in 2026 CSV — currently always NULL**, USGS may add later |

### Geopolitical / Trade pillar inputs

The pure scorer is `score_geopolitical_trade(country_concentration, export_restriction_exposure, tariff_exposure)` in
[`app/services/scoring/geopolitical_risk.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/geopolitical_risk.py).

| Input | What it means | Primary tables / fields | Primary sources |
|---|---|---|---|
| `country_concentration` | dependency on high-concentration geographies | company: `company_material_exposures.source_geography` + `facilities.country`; market: geography itself + HCG flag | seeded exposures (live), facilities seeds (live) |
| `export_restriction_exposure` | export bans/licensing/control exposure | `risk_events` tagged `GEOPOLITICAL_TRADE` and classified as `EXPORT_RESTRICTION` subtype | GTA (live), Federal Register (live), Census trade (live) |
| `tariff_exposure` | tariff and trade-policy burden | `risk_events` tagged `GEOPOLITICAL_TRADE` and classified as tariff/trade-policy subtypes | Census trade (live), Federal Register (live), GTA (live) |

### Regulatory & Compliance pillar inputs

The pure scorer is `score_regulatory_profile(top_event_impacts, active_obligations, policy_proximity_adjustment)` in
[`app/services/scoring/regulatory_risk.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/regulatory_risk.py).

| Input | What it means | Primary tables / fields | Primary sources |
|---|---|---|---|
| `top_event_impacts` | regulatory events (severity/confidence/recency/relevance) aggregated | `risk_events` tagged `REGULATORY_COMPLIANCE` → impacts via `event_impact.py` | Federal Register (live), EUR-Lex (live), SEC/news stubs (limited) |
| `active_obligations` | hard obligations with uplift points and per-entity weight | company: `company_regulation_exposure` → `(obligation_key, weight)`; market: `regulations.geography_compliance_weights` (migration 017) | seeded/ingested regulations (live) |
| `policy_proximity_adjustment` | near-term enforcement uplift | derived at aggregation time from event/regulation effective dates | Federal Register (live), EUR-Lex (live) |

### Operational pillar inputs

Operational scoring is computed in the company orchestrator (`_score_operational` in
[`app/services/scoring/orchestrator.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/orchestrator.py)) and mirrored for the market layer.

| Input | What it means | Primary tables / fields | Primary sources |
|---|---|---|---|
| `structural_dependency` | baseline operational fragility (capacity/single-source/capex dependency) | company facilities roster + operational event presence | facility seeds (live), SEC/news events (limited) |
| `weighted_event_impacts` | operational disruptions aggregated | `risk_events` tagged `OPERATIONAL` | SEC EDGAR (live), news (stub), Federal Register (some) |

### Financial Pressure pillar inputs

The pure scorer is `score_financial_pressure(base_filing_signal, leverage_warning_bonus, liquidity_stress_bonus, filing_count)` in
[`app/services/scoring/financial_pressure.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/financial_pressure.py).

| Layer | Primary input source | Tables / fields | Notes |
|---|---|---|---|
| Company | company filings → risk events & derived signals | SEC-derived `risk_events` tagged `FINANCIAL_PRESSURE` + evidence aggregation | sparse by design; sparse-evidence cap applies |
| Market | commodity prices + producer stress events | `commodity_prices` (Pink Sheet) + `risk_events` tagged `FINANCIAL_PRESSURE` | market_aggregator uses price CV and spike/crash thresholds |

### Supply-Chain Propagation pillar inputs (company-only)

Propagation is a sixth pillar scored by `propagation_risk.py` and persisted into `company_scores`.

| Input | What it means | Tables / fields | Primary sources |
|---|---|---|---|
| Supplier graph | buyer → supplier edges | `company_supply_relationships` (especially `volume_share_pct`) | seeded supply relationships (live) |
| Supplier scores | risk signal from upstream suppliers | suppliers’ latest `company_scores.overall_risk_score` | requires prior rescores |

---

## Layer dependency map (what must exist for each layer to work)

### Market pair scoring (material × geography)

Scored by [`app/services/scoring/market_aggregator.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/market_aggregator.py).

Minimum viable inputs:
- **Criticality**: `material_criticality_signals` (USGS MCS today) or `materials.criticality_score` fallback.
- **Events**: `risk_events` plus **material/geography tags** (`risk_event_materials`, `risk_event_geographies`) so events can be scoped to the pair.
- **Prices**: `commodity_prices` improves market-financial-pressure; missing prices degrades that pillar.
- **Regulation scopes**: `regulation_material_scope`, `regulation_geography_scope` plus `regulations.geography_compliance_weights`.

### Material rollup (global material view)

Scored by [`app/services/scoring/global_rollup.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/global_rollup.py).

It rolls up a material’s `material_geography_risk_scores` across geographies using (in order):
1. **Trade flow export value**: `trade_flows` (preferred)  
2. **Production shares**: `material_production_shares` (USGS-derived)  
3. **Equal weights** fallback (logged at WARNING)

### Chemistry rollup (methodology 2.0)

Computed by `score_chemistry_from_rollup` in
[`app/services/scoring/chemistry_risk.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/chemistry_risk.py).

Dependencies:
- `battery_chemistry_materials` (seeded chemistries + intensities)
- `material_global_risk_scores` for each constituent material as of date  
  Missing global scores cause that material to be excluded from the intensity-weighted average (recorded in `metadata_json`).

### Company scoring (six pillars, v3.0)

Computed by `rescore_company` in
[`app/services/scoring/orchestrator.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/orchestrator.py).

Dependencies:
- `company_material_exposures`, `facilities`, `company_supply_relationships`, `company_regulation_exposure`
- `risk_events` selected by `RiskCategory` tag
- **Important:** company-linked event evidence (`risk_event_companies`) is currently **suppressed by default** (feature flag `LINK_EVENTS_TO_COMPANIES=False`). This does **not** affect market scoring (which relies on material/geography event tags).

---

## Source catalog (current + planned) — shortened appendix

This appendix lists each source once. Detailed “how it scores” lives in the pillar sections above.

### Current sources (live)

| Source | How you run it | Primary tables written | Primary scoring role |
|---|---|---|---|
| USGS MCS — main CSV (2025 wide / 2026 long) | `bdi-ingest ingest-usgs <file>` | `material_criticality_signals` (criticality, HHI, reserve_hhi_score, reserve_life_index, production_yoy_pct, capacity_utilization, metadata_json[us_net_import_reliance, us_apparent_consumption]); `material_production_shares`; `hs_code_production_shares` (`market_scope='global'` for sub-type splits + secondary chapters; `market_scope='us'` for 2026 Import Sources); `materials.criticality_score`+`price_unit` cache back-sync | baseline criticality + production shares + reserve metrics + capacity utilization for rollups; tariff-adjacent HHI inputs for HS-node scoring |
| USGS MCS — Fig 10 prices CSV (2026 only) | `bdi-ingest ingest-mcs-prices <file>` | `material_criticality_signals.price_yoy_pct`, `price_cagr_5yr_pct`, `metadata_json[fig10_source_rows]` | financial-pressure (NOT YET CONSUMED — see "Stored but unconsumed" below) |
| USGS MCS — annual PDF (tariff schedules) | `bdi-ingest ingest-mcs-pdf <file>` | `hs_code_material_mappings` (10-digit US HTS + derived 6-digit global); `hs_code_production_shares` (global production leaders + US import sources) | source-of-truth for granular HS codes (only the PDF carries 6/8/10-digit codes) and per-HS-node country shares feeding HS-node scorer |
| World Bank Pink Sheet | `bdi-ingest ingest-worldbank` | `commodity_prices` | market financial-pressure (price volatility), contributes to trade volatility signals |
| UN Comtrade | `bdi-ingest ingest-comtrade` | `trade_flows`, `source_documents` | rollup weights (trade value), geo concentration inputs |
| U.S. Census Trade | `bdi-ingest ingest census-trade` | `trade_flows`, `risk_events`, `source_documents` | geopolitical + material trade-event evidence; supplements trade coverage |
| Global Trade Alert (GTA) | `bdi-ingest ingest-gta` | `risk_events`, `risk_event_materials`, `risk_event_geographies` | export restriction evidence (GEOPOLITICAL_TRADE) at market + company layers |
| EUR-Lex (EU regulations) | `bdi-ingest ingest-eurlex` | `regulations`, `regulation_*_scope` | regulatory scope and obligations |
| Federal Register | `bdi-ingest ingest federal-register` | `regulations`, `risk_events`, `source_documents`, `document_chunks` | U.S. regulatory evidence + events (company linking gated) |
| SEC EDGAR | `bdi-ingest ingest sec-edgar` | `risk_events`, `source_documents` | financial-pressure and operational evidence |
| OpenSanctions | `bdi-ingest ingest-opensanctions` | `companies`, `company_aliases`, `source_documents` | entity/alias enrichment (scoring contribution is indirect) |
| Manual seed — materials register | `bdi-ingest seed-materials` | `materials` (39 rows), `battery_chemistry_materials` | canonical material register; chemistry composition foundation |
| Manual seed — material source aliases | `bdi-ingest seed-material-aliases` | `material_source_aliases` (215 rows) | translates parser source_name strings to canonical materials at ingest time |
| Manual seed — HS mappings | `bdi-ingest seed-hs-mappings` | `hs_code_material_mappings` (286 rows with `supply_chain_stage`) | bridges HS codes → materials AND supplies stage assignments for HS-node scoring |
| News (stub) | `bdi-ingest ingest news` | `risk_events`, `source_documents`, `document_chunks` | operational evidence placeholder |

### Stored but unconsumed (May 2026)

Three signal categories the parsers now write but no scorer reads yet. Closing these gaps is the next scoring-engine work item:

| Signal | Where it's written | Who should consume it | Effort |
|---|---|---|---|
| `price_yoy_pct`, `price_cagr_5yr_pct` (Fig 10) | `material_criticality_signals` | `score_financial_pressure` market layer — currently uses Pink Sheet `commodity_prices` only | Wire as a secondary input alongside Pink Sheet CV; partner-decide how to combine |
| `hs_code_production_shares` rows with `market_scope='us'` (2026 Import Sources) | `hs_code_production_shares` | An IRA-domestic-content / US-trade-dependency scorer that doesn't exist yet — distinct from global HHI | Greenfield; defer until partner specs the US-scope view |
| `metadata_json[us_net_import_reliance]`, `[us_apparent_consumption]` (2026 Salient) | `material_criticality_signals.metadata_json` | Could feed a "US dependency" sub-score in Material Concentration or as a separate IRA pillar | Decide where it belongs before wiring |

The scoring documentation **must not** describe these as live scoring inputs — they're stored data awaiting consumer code.

### Planned sources (not yet ingested)

| Source | Target tables | Intended scoring role |
|---|---|---|
| EU CRMA assessments | `material_criticality_signals`, `materials` | highest-priority criticality signals + regulatory obligation context |
| IEA Critical Minerals reports | `material_criticality_signals` | forward-looking demand/supply gap signals |
| EPO PATSTAT | `material_criticality_signals` | patent trend signals feeding criticality adjustments |

---

## Criticality signal source priority (used by market scoring)

The market scorer prefers criticality signals in this order (highest → lowest) as implemented in `market_aggregator.py`:

1. `eu_crma` (planned)
2. `iea_report` (planned)
3. `usgs_mcs` (live)
4. `manual` (live)
5. `patstat` (planned)

If no signal exists, the scorer falls back to `materials.criticality_score`.

---

## Related reading

- [Scoring](scoring.md) — exact formulas, weights, and rollups
- [Overview](overview.md) — system architecture, layers, and scheduled jobs
- [Database architecture](database_architecture.md) — full schema details
- (Deprecated soon) [Ingestion pipeline](ingestion-pipeline.md), [Parsing & normalization](parsing-and-normalization.md), [Seed staleness review](seed-review.md) — will be merged into `docs/operations.md`
