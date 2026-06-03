# Deferred Work & Follow-Up Items

Compiled from the May 2026 section-by-section code walkthroughs.
Last updated: 2026-05-31.

This document captures every item that was raised during the audits but
deferred — either because it requires partner input, awaits a forcing
function, or is hygiene that didn't make the per-section fix scope.
Items are grouped by domain, then ordered by priority within each domain.

Each item lists:
- **What** — concise description of the gap
- **Why deferred** — partner input, forcing function, scope, etc.
- **Impact** — what scoring or workflow is affected
- **Effort** — rough size estimate
- **Tracked in** — task ID where applicable

---

## 1. Partner input required

These items need partner judgment before code can land.

### 1.1 — Bottleneck-stage weights for 12 of 16 supply chain stages
- **What:** `supply_chain_stages.bottleneck_weight` carries methodology-anchored values for 4 stages (mining 0.80, refining 1.20, CAM 1.30, cell_making 1.40). The other 12 (beneficiation, precursor_production, anode_active_material, separator_production, electrolyte_production, module_assembly, pack_assembly, vehicle_assembly, recycling, trading, financial, integrated) carry extrapolated PRELIMINARY values.
- **Why deferred:** Methodology calibration; weights propagate directly into scoring.
- **Impact:** Affects every company-level score for materials/companies operating at the un-calibrated stages. Most impactful to revisit: anode_active_material (currently 1.30 by CAM analogy), recycling (1.15), trading (1.00).
- **Effort:** Partner judgment + update seed values + re-seed.
- **Tracked in:** Notes column of `supply_chain_stages` table; surfaced in handbook v3 PARTNER INPUT REQUESTED callout.

### 1.2 — Iterate company seed template with partner feedback
- **What:** Partner reviews `company_seed_template.xlsx` (35 cols + 5 reference tabs) before Workstream A schema lands fully. Column set may need adjustment.
- **Why deferred:** Need partner sign-off on the curation surface.
- **Impact:** Blocks full SEC issuer-enrichment loader work and downstream CompanyMaterialExposure curation.
- **Effort:** 1–2 partner-review cycles, then load-side adjustments.
- **Tracked in:** Task #39.

### 1.3 — TELLURIUM `duplicate_suspect` adjudication
- **What:** MCS Section 5 walkthrough flagged TELLURIUM's `refinery production` + `refinery production: concentrate` per-stage consolidation as `duplicate_suspect`. Real-data shows the two details cover disjoint country sets (additive in practice), but the heuristic conservatively flagged it.
- **Why deferred:** Partner-review needed to confirm whether concentrate is a distinct flow vs. a different measurement of the same flow.
- **Impact:** Tellurium scoring shares could double-count for any country present in both buckets. Today disjoint → safe; future edition could change.
- **Effort:** Partner adjudication; if duplicate-confirmed, add per-chapter override.

### 1.4 — PGM + TITANIUM MINERAL CONCENTRATES `duplicate_suspect` review
- **What:** Both flagged by the consolidation heuristic because their per-stage buckets fully overlap (PGM palladium+platinum on same 5 countries; TITANIUM MINERAL CONCENTRATES ilmenite+rutile on same 12). Substantively these are additive (different products from same mines).
- **Why deferred:** Heuristic false positives — partner can confirm "additive" once and the flag becomes informational rather than gate.
- **Impact:** Currently the math sums correctly; the flag is purely diagnostic. No scoring impact until partner decides to use the flag.
- **Effort:** Partner sign-off; optionally add per-chapter "known-additive" allowlist.

### 1.5 — Bottleneck companies coverage list expansion
- **What:** Recommended Coverage tab in `company_seed_template.xlsx` lists 41 companies across 4 tiers. Tier 1 includes 11 companies (Albemarle, SQM, Ganfeng, Tianqi, Glencore, CMOC, Sumitomo, Vale, Norilsk, IBC, Tsingshan, Huayou, GEM, BTR, Shanshan). Five of these need substantial facility-data work before they're scorable end-to-end.
- **Why deferred:** Facility data curation is in progress with partner.
- **Impact:** Scoring coverage. Today only 4 of 11 Tier-1 companies (Albemarle, MP, Lynas, MRL) have non-trivial facility data.
- **Effort:** Multi-cycle partner curation, 5–10 hours per company.
- **Tracked in:** Task #39 covers the template; coverage expansion is a downstream phase.

---

## 2. Methodology refinements

Calibration / formula choices that have non-trivial scoring impact but
don't have a forcing function to fix yet.

### 2.1 — Qualitative reserves dropped (Issue 2.3 from MCS walkthrough)
- **What:** ~45 MCS cells use qualitative descriptors for reserves: `'Large'`, `'Small'`, `'Moderate'`, `'Variable, depending on type'`. Currently all return `None` from `_parse_value`. Affected countries silently contribute zero to `reserve_hhi`.
- **Why deferred:** Methodology choice — translating qualitative labels to synthetic numbers risks corrupting the scoring inputs.
- **Impact:** Underestimates reserves diversification for any material with qualitative-only reserve disclosures.
- **Effort:** Methodology call (do we synthesize? to what bucket?) + 5 lines of code.

### 2.2 — Volume-bound direction loss (Issue 2.4 from MCS walkthrough)
- **What:** `_parse_value` strips `<` / `>` bounds; `<200,000` and `>540,000` collapse to their bound values. For reserves and other non-percentage volume cells, direction is sacrificed.
- **Why deferred:** Direction matters only if reserves accuracy becomes a scoring sub-input.
- **Impact:** Low today (reserves not a load-bearing sub-input).
- **Effort:** Add `_parse_value_with_bound` mirror of `_parse_percent_with_bound`.

### 2.3 — `intermediate` stage underused (Issue 3.4 from MCS walkthrough)
- **What:** Only 1 (chapter, detail) pair classifies as `intermediate` today — BAUXITE AND ALUMINA's "alumina, refinery" row. Real intermediate inputs to CAM (nickel sulfate, cobalt sulfate, MHP, lithium chloride) classify as `refined` because the patterns map `"refinery production"` → refined.
- **Why deferred:** Awaits per-product HS attribution work (item 3.5 below).
- **Impact:** Stage-aware scoring rollup misses the intermediate vs. refined distinction for CAM precursors.
- **Effort:** Methodology refinement + pattern additions, ~30 min once partner confirms naming.

### 2.4 — Production YoY uses 2-year window only
- **What:** MCS 2026 production rows have only 2024 + 2025 in their Year column. Previous editions had more years. YoY is therefore single-year delta, not a multi-year trend.
- **Why deferred:** Out of our control — depends on USGS publication shape.
- **Impact:** YoY signal is noisier than a 5-year trend would be.
- **Effort:** None on our side until USGS expands the time series.

### 2.5 — Capacity utilization for ALUMINUM + MAGNESIUM METAL gated to `None` (Section 6 finding)
- **What:** Section 6 capacity-utilization fix surfaced that ALUMINUM Primary + Secondary scrap production sums to >100% of primary smelter capacity. Sanity gate correctly returns `None` rather than emit `3.252`. To recover the signal, the numerator needs to filter to "Primary" sub-types only.
- **Why deferred:** Per-chapter numerator-filter design requires methodology call.
- **Impact:** ALUMINUM and MAGNESIUM METAL contribute no capacity_utilization signal today. TITANIUM AND TITANIUM DIOXIDE works (73.5%).
- **Effort:** Per-chapter "include sub-type pattern" config + filter wiring, ~2 hours.

### 2.6 — `criticality_score` is raw HHI, not partner-curated composite (handbook discrepancy)
- **What:** Handbook v3 describes `Material.criticality_score` as "Chemistry-weighted average of partner-curated exposure scores per material." Pipeline writes raw HHI from MCS production shares. These don't match.
- **Why deferred:** Either the handbook or the pipeline needs updating; methodology question first.
- **Impact:** Conceptual / documentation accuracy; partner reading the handbook would expect a different value than they see in the database.
- **Effort:** Either handbook rewrite OR pipeline composite refactor.

### 2.7 — Reserves / production unit alignment assumed (Issue 8.7 from MCS walkthrough)
- **What:** `reserve_life_index = world_reserves / world_prod` assumes both are in matching units. `_RLI_MIN_PLAUSIBLE = 2.0` catches gross unit-scale mismatches but not the same-magnitude / different-unit case.
- **Why deferred:** MCS publishes both in tonnes today; no observed case.
- **Impact:** Low — depends on future USGS format changes.
- **Effort:** Add per-chapter unit validation, 10 lines.

### 2.8 — `_latest_two` estimated-suffix preference (Issue 4.8 from MCS walkthrough)
- **What:** `_pick_latest_year` strips `_estimated` suffix when parsing but doesn't deprioritize estimated vs. actual when both exist for the same year. Currently no MCS rows use the suffix.
- **Why deferred:** Latent; no current trigger.
- **Impact:** None today.
- **Effort:** Add `prefer_actual=True` flag.

### 2.9 — Heuristic detection of additive vs. duplicate consolidation (Issue 5.4 from MCS walkthrough)
- **What:** Current consolidation always sums; the flag distinguishes `additive` vs. `duplicate_suspect` purely by country-overlap geometry. A name-based heuristic (e.g., `"X" + "X: content equivalent"` pattern detection) could refine classification.
- **Why deferred:** Premature optimization until more tracked materials hit the case.
- **Impact:** Diagnostic flag accuracy; currently 5 of 6 tracked chapters classified correctly via geometry.

---

## 3. Architecture / Schema migrations

Schema work that's been scoped but not yet implemented. Each has a
forcing function dependency.

### 3.1 — Three-edge company × material exposure model
- **What:** Saved methodology defines three edges: facility-derived, supplier-graph-derived, partner-curated. Currently only facility-derived path is wired (partially) and partner-curated path lives in `company_material_exposures`.
- **Why deferred:** Facility data layer is the prerequisite; partner curation in progress.
- **Impact:** OEM gap — Tesla, Ford, GM, VW have thin facility data so their material exposure is incomplete.
- **Effort:** Facility layer first (multi-week), then wiring (~1 week).
- **Tracked in:** Memory `project_company_material_model.md`.

### 3.2 — `CompanyMaterialExposure.supply_chain_stage_fk` FK migration
- **What:** Migration 044 added the FK column on `companies.primary_activity_stage_fk` but deferred the same migration on `company_material_exposures.supply_chain_stage`. Phase 1 / Phase 2 coexistence approach when it lands.
- **Why deferred:** Reaches into geopolitical pillar's stage rollup query; bounded blast radius for migration 044.
- **Impact:** `company_material_exposures` still uses free-string stage values that don't FK to `supply_chain_stages`.
- **Effort:** Migration + scoring code path update, ~1 day.

### 3.3 — `companies.supply_chain_stage` legacy column drop
- **What:** Migration 044 added `primary_activity_stage_fk` alongside the legacy free-string `supply_chain_stage`. The legacy column gets dropped in a later cleanup once scoring code migrates to the FK.
- **Why deferred:** Phase 1 coexistence per migration 044 design.
- **Impact:** Schema redundancy.
- **Effort:** Audit scoring callers (a few hours), then migration.

### 3.4 — SEC Workstream B: real filing body fetch
- **What:** SEC ingester reads only the submissions metadata endpoint. `ParsedFiling.narrative_excerpt` is a hardcoded placeholder. To extract material attribution from 10-K Items 1A/2/7 and 8-K item 1.01 contract exhibits, we need to fetch and parse filing body text.
- **Why deferred:** Substantial engineering work; deferred until company-side and facility-side work resumes.
- **Impact:** Today no SEC events attach materials via narrative; the Haiku classifier is gated off.
- **Effort:** Multi-week — document fetching with SEC rate limits, HTML→text section extraction, LLM extraction prompts.

### 3.5 — Per-product HS attribution for US import sources (Section 7.7)
- **What:** `_DETAIL_TO_HS_PREFIX` only contains SILICON entries. For every other tracked chapter, import-source sub-types (e.g. ANTIMONY's Ore + Oxide + Unwrought metal) collapse to the material's primary HS prefix.
- **Why deferred:** Methodology gap — requires partner-curated HS-prefix entries for top-impact chapters.
- **Impact:** Per-product import attribution lost in `hs_code_production_shares` for market_scope='us'. Material-level geopolitical scoring still works.
- **Effort:** Partner-curated table additions for ~5 chapters (ANTIMONY, NIOBIUM, RARE EARTHS, TITANIUM, TUNGSTEN).
- **Tracked in:** Task #53.

### 3.6 — Non-SEC issuer financial-pressure signal gap
- **What:** SEC ingester covers US-listed and foreign private issuers filing 20-F. CATL, LG Energy Solution, SK On, Ganfeng, Tianqi, BYD don't file with the SEC and produce zero Financial Pressure signal.
- **Why deferred:** Korean DART and Chinese HKEX/SZSE filing feeds have no clean free APIs.
- **Impact:** Largest cell makers + midstream Chinese refiners — the most likely supply-chain risk vectors — have no financial-pressure signal.
- **Effort:** Per-feed ingester build, weeks to months.

### 3.7 — EUR-Lex HS-level regulation scope (Task #20)
- **What:** Today regulations declare material-level scopes only. A regulation cannot express "cobalt ore is banned but cobalt cathode active material is only restricted." Adding HS-level granularity requires schema migration + partner curation per regulation.
- **Why deferred:** Pending concrete forcing function — likely a stage-specific regulation we want to track (EU's upcoming end-of-life vehicle directive amendments are likely candidates).
- **Impact:** Per-stage regulatory pillar scoring won't differentiate when the same regulation hits multiple stages with different intensities.
- **Effort:** Schema migration + partner curation per affected regulation.
- **Tracked in:** Task #20.

---

## 4. SEC ingester follow-ups

### 4.1 — SEC `adapters/sec_edgar.py` adapter cleanup
- **What:** The old `SecEdgarAdapter` class is still imported by `pipeline.py:137` even though the CLI route was removed during the May 2026 refactor. It's effectively dead but not safe to delete without auditing the generic pipeline's surviving programmatic callers.
- **Why deferred:** Bounded blast radius for the Workstream A cleanup.
- **Impact:** Dead code in the adapter registry.
- **Effort:** Audit `IngestionPipeline.run_for_source_id` callers, then delete (~1 hour).

### 4.2 — SEC ingester paginated history fetching
- **What:** Ingester reads only the most-recent ~1000 filings per CIK (SEC's "recent" window). Adding a new CIK to the curated list mid-stream means losing that issuer's history; the ingester picks up from the most-recent N going forward.
- **Why deferred:** Workstream B scope overlap; backfill not yet a priority.
- **Impact:** No historical depth for newly-added issuers.
- **Effort:** Add `filings.files[]` pagination handler (~1 day).

### 4.3 — SEC 8-K item code subtype expansion
- **What:** `sec_subtype_map.ITEM_8K_MAP` covers 21 of ~30 possible 8-K item codes today. Adding the missing items (e.g., 5.06 change in shell-company status, 9.01 attached exhibits already, items 6.x reserved) when partner needs them.
- **Why deferred:** Marginal items unlikely to drive scoring signal.
- **Impact:** Edge-case 8-K filings get the safe-default classification.

---

## 5. MCS PDF parser tech debt

All low-to-medium-severity items from the in-progress PDF walkthrough.
Section 1 items tracked in **Task #58**; Section 2 items below.

### 5.1 — Delete `_MCS_COMMODITY_MAP` deprecated dict (Section 1.2) — ✅ DONE 2026-06 (Section 4.3)
- **Resolved:** Dict deleted. `material_source_aliases` (source_system='mcs_pdf') is sole source. Path B (`_split_into_commodity_sections` and `_resolve_pdf_heading`) now raises `RuntimeError` when constructed without a resolver — matches the docstring contract that was already in place.

### 5.2 — Extend `_NOT_HEADINGS` deny-list (Section 1.3) — ✅ DONE 2026-06 (Section 4.2 + 4.5)
- **Resolved:** Added `APPENDIX A/B/C/D`, `CONTENTS`, `EXPLANATION`, `FOREWORD`, `INSTANT INFORMATION`, `INTRODUCTION`, `KEY PUBLICATIONS`, `MINERAL COMMODITY`, `WHERE TO OBTAIN PUBLICATIONS`, plus 2026 layout artifacts `ARAB`, `ASIA AND EURASIA SAUDI`, `IRZ IRZ`.

### 5.3 — `_COMMODITY_HEADING_RE` usage comment (Section 1.4)
- **What:** Regex uses `^...$` anchors without `re.MULTILINE`. Works because caller uses `.match(stripped)` on single lines, but a reader could try `.finditer()` against full text and conclude the regex is broken.
- **Why deferred:** Trivial documentation fix.
- **Impact:** Code clarity.
- **Effort:** 1 line.

### 5.4 — Fixture test for `_MCS_PRODUCTION_STAGE_PREFERENCE` (Section 1.5)
- **What:** 5-entry override dict assumes specific stages exist in `hs_code_material_mappings`. If `seed_hs_mappings` changes stage taxonomy for one of these materials, the override silently fires the wrong stage.
- **Why deferred:** Defensive regression guard; current data fine.
- **Impact:** Silent stage-routing risk on schema changes.
- **Effort:** ~15 lines of test code.

### 5.5 — `_PRODUCTION_ROW_RE` docstring example (Section 1.6)
- **What:** Dense regex with no example of what it matches.
- **Why deferred:** Trivial docs.
- **Impact:** Code clarity.
- **Effort:** 3-line comment.

### 5.6 — LLM locator cache invalidation (Section 2.1) ⚠ MEDIUM PRIORITY
- **What:** `data/mcs<year>_locator_result.json` is keyed only on `reference_year`. No fingerprint over PDF content, `canonical_materials` list, prompt, or model version. Stale cache silently fires after:
  - Partner adds a new material to the `material_source_aliases` (`source_system='mcs_pdf'`) table → cached result has no entry → chapter silently skipped.
  - PDF is replaced with a different vintage but the year string stays the same → cache returns bounds from the prior file → wrong chapter text spliced.
  - LLM prompt is updated → cache returns old output shape.
  - Model upgraded (e.g., claude-sonnet-4-6 → 4-7) → cache returns prior model's output.
- **Why deferred:** Wants Section 9 (locator internals) walkthrough first to design the right cache-wrapper shape.
- **Impact:** Most likely real-world failure mode. Partner adding a new material to the alias table will look like it worked, but the chapter won't be ingested.
- **Effort:** ~30 lines including the metadata blob + content hash + mismatch detection. Probably refactor the cache wrapper into the locator.

### 5.7 — `parser.parse()` direct invocation bypasses canonical filter (Section 2.4)
- **What:** The May 2026 chapter-canonical filter that restricts the LLM's allowed-list to materials with curated `mcs_pdf` aliases lives in `seed_to_db`, not in `parse()`. A test or debug-script caller using `parser.parse(canonical_materials=...)` directly can re-introduce the RARE EARTHS fanout bug.
- **Why deferred:** Adding the filter to `parse()` requires a session for `material_source_aliases` lookup, which complicates the parser's testability.
- **Impact:** Low (CLI route is the only production caller and it's protected); test/debug callers could regress.
- **Effort:** Either push the filter down (~15 lines + plumbing) or add an explicit assertion (~5 lines).

### 5.8 — LLM cache path is CWD-relative (Section 2.5)
- **What:** `cache_path = Path("data") / f"mcs{year}_locator_result.json"` resolves relative to the process CWD. If `bdi-ingest ingest-mcs-pdf` runs from anywhere other than the project root, cache lookup misses (re-calls LLM at cost) and writes land in the wrong place.
- **Why deferred:** Operational gotcha, not a data-quality risk; current invocation patterns work.
- **Impact:** Lost LLM-cache savings + log noise if invoked from a different working directory.
- **Effort:** Anchor to `pdf_path.parent` or thread a `cache_dir` parameter, ~5 lines.

### 5.9 — No graceful LLM failure fallback (Section 2.6)
- **What:** When `locate_commodity_chapters()` raises (Anthropic outage, schema validation failure, prompt timeout), `_parse_via_llm_locator` propagates. There's no try/except that falls back to Path B.
- **Why deferred:** Probably the right policy — fail loudly rather than silently degrade — but the design choice isn't documented.
- **Impact:** If Anthropic is down, the entire ingest run fails. Operators don't have a documented "use --no-llm-sections to bypass" workaround surfaced from the error.
- **Effort:** Docstring paragraph + the existing error message could mention the `--no-llm-sections` CLI flag explicitly.

### 5.10 — PDF re-read between paths (Section 2.7)
- **What:** `_read_pdf_text()` called in both `_parse_via_llm_locator` and `_parse_via_regex`. No memoization. Since only one path runs per call, no real overhead today.
- **Why deferred:** Cosmetic; relevant only if Section 5.9 (graceful fallback) is implemented and a single `parse()` call could fire both paths.
- **Impact:** None today.
- **Effort:** Cache on `_path` + `_pdf_text` instance attribute, ~3 lines.

### 5.11 — `chapter.pdf_heading` flows through unsanitized (Section 2.9)
- **What:** `section.heading = chapter.pdf_heading` — whatever the LLM returns lands in the output dataclass. Footnote markers (`ALUMINUM¹`), leading/trailing whitespace, or other LLM artifacts propagate.
- **Why deferred:** Cosmetic — downstream DB writes use `canonical_name`, not `heading`. Path B already normalises via `_commodity_heading_text`.
- **Impact:** Output `CommoditySection.heading` may contain LLM artifacts; not consumed downstream.
- **Effort:** Strip + footnote-digit removal, ~2 lines.

---

## 6. Remaining walkthrough sections

The PDF path walkthrough is in progress. Remaining sections:

| Section | Topic | Lines |
|---|---|---|
| 2 | `MCSPdfParser` init + parse() entry + LLM-vs-regex dispatch | 235-461 |
| 3 | `seed_to_db` — DB write orchestrator | 462-732 |
| 4 | ✅ Section parsing helpers (`_split_into_commodity_sections`, `_resolve_pdf_heading`, `_commodity_heading_text`) — DONE 2026-06 | 837-995 |
| 5 | ✅ Per-commodity extractors (tariff, production, import, salient) — DONE 2026-06; production-leaders extractor removed (CSV path is sole populator); salient + import-source year + tariff description-wrap fixes applied | 892-1116 |
| 6 | ✅ Stage assignment + country/material map builders + utilities — DONE 2026-06; _assign_stage 10-digit Step 1 skip + _resolve_country / _build_country_map deletion | 1118-1456 |
| 7 | ✅ LLM locator — config + system prompt + tool definition — DONE 2026-06; env-overridable model + docstring tightening | mcs_pdf_llm_locator.py 1-260 |
| 8 | ✅ LLM locator — anchor resolution + page indexing — DONE 2026-06; page-index scan restricted to after-break window | 260-535 |
| 9 | ✅ LLM locator — API calls + caching + public entry — DONE 2026-06 (no fixes needed; cache invalidation still tracked as 5.6) | 538-end |
| 10 | ✅ LLM schemas + dead sub-section sweep — DONE 2026-06; deleted SectionType, CommoditySection, sections field on CommodityChapter, _LLMSection, _find_anchor_line, section_text helper, sub-section resolution loop in _resolve_chapter | mcs_pdf_llm_schemas.py + locator.py |

Tracked under Task #56.

---

## Quick-reference cross-table

| Item | Tracked in | Priority | Forcing function |
|---|---|---|---|
| Bottleneck weights (12 stages) | handbook callout | Med | Methodology calibration |
| Company seed template iteration | #39 | Med | Partner cycle |
| TELLURIUM duplicate adjudication | — | Low | Partner review |
| Qualitative reserves treatment | — | Low | Methodology call |
| `intermediate` stage expansion | — | Low | After per-product HS attribution |
| Capacity utilization Primary-only filter | — | Med | Methodology call |
| Handbook vs. pipeline criticality_score mismatch | — | Med | Doc / pipeline alignment |
| Three-edge company-material model | memory note | High | Facility data lands |
| CompanyMaterialExposure FK migration | — | Med | Geopolitical pillar refactor scope |
| `companies.supply_chain_stage` legacy column drop | — | Low | After scoring code migrates |
| SEC Workstream B (real body fetch) | — | High | After company/facility work resumes |
| Per-product HS attribution (import sources) | #53 | Med | Partner curation of `_DETAIL_TO_HS_PREFIX` |
| Non-SEC issuer feeds (Korean DART, HKEX/SZSE) | — | High | Customer demand / budget |
| EUR-Lex HS-level regulation scope | #20 | Low | Stage-specific regulation appears |
| SEC adapter cleanup | — | Low | Generic pipeline audit |
| SEC paginated history fetch | — | Low | Workstream B scope |
| MCS PDF Section 1 tech debt (1.2-1.6) | #58 | Low | Section 4 walkthrough first |
| MCS PDF LLM cache invalidation (2.1) | — | **Med** | Section 9 walkthrough first |
| MCS PDF Section 2 minor tech debt (2.4-2.7, 2.9) | — | Low | Section 9 walkthrough first |
| PDF walkthrough Sections 3-10 | #56 | Active | In progress |
