# Scoring V1 Specification

**Status:** Draft for Nicole's review · 2026-07-17
**Replaces:** the accreted 3.x stack (stage-weighted rollup, HHI lift, purity filter, facility-presence floor, operational blend) as the *scoring* path. Ingestion, events, dedupe, and the HS mapping layer are unchanged.
**Reference material:** Cobalt — the first material with full evidence coverage; used throughout as the worked example and as the onboarding template for every other material.

---

## 1. Principles

1. **Legibility over sophistication.** Every score must be explainable in one sentence from its stored inputs. If a layer needs a diagram to explain, it waits for V2.
2. **Data-honesty.** No fabricated signal: no floors from unvetted facilities, no imputed midpoints, no placeholder dependencies. Missing data reads as "no signal," visibly.
3. **Each fact scores once.** An event lives in exactly one pillar. Structural numbers live in exactly one pillar. The double-counting of events into both node composites and the geopolitical pillar is removed.
4. **Additive versioning.** Downstream data, facility structural signals, and new pillars arrive as versioned upgrades with a rescore — never as silent formula changes.

---

## 2. Architecture

```
share tables (per stage)  ──►  Concentration pillar ─┐
events (by category)      ──►  Geopolitical pillar  ─┤
regulations + reg events  ──►  Regulatory pillar    ─┼──►  material × geography score ──►  material global (L2)
curated operational events──►  Operational pillar   ─┘         (4 pillars, weighted)         (share-weighted rollup
                                                                                              + price/financial context)
```

**What changes from 3.x:**

- **HS-node composite scores are no longer a scoring layer.** HS mappings remain the attachment point for trade flows and tariff/export-restriction events (that plumbing is correct and stays). But no more `composite_node_score` → stage-weighted average → lift → blend chain.
- **Stages are sub-scores, not weights.** Each supply-chain stage that has real share data gets its own concentration sub-score. The pillar value is the **max** across stages ("your strongest chokehold defines you"). All stage sub-scores are stored in rationale and displayable. No stage-importance weights (the old 0.10→0.30 ladder) — the IEA data supports refining concentration being *at least* as serious as mining (top-3 refiner share 86% vs 77% for mining, 2024), which max() captures without asserting a precise ratio we can't defend.
- **Parent/child HS nodes can't double-count** because nodes aren't averaged at all.
- **Financial pressure moves to the material level (L2) only.** Price volatility and producer-company financial stress are material-wide signals — the current per-geography value is flat (56.2 everywhere for cobalt), which proves it carries no geographic information. Scoring it per-geography just compresses spread.
- **The material×geography score is 4 pillars** (weights §7).

---

## 3. Pillar: Material Concentration (structural, numbers only)

**One-sentence explanation:** "How much of stage X does this country control, and how concentrated is that stage globally?"

**Formula:** per stage `s` with share data: `sub_s = hhi_cliff(HHI_s) × √share_geo,s × 100`. Pillar = `max_s(sub_s)`. Non-producers score 0 — import dependence is a different product story, not producer concentration.

| | |
|---|---|
| **Evidence in** | Per-stage global production shares: USGS MCS (ore, annual), benchmark workbooks (intermediate/refined/battery-grade — CI/Benchmark for cobalt, equivalents per material), audited via the `benchmark_shares` loader gates (audited=Y, basis+source required, per-node sum ≤ 1.05) |
| **Explicitly out** | Events (→ geopolitical), facility statuses (→ operational), criticality/volatility blends (the old `score_material_exposure` fallback — retired) |
| **Expected output (cobalt)** | CD 83 (ore 75%, intermediate 76%), CN 89 (refined 79%, battery-grade 85%), ID 36, FI 26, RU 15, tail < 12, non-producers 0. Stage HHIs: ore .59, intermediate .59, refined .63, battery-grade .72 — all cliff to ≥ .95 ("extreme" tier) |
| **Seed sources to manage** | USGS MCS (annual, ore stage, all materials — already loaded); benchmark PDF per material, read in full (cobalt ✓; nickel, lithium, graphite, REE pending) |
| **Later evidence** | Comtrade export-share concentration as a *proxy* for stages with no production data (flagged as proxy, never mixed silently); USGS reserves HHI (future concentration); supplier-company HHI (CMOC alone = 41% of cobalt — company-level concentration is a real second axis); secondary-supply share as a concentration reducer. WGI weighting deliberately NOT here — governance lives in the geopolitical pillar only (see §10 Q2) |
| **Live sources** | None needed — structural signal moves annually. Re-read benchmark reports on publication (≈ 5-month lag is acceptable) |
| **Data-quality note** | This pillar is only as good as stage coverage. Cobalt has 4 stages; most materials have ore only until their benchmark workbook is done. A material scored on ore alone is *correct but incomplete* — display which stages are covered. |

## 4. Pillar: Geopolitical & Trade

**One-sentence explanation:** "What have governments actually done that restricts or distorts trade in this material, involving this geography?"

| | |
|---|---|
| **Evidence in** | Published events: export restrictions/bans/quotas, tariffs (importer-side, attributed to affected producer), sanctions, licensing regimes, resource-nationalism measures. Sources: GTA, IEA policy tracker, manual curation. Direct-relevance weighting (breadth multiplier, migration 056), dedupe (duplicate_of, migration 055), participation gate for trade attribution — all retained as-is |
| **Explicitly out** | Operational disruptions (shutdowns, strikes → operational); regulations with compliance character (→ regulatory) |
| **Expected output (cobalt)** | CD elevated (2025 export ban + quota regime — the single most significant cobalt trade event); CN elevated (export-control expansion); clear daylight between geographies with real measures and the tail |
| **Seed sources** | GTA (loaded), IEA policy tracker (loaded), manual workbook (CI-report extraction pending your direction/subtype audit) |
| **Later evidence** | World Bank WGI / political-stability weighting on the concentration side (the EU CRMA weights HHI by WGI — principled, standard, and cheap to add); state-ownership share of production; trade-agreement/alignment context |
| **Live sources** | GTA updates (periodic re-ingest), IEA tracker updates; later: curated news monitoring with the same "fewer, confident, significant" bar |

## 5. Pillar: Regulatory & Compliance

**One-sentence explanation:** "What rule changes affect the cost or right to produce/trade this material here?"

| | |
|---|---|
| **Evidence in** | Verified regulations (the `verified` gate you're curating), regulatory-category events |
| **Expected output** | Currently near-flat (89.1/72.6 split for cobalt) — this pillar carries little geographic signal today. **V1 keeps it at reduced weight and flags it "needs data work"** rather than pretending it differentiates |
| **Seed sources** | Your regulation curation workflow (in progress); pending_review theme triage |
| **Later evidence** | EU Battery Reg / CRMA compliance exposure (recycled-content and sourcing quotas per material), due-diligence regimes (CSDDD, Dodd-Frank 1502 for 3TG), permitting-regime speed by jurisdiction |
| **Live sources** | Official journals / agency feeds per priority jurisdiction (EU, US, CD, CN, ID) — manual-first, same verification gate |

## 6. Pillar: Operational (event-only in V1)

**One-sentence explanation:** "What has actually disrupted, curtailed, or idled supply of this material in this geography?"

**V1 decision (agreed 2026-07-17):** the 40% MRDS structural-dependency blend is removed. The pillar is 100% curated operational events — the code's existing `struct_dep=None` path becomes the only path. Zero events = score 0 = "no known disruptions," honestly.

| | |
|---|---|
| **Evidence in** | Curated operational events: facility shutdowns/curtailments (MKM cobalt line 2024, MMG Kinsevere early 2025, STL Big Hill), care-and-maintenance waves (AU: Savannah, Nickel West, Avebury, Ravensthorpe), guidance cuts (Glencore 2025/2026), production-loss incidents |
| **Explicitly out** | MRDS facility statuses (unvetted, no production data — demoted to discovery/reference layer, never scores); the facility-presence floor (deleted concept); trade measures (→ geopolitical) |
| **Event types to add to the curation checklist** | Power/grid crises (Zambia 2024 hydro drought, ZA load-shedding), labor strikes, weather (WA cyclones, Madagascar), tailings/accidents, force majeure declarations, court/permit stoppages (Cobre Panamá pattern), logistics chokepoints (Kolwezi–Durban trucking, Lobito rail), water constraints (Atacama). None of these arrive via GTA/IEA — this pillar is manual-first by nature |
| **Expected output (cobalt)** | AU/ZM/CD carry real operational signal from curated events; most geographies 0 |
| **Facility data path (your plan, endorsed)** | Curate a vetted facility registry (partner seed) → subscribe to a mining data feed queried *against the watchlist* (monitored facilities only) rather than bulk-seeding unvetted rows. Candidates to evaluate: S&P Capital IQ Pro (Mine Economics), Wood Mackenzie, Benchmark, GlobalData, Mining Data Online. When statuses are trustworthy per material, capacity-weighted structural dependency returns as a versioned upgrade |
| **Live sources** | Company production reports (quarterly — the Glencore/CMOC cadence), the mining feed once selected, curated news |

## 7. Weights, L2, and bands

**Material × geography** = `0.333 × concentration + 0.267 × geopolitical + 0.267 × regulatory + 0.133 × operational` (the current relative weights with financial removed and renormalized — deliberately *not* re-tuned by feel; re-weighting waits for band calibration against the full V1 output).

**Material global (L2)** = share-weighted rollup of geography scores (existing mechanism) + financial/price context applied at this level only (Pink Sheet volatility, producer-company financial pressure). L2 is where "cobalt is riskier than copper" lives; the geography score is where "CD vs CN vs ID" lives.

**Bands (30/60/80)** need recalibration after the first full V1 rescore — under V1, cobalt shows CD ≈ 70, CN ≈ 66, ID ≈ 43, tail ≈ 30s, which likely means the HIGH band starts working again. Do not re-tune band edges until all materials are rescored.

**Chemistry (L3)** — unchanged mechanically, consumes L2.

## 7b. Recency & rescore semantics (added 2026-07-17, Nicole's questions)

**A score is a pure function of (evidence in DB, as-of date).** Every scoring run — first or subsequent — is a full recompute over the evidence window. There is no incremental "new events only" mode, for three reasons: (1) decay makes old events' contributions time-varying, so freezing them at last-run values is incoherent; (2) history gets edited (dedupe marks, direction audits, relevance regrades, reg verification) and only a full recompute picks corrections up; (3) incremental state is unreproducible — a pure recompute can be audited from the DB at any time.

**Evidence windows by input type:**

| Input | Window rule |
|---|---|
| Events (all pillars) | 24-month look-back from as-of date (existing), with severity decay inside the window. Events age out at the window edge |
| Production shares | Latest reference year per stage, **and** reference year must be within 24 months of as-of date to qualify as a scoring input. Older rows: display-only, flagged stale, excluded from the stage max |
| Regulations | Status-governed, not age-governed: an in-force (enacted/effective, verified) regulation is current evidence regardless of enactment date. Proposed regs count while pending; repealed ones drop |
| Facility watchlist (future) | Current status as of feed date; status older than the feed's refresh cadence flags stale |

**Stage sub-scores clarified:** sub-scores by supply-chain stage exist only inside the Material Concentration pillar (its evidence is natively stage-structured). Geopolitical, regulatory, and operational pillars are stage-agnostic in V1. Material × geography = 4 pillars; material global (L2) = share-weighted rollup of geography scores. Stages appear exactly once in the hierarchy.

**History/trend:** score rows are kept per as-of date; the time series of full recomputes is the trend line, and any two runs diff cleanly to explain movement (evidence added/aged/corrected between them).

**Scored-geography universe (added 2026-07-18, Nicole's call):** a (material × geography) pair is scored only when the geography is a **producer** (material-level or stage-level share > 0) or a **trade-gate exporter** (≥ 1% of the 4-digit-family export total AND ≥ $5M — the same thresholds as the L0 participation gate). Event-only geographies are not scored: their concentration is 0 by definition and their weight in the L2 trade-flow rollup is 0 by construction, so skipping them changes no number anyone sees while cutting ~85% of pairs (cobalt: 127 → ~24). Implementation: `stage_concentration.derive_scoring_geographies`, used by all three scoring entry points. Events tagged to unscored jurisdictions remain fully visible in event views — they just don't generate score rows.

## 8. What's deleted, and why (bug ledger)

| Removed | Was | Why |
|---|---|---|
| Stage-weighted node rollup | ore .10 → battery .30 average over node composites | Averaged empty/event-only nodes against structural ones → DRC 39 < Belarus 50. Weights asserted a precision the data can't support |
| Operational blend in node composites | HHI weight cut .50→.40 when facilities exist | Operating facilities *demoted* scores (op signal 0 × weight .20). A health signal must never subtract |
| Material-HHI lift | Blend toward material floor when HHI coverage low | Dead code since the purity filter (fraction always 1.0); its job is done structurally by max-stage |
| Purity filter | Concentration rollup restricted to hhi-anchored nodes | Obsolete — there is no rollup to purify |
| Facility-presence floor | 0.02 concentration for any facility link | Fabricated presence from unvetted MRDS rows (AL/JP/CU cobalt). Deleted concept, not just status-filtered |
| MRDS structural dependency | 40% of operational pillar | Statuses stale/unvetted except cobalt's triaged 42; no production weighting possible. MRDS → reference layer |
| `score_material_exposure` fallback for non-producers | crit/conc/volatility blend ≈ flat 48 | Gave Belarus a higher cobalt concentration score than DRC. Non-producer concentration is 0 by definition |
| Per-geography financial pillar | flat 56.2 | No geographic content; moves to L2 |

**Kept, unchanged:** event dedupe (055), breadth/direct multipliers (056), participation gate + family matching, tariff affected-country attribution, HS parent/child event propagation (for event attribution, not node averaging), HHI cliff mapping, √share weighting, benchmark share loader gates, decay/severity/confidence event-impact math.

## 9. Material onboarding framework (the cobalt template)

To bring any material to full V1 scoring, in order of impact:

1. **Ore-stage shares** — USGS MCS (already loaded for all) → concentration works at ore stage immediately.
2. **Benchmark document for downstream stages** — one authoritative per-material report, read in full, loaded via `benchmark_shares` with audit gates (cobalt: CI 2024 ✓). Unlocks the refining-stage story, which for most materials is the China story.
3. **Event pass** — GTA/IEA auto-ingested; manual extraction from the benchmark doc (trade + operational events); dedupe review; direct/broad audit.
4. **Operational curation** — the §6 checklist against the past ~24 months.
5. **Regulation tagging** — verified regs touching the material.
6. **Facility watchlist** — vetted rows only, from filings (units audit discipline: saleable-product basis at output stage).

Cobalt status: 1–3 done, 4 partially (CI extraction pending audit), 5 partial (9/19 verified), 6 done for the big five. **Next materials by leverage: nickel, graphite, lithium** (benchmark docs pending upload).

## 10. Open questions — ALL RESOLVED (Nicole, 2026-07-17)

1. **UI labels the driving stage** — yes. Each geography's concentration score displays which stage drives it (CD ← intermediate, CN ← refined). Stage sub-scores stored in rationale_json for this.
2. **WGI** — already implemented: `apply_wgi_governance_overlay` (geopolitical_risk.py:132, applied market_aggregator.py:1000), JRC-aligned α=0.5 discount on country_concentration in the *geopolitical* pillar, fed by ingest_worldbank_wgi. Kept as-is under V1. Decision: WGI lives in geopolitical only — do NOT also weight the concentration pillar's HHI by WGI (would double-count governance). Removed from concentration's later-evidence list.
3. **Battery-grade CN share (85%, 2022)** — resolved by §7b freshness gate: stale-excluded from scoring, display-only. CN scores from refined stage (79%, 2024) → concentration ≈ 85.
4. **Mining data feed** — tabled until the facility-watchlist phase.
5. **Scoring version = `4.0`** — 3.x rows deleted after post-rescore verification.
