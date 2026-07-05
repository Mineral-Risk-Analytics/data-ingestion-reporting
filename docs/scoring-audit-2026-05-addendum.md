# Scoring Engine Audit — Ingester Coverage Addendum

> **Date:** 2026-05-05 (last status update 2026-05-11)
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

## Status as of 2026-05-06

The N1–N5 gaps and most of the G-series carry-overs from the original audit
have landed.  Updated table:

| Item | 2026-05-05 status | 2026-05-06 status |
|---|---|---|
| N1 — `eurlex.py` material/HS attribution | P0 open | **Resolved.** EU regulations refactor; uses `RegulationMaterialScope` (not `RiskEventMaterial`) — verified scoring path. |
| N2 — `opensanctions.py` material/HS attribution | P1 open | **Resolved.** Writes `CompanyMaterialExposure` for company events, `MaterialProductionShare` for geo events. |
| N3 — `pipeline.py` HS junction writes | P1 open | **Resolved (census trade) + partial (rest).** Census trade attribution wired. SEC EDGAR migrated to dedicated `ingest_sec_edgar.py` module. News still uses pipeline.py (and is still a `StubNewsProvider` — no real-world impact today). |
| N4 — `mrds.py` writes facilities with NULL `supply_chain_stage` | P2 open | **Resolved.** Granular `MRDS_OPER_TYPE_MAP` + name-keyword fallback (`refinery`/`concentrator`/`quarry`/etc.) infers stage for the 48% of MRDS rows with empty/unknown `oper_type`. Active-only filter (skip Past Producer + Prospect) cuts ingest volume from 304k → ~26k rows. `facility_type`+`supply_chain_stage` mutable on re-run. |
| N5 — 10-digit HTS keyword coverage | P1 open | Still open. Optional — HS-code-only longest-prefix routing remains acceptable. |
| G2 — Geopolitical pillar consumes HS node sub-scores | P1 open | **Resolved.** Now aggregates HS node `tariff_exposure` / `export_restriction` via `STAGE_ROLLUP_WEIGHTS` when nodes exist. |
| G3 — `_TARIFF_SUBTYPES` filter alignment | P0/P1 verify | **Resolved.** Migration 040 promoted `event_subtype` to typed column. Both ingesters and scorer aligned. |
| G4a — MRDS auto-stage | P1 open | **Resolved (= N4).** |
| G4 Half 1 — Operational pillar stage-awareness | Not yet scoped | **Resolved (2026-05-06).** Stage-aware structural_dependency lands. Per-stage breakdown in rationale_json. No default-fill for missing stages. |
| G4b — GEM Iron Ore Mines | P1 open | Still open. Bounded scope (LFP/iron-ore only). |
| G4c — Partner-curated facility seed | P1 open | **Engineering: LANDED 2026-05-09.** Loader (`seed_facilities_partner.py`) + CLI + synthetic test (9/9). XLSX template at `Automotive Data Solutions/facility_seed_template.xlsx`. Data still pending — partner is filling in the launch-list materials. |
| G4d — Paid subscription | P1 future | Still future. |
| G6 — `_STAGE_ROLLUP_MIN_NODES = 2` | P1 open | **Resolved 2026-05-09.** Threshold lowered to 1. Measurement against launch-10 showed 22/36 (61%) of pairs were falling to legacy fallback at threshold=2. Single-node rollup math is identity (`(node × stage_weight) / stage_weight = node`); Option-1 fallback covers the no-data case. |
| G7 — Equal-weight fallback measurement | P0 (deferred) | **Resolved.** `coverage.py::report_hs_coverage()` provides the aggregate. Also fixed `banned` scope_type missing from `_SCOPE_TYPE_WEIGHT`. |
| G8 — `_trade_weights` single-period fragility | P2 | **Resolved (2026-05-06).** 3-year rolling annual / 12-month rolling monthly. Also fixed mixed-period bug where Census `YYYY-MM` lex-won over Comtrade `YYYY`, silently zeroing non-US weights. |
| G9 — `commodity_prices.hs_mapping_id` / `price_form` | P2 | **Resolved** (verified 2026-05-06). Pink Sheet ingester now writes both columns via `_HEADER_TO_HS_PREFIX` (11 headers, covers LME-convention metals + battery-grade lithium). Bare-metal headers (Graphite, Manganese without "ore") correctly get NULL — Pink Sheet doesn't disclose the form. |
| G10 — Inngest dependency chain | P3 | Still open. Watch list. |
| G11 — HS node event filter ignores country scope | P2 | **Resolved (2026-05-06).** Country-scope filter + `geography_context` differentiation (tariff = `affected`, export = `primary`). Concurrent GTA Scope 2 fixes: CPC rejection, Affected Jurisdictions parsing, severity multipliers (subnational/supranational/horizontal/multilateral), `_TARIFF_SUBTYPES` expanded to include `IMPORT_DISRUPTION` (was matching nothing). |
| G12 — Cache refresh path verified | P3 | Still verified intact. |

### New items discovered 2026-05-06

* **EXPORT_SUBSIDY / TRADE_FINANCE event_subtype.** While decoding GTA's
  `NFI` (National Financial Institution) and `IFI` (International Financial
  Institution) implementation-level codes, identified that 306 of 1728
  battery-relevant interventions are subsidy-type events (state loans, EXIM
  trade finance, EIB financial-investment-support).  None map to existing
  `_INTERVENTION_SUBTYPE_MAP` keys, so they get `event_subtype=NULL` and skip
  all HS-node sub-scores.  Distinct scoring question from sourcing risk —
  new sub-score, not a fix.  Logged for future consideration.
* **GTA `NFI` / `IFI` implementation_level multipliers.** Decoded; both now
  default to 1.0× since they describe subsidy interventions that don't
  feed the HS-node sub-scores anyway.

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
| `worldbank_pinksheet.py` (Pink Sheet weekly prices) | ✅ MaterialAliasResolver | n/a | ✅ writes `hs_mapping_id` + `price_form` (11 headers covered) | ✅ `commodity_prices` with stage attribution where Pink Sheet discloses form (LME-convention metals + battery-grade lithium); NULL for ambiguous headers | ✅ Wired (G9 resolved) |

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
| `eurlex.py` (EU regulations, quarterly) | ✅ Regulations refactor (RegulationAliasResolver) | n/a — typed CELEX manifest | ✅ via `RegulationMaterialScope` + `RegulationGeographyScope` | ✅ `regulations`, `regulation_material_scopes`, `regulation_geography_scopes` — scoring path verified to use these (not `RiskEventMaterial`) | ✅ Wired (was N1 — resolved 2026-05-06) |
| `opensanctions.py` (sanctions data) | ✅ for material attribution | n/a — entity-driven, not text | ✅ writes `CompanyMaterialExposure` for company events + `MaterialProductionShare` for geo events | ✅ `risk_events`, `risk_event_companies`, `risk_event_geographies`, `company_material_exposures`, `material_production_shares` | ✅ Wired (was N2 — resolved 2026-05-06) |
| `pipeline.py` (generic ingestion pipeline) | ✅ MaterialResolver | ✅ MaterialCache wired | ✅ writes `RiskEventHsMapping` for census trade path; SEC EDGAR migrated to dedicated `ingest_sec_edgar.py` | ✅ `risk_events`, `trade_flows`, junctions on census/SEC paths | Partial (was N3) — census + SEC done; news still uses pipeline.py but is a `StubNewsProvider` (no real-world impact today) |
| ~~`iea_reports.py`~~ (REMOVED 2026-05-06) | n/a | n/a | n/a | n/a | ❌ Removed — overlapped with usgs_mcs for criticality signals + had a SourceDocument schema bug. See `_CRITICALITY_SOURCE_PRIORITY` change in `market_aggregator.py`. |

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

> **STATUS 2026-05-06: RESOLVED.** The fix landed via the regulations
> refactor (migration 039 + `seed_regulation_aliases.py` +
> `RegulationAliasResolver`).  EU regulations are now modelled as
> `Regulation` rows with `RegulationMaterialScope` and
> `RegulationGeographyScope` junction tables — a different (and better)
> attribution path than `RiskEventMaterial` / `RiskEventHsMapping`.
> Verified the scoring path uses the new tables: `evidence_query`'s
> regulation reads filter on `Regulation.verified=True` (audit gap G7-
> related fix) and join through the scope tables.  Original "wire up
> MaterialCache" prescription below is OBE — the regulations refactor
> obviated it.  Original analysis preserved for context.

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

> **STATUS 2026-05-06: RESOLVED.** Sanctions events now write
> `CompanyMaterialExposure` rows for company-targeted events and
> `MaterialProductionShare` rows for geography-level events.  The
> "wholesale Russian aluminum sanctions" scenario from the original
> framing now correctly elevates Aluminum × RU material exposure,
> not just the named entity's company score.  Verified end-to-end.
> Original analysis preserved for context.

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

> **STATUS 2026-05-06: PARTIALLY RESOLVED.**
>   * **Census trade**: fixed in pipeline.py — passes `material_id` AND
>     `hs_mapping_id` through to `_add_risk_event` so junctions land for
>     trade-derived events.
>   * **SEC EDGAR**: extracted to a dedicated `ingest_sec_edgar.py`
>     module with its own MaterialCache wiring.  CLI + Inngest job both
>     point at the new module.  No longer goes through pipeline.py.
>   * **News**: still uses pipeline.py.  Today the news adapter is just
>     `StubNewsProvider` — no real-world impact until a real news
>     provider (NewsAPI / GDELT / licensed feed) is wired in.  When that
>     happens, the news ingest path will need the same MaterialCache +
>     junction-write treatment.
>
> Original analysis preserved for context.

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

> **STATUS 2026-05-06: RESOLVED.** `mrds.py` now sets
> `supply_chain_stage` on every FacilityMaterialLink row using a
> two-step inference: (1) `MRDS_OPER_TYPE_MAP` translates `oper_type` →
> facility_type → stage; (2) for the ~48% of rows where `oper_type` is
> empty/unknown, a name-keyword fallback (`_classify_from_name`) parses
> `site_name` / `names` for keywords like "refinery" / "concentrator"
> / "quarry".  Active-only filter (skip Past Producer + Prospect)
> reduces ingest from 304k → ~26k rows.  The G4 audit gap is half-
> resolved: the **read side** (G4 Half 1) also landed —
> `_facility_structural_dependency` in `market_aggregator.py` is now
> stage-aware and rolls up via `STAGE_ROLLUP_WEIGHTS` with no default-
> fill.  Remaining: G4b (GEM Iron Ore) / G4c (partner-curated seed —
> loader landed 2026-05-09, awaiting partner data) / G4d (paid sub).
> Original analysis preserved.

**Where:** `app/services/ingestion/mrds.py`. Writes `Facility` and
`FacilityMaterialLink` rows. The `hs_mapping_id` field exists (×2
references) but `supply_chain_stage` is left for manual confirmation
post-ingest per the docstring in `app/cli.py` setup-all step 8.

**Effect:** This is part of the same gap the original audit flagged as
G4 ("no GEM ingester; operational pillar can't be stage-aware").  See
the **G4 reframing note** below the original audit's G4 entry — the
"GEM ingester" prescription was based on a misreading of GEM's project
list and is not actually achievable.  MRDS auto-stage mapping is the
realistic free-data path forward.

The MRDS `dev_stat` and `oper_type` columns map cleanly to `mine` →
`ore` and `processing_plant` → `intermediate` or `refined`.  Without
this, every MRDS-ingested facility is stage=NULL even though the
source data implies it.

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

USGS MRDS is mostly US-focused and historical — not "live" capacity —
so this won't fully populate the operational pillar's stage-awareness
pathway for non-US assets.  The operational pillar's global coverage
gap is real, but addressing it requires a paid data subscription
(Benchmark Mineral Intelligence, S&P Global Market Intelligence, or
Wood Mackenzie) — see the G4 reframing below.

### Reframing G4 — "GEM ingester" is not actually achievable

**Date noted:** 2026-05-06.  The original audit (G4 in
`scoring-audit-2026-05.md`) prescribed building a "GEM ingester" to
populate `facility_material_links.supply_chain_stage` and unlock the
operational pillar's stage-awareness pathway.

**Verified by walking the projects page:** Global Energy Monitor
(globalenergymonitor.org) does not publish critical-mineral mining or
processing trackers.  Their full project catalogue is power-generation
trackers (coal, gas, oil, solar, wind, etc.), industrial-emitter trackers
(cement, iron + steel, methane), and one mining tracker — **iron ore
only**.  No lithium, cobalt, nickel, copper, graphite, or REE coverage.

**Source of the original confusion:** there is a separate organisation
called **Energy Monitor** (energymonitor.ai, run by GlobalData / New
Statesman) that publishes a "Critical Mineral Tracker."  That is a
journalism-led aggregate-data product, not an asset-level facility
dataset like GEM's plant trackers.  It would not give us the
facility-level stage attribution the operational pillar needs.

**What this means for G4:** the original prescription is impossible.
The operational stage-awareness gap is real but the path forward is not
a free-data ingester.

**Realistic options to populate `facility_material_links.supply_chain_stage`,
ranked by feasibility:**

| Source | Coverage | Cost | Stage-aware? | Path |
|---|---|---|---|---|
| **USGS MRDS** (auto-stage from `dev_stat`/`oper_type`) | US-heavy + some global, historical | Free | After mapping (N4 fix) | Already partially integrated; close N4 to enable |
| **GEM Iron Ore Mines Tracker** | Iron ore only — relevant to LFP cathodes | Free | Yes | One-off ingester for `Iron Ore` material |
| **Manually curated partner facility list** | Whatever partner can curate by hand | Free (partner labour) | Yes | Spreadsheet → seed file pattern |
| **Benchmark Mineral Intelligence** | Battery-grade Li / Co / Ni / graphite, comprehensive | $$$ paid sub | Yes (their core product) | Subscription decision |
| **S&P Global Market Intelligence — Metals & Mining** | Comprehensive global asset-level | $$$$ paid sub | Yes | Subscription decision |
| **Wood Mackenzie supply chain** | Comprehensive | $$$$ paid sub | Yes | Subscription decision |

**Reframed G4 sub-tasks:**

* **G4a — MRDS auto-stage mapping** (was N4 — small fix, ~10 LOC).
  Populates stage for the US-focused subset of MRDS-ingested facilities.
* **G4b — GEM Iron Ore Mines ingester.** Small, one-off.  Closes the
  iron-ore (LFP) part of the gap with free open data.  Worth doing.
* **G4c — Partner-curated battery-grade facility seed.** Engineering
  landed 2026-05-09: `seed_facilities_partner.py` reads a partner-edited
  XLSX (`Automotive Data Solutions/facility_seed_template.xlsx`) and
  upserts companies → facilities → company_facilities →
  facility_material_links with full stage / HS-mapping / ownership
  capture. CLI: `bdi-ingest seed-facilities-partner <file>` (with
  `--dry-run` for preview). Partner labour ongoing — list known
  facilities for Li, Co, Ni, graphite, REE conversion / anode / cathode
  / cell production. Coverage bounded by partner knowledge but free.
* **G4d — Paid subscription** (Benchmark / S&P / Wood Mackenzie).
  Defer until business case justifies it.  Probably $30–80k/yr at
  startup tier; comprehensive global stage-aware coverage.

**G4a + G4c engineering have shipped.** The operational pillar's
stage-aware path now has a complete code path; what's still NULL in
prod is downstream of the partner data not yet being entered. The G5
audit gap (no operational sub-score in hs_node_scorer composite) is
unblocked from a code-prerequisites standpoint and can be wired up as
soon as the partner seeds her first batch of facilities.

---

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
| `material_criticality_signals` (HHI, RLI, capacity, prices, NIR) | `ingest-usgs`, `ingest-mcs-prices` | Annual | ~39 materials. (`iea_reports.py` removed 2026-05-06 — overlapped + bug.) |
| `material_production_shares` | `ingest-usgs` | Annual | ~39 materials |
| `hs_code_production_shares` (global + US) | `mcs2026_parser.py`, `mcs_pdf_parser.py` | Annual | Materials covered by MCS |
| `commodity_prices` (with `hs_mapping_id` + `price_form`) | `worldbank_pinksheet.py` | Weekly | 11 stage-attributed headers (Cu/Ni/Co/Al/Sn/Zn/Pt at LME convention; Li carbonate battery-grade; Mn ore). Bare metal headers honestly NULL. |
| `trade_flows` (with `hs_mapping_id`) | `comtrade.py` | Daily backfill (until complete, then swap to weekly) | New rows + historical backfill in progress |
| `risk_events` + `risk_event_hs_mappings` | `trade_signal_builder.py` (HS-driven), `ingest_federal_register.py` (text), `iea_policy_tracker.py` (text), `gta.py` (HS-driven, post-Scope-2 fixes) | Daily–weekly | Strong on these 4. EUR-Lex now writes via `RegulationMaterialScope` (different table). News path still routes through pipeline.py but is StubNewsProvider — zero real-world impact. |
| `risk_event_materials` + `risk_event_geographies` | Same as above + opensanctions material attribution (was N2; now writes `CompanyMaterialExposure` + `MaterialProductionShare`) | Daily–weekly | Was-gap-now-resolved on opensanctions side. |
| `regulations` + `regulation_material_scopes` + `regulation_geography_scopes` | `eurlex.py` (was N1; now uses RegulationAliasResolver + scoped writes) | Quarterly | EU CRMA, EU Battery Reg, EU CBAM, etc. — full attribution. |
| `facility_material_links.supply_chain_stage` | `mrds.py` (was N4 / G4a — now sets stage via oper_type map + name-keyword fallback) | Semi-annual auto-download | ~26k rows after active-only filter. Refining-stage coverage thin until G4c partner-curated seed lands. |
| `us_import_sources` | `mcs2026_parser.py` | Annual | Populated — but **not yet read by any scorer** (US-dependency tier deferred per partner). |

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

**Legend** (added 2026-05-06): the ❌ cells have two distinct meanings —
a row not feeding a pillar isn't always a gap.

  * **❌ (expected)** — the source by design doesn't speak to that pillar.
    A trade-volume feed (Comtrade) has nothing useful to say about
    facility status (Operational); a price feed (Pink Sheet) doesn't
    speak to event-driven Geopolitical risk.  Not a gap.
  * **🔶 (gap)** — the source COULD feed that pillar with available data
    but currently doesn't.  These are real coverage holes worth
    tracking, even if they're not P0.
  * **partial** — wired but incomplete.

|  | Geopolitical | Operational | Regulatory | Financial | Material Concentration |
|---|---|---|---|---|---|
| `comtrade.py` | ✅ via trade_signals | ❌ (expected — trade flows, not facility status) | ❌ (expected — volume, not policy) | ❌ (expected — covered by Pink Sheet + Fig 10) | ✅ via prod shares (indirect) |
| `worldbank_pinksheet.py` | ❌ (expected — prices, not events) | ❌ (expected — prices, not facilities) | ❌ (expected — prices, not regulation) | ✅ stage-attributed for 11 LME-convention headers | ❌ (expected — prices don't speak to concentration) |
| `ingest-usgs` | ❌ (expected — supply data, not events) | ❌ (expected — material-level reserve data, not facility-level) | ❌ (expected — supply data, not regulation) | ✅ Fig 10 prices, Tier 1.5 | ✅ HHI, RLI |
| `ingest_federal_register.py` | ✅ stage-attributed | 🔶 (gap — regs about mine permits / EPA shutdowns could feed Operational; not currently parsed for that signal) | ✅ stage-attributed | ❌ (expected — regs aren't a financial-pressure source) | ❌ (expected — events don't speak to concentration) |
| `iea_policy_tracker.py` | ✅ stage-attributed | ❌ (expected — IEA tracks policy, not operational events) | ✅ stage-attributed | 🔶 (gap — INVESTMENT_PLEDGE / state-loan events are subsidy signals; the EXPORT_SUBSIDY subtype on the P2 backlog would close this) | ❌ (expected) |
| `gta.py` (post-Scope-2) | ✅ stage + country-attributed (G11 fix) | 🔶 (gap — export bans curtail supply; could feed Operational structural_dependency, not currently consumed there) | ✅ via tariff/export | 🔶 (gap — IMPORT_DISRUPTION events affect importing-country margins; not yet wired to Financial pillar) | ❌ (expected) |
| `eurlex.py` (post-refactor) | ✅ via RegulationGeographyScope | ❌ (expected — EU regs are policy, not facility status) | ✅ via RegulationMaterialScope | ❌ (expected) | ❌ (expected) |
| `opensanctions.py` (post-N2) | ✅ via geo + CompanyMaterialExposure + MaterialProductionShare | 🔶 (gap — sanctions on a refiner curtail that refiner's output; could feed Operational, currently doesn't) | 🔶 (gap — sanctions ARE regulatory action; currently only feed Geopolitical) | ❌ (expected — sanctions aren't a price or earnings signal) | ❌ (expected) |
| `pipeline.py` (news only — SEC moved out, Census fixed) | partial — news still stub | 🔶 (future gap — news articles routinely cover mine fires / strikes / facility shutdowns; will feed Operational once a real news provider replaces StubNewsProvider) | partial — news still stub | 🔶 (future gap — news covers earnings releases / bankruptcies; will feed Financial post-stub) | ❌ (expected) |
| `ingest_sec_edgar.py` (dedicated module post-N3) | ❌ (expected — SEC filings are corporate-financial, not geopolitical) | 🔶 (gap — 10-K risk-factor sections explicitly disclose facility shutdowns and capacity constraints; not yet parsed for Operational signals) | ❌ (expected — SEC filings aren't a regulation source) | ✅ company-level financial pressure | ❌ (expected) |
| `mrds.py` (post-N4) | ❌ (expected — facility data, not events) | ✅ stage-attributed (operational pillar reads stage breakdown) | ❌ (expected) | ❌ (expected) | 🔶 (gap — facility distribution by country could feed an HHI signal; currently MatConc gets HHI from USGS only) |
| ~~`iea_reports.py`~~ (REMOVED 2026-05-06) | n/a | n/a | n/a | n/a | n/a |

**Read of the gap markers:**

  * The 5 🔶 gaps in the **Operational** column (Federal Register, GTA,
    OpenSanctions, news-via-pipeline, SEC EDGAR) are all "events that
    imply facility curtailment but the operational pillar doesn't read
    them today."  None of these are blocking; the structural_dependency
    signal from MRDS is doing the heavy lifting after the N4 / G4 Half 1
    fixes.  Worth tackling once at least one of G4b/c/d lands and
    structural_dependency has fuller coverage to combine with.

  * The IEA Policy Tracker + GTA gaps in **Financial** point at the same
    underlying gap as the new EXPORT_SUBSIDY/TRADE_FINANCE subtype
    discovery (P2 backlog item) — subsidy events are a real competitive
    signal not currently captured.

  * MRDS contributing to **Material Concentration** is the only gap that
    overlaps with already-good USGS coverage; lower priority.

  * `news-via-pipeline.py` gaps are all marked "future" because the
    news adapter is `StubNewsProvider` today — they'll matter the day
    a real news source is wired in.

The Operational pillar now has stage-aware coverage from MRDS via the
2026-05-06 N4/G4 Half 1 fixes.  Coverage is mining-heavy — refining-stage
data is thin until partner facility data is entered into the G4c
loader (engineering shipped 2026-05-09; data entry ongoing).
Materials whose true bottleneck is refining (Li hydroxide, NMC precursor,
separated REEs) currently show ore-stage-only signals at the operational
pillar; this is honest representation of the data gap rather than a
default-fill.

---

## Updated priority list

Combining the original audit's open items with this addendum's findings.
Items completed since 2026-05-03 omitted.

### Resolved 2026-05-06 (no longer in P0/P1)

- ~~N1 — eurlex material/HS attribution~~ → resolved via regulations refactor (RegulationMaterialScope/GeographyScope)
- ~~N2 — opensanctions material attribution~~ → CompanyMaterialExposure + MaterialProductionShare
- ~~N3 — pipeline.py HS junction writes~~ → census fixed in pipeline.py; SEC EDGAR extracted to dedicated module; news still stub (zero impact)
- ~~N4 / G4a — MRDS auto-stage mapping~~ → granular oper_type map + name-keyword fallback
- ~~G2 — Geopolitical pillar consume HS node sub-scores~~ → STAGE_ROLLUP_WEIGHTS aggregation when nodes exist
- ~~G3 — `_TARIFF_SUBTYPES` filter alignment~~ → migration 040 typed event_subtype column
- ~~G4 Half 1 — Operational pillar stage-aware structural_dependency~~ → per-stage rollup with no default-fill
- ~~G7 — Equal-weight fallback measurement~~ → coverage.py + `banned` scope_type fix
- ~~G8 — Multi-period trade weighting + mixed-period bug~~ → 3-yr annual / 12-mo monthly rolling, separated by granularity
- ~~G9 — Pink Sheet `hs_mapping_id` / `price_form`~~ → 11 LME-convention headers wired
- ~~G11 — Country-scope HS node event filter~~ → Path B Scope 2 with affected/primary geography_context
- ~~G6 — `_STAGE_ROLLUP_MIN_NODES` over-triggers fallback~~ (resolved 2026-05-09) → threshold lowered to 1 after measurement showed 61% fallback firing rate
- ~~G5 — HS node operational sub-score~~ (resolved 2026-05-09) → 4-component composite `0.40×HHI + 0.20×tariff + 0.20×export + 0.20×operational` when facility capacity data is present; auto-fallback to 3-component formula otherwise. Five `score_method` values (`hhi_anchored`, `hhi_anchored_with_operational`, `event_only_no_hhi`, `event_only_with_operational`, `operational_only`).  Synthetic test `scripts/audit/test_g5_operational_subscore.py` covers all paths.
- ~~G-Cov-3 — EXPORT_SUBSIDY event_subtype + geopolitical sub-input~~ (resolved 2026-05-09) → GTA classifies 11 subsidy intervention types, IEA tags INVESTMENT_PLEDGE events, classifier returns 3-bucket split (export/tariff/subsidy), Geopolitical pillar runs 4-component profile (`0.40 × country_concentration + 0.30 × export + 0.20 × tariff + 0.10 × subsidy_distortion`) when subsidy data present.  Synthetic test `scripts/audit/test_gcov_3_export_subsidy.py` covers parser, classifier, and 3-vs-4-component math.
- ~~Tier 1.4-extension — GTA / Comtrade confidence threading~~ (resolved 2026-05-09) → `_resolve_material_id` extended to return `(material_id, hs_mapping_id, confidence)`. GTA event-write loop scales `RiskEventMaterial.relevance_score` and `RiskEventHsMapping.relevance_score` by mapping confidence (best-route-wins when a material has multiple HS-code routes in the same event).  `trade_signal_builder._get_annual_totals` and `global_rollup._aggregate_trade_values_for_material` apply `COALESCE(HsCodeMaterialMapping.confidence, 1.0)` as a multiplier on `TradeFlow.trade_value_usd` so country shares reflect mapping confidence (legacy NULL rows fall through at 1.0).  Census trade already threaded via Tier 1.4 in `pipeline.py`.
- ~~EUR-Lex under-attribution — RegulationMaterialScope → RiskEventMaterial junctions~~ (resolved 2026-05-09) → `eurlex.ingest_eurlex` now writes `RiskEventMaterial` rows from `RegulationMaterialScope` on every newly-emitted event, with relevance keyed off `scope_type` (`banned`=1.00, `restricted`=0.90, `covered`=0.80, `disclosure_required`=0.70). Backfill helper `backfill_regulation_event_materials` adds junctions to pre-existing EUR-Lex events that lack them — idempotent on re-run.  Synthetic test `scripts/audit/test_confidence_and_eurlex_attribution.py` covers CT.1–CT.3 + EL.1–EL.2 (5 groups, all pass).
- ~~pipeline._add_risk_event dedup gap~~ (resolved 2026-05-09) → Census trade events through `pipeline.py._add_risk_event` were inserted without `content_hash` and without an existence check, so every re-run produced duplicate RiskEvent rows. Function now computes `content_hash = sha256(f"{title}|{summary}|{event_date_iso}")` per the standard pattern and returns the existing row when the hash matches. Synthetic test `scripts/audit/test_pipeline_dedup.py` (4 groups).
- ~~Operational tooling — `reset-events` + `reingest-all-events` CLI commands~~ (added 2026-05-09) → `bdi-ingest reset-events --yes` truncates risk_events + 5 junction tables in FK-safe order while preserving source_documents (so re-ingest skips cached fetches). `bdi-ingest reingest-all-events` orchestrates the full re-ingest workflow in dependency order (EUR-Lex → GTA → IEA → OpenSanctions → Federal Register → SEC EDGAR → build-trade-signals). Optional `--reset` flag combines both. Skips ingest-comtrade (confidence weighting applies at query time, re-fetch costs API quota). Dry-run via `--dry-run` on reset-events shows row counts that would be deleted.
- ~~tests/test_comtrade.py drift~~ (fixed 2026-05-09) → The TestResolveMatertialId class expected the pre-Phase-1.5 single-int return signature. Updated to match the current 3-tuple `(material_id, hs_mapping_id, confidence)` signature plus the `dict[str, list[tuple[int, float, int]]]` mock map shape. Now 40/40 passing including new coverage for confidence-flow-through and tied-confidence-returns-None.
- ~~TradeFlow.hs_mapping_id backfill~~ (added 2026-05-09) → `bdi-ingest backfill-trade-flow-hs-mappings` re-resolves rows where `material_id IS NOT NULL AND hs_mapping_id IS NULL` (pre-Phase-1.5 rows that lost the FK). Match → UPDATE; material drift (resolved ≠ stored) → logged + left untouched (never silently rewrites material_id). Cursor-paginates by id to avoid re-counting leftover unresolvable rows. Idempotent. Synthetic test `scripts/audit/test_trade_flow_backfill.py` (5 groups).
- ~~Comtrade reattribution after seed expansion~~ (added 2026-05-09) → Sister command `bdi-ingest reattribute-unmapped-trade-flows` covers the inverse case: rows with `material_id IS NULL`. Writes BOTH material_id and hs_mapping_id when the current seed gives an answer. Logs at WARNING per row (vs DEBUG for backfill) because this IS a data change, not just FK fill-in. Used after expanding `hs_code_material_mappings`. Synthetic test `scripts/audit/test_reattribute_unmapped_trade_flows.py` (7 groups).
- ~~Top-3 unresolved HS prefix coverage~~ (added 2026-05-11) → 6 new mappings added to `seed_hs_mappings.py` covering the high-volume unresolved prefixes from the dev-DB diagnostic: `720250 → Chromium` (only missing 7202 subheading), `281810 / 281830 / 2818 → Aluminum` (Alumina chapter), `262030 → Copper` and `262040 → Aluminum` (battery recycling slag).  Diagnostic confirmed 32 of 274 unresolved trade_flows rows recovered; remaining 242 are noise prefixes (3206 / 2915 / 3818 / 2615 / etc.).  **Conflict flag preserved in seed comments:** the new `262040 → Aluminum @ 1.0` dominates the existing `262040 → Vanadium @ 0.9` entry (Vanadium classification dates from pre-WCO-2022 interpretation; partner review pending).  Synthetic test `scripts/audit/test_hs_seed_unresolved_coverage.py` (7 groups).
- ~~N5 decision — 10-digit HTS keyword coverage~~ (decided 2026-05-11) → **Won't fix; keep current implementation.** Diagnostic over 206 10-digit rows in the dev DB showed 72 (35%) carry information the 6-digit path can't recover: 17 REE disambiguation cases (Dy / Tb / Nd / Pr separated from generic REE), 7 non-REE UNIQUE_ATTRIBUTION cases under multi-metal 6-digit prefixes, 21 RECOVERS_FROM_AMBIGUITY cases where 6-digit returns None. Plus 27 CONFIDENCE_DELTA_ONLY cases tightening attribution via Tier 1.4 confidence threading. The current resolver (longest-prefix match, 10 → 6 → 4) handles all these correctly without modification. No design change needed. Diagnostic script at `scripts/audit/diagnose_10digit_hs_coverage.py` for future audits.
- ~~Parameter-stable content_hash + UPSERT~~ (resolved 2026-05-11) → `trade_signal_builder` and `opensanctions` ingesters used title-based `content_hash` where the title encoded mutable values (percentage in trade signals, sanctioned-entity count in OpenSanctions geo events, `event_date=now()` in OpenSanctions company events). Re-running after a logic change produced new rows alongside the prior ones. Switched all three to parameter-stable identifiers: `f"{event_subtype}\|material_id={mid}\|country={cc}\|year={yr}"` for trade signals; `f"opensanctions_company\|company_id={uuid}"` and `f"opensanctions_geo\|country={iso2}"` for sanctions. On stable-hash match the existing row is UPDATEd in place — title / severity / summary / metadata refreshed, material + HS junctions rewritten (delete + re-insert), company junctions left alone (partial-rewrite per audit decision). `RiskEvent.updated_at` column added via migration 041 with `onupdate=func.now()` so the most-recent-update timestamp is queryable. Also fixed `build_trade_risk_event` to derive event_date from the `period` parameter (`YYYY-MM` / `YYYYMM` / `YYYY`) instead of `now()` so census trade re-runs hit the pipeline dedup correctly. Synthetic test `scripts/audit/test_parameter_stable_upsert.py` (6 groups, all pass).
- ~~NFI/IFI multiplier recalibration~~ (PARKED 2026-05-11) → Survey of dev-DB data (29 NFI + 4 IFI events) drafted; calibration values (NFI=0.75, IFI=0.50) recommended based on structural reasoning (single-country state finance less binding than tariffs; multilateral development bank financing exempt under WTO Subsidies Agreement). Held pending partner input on whether the domain framing matches. Survey script at `scripts/audit/diagnose_gta_nfi_ifi.py`. Will land once partner confirms.

### P1 — open

- **G4b — GEM Iron Ore Mines ingester** (small, iron-ore-only — relevant to LFP cathode).
- **G4c — Partner-curated facility seed.**
   - **Engineering: LANDED 2026-05-09.** `app/services/ingestion/seed_facilities_partner.py` (loader) + CLI `bdi-ingest seed-facilities-partner <file>` + synthetic test (`scripts/audit/test_g4c_partner_facility_loader.py`, 9/9 assertions). Partner template at `Automotive Data Solutions/facility_seed_template.xlsx`.
   - **Data: still pending.** Partner is filling in the launch-list materials (Lithium / Cobalt / Nickel / Graphite / Phosphate / Copper / REE + Manganese / Aluminum / Tungsten as recommended adds). Phosphate has zero facilities currently — flagged as a launch-blocker for that mineral.
   - **G5 operational sub-score is auto-gated on this data.** Until facility rows with non-NULL `annual_capacity_tpy` exist for a (material × stage × country) tuple, the scorer falls back to the 3-component formula for that tuple — no error, just lower informational content.
- **G4d — Paid subscription** (Benchmark / S&P / Wood Mackenzie) — deferred until business case justifies it.

### P2 — backlog

- **N3-news follow-up — wire MaterialCache + junctions when a real news provider replaces StubNewsProvider.** Zero impact today.
- **N5 — 10-digit HTS keyword coverage** (or accept HS-code-only longest-prefix routing as the design).
- ~~EXPORT_SUBSIDY / TRADE_FINANCE event_subtype~~ — closed by G-Cov-3 (resolved 2026-05-09). See resolved-items list above.
- **US-dependency tier scoring** (NIR + apparent consumption + import-source HHI) — **deferred per partner consultation**.
- **Dynamic HCG derivation** — `evidence_query.py:81` still hardcoded as `frozenset({"CN","CD","RU"})`; supply_chain_contexts.high_concentration_geos column unused.
- **WGI / structural country governance baseline.**
- **USITC HTS structural tariff baseline.**
- **GTA NFI/IFI multiplier recalibration** — PARKED pending partner input (see resolved-items list above). Survey + recommended values (NFI=0.75, IFI=0.50) drafted; will land once partner confirms framing.

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
5. *(Updated 2026-05-06)* `SELECT count(*) FROM risk_events WHERE id NOT IN (SELECT risk_event_id FROM risk_event_materials);`
   Should be small after the N1/N2/N3 fixes.  Large counts now
   would point at the news-via-pipeline.py path (still gap-pending) or
   any ingester not yet rerun against the post-fix code.
6. *(Updated 2026-05-06)* `SELECT source_id, count(*) FROM risk_events GROUP BY source_id ORDER BY 2 DESC;`
   Quantifies per-ingester volume.  Cross-reference with `sources` table
   for human-readable names.  Useful after re-ingest to verify each
   ingester is actually producing events.
7. *(New 2026-05-06)* `SELECT geography_context, count(*) FROM risk_event_geographies WHERE risk_event_id IN (SELECT id FROM risk_events WHERE event_subtype = 'IMPORT_DISRUPTION') GROUP BY 1;`
   After the G11 Path B Scope 2 GTA fix + re-ingest, both `primary` AND
   `affected` rows should appear for IMPORT_DISRUPTION events.
8. *(New 2026-05-06)* `SELECT supply_chain_stage, count(*) FROM facility_material_links GROUP BY 1 ORDER BY 2 DESC;`
   After the N4 MRDS fix + re-ingest, expect `ore` (most), then
   `concentrate` / `intermediate` / `refined`.  NULL rows are pre-N4
   data — should be 0 for newly-ingested rows.

---

## What this addendum did NOT do

- I did not re-audit the scoring engine itself; that was the previous
  audit's domain and the math hasn't changed.  The G4 Half 1 stage-
  aware operational pillar update (2026-05-06) is the only structural
  scoring change since.
- I did not run anything against the live DB. All findings are
  source-tree verified.

---

## Cron schedule (added 2026-05-06)

After the cron-cleanup batch landed 2026-05-06, the registered Inngest
functions are:

| Job | Cron | Mode |
|---|---|---|
| `ingest-comtrade-daily`  *(scoring_jobs.py)* | Daily 04:00 UTC (midnight EDT) | Auto-download (year×prefix×flow). Logs `comtrade_job.backfill_complete` once caught up — swap to weekly when seen. Moved from 06:00 → 04:00 UTC on 2026-06-06 to absorb runtime growth from the 11.2 HS-prefix expansion. |
| `ingest-opensanctions-weekly` | Sun 22:00 UTC | Auto-download |
| `ingest-federal-register-weekly` | Sun 22:30 UTC | Auto-download |
| `ingest-worldbank-weekly` (Pink Sheet) | Sun 23:00 UTC | Auto-download (25-day gate) |
| `rescore-hs-nodes` (Level 0) | Mon 01:00 UTC | Scoring chain |
| `rescore-market-scores` (Level 1) | Mon 02:00 UTC | Scoring chain |
| `rescore-global-rollups` (Level 2) | Mon 03:00 UTC | Scoring chain |
| `rescore-all-chemistries` (Level 3) | Mon 04:00 UTC | Scoring chain |
| `ingest-eurlex-quarterly` | Q1 of Jan/Apr/Jul/Oct, 00:30 UTC | Auto-download |
| `ingest-sec-edgar-quarterly` | Q1 of Jan/Apr/Jul/Oct, 01:00 UTC | Auto-download |
| `ingest-mrds-semiannual` | 1st of Jan/Jul, 09:00 UTC | Auto-download |
| `gta-refresh-reminder-quarterly` | Q1 of Jan/Apr/Jul/Oct, 09:00 UTC | Reminder log only — partner manually exports CSV |
| `iea-policy-tracker-reminder-quarterly` | Q1 of Jan/Apr/Jul/Oct, 09:30 UTC | Reminder log only — partner manually exports CSV |
| `usgs-mcs-refresh-reminder-annual` | 15 April, 09:00 UTC | Reminder log only — partner downloads new MCS files |

Removed 2026-05-06: `ingest-comtrade-quarterly` (duplicated daily job).
Replaced 2026-05-06: `mrds-refresh-reminder-quarterly` (was log-only) →
`ingest-mrds-semiannual` (real auto-download).
