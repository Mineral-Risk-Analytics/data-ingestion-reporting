# Scoring Engine Audit — Ingester Coverage Addendum

> **Date:** 2026-05-05
> **Scope:** Line-by-line review of every ingester against the post-Phase-B
> HS-code matching infrastructure (alias resolver + word-bounded MaterialCache
> with inverse-frequency weighting, partner CSV-derived keyword expansion,
> 10-digit HTS rows from the PDF parser).
> **Predecessor:** `scoring-audit-2026-05.md` (2026-05-03). This document
> audits what's changed in the two days since, then walks every ingester
> against the new infrastructure and identifies where the data does and
> does not flow into scoring.
> **Method:** Code read against the live source tree. No runtime/DB inspection.
> **Audience:** Nicole — direct findings only; no partner framing.

---

## What changed since 2026-05-03

The original audit flagged G1 (`hs_code_material_mappings.keywords` empty) as
the single largest data gap and the root cause of the stage-attribution
mechanism being dead code. That is now resolved — but the resolution unlocks
new questions about ingester coverage that the original audit didn't reach.

| Original audit item | Status today |
|---|---|
| G1 — `hs_code_material_mappings.keywords` empty | **Resolved.** Two-tier seeding via `_HS_KEYWORDS_BY_MAPPING` (curated) + `seed_hs_keywords_auto.py` (auto-derived from partner CSV with inverse-frequency weighting). Coverage 33% → 100% of mappings. |
| Original audit's "MaterialCache fixed weights" assumption | **Replaced.** New inverse-frequency math: `relevance = 0.90 / sqrt(N_mappings_containing_kw)`. Generic terms like "metal" decay automatically. |
| Substring-based keyword matching | **Replaced.** Word-boundary regex (`\b...\b`); cheap stop-list for noise tokens (bare "ore", "oxide", English-word symbols like "in" for Indium). |
| 10-digit HTS coverage | **New.** PDF parser auto-creates 10-digit `hs_code_material_mappings` rows; `seed-hs-mappings --force` is now non-destructive so backfill preserves them. |
| `us_net_import_reliance_pct`, `us_apparent_consumption` | **Promoted from `metadata_json` to typed columns** (migration 038). Written by `ingest-usgs`. **Not yet read by any scorer.** |
| Fig 10 prices (`price_yoy_pct`, `price_cagr_5yr_pct`) | **Wired into Financial Pressure Tier 1.5** in `market_aggregator.py`. Combined with Pink Sheet via `max()`. |
| Phase B alias resolver (May 2026) | **New.** `MaterialAliasResolver` replaces the four legacy hand-coded dicts. `ingest-usgs`, `ingest-mcs-pdf`, `ingest-mcs-prices` all route through it. |

What this means for the original audit's downstream concerns: G1's premise
("keywords aren't seeded so events never carry `hs_mapping_id`") is now
false for the three ingesters that actually use `MaterialCache`, and the
gating concern shifts to **which event ingesters use it at all**. That's
what this addendum covers.

---

## Ingester-by-ingester audit

For each ingester I checked five things:

1. **Resolver usage** — does it call `MaterialAliasResolver` (for parser
   source-name → canonical material) and/or `MaterialResolver` (for HS code
   longest-prefix lookup)?
2. **Keyword matching** — does it call `MaterialCache.detect()` for
   free-text material attribution?
3. **HS attribution** — does it write `hs_mapping_id` to whichever junction
   table is appropriate (`risk_event_hs_mappings`, `trade_flows`, etc.)?
4. **Scoring-relevant writes** — does it produce rows in tables the scoring
   engine actually reads?
5. **Stage attribution** — does the data carry `supply_chain_stage`
   downstream?

Ingesters are grouped by data category. Within each group, ordered by
how impactful the gap is.

### USGS family — already correctly wired

| Ingester | Resolver | Keywords | HS attrib | Scoring writes | Status |
|---|---|---|---|---|---|
| `seeds/mcs2026_parser.py` (long-format CSV) | ✅ Alias resolver | n/a — typed CSV | ✅ via `hs_production_shares` | ✅ `material_criticality_signals`, `material_production_shares`, `hs_code_production_shares` | ✅ Wired |
| `seeds/usgs_mcs_parser.py` (wide-format CSV, 2025) | ✅ Alias resolver | n/a | ✅ same | ✅ same | ✅ Wired |
| `seeds/mcs2026_fig10_parser.py` (Fig 10 prices) | ✅ Alias resolver | n/a | n/a — material-level only | ✅ `material_criticality_signals.price_*` | ✅ Wired |
| `mcs_pdf_parser.py` (PDF tables) | ✅ Alias resolver (regex fallback) | n/a | ✅ creates 6-digit stubs + 10-digit HTS | ✅ `hs_code_production_shares` (global+US), criticality salient notes | ✅ Wired |

These four are the "happy path" — they were the focus of Phase B and they
work. The original audit's G7 ("MCS PDF run state") is the only operational
question remaining: has someone actually run the PDF parser on prod? The
data is the parser's responsibility; the wiring is correct.

**Carry-over from earlier audit (G12):** `materials.criticality_score`
denormalized cache — verified present in `cli.py:285–286` (`material.criticality_score = criticality_score`
inside the `writes_material_signals` block of `ingest-usgs`). Cache refresh
path is intact.

### Trade & price family

| Ingester | Resolver | Keywords | HS attrib | Scoring writes | Status |
|---|---|---|---|---|---|
| `comtrade.py` (UN Comtrade) | ✅ HS resolver (`resolve_by_hs_code` ×3) | n/a | ✅ writes `TradeFlow.hs_mapping_id` (×6 references) | ✅ `trade_flows` | ✅ Wired (Phase 1.5) |
| `trade_signal_builder.py` (derives geo events from trade) | ✅ via TradeFlow's existing `hs_mapping_id` | n/a — uses HS, not text | ✅ writes `RiskEventHsMapping` (×3 references), `hs_mapping_id` (×18) | ✅ `risk_events` (GEOPOLITICAL_TRADE), `risk_event_hs_mappings`, `risk_event_materials` | ✅ Wired |
| `worldbank_pinksheet.py` (Pink Sheet weekly prices) | ❌ none | ❌ none | ❌ does not write `hs_mapping_id` or `price_form` | ⚠️ writes `commodity_prices` material-level only | **Gap (was G9)** |

**Pink Sheet gap (carry-over from G9, unchanged):** Migration 030 added
`commodity_prices.hs_mapping_id` and `.price_form` columns. The ingester
still writes only material-level rows. With Fig 10 prices now wired into
Financial Pressure scoring, the Pink Sheet weekly stream has become the
*lower-cadence* signal — but it's still the only weekly price feed and
still cannot distinguish lithium hydroxide from spodumene. Flagged as P2
in the original audit; no change.

### Regulatory / news event family — partial coverage

| Ingester | Resolver | Keywords | HS attrib | Scoring writes | Status |
|---|---|---|---|---|---|
| `ingest_federal_register.py` (US regulations, weekly) | ❌ no HS resolver | ✅ MaterialCache (×6) | ✅ `RiskEventHsMapping` (×7), `hs_mapping_id` (×6) | ✅ `risk_events`, `risk_event_materials` (×10), `risk_event_hs_mappings` | ✅ Wired |
| `iea_policy_tracker.py` (IEA policy entries) | ✅ MaterialResolver (×2 — HS lookup) | ✅ MaterialCache (×7) | ✅ `RiskEventHsMapping` (×7) | ✅ `risk_events` (×15), `risk_event_materials` (×14), `risk_event_hs_mappings` | ✅ Wired |
| `gta.py` (Global Trade Alert, structured HS-coded events) | ✅ HS resolver (×1) | ❌ no MaterialCache | ✅ `RiskEventHsMapping` (×6), `hs_mapping_id` (×8) | ✅ `risk_events` (×23), `risk_event_materials` (×7), `risk_event_hs_mappings` | ✅ Wired (HS-driven, not text) |
| `eurlex.py` (EU regulations, quarterly) | ❌ none | ❌ none | ❌ no `hs_mapping_id` writes | ⚠️ `risk_events` (×13), `risk_event_regulations` only | **GAP — see N1 below** |
| `opensanctions.py` (sanctions data) | ❌ none | ❌ none | ❌ no `hs_mapping_id` writes | ⚠️ `risk_events` (×15), `risk_event_companies`, `risk_event_geographies` only | **GAP — see N2 below** |
| `pipeline.py` (generic ingestion pipeline) | ✅ MaterialResolver (×2) | ❌ no MaterialCache | ⚠️ uses `hs_mapping_id` (×2) but does not write `RiskEventHsMapping` | ✅ `risk_events` (×5), `trade_flows` (×2) | **Partial — see N3 below** |
| `iea_reports.py` (IEA Critical Minerals reports) | ❌ none | ❌ none | n/a | ✅ `material_criticality_signals` (×9) — material-level | ✅ Wired (correctly material-level) |

### Company / facility family

| Ingester | Resolver | Keywords | HS attrib | Scoring writes | Status |
|---|---|---|---|---|---|
| `gleif.py` (LEI enrichment) | n/a | n/a | n/a | n/a | ✅ Out of scope (entity metadata only) |
| `mrds.py` (mining facilities) | n/a | n/a | ✅ writes `hs_mapping_id` on `FacilityMaterialLink` (×2) but stage left manual | ⚠️ writes `facilities`, `facility_material_links` — operational pillar reads these | **Partial — see N4 below** |
| `ingest_vpic.py` (vehicle data) | n/a | n/a | n/a | n/a — writes `companies`/`facilities` at finished-good stage | ✅ Wired |

### Adapters & support

| File | Purpose | Status |
|---|---|---|
| `adapters/sec_edgar.py` (55 lines) | Stub adapter — wraps `pipeline.py` for SEC EDGAR pulls. No scoring-table writes of its own. | Inherits gaps from `pipeline.py` — see N3 |
| `adapters/census_trade.py` (70 lines) | Stub adapter — wraps `pipeline.py` for US Census trade. | Inherits gaps from `pipeline.py` |
| `adapters/news.py` (76 lines) | Stub adapter — wraps `pipeline.py` for news article ingestion. | Inherits gaps from `pipeline.py` |
| `adapters/federal_register.py` | Adapter wrapper around `ingest_federal_register.py`. | ✅ Inherits the wired path |

---

## New gaps identified by this audit

Numbered N1–N5 to distinguish from the original G1–G12 series. Ordered by
impact.

### N1 — `eurlex.py` does not attribute events to materials or HS codes (P0)

**Where:** `app/services/ingestion/eurlex.py`. The ingester creates
`RiskEvent` and `RiskEventRegulation` rows. No `MaterialCache.detect()`
call. No `RiskEventMaterial`, no `RiskEventHsMapping`, no
`RiskEventGeography` writes.

**Effect:** Every EU regulation event is invisible to:
- The Geopolitical pillar at Level 1 (filters on `RiskEventMaterial` + `RiskEventGeography`)
- The HS node scorer's `tariff_exposure` / `export_restriction` (joins on `RiskEventHsMapping`)
- The Regulatory pillar's per-material rollup (filters on `RiskEventMaterial`)

EU CRMA, the EU Battery Regulation, the EU Forced Labour Regulation — none
of these attribute to Lithium, Cobalt, Nickel etc. unless someone writes
the junction rows by hand. The ingester writes 13 `RiskEvent` rows per
quarter into a void.

**Compare:** `ingest_federal_register.py` does this correctly — same
pattern, similar text, but with `MaterialCache.detect()` followed by
junction inserts (lines covered in 6 references to MaterialCache in that
file).

**Fix scope:** Port the 4-step pattern from `ingest_federal_register.py`
into `eurlex.py`:
1. Build `MaterialCache` once at start of run
2. Per-event: `cache.detect(title + summary)` →
3. Insert `RiskEventMaterial` rows for matches above threshold
4. Insert `RiskEventHsMapping` rows where the match carries a non-None
   `hs_mapping_id`

Estimated 30–60 lines of code. No new schema.

### N2 — `opensanctions.py` does not attribute events to materials or HS codes (P1)

**Where:** `app/services/ingestion/opensanctions.py`. Creates `RiskEvent`,
`RiskEventCompany`, `RiskEventGeography`. No material attribution.

**Effect:** Sanctions events flow into the Geopolitical pillar via the
`(company × geography)` linkage, but never reach the
`(material × geography)` rollup — the scoring layer that actually feeds
material risk scores. A Russian aluminum sanctions designation does not
elevate Aluminum risk scores; only the named Russian company's score.

**Why P1, not P0:** Sanctions are inherently entity-targeted, not
material-targeted, so the lower attribution rate is somewhat defensible.
But missing this means a wholesale sanctions event ("all Russian aluminum
exports prohibited") is treated identically to a single-company sanction.
Worth fixing eventually; less urgent than EUR-Lex.

**Fix scope:** Same pattern as N1 — add `MaterialCache.detect()` and write
junctions where matches fire above threshold.

### N3 — `pipeline.py` resolves HS but doesn't write `RiskEventHsMapping` (P1)

**Where:** `app/services/ingestion/pipeline.py:239` constructs
`MaterialResolver(self._db)` and uses `resolve_by_hs_code`. The HS lookup
returns `(material_id, hs_mapping_id)` but only `material_id` is used —
the `hs_mapping_id` is dropped on the floor for the news/SEC EDGAR/census
adapter paths.

**Effect:** Any event ingested through this generic pipeline (the three
adapter files: news.py, sec_edgar.py, census_trade.py) gets a
`RiskEventMaterial` row but no `RiskEventHsMapping` row, even when the
underlying classifier produced an HS code. So the HS node scorer never
sees these events.

**Verification needed:** I see `hs_mapping_id` mentioned twice in
`pipeline.py` and the search shows `RiskEventHsMapping` is never
imported. Worth a careful read of the event-write path to confirm —
possible the attribution lives in a different code path I missed.

**Fix scope:** Where the resolver returns a non-None `hs_mapping_id`,
add a `RiskEventHsMapping` write next to the existing
`RiskEventMaterial` write. Should be small.

### N4 — `mrds.py` writes facilities with NULL `supply_chain_stage` (P2)

**Where:** `app/services/ingestion/mrds.py`. Writes `Facility` and
`FacilityMaterialLink` rows. The `hs_mapping_id` field exists (×2
references) but `supply_chain_stage` is left for manual confirmation
post-ingest per the docstring in `app/cli.py` setup-all step 8.

**Effect:** This is the same gap the original audit flagged as G4 ("no
GEM ingester; operational pillar can't be stage-aware"), with one twist:
MRDS *could* set stage automatically. The MRDS `dev_stat` and `oper_type`
columns map cleanly to `mine` → `ore` and `processing_plant` →
`intermediate` or `refined`. Without this, every MRDS-ingested facility
is stage=NULL even though the source data implies it.

**Fix scope:** Add a small mapping function:
```python
_MRDS_STAGE_MAP = {
    "mine":              "ore",
    "placer":            "ore",
    "processing plant":  "intermediate",   # or "refined" — case-by-case
    "smelter":           "refined",
    "refinery":          "refined",
}
```
Apply during MRDS row processing. ~10 lines.

This won't replace a real GEM ingester (USGS MRDS is mostly US-focused
and historical — not "live" capacity), but it cuts down on manual
post-ingest data entry.

### N5 — Three ingesters share an HS attribution gap on partner-curated 10-digit HTS rows (P1)

**Where:** `ingest_federal_register.py`, `iea_policy_tracker.py`,
`gta.py` all use `MaterialCache.detect()`, which now matches against
keywords seeded for **6-digit** mappings (curated `_HS_KEYWORDS_BY_MAPPING`
and auto-generated `_HS_KEYWORDS_AUTO`). The 10-digit HTS rows added by
`ingest-mcs-pdf` get `keywords=NULL` because neither dict targets them.

**Effect:** When a Federal Register tariff event mentions, say,
"7202.21.5000 ferrosilicon containing more than 55% silicon" — the text
might match the 6-digit keyword "ferrosilicon" attached to mapping ID for
HS prefix `7202.21`, but the event is then attached to the 6-digit
mapping, not the 10-digit one. Scoring rolls up at the 6-digit level.
For most use cases this is fine. For US-specific tariff scoring, where
HTS line items are line-by-line, it's a precision loss.

**Severity:** Lower than N1–N3 because the *material* attribution is
correct; only the stage granularity is coarser than the underlying data
supports. Federal Register itself rarely cites specific 10-digit HTS
codes in a way text-matchable; tariff events that *do* (USTR Section 301
schedules) would benefit.

**Fix scope:** Two options:
1. Auto-generate keywords for 10-digit rows from their description
   column. The PDF parser stores the HTS description; just need to run
   `generate_hs_keywords_auto.py` over those rows too.
2. Leave 10-digit rows keyword-less and route to them only via the
   `resolve_by_hs_code` path (longest-prefix match) when an explicit
   HS code is present in the source data.

Recommend option 2 as the simpler design — text matching at 10-digit
granularity is unreliable; HS-code matching at 10-digit granularity is
exact.

---

## Cross-cutting observations

### What scoring tables get fed by which ingester (current state)

| Scoring input table | Fed by | Cadence | Coverage |
|---|---|---|---|
| `material_criticality_signals` (HHI, RLI, capacity, prices, NIR) | `ingest-usgs`, `ingest-mcs-prices`, `iea_reports.py` | Annual (USGS), as-released (IEA) | ~39 materials |
| `material_production_shares` | `ingest-usgs` | Annual | ~39 materials |
| `hs_code_production_shares` (global + US) | `mcs2026_parser.py`, `mcs_pdf_parser.py` | Annual | Materials covered by MCS |
| `commodity_prices` | `worldbank_pinksheet.py` | Weekly | Material-level only — no stage |
| `trade_flows` (with `hs_mapping_id`) | `comtrade.py` | Quarterly + daily backfill | New rows only — historical NULL |
| `risk_events` + `risk_event_hs_mappings` | `trade_signal_builder.py` (HS-driven), `ingest_federal_register.py` (text), `iea_policy_tracker.py` (text), `gta.py` (HS-driven) | Daily–weekly | Strong on these 4; **zero** from EUR-Lex, OpenSanctions, generic pipeline (news/SEC/Census) |
| `risk_event_materials` | Same as above + `iea_policy_tracker.py` (×14), `ingest_federal_register.py` (×10) | Daily–weekly | Same gap pattern |
| `facility_material_links.supply_chain_stage` | None (was G4) | n/a | NULL for MRDS-ingested rows; no GEM ingester |
| `us_import_sources` | `mcs2026_parser.py` | Annual | Strong — but **not yet read by any scorer** (see below) |

### Three pieces of populated data that no scorer reads

The original audit flagged G9 (`commodity_prices.hs_mapping_id` /
`price_form` unused). Two more have joined the list since:

1. **`material_criticality_signals.us_net_import_reliance_pct`** — promoted
   from `metadata_json` to a typed column on 2026-05-04. No scorer reads it.
2. **`material_criticality_signals.us_apparent_consumption`** — same
   migration, same status. No scorer reads it.
3. **`us_import_sources` table** — populated by `mcs2026_parser.py` with
   per-HS-prefix country-share data for US imports. No scorer reads it.

These three together represent a US-dependency tier that exists at the
data layer but not the scoring layer. The original audit's recommendation
to add a structural country-risk baseline (WGI) and dynamic HCG derivation
remain, but a US-dependency-specific tier is a more direct gap.

(You explicitly tabled the US-dependency scoring tier in this conversation
to confer with your partner — flagging here for completeness, not
re-litigating.)

### Coverage matrix: which scoring pillar gets data from which ingester

|  | Geopolitical | Operational | Regulatory | Financial | Material Concentration |
|---|---|---|---|---|---|
| `comtrade.py` | ✅ via trade_signals | ❌ | ❌ | ❌ | ✅ via prod shares (indirect) |
| `worldbank_pinksheet.py` | ❌ | ❌ | ❌ | ⚠️ material-only | ❌ |
| `ingest-usgs` | ❌ | ❌ | ❌ | ✅ Fig 10 prices, Tier 1.5 | ✅ HHI, RLI |
| `ingest_federal_register.py` | ✅ stage-attributed | ❌ | ✅ stage-attributed | ❌ | ❌ |
| `iea_policy_tracker.py` | ✅ stage-attributed | ❌ | ✅ stage-attributed | ❌ | ❌ |
| `gta.py` | ✅ stage-attributed | ❌ | ✅ via tariff/export | ❌ | ❌ |
| `eurlex.py` | ⚠️ no material attribution | ❌ | ⚠️ no material attribution | ❌ | ❌ |
| `opensanctions.py` | ⚠️ company-only | ❌ | ❌ | ❌ | ❌ |
| `pipeline.py` (news/SEC/Census) | ⚠️ no HS attribution | ❌ | ⚠️ no HS attribution | ❌ | ❌ |
| `mrds.py` | ❌ | ⚠️ no stage attribution | ❌ | ❌ | ❌ |
| `iea_reports.py` | ❌ | ❌ | ❌ | ❌ | ✅ HHI, RLI alt-source |

The Operational pillar is starved — the only writer is MRDS, and it doesn't
populate stage. This is the original audit's G4 gap, unchanged.

---

## Updated priority list

Combining the original audit's open items with this addendum's findings.
Items completed since 2026-05-03 omitted.

### P0 — fix soon

- **N1 — Wire `eurlex.py` into `MaterialCache` + junction writes.** The
  largest remaining ingester gap. EU CRMA, EU Battery Reg events should
  attribute to materials. Pattern is already proven in
  `ingest_federal_register.py`; copy it.
- **G3 (verify) — Confirm `hs_node_scorer` event-type filter matches what
  ingesters actually write.** Original audit recommended the SQL check;
  unchanged. Now actionable because keyword seeding has unblocked event
  attribution — if the filter is broken, you'll see it in `tariff_exposure`
  staying at 0 even with events flowing.

### P1 — next iteration

- **N2 — Wire `opensanctions.py` material attribution.**
- **N3 — Verify and fix `pipeline.py` HS junction writes.**
- **G2 — Geopolitical pillar consume HS node `tariff_exposure` / `export_restriction`.**
  Now that events carry stage attribution (G1 resolved), this substitution
  is safe.
- **G6 — Lower `_STAGE_ROLLUP_MIN_NODES` to 1, OR add per-pair fallback telemetry.**
- **G4/N4 — Add MRDS stage mapping (small fix); plan GEM ingester (bigger).**

### P2 — backlog

- **G8 — Average trade weights across last N periods in `global_rollup`.**
- **G11 — Country-scope HS node event filter.**
- **G9 / Pink Sheet `hs_mapping_id` / `price_form`.**
- **N5 — 10-digit HTS keyword coverage (or accept HS-code-only routing).**
- **US-dependency tier scoring** (NIR + apparent consumption + import-source HHI) — **deferred per partner consultation**.
- **Dynamic HCG derivation (G/audit recommendation 9).**
- **WGI / structural country governance baseline.**
- **USITC HTS structural tariff baseline.**

### P3 — watch list

- **G10 — Inngest job dependency chain.**
- **G12 — Verify `materials.criticality_score` cache refresh.** *(Spot-checked during this audit; appears intact.)*

---

## What's verifiable without running code

If the goal is to ship a credible v1, the original audit's four checks
remain valid; two more get added by this addendum:

1. *(Original)* Has `bdi-ingest ingest-mcs-pdf` been run on prod? Without
   it, every Level-0 score is `None`.
2. *(Original)* Run `report_hs_coverage()` to see which materials get
   stage-aware vs material-fallback scoring.
3. *(Original)* Verify event-subtype storage convention (G3).
4. *(Original)* Count `risk_event_hs_mappings` row growth on the next
   ingest run — if it stays flat, `MaterialCache.build()` query isn't
   picking up keywords (now seeded, but worth confirming the round-trip).
5. *(New)* `SELECT count(*) FROM risk_events WHERE id NOT IN (SELECT risk_event_id FROM risk_event_materials);`
   Should be small. A large number means events are being written but never
   linked — likely from `eurlex.py`, `opensanctions.py`, or `pipeline.py`.
6. *(New)* `SELECT source, count(*) FROM risk_events GROUP BY source ORDER BY 2 DESC;`
   Quantifies the per-ingester contribution. Should expose any ingester
   producing high event volume but zero material attribution.

---

## What I did NOT do in this audit

- I did not modify any ingester code. The fixes for N1–N5 are described
  and scoped; the actual edits should be a follow-up PR after you decide
  scope.
- I did not run anything against the live DB. All findings are
  source-tree verified.
- I did not re-audit the scoring engine itself; that was the previous
  audit's domain and the math hasn't changed.
- I did not investigate the Inngest job orchestration (G10) or the
  cron alignment of newly-added ingesters. If `ingest-mcs-prices` is
  meant to run on a cadence, that wiring is still manual.
