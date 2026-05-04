# Scoring Engine Audit — HS Code Redesign Coverage

> **Date:** 2026-05-03
> **Scope:** HS code Level-0 layer + full rollup chain (stage → material+geo → global → chemistry)
> **Method:** Code read against `docs/scoring.md` and `docs/hs-code-redesign.md`. Verified file-by-file; no runtime/DB inspection.
> **Audience:** Nicole. No partner-friendly framing — direct findings only.

---

## TL;DR

The scoring code itself is in better shape than the data feeding it. Almost every redesign PR (Phase 1, 1.5, 2, 3) has landed in code, but three structural gaps mean the new HS-code stage layer is mostly a pass-through right now:

1. **`hs_code_material_mappings.keywords` is empty.** Migration 028 added the column. The seed file's docstring acknowledges keywords need to be seeded but `_MAPPINGS` is still a 7-tuple with no keywords. The whole `relevance=0.90` stage-attribution path through `MaterialCache` is dead code today. Federal Register, EUR-Lex, and news events therefore attach to materials via canonical-name only and **never carry an `hs_mapping_id`**, which means they never appear in `hs_code_geography_risk_scores.tariff_exposure` or `.export_restriction`.
2. **The HS node composite formula has no operational signal.** `score_hs_node_geography()` is `0.50 × HHI + 0.25 × tariff + 0.25 × export`. The redesign doc Phase 2.5 specified stage-aware operational risk via `facility_material_links.supply_chain_stage` — that path is not in the scorer at all, even though migration 029 added the schema. There is also no GEM ingester to populate the column, so even if the scorer read it, it would be NULL.
3. **Stage-aware data only lifts ONE pillar.** Geopolitical, regulatory, operational, and financial pillars all bypass HS-node sub-scores at Level 1. The HS node already computes `tariff_exposure` and `export_restriction` per stage × country, but the Geopolitical pillar at Level 1 ignores them and re-derives a material-level signal. This is the single largest formula-level lost opportunity in the current code.

The chain itself (Level 0 → 1 → 2 → 3) is wired correctly. The orchestration jobs all exist with correct cron times. The math is correct where it runs. The issue is upstream coverage and a handful of formula-level connections that were specced but not built.

---

## Verified state of the code

I read each file myself rather than rely on the documented spec. Findings:

### Level 0 — `hs_node_scorer.py`

Implemented and matches the spec. Sub-scores: `production_share`, `hhi_at_stage`, `tariff_exposure`, `export_restriction`, `composite_node_score = 50% HHI + 25% tariff + 25% export`. Idempotent upsert keyed on `(hs_mapping_id, country_code, as_of_date, market_scope)`. Reads `RiskEventHsMapping` for tariff/export events. Batch entry point `score_all_hs_nodes()` enumerates every `(hs_mapping_id × country_code)` pair with non-zero production share.

**Gaps:**
- No operational sub-score. Phase 2.5 specified `at_risk_tpy_for_node` from `facility_material_links` — not present.
- The composite weighting is HHI-dominated. Because tariff/export inputs are sparse (see keywords gap below), the score effectively reduces to `~50 × HHI` for most nodes today.
- `_TARIFF_SUBTYPES` / `_EXPORT_SUBTYPES` filter on `RiskEvent.event_type`, not on `metadata_json.event_subtype` — the convention used elsewhere (e.g. `market_aggregator._classify_geo_events` uses `metadata_json["event_subtype"]`). If ingesters write subtype to `metadata_json`, the HS node scorer's tariff filter never matches and `tariff_exposure` is permanently 0. **This is worth a 5-minute verification.**

### Level 1 — `market_aggregator.py` + `material_risk.py`

Stage-weighted rollup is correctly implemented for the **Material Concentration pillar only**. `STAGE_ROLLUP_WEIGHTS` matches the doc: ore=0.10, concentrate=0.15, intermediate=0.20, refined=0.25, battery_grade=0.30. `_STAGE_ROLLUP_MIN_NODES = 2` triggers the fallback path; `stage_rollup_method` and `stage_rollup_count` are persisted on every row.

**Gaps:**
- **Geopolitical pillar bypasses HS node data.** `_derive_market_geopolitical_inputs()` uses `MaterialProductionShare` (material-level) for `country_concentration` and re-derives tariff/export from `geo_trade_events` filtered by event title/subtype. The HS node sub-scores `tariff_exposure` / `export_restriction` are never consulted, even though they are more precisely scoped (per-HS-code). This is the single most impactful formula-level miss in the current build.
- **The 2-node minimum is too aggressive.** Many materials have only one mapped HS code per country at the moment (e.g. Lithium × CL has one ore mapping). They will silently fall back to `material_fallback` even when stage data exists. Lowering to 1 would still produce a meaningful stage-weighted score.
- **Operational pillar at Level 1 also ignores HS nodes.** `_facility_structural_dependency()` is material-level (uses `FacilityMaterialLink.material_id` only). When migration 029's stage column is populated, the scorer should weight by stage × country, not aggregate. As written, even a future GEM ingester populating stage won't help unless the scorer is updated.

### Level 2 — `global_rollup.py`

Implemented and correct. Three-tier weight resolution (trade flow → production share → equal). Per-pillar weighted average across geographies, then `MARKET_PILLAR_WEIGHTS` applied for the overall. Per-row commits avoid one bad material killing the batch.

**Gaps:**
- Equal-weight fallback is logged at WARNING only. There is no aggregation report or coverage metric. After a full rescore you have no easy answer to "how many (material, geo) pairs fell back to equal weighting?" — you have to grep logs.
- `_trade_weights()` filters on `import_export_flag == "export"` and uses the *single most recent period* with any export data. If China hasn't exported a particular HS code in the last quarter but has historically, weight collapses to zero. Consider averaging the last N periods.

### Level 3 — `chemistry_risk.py`

Implemented and correct. Only the v2 rollup path remains (v1 was deleted in PR 10 cleanup, per source comment). Intensity weights from `BatteryChemistryMaterial.intensity`. Per-pillar weighted sum, normalized by total intensity, composited with `MARKET_PILLAR_WEIGHTS`. Confidence penalty multiplied per material based on `material.data_availability` (commercial=1.00, limited=0.85, no_benchmark=0.65). Records `materials_missing_global_score` in metadata.

**Gaps:**
- None at the formula level. This module is the cleanest in the chain.
- One operational concern: when a material is missing a `MaterialGlobalRiskScore`, it is silently excluded and the chemistry score is computed without it. If Cobalt is missing from an NMC811 score, the result still reports `composite_risk_score` but it's not really an NMC811 score anymore. Consider raising a `score_confidence` floor that drops more aggressively when major-intensity materials are missing.

---

## Ingestion vs seeding — what's coming from where

| Scoring input | Current source | Stage-aware? | Dynamic? | Gap |
|---|---|---|---|---|
| `hs_code_production_shares` | MCS PDF parser (`bdi-ingest ingest-mcs-pdf`) | ✅ stage-level via `hs_mapping_id` | ⚠️ Annual; only when CLI is run | **Has parser been run on current DB?** Without this, every `hs_node_scorer` call returns `None`. Also unpopulated for any material not in MCS (see `_MCS_COMMODITY_MAP`). |
| `material_production_shares` | `ingest-usgs` (USGS MCS CSV) | ❌ material-level | ⚠️ Annual | Used as Level-1 geopolitical fallback. Reasonably complete. |
| `material_criticality_signals` (HHI, RLI, capacity, YoY) | `ingest-usgs` | ❌ material-level | ⚠️ Annual | Single source. Per redesign OQ-2, should remain a fallback when stage HHI is computed. |
| `commodity_prices` | World Bank Pink Sheet (weekly cron) | ❌ material-level | ✅ Weekly | Migration 030 added `hs_mapping_id` and `price_form`; **unused by Pink Sheet ingester**. Spodumene vs lithium hydroxide indistinguishable. |
| `trade_flows` | UN Comtrade quarterly + daily backfill | ✅ stage-level via `hs_mapping_id` (new rows only) | ✅ Quarterly + daily backfill | Historical rows have NULL `hs_mapping_id`. Stage attribution builds gradually. |
| `risk_events` (tariff, export, regulatory) | Federal Register (weekly), EUR-Lex (quarterly), GTA, Census trade, SEC EDGAR (quarterly) | ❌ material-level only when keywords missing | ✅ Weekly–quarterly | **Critical gap: keywords unpopulated → events never attach to `RiskEventHsMapping` from text-based ingesters.** Only trade-flow-derived events get stage attribution. |
| `risk_event_hs_mappings` | `trade_signal_builder.py` only | ✅ when `TradeFlow.hs_mapping_id` is set | depends on Comtrade re-ingest | Nothing else writes to this table today. Federal Register / EUR-Lex / news produce zero rows. |
| `facility_material_links.supply_chain_stage` | **Nothing** | N/A | N/A | No GEM ingester. Schema present (migration 029), no writer. Operational pillar can never be stage-aware until this is built. |
| `commodity_prices.hs_mapping_id` / `.price_form` | **Nothing** | N/A | N/A | Schema present (migration 030), no writer. |
| `battery_chemistry_materials.intensity` | Manual seed | ❌ material-level (correct) | ❌ Static | This is appropriate — chemistries are slow-moving design choices, not real-time signals. |
| HCG list (CN/CD/RU) | Hard-coded constant in `evidence_query.HIGH_CONCENTRATION_GEOS` | N/A | ❌ Static | Could be derived dynamically from production-share concentration. See "Dynamic data opportunities" below. |
| Country political risk | None | N/A | N/A | Geopolitical pillar relies entirely on event evidence. No structural country-risk baseline. |

### Bottom-line ingestion gaps

- **Keywords on `hs_code_material_mappings`** — single largest data gap. Without it, the redesign's stage-attribution mechanism for non-trade-flow events is non-functional.
- **MCS PDF ingestion run state** — the parser exists; verify it has been run against the current Neon DB. If not, `hs_node_scorer` produces zero rows.
- **GEM ingester** — schema-only feature. The operational stage-awareness pathway is dark until this is built.
- **Comtrade historical backfill** — historical rows lack `hs_mapping_id`, so stage attribution from trade signals only grows from the date Phase 1.5 deployed forward. This is acceptable per the redesign doc, but worth tracking as a coverage curve.

---

## Dynamic data opportunities

For each currently-seeded or static value, can it be replaced by an ingestion job? Realistic for your stack (Python + FastAPI, Inngest, Modal):

| Value | Today | Realistic dynamic source | Cost | Complexity | Worth it? |
|---|---|---|---|---|---|
| Stage-level HHI | Computed from `hs_code_production_shares`, depends on MCS PDF | Already dynamic — but MCS PDF is annual. **No realistic improvement** unless you pay for S&P Global Market Intelligence or Wood Mackenzie. | Free → $$$$ | Low (already wired) | Already optimal for free data |
| Country production shares per HS code | MCS PDF (annual) | Same — paid alternatives only | $$$$ | Medium | No — MCS is the right source |
| HCG list (high-concentration geos) | Hardcoded `{CN, CD, RU}` | Derive from `material_production_shares`: any country with ≥40% of any battery material qualifies | Free | Low | **Yes** — removes a hardcoded political assumption, makes the system reflect actual data |
| Country political risk baseline | None | World Bank WGI (free, annual), Heritage Index of Economic Freedom (free, annual), Fragile States Index (free, annual) | Free | Medium | **Yes** — adds structural country risk to Geopolitical pillar instead of relying solely on events |
| Tariff baseline (structural, not events) | Event-only | USITC HTS Online (free, queryable by HS), WTO TARIC | Free | Medium | **Yes for US** — provides structural tariff baseline so a country without recent tariff *events* doesn't look risk-free |
| Export restrictions (structural) | Event-only via GTA/Federal Register | OECD Export Restriction Inventory (free, semi-structured), GTA (already live) | Free | Low | Marginal — GTA already covers this well |
| Commodity prices | World Bank Pink Sheet weekly | LME tick data (paid), Fastmarkets/Benchmark Mineral Intelligence (paid). USGS year-end averages free but lower frequency | Free → $$$$ | Low → High | No — Pink Sheet is the right free option for monthly/weekly cadence |
| Recycled-content fraction | None | USGS Recycling Statistics (free, annual PDF) | Free | Medium | **Yes for IRA/EU Battery Reg compliance scoring** — currently you have nothing |
| Reserve life index, capacity utilization | USGS MCS only | IEA Critical Minerals Outlook (free, annual; planned per `_CRITICALITY_SOURCE_PRIORITY`) | Free | Medium | Already on roadmap |
| Battery chemistry intensities | Manual seed | Argonne BatPaC model (semi-public spreadsheet), academic LCA databases | Free | Medium | No — chemistries change slowly; manual seed is correct |

**Top three dynamic opportunities ranked by impact:**

1. **WGI country governance scores** → feed Geopolitical pillar as a structural baseline so every country has a non-zero `country_concentration` floor that reflects governance stability, not just whether someone tweeted a tariff at it last week.
2. **Dynamic HCG derivation** → remove the hardcoded `{CN, CD, RU}` and derive from concentration thresholds. Otherwise you'll add a new HCG (e.g. Indonesia for nickel) and the only way to fix it is a code deploy.
3. **USITC HTS structural tariff** → provides a tariff baseline so the `tariff_exposure` sub-score isn't 0 for every HS code without a recent event.

---

## Rollup chain audit — does the data flow as documented?

I traced each rollup level against the documented contract.

**Level 0 → Level 1 (Material Concentration pillar only)**
- Reads: `get_hs_nodes_for_material(db, material_id, country_code, as_of_date, market_scope="global")` returns most-recent-per-mapping `HsCodeGeographyRiskScore` rows joined to `HsCodeMaterialMapping` for stage info.
- Path eligibility: filter to nodes with `composite_node_score IS NOT NULL` and `supply_chain_stage IN STAGE_ROLLUP_WEIGHTS`. If `len >= 2`, stage-weighted rollup; otherwise material_fallback.
- ✅ Implemented correctly in `score_material_geography()`.
- ⚠️ The other four pillars at Level 1 ignore HS nodes. **This is the most impactful formula-level miss.**

**Level 1 → Level 2 (per-material global)**
- Three-tier weight: trade flow value → production share → equal. ✅ Correct.
- Pillar-by-pillar weighted average across geos. ✅ Correct.
- Final composite uses `MARKET_PILLAR_WEIGHTS`. ✅ Correct.
- ⚠️ Trade weight uses single most recent period — fragile to seasonality.

**Level 2 → Level 3 (chemistry intensity-weighted)**
- For each `BatteryChemistryMaterial` active on `as_of_date`: pull latest `MaterialGlobalRiskScore`. Weight pillar scores by `intensity`. Normalize by total intensity. Composite with `MARKET_PILLAR_WEIGHTS`.
- ✅ Implemented correctly.
- Missing materials silently excluded; recorded in `metadata_json.materials_missing_global_score`.

**Level 3 → Level 4 (company)**
- Out of scope for this audit, but `MARKET_PILLAR_WEIGHTS` (Level 1–3) and company `PILLAR_WEIGHTS` (Level 4, six-pillar) intentionally differ. Documentation reflects this.

---

## Concrete gaps the audit found

Numbered, ordered by impact:

### G1 — `hs_code_material_mappings.keywords` is empty (P0)
- **Where:** `app/services/ingestion/seed_hs_mappings.py`, `_MAPPINGS` is a 7-tuple `(hs_prefix, canonical_name, description, confidence, stage, digit_count, market_scope)`. No keywords field. The docstring at line 33 even says "Migration 026 has run and keywords need to be seeded" but the implementation was never added.
- **Effect:** `MaterialCache.build()` adds 0.90-relevance keyword entries with `hs_mapping_id` only when `hs_code_material_mappings.keywords` is non-empty. With keywords empty, every event from Federal Register / EUR-Lex / IEA / news falls back to canonical-name matching at relevance 0.85 with `hs_mapping_id=None`. They write `risk_event_materials` rows but no `risk_event_hs_mappings` rows.
- **Downstream effect:** `hs_node_scorer.score_hs_node_geography()` reads `RiskEventHsMapping` for `tariff_exposure` and `export_restriction`. With no rows present (except from trade signal builder), those sub-scores are zero. The composite is `~50 × HHI` for most nodes. The HS code redesign's central claim — that you can score "Cobalt hydroxide × DRC" with stage-specific event evidence — is not delivered.
- **Fix:** P0 code change below.

### G2 — Geopolitical pillar at Level 1 ignores HS node tariff/export sub-scores (P0)
- **Where:** `app/services/scoring/market_aggregator.py::_derive_market_geopolitical_inputs()`. Pulls `country_concentration` from `MaterialProductionShare` (material-level) and derives `export_restriction_exposure` / `tariff_exposure` from `geo_trade_events` re-classified by event title/subtype.
- **Effect:** The Level-0 layer computes `tariff_exposure` and `export_restriction` per HS node × country with the more precise `RiskEventHsMapping` linkage. None of that propagates upward except into the Material Concentration pillar's composite_node_score. The Geopolitical pillar — the one that should most directly benefit — does not consult HS nodes.
- **Fix:** When `eligible_nodes` is non-empty (already computed for Material Concentration), aggregate the HS node `tariff_exposure` and `export_restriction` sub-scores by the same `STAGE_ROLLUP_WEIGHTS` and pass those to `geopolitical_risk.score_geopolitical_trade()` as the geopolitical sub-inputs (with a fallback to the existing event-classification path when nodes are absent). Recommend doing this in a later PR after the keywords gap is closed and you have real event data flowing through HS nodes — otherwise this change just substitutes one zero for another.

### G3 — `hs_node_scorer` `_TARIFF_SUBTYPES` filter likely doesn't match real events (P1, needs verification)
- **Where:** `app/services/scoring/hs_node_scorer.py` lines 70–71, and the SQL filter `RiskEvent.event_type.in_(_TARIFF_SUBTYPES)` at line 188.
- **Suspicion:** Other code paths classify subtype via `metadata_json["event_subtype"]` (e.g. `market_aggregator._classify_geo_events`). If ingesters write to `metadata_json["event_subtype"]` rather than `RiskEvent.event_type`, the HS node scorer's filter never matches and `tariff_exposure` is permanently zero regardless of keyword coverage.
- **Verification:** Run `SELECT event_type, metadata_json->>'event_subtype', count(*) FROM risk_events GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20;` against the live DB. If `event_type` is NULL or generic and the subtype lives in metadata, this is broken.

### G4 — No GEM ingester; operational pillar can't be stage-aware (P1)
- **Where:** Migration 029 added `facility_material_links.supply_chain_stage` and `.hs_mapping_id` columns. No writer exists. `app/services/ingestion/gem.py` does not exist.
- **Effect:** Migration 029 is dormant schema. `_facility_structural_dependency()` in `market_aggregator.py` aggregates by material_id only. A cobalt ore mine outage and a cobalt hydroxide refinery outage are scored identically under "Cobalt × DRC."
- **Fix:** Two changes needed: (a) build the GEM ingester (substantial — out of P0 scope), (b) update `_facility_structural_dependency()` to filter by `supply_chain_stage` when called from a stage-aware path. (b) is a no-op until (a) ships.

### G5 — `hs_node_scorer` composite has no operational sub-score (P1)
- **Where:** `score_hs_node_geography()` composite is `0.50 × HHI + 0.25 × tariff + 0.25 × export`. No operational input. Phase 2.5 specified `at_risk_tpy_for_node` from `facility_material_links`.
- **Effect:** A node where 80% of capacity is mothballed scores the same as one where 0% is mothballed. The node can technically be scored as "low HHI, no events = low risk" while in fact the world's only refining capacity is offline.
- **Fix:** Defer until G4 lands. Without facility stage data, this fix has nothing to read.

### G6 — `_STAGE_ROLLUP_MIN_NODES = 2` over-triggers the fallback (P1)
- **Where:** `market_aggregator.py` line 112.
- **Effect:** A material with one mapped HS code per country (common when MCS PDF only covers one stage per commodity) reverts to the legacy material-level path even though Level-0 data exists. The user does not see the benefit of stage-aware scoring for these materials.
- **Fix:** Lower to 1, OR keep 2 but emit a structured log when fallback fires for nodes that *exist*. Recommend keeping the 2-node threshold but adding a coverage diagnostic. See P0 change below.

### G7 — Equal-weight fallback in `global_rollup` is logged but not measured (P2)
- **Where:** `global_rollup.py::_resolve_weights()` warns when both trade flow and production share are missing for a geography. No aggregate count or coverage metric.
- **Effect:** After a full rescore you cannot answer "what fraction of (material × geo) pairs fell back to equal weight?" without grepping logs.
- **Fix:** Add a coverage diagnostic. See P0 change below.

### G8 — `MaterialProductionShare` weight uses only the most recent period (P2)
- **Where:** `global_rollup.py::_trade_weights()` filters `period == latest_period`.
- **Effect:** A geography with no exports in the latest quarter (e.g. China during a Lunar New Year period) gets weight 0 even if it's the world's dominant exporter.
- **Fix:** Average the last 3–4 quarters.

### G9 — `commodity_prices.hs_mapping_id` and `price_form` unused (P2)
- **Where:** Migration 030 added both columns. No ingester populates them.
- **Effect:** Pink Sheet writes material-level rows. Spodumene vs lithium hydroxide both go to "Lithium" with no `price_form`. The financial-pressure pillar's price-volatility input cannot distinguish stages.
- **Fix:** Manual or scheduled enrichment from Fastmarkets / Benchmark Mineral Intelligence (paid). Lower priority unless you have a paid feed.

### G10 — Inngest jobs run on cron schedule with no dependency enforcement (P2)
- **Where:** `app/tasks/scoring_jobs.py` registers four functions on cron `0 1`, `0 2`, `0 3`, `0 4` Monday UTC.
- **Effect:** If `rescore-hs-nodes` (Job 0) runs >60 min, Job 1 starts on stale Level-0 data. Possibly fine in practice (Job 0 is fast — fewer than 100 HS mapping rows × ~30 countries = ~3000 nodes), but there's no safety mechanism.
- **Fix:** Use Inngest's `step.invoke` or `step.waitForEvent` to chain jobs; or add a Job-0-completed event the others wait on. Defer until you observe an actual race.

### G11 — `score_hs_node_geography` event filter ignores per-event scope (P2)
- **Where:** Lines 183–214. `tariff_rows` and `export_rows` queries do not filter by country (the node is a stage × country pair, but the event query joins only on `hs_mapping_id` regardless of where the event was geographically scoped).
- **Effect:** A tariff event tagged to "China" on cobalt hydroxide will contribute to `tariff_exposure` for Cobalt hydroxide × DRC just as much as Cobalt hydroxide × CN. Probably not what you want — the node represents a specific (stage × country) pair.
- **Fix:** Add a country-scoped filter via `RiskEventGeography`. Caveat: many events will have NULL geographies, which would over-filter. Could be opt-in via a parameter.

### G12 — `materials.criticality_score` and `patent_occurrence_trend` are denormalized caches but never refreshed (P3)
- **Where:** Per `hs-code-redesign.md` Decision 1, both should be updated by `ingest-usgs` and `_sync_patent_trend` respectively. `_sync_patent_trend` is implemented in `chemistry_risk.py`.
- **Verification needed:** Check that `ingest-usgs` actually calls into the materials.criticality_score update. If not, this column is stale and any code path using it as fallback gets old data.

---

## Concrete recommendations

Ordered by impact-vs-effort.

### P0 — Implement now (in this PR)

1. **Seed `hs_code_material_mappings.keywords`.** Without this, the entire stage-attribution mechanism is dead for non-trade-flow events. I'm implementing this as a separate `_HS_KEYWORDS` dict + `upsert_hs_keywords()` function alongside the existing seed, plus calling it from the same CLI entry point. See code changes below.
2. **Add HS coverage diagnostic helper.** A function `report_hs_coverage(db)` that prints: per-material, how many HS nodes exist, how many have stage data, how many fall back to `material_fallback` at Level 1. Stops you from flying blind on data coverage. See code changes below.

### P1 — Next iteration (separate PR)

3. **Verify and fix `hs_node_scorer` event-type filter (G3).** Run the SQL above first.
4. **Wire HS-node `tariff_exposure` / `export_restriction` into the Geopolitical pillar at Level 1 (G2).** Requires keyword seeding to land first or you're substituting one zero for another.
5. **Lower `_STAGE_ROLLUP_MIN_NODES` to 1, OR add per-pair fallback telemetry (G6).**
6. **Build the GEM ingester (G4)** so `facility_material_links.supply_chain_stage` actually gets populated. Then add a stage-aware operational sub-score to `hs_node_scorer` (G5).

### P2 — Backlog

7. **Average trade weights across last N periods in `global_rollup` (G8).**
8. **Country-scope HS node event filter (G11).**
9. **Dynamic HCG derivation** instead of hardcoded `{CN, CD, RU}`.
10. **WGI country governance baseline** as a structural input to the Geopolitical pillar.
11. **USITC HTS structural tariff baseline** so tariff_exposure isn't zero by default.

### P3 — Watch list

12. **Inngest job dependency chain (G10).**
13. **Verify `materials.criticality_score` cache refresh path (G12).**
14. **`commodity_prices.hs_mapping_id` / `price_form` (G9)** — only relevant if you commit to a paid stage-resolved price feed.

---

## What I changed in code (companion PR)

See the diff in this commit. Three files touched, all additive:

- `app/services/ingestion/seed_hs_keywords.py` — **new file** with `_HS_KEYWORDS` dict (covers the materials with the most regulatory/news exposure today: Lithium, Cobalt, Nickel, Manganese, Graphite, Copper, REE) and `upsert_hs_keywords()` function. Idempotent, force-flag-aware. Designed to be re-run independently of the main `_MAPPINGS` seed.
- `app/services/ingestion/seed_hs_mappings.py` — call `upsert_hs_keywords` after `upsert_hs_mappings` so a single `bdi-ingest seed-hs-mappings` run handles both. Updates the docstring's stale "Migration 026 has run and keywords need to be seeded" line.
- `app/services/scoring/coverage.py` — **new file** with `report_hs_coverage()` returning a structured dict counting: materials with ≥1 / ≥2 HS nodes, per-stage node counts, materials falling back to `material_fallback` at Level 1. Companion CLI invocation suggested in the file's docstring.

These are deliberately small, low-risk, additive changes. None of them alter the scoring formulas. They make the diagnosed gaps observable and remediable.

---

## What I did NOT change

- I did not modify any scoring formula or weight. Even when the audit identified a clear gap (G2: Geopolitical pillar should consume HS node sub-scores), I left that as a P1 recommendation. Reason: substituting a zero (HS-node-derived) for an existing non-zero (event-derived) value before keyword seeding lands would silently *lower* observed scores. Better to land coverage telemetry first, confirm keywords are flowing through, then make the formula switch as a measurable change.
- I did not add the GEM ingester or write new ingestion sources. Both are substantial pieces of work that need their own design conversations.
- I did not run anything against a live DB. The diagnostic helper is read-only and safe to run, but I haven't run it.

---

## What I'd want to verify before relying on these scores

If the goal is to ship a credible v1, the four highest-priority checks are:

1. **Has `bdi-ingest ingest-mcs-pdf` been run on the production Neon DB?** Without it, every Level-0 score is `None`. Run `SELECT count(*) FROM hs_code_production_shares;` — should be in the hundreds, not zero.
2. **Run the new `report_hs_coverage()` helper.** Tells you which materials are actually getting stage-aware scores vs falling back to material-level.
3. **Verify event subtype storage convention (G3).** One SQL query, decisive answer.
4. **After seeding keywords, count `risk_event_hs_mappings` row growth on the next ingest run.** If keyword seeding works, this number should grow with each Federal Register / EUR-Lex pull. If it stays flat, the `MaterialCache.build()` query isn't picking up the keywords.

If any of those four reveals the upstream isn't flowing, the audit's downstream gaps don't matter — you're just adding sophistication on top of nothing.
