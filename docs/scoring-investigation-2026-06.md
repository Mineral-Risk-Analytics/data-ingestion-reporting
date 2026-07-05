# Scoring Engine Investigation — June 2026

**Status:** Live tracking document. Update checkboxes and status notes as work lands.
**Last updated:** 2026-06-17 (added ingestion cadence + new sources section)
**Owner:** Nicole
**Scope:** Findings from items 1–5 of the post-Step-3 scoring investigation, with prioritized fix list.

---

## Context

After landing Steps 1–3 of the scoring engine work (HHI cliff mapping in `hs_node_scorer`, material-level HHI lift in `market_aggregator`, WGI governance overlay on Geopolitical country_concentration) and re-running the global rescore on clean post-Step-3 data, the investigation goal was: **identify what's actually driving scores, what's dark, and what's mis-calibrated, so we can prioritize the next round of scoring work.**

Five investigation items were run:

1. Per-pillar weighted contribution per material (sanity-check pillar weights)
2. ~~Pillar attribution decomposition~~ (rolled into item 1)
3. Event sensitivity test (what if all events were zeroed?)
4. Per-source contribution per material (which data sources are load-bearing?)
5. Coverage gap inventory (this document)

---

## Headline findings

- **GTA is the load-bearing event source by 1–2 orders of magnitude.** 200–347 events per material in the last 24 months. Everything else combined: 5–50. If GTA were to go down for a week, the dynamic signal collapses.
- **EUR-Lex is not broken in the technical sense.** It successfully emits 6 standing-obligation events with `event_date=None`, refreshed yesterday (2026-06-16), feeding the Regulatory pillar correctly. What's missing is *scope*: no discovery of new regulations, no transient status-change events, no enforcement actions, no delegated/implementing acts.
- **Operational pillar has zero events ever in the entire DB.** Pillar is firing purely on structural facility data (MRDS + partner seed). Pillar weight is 11.8% but the dynamic half of the formula is unreachable.
- **Capacity data is missing for 9 of 10 launch materials.** Only Aluminum has `material_capacity_shares` rows (12). The `cap_stress` and `cap_utilization` sub-inputs inside Material Concentration are silently zero for everyone else.
- **Cobalt is the only structurally-anchored material** — if all events were removed, Cobalt's overall score drops only ~12 points (DRC production concentration carries the load). Every other launch material's score drops 22–30 points without events.
- **Pricing is bimodal.** Pink Sheet covers Ni/Cu/Al (~800 obs each, monthly). The other 7 materials have 5–45 USGS MCS annual price points total. Financial Pressure pillar for those 7 has very thin volatility signal.

---

## Cross-cutting observation

The structural data layer (USGS criticality + production HHI + WGI for 129 countries + 19 active regulations + facility data for 9 of 10 materials) is reasonably complete. The **event ingestion topology is the brittle layer**:

| Source | Status | Coverage notes |
|---|---|---|
| GTA | Active, dominant | 200–347 events/material/24mo |
| Federal Register | Active | 1–10 events except Aluminum (38, Section 232 docket) |
| IEA Critical Minerals Policy Tracker | Active but thin | 1–11 events/material |
| EUR-Lex | Active, scope-limited | 6 standing-obligation events (one per seeded EU reg) |
| OpenSanctions | Active | 16 synthetic events, 5 at sev=1.0 |
| Comtrade (synthetic GEOPOLITICAL_TRADE) | Active | 63 events at avg sev 0.74 |
| SEC EDGAR | Workstream A complete, B in-progress | 0 risk_events; awaiting body-text fetch |
| MSHA / EM-DAT / GDELT / news APIs | Not yet ingested | Operational pillar event coverage = 0 |

---

## Per-pillar contribution snapshot (post-Step-3, 2026-06-16)

Pillar weights (renormalized): Material 0.294 · Geopolitical 0.235 · Regulatory 0.235 · Operational 0.118 · Financial 0.118

| Material | Overall | Mat (29%) | Geo (24%) | Reg (24%) | Op (12%) | Fin (12%) |
|---|---:|---:|---:|---:|---:|---:|
| Nickel | 51.5 | 15.6 (30%) | 8.8 (17%) | 16.5 (32%) | 1.6 (3%) | 9.0 (18%) |
| Cobalt | 46.0 | 13.7 (30%) | 4.3 (9%) | 17.1 (37%) | 0.7 (2%) | 10.2 (22%) |
| Aluminum | 48.0 | 14.9 (31%) | 7.1 (15%) | 18.8 (39%) | 1.0 (2%) | 6.2 (13%) |
| Natural Graphite | 48.9 | 13.1 (27%) | 8.1 (17%) | 18.3 (37%) | 2.5 (5%) | 6.9 (14%) |
| Rare Earth Elements | 45.5 | 13.4 (29%) | 7.5 (16%) | 15.2 (33%) | 1.3 (3%) | 8.1 (18%) |
| Copper | 44.7 | 12.5 (28%) | 5.0 (11%) | 18.2 (41%) | 1.0 (2%) | 8.0 (18%) |
| Lithium | 41.6 | 12.5 (30%) | 6.1 (15%) | 15.5 (37%) | 1.0 (2%) | 6.5 (16%) |
| Phosphate | 39.0 | 10.2 (26%) | 5.8 (15%) | 16.9 (43%) | 0.0 (0%) | 6.1 (16%) |
| Manganese | 38.4 | 10.8 (28%) | 6.1 (16%) | 14.8 (39%) | 0.0 (0%) | 6.7 (17%) |
| Iron Ore | 37.6 | 10.7 (28%) | 5.3 (14%) | 15.1 (40%) | 0.5 (1%) | 6.0 (16%) |

Regulatory is consistently 30–43% of the overall score. Operational is 0–5% for every material. Material Concentration is the second-most-stable contributor.

---

## Event sensitivity ("what if all events were zeroed?")

For each launch-10 material, decomposing the Material + Geopolitical pillars from actual sub-input math, and applying flat assumptions for Reg/Op/Fin event-share (85% / 15% / 40%):

| Material | Overall | No-events overall | Delta | % event-driven |
|---|---:|---:|---:|---:|
| Lithium | 41.6 | 18.1 | 23.5 | 57% |
| Cobalt | 48.7 | 25.3 | 23.4 | **48%** ← only material below 50% |
| Nickel | 49.6 | 23.9 | 25.8 | 52% |
| Manganese | 38.4 | 16.1 | 22.3 | 58% |
| Natural Graphite | 48.9 | 22.6 | 26.3 | 54% |
| Phosphate (BG) | 39.0 | 15.7 | 23.3 | 60% |
| Iron Ore (LFP) | 37.6 | 15.6 | 21.9 | 58% |
| Copper | 44.7 | 20.1 | 24.6 | 55% |
| Aluminum | 48.0 | 22.0 | 26.0 | 54% |
| REE | 45.5 | 21.8 | 23.7 | 52% |

Honest caveats on this table: Reg/Op/Fin event-share assumptions are estimates, not decomposed. Material and Geopolitical numbers are real sub-input math. The narrow 48–60% range is largely an artifact of the flat assumptions; the *material-to-material differentiation* lives in the Material + Geo deltas, which are sub-input-driven.

**Cobalt is the standout structural-anchor case.** Mat drops only 4, Geo only 8 (DRC concentration carries it). Every other material's geo pillar collapses to single digits without events.

---

## Per-source coverage matrix

```
EVENTS (last 24mo)                          STRUCTURAL                  REGS  FACILITIES  PRICES
material                    GTA  IEA   FR  EUR  SEC  Syn  USGS(C/P/Cp)   Reg Fac  Prices
Lithium                     347    8    4    6*   0    4  C1/P9/Cp0       5    26  USGS=5
Cobalt                      319    9    2    6*   0    1  C1/P12/Cp0      7    74  USGS=10
Nickel                      311    7    5    6*   0    3  C1/P9/Cp0       5   135  WB=797+USGS=10
Manganese                   255    3    1    6*   0    3  C1/P8/Cp0       5   400  USGS=5
Natural Graphite            293    4    5    6*   0    3  C1/P18/Cp0      5   106  USGS=15
Phosphate (BG)              200    2    3    6*   0    5  C1/P24/Cp0      4     0  USGS=5
Iron Ore (LFP)              268    2    4    6*   0    5  C1/P17/Cp0      2   756  USGS=5
Copper                      269    7   10    6*   0    2  C1/P17/Cp0      5  1016  WB=797+USGS=15
Aluminum                    324    1   38    6*   0    5  C1/P12/Cp12     6   239  WB=797+USGS=5
Rare Earth Elements         347   11    4    6*   0    7  C1/P12/Cp0      4    66  USGS=45
```

`* EUR-Lex = 6 standing-obligation events per material (event_date=None, treated as always-fresh by the scoring engine).`
`Cp = material_capacity_shares row count.`

---

## Work item inventory

Tier rationale: **T1** = unblocks a pillar or visibly moves scores. **T2** = bias correction / coverage improvement. **T3** = hygiene + future-proofing.

---

### Tier 1 — Pillar-unblocking gaps

#### T1-1. Operational pillar has zero events
- [ ] **Status:** Not started
- **Affects:** All 10 materials
- **Impact:** Pillar weight is 11.8%; pillar can't surface mine floods, strikes, smelter fires, accidents until news cycle reroutes
- **Recommended sources (in priority order):**
  - [ ] MSHA (US mining incidents, free, structured) — ~2-3 days parser
  - [ ] EM-DAT (country-level disasters) — ~1-2 days parser
  - [ ] GDELT (event-mention, noisier) — deferred
  - [ ] News APIs (apitube, NewsAPI) — deferred; not a facility-status source
- **Honest caveat:** Even with MSHA + EM-DAT, US-only and country-level. Most mining/refining is outside the US. The pillar will still be thin
- **Open questions:**
  - Should operational pillar weight be temporarily reduced to ~5% until events land? (See T3-3)

#### T1-2. SEC Workstream B body-text fetch
- [x] **Status:** In progress (existing task #177)
- **Affects:** Financial Pressure pillar for the 7 materials without Pink Sheet coverage (Li/Co/Mn/Graphite/Phosphate/Iron Ore/REE)
- **Impact:** Financial pillar currently leans on 5-45 USGS MCS annual price points for those 7. Material attribution from 10-K filings would add scope_obligation-style structural depth
- **Remaining work:**
  - [ ] Run body-text fetcher on 1,000+ existing filings
  - [ ] Populate Item 1A/2/7 (10-K) and 3D/4D/5 (20-F) sections
  - [ ] Verify regex anchors are extracting correctly (negative lookaheads landed)
  - [ ] Verify LLM-locator fallback fires when regex misses

#### T1-3. Capacity data missing for 9 of 10 materials
- [ ] **Status:** Not started
- **Affects:** 9/10 materials (only Aluminum has 12 capacity_shares rows)
- **Impact:** `cap_stress` and `cap_utilization` sub-inputs silently zero across the board for Li/Co/Ni/Mn/Graphite/Phosphate/Iron Ore/Copper/REE
- **Recommended first step:** Walk through Lithium MCS PDF capacity extraction step-by-step to determine whether (a) the section-anchor regex isn't finding the table, (b) the LLM locator isn't being invoked, or (c) the source PDFs simply don't have capacity tables for these minerals
- **Open questions:**
  - Is capacity data even reliably reported in USGS MCS for non-Aluminum minerals? Worth verifying before fixing the parser

#### T1-4. Synthetic `sanctions_listing` events at sev=1.0
- [ ] **Status:** Not started — audit only
- **Affects:** Any geography flagged in OpenSanctions (RU, NK, IR, BY, MM)
- **Impact:** 5 events at severity=1.0 maximum, propagating through every material touching those geographies. Combined with 11 `geography_sanctions_exposure` synthetic events (avg sev=0.53, 4 at ≥0.9), the sanctions signal is firing hot
- **Audit work:**
  - [ ] Verify the `_event_impact` calc in market_aggregator caps total contribution per geo
  - [ ] Check whether sanctions_listing events compound with geography_sanctions_exposure events
  - [ ] Sample one geo (Russia) and trace the full impact chain
- **Honest caveat:** Magnitude impact may be small (only 5 events); this is hygiene more than fix

---

### Tier 2 — Coverage gaps that bias rankings

#### T2-1. Phosphate has 0 facility links
- [ ] **Status:** Not started
- **Affects:** 1 material (Phosphate, battery-grade)
- **Impact:** Phosphate's Operational pillar runs on (non-existent) events. Phosphate's Op = 0 in current data
- **Fix path:** Partner-curated facility seed for Phosphate
- **Honest caveat:** MRDS may not cover ag-grade phosphate; battery-grade phosphate facilities are concentrated in China + Morocco. May need partner-direct knowledge

#### T2-2. Iron Ore has only 2 active regulations scoped
- [ ] **Status:** Not started
- **Affects:** 1 material (Iron Ore LFP grade)
- **Impact:** Reg pillar suppressed for Iron Ore vs median 5 regs
- **Audit work:**
  - [ ] Verify CBAM scope in seed_regulations.py — does it actually cover iron ore?
  - [ ] Verify EU Battery Reg, EU CSDDD scope
  - [ ] Add scope rows if missing

#### T2-3. Monthly price benchmarks missing for 7 materials
- [ ] **Status:** Not started (related to existing task #109 pending)
- **Affects:** Li, Co, Mn, Graphite, Phosphate, Iron Ore (LFP), REE
- **Impact:** Financial pillar base_filing_signal lacks Pink Sheet volatility for these 7
- **Source survey candidates:**
  - LME (cobalt, nickel; no Mn/Graphite/Phosphate)
  - Fastmarkets API (paid)
  - Asian Metal (paid)
  - USGS Daily Industrial Minerals (no battery-grade prices)
- **Honest caveat:** There may be no free monthly source for some materials. Decision needed on paid feeds vs. accept gap

#### T2-4. EUR-Lex scope expansion
- [ ] **Status:** Not started
- **Affects:** Regulatory pillar EU-side
- **Three options, ranked by effort:**
  - [ ] **Option A** (low effort, ~1 day): Add 5-10 more CELEX numbers to `seed_regulation_aliases.py`. Captures named must-track regs (Forced Labour Reg 2024, EU Battery Reg secondary acts, CRMA delegated acts)
  - [ ] **Option B** (medium, ~1 week): Poll EUR-Lex search API + Haiku-classify new regs. Adds discovery
  - [ ] **Option C** (high, ~2 weeks): Wire enforcement action detection (EU Court of Justice rulings, infringement proceedings). Adds transient events
- **Recommendation:** Option A first; defer B and C until forcing function

#### T2-5. WGI single-source dependency
- [ ] **Status:** Not started
- **Affects:** Geopolitical pillar (overlay on all materials)
- **Impact:** WGI quietly modulates every Geopolitical score. If next World Bank release fails or shifts methodology, signal drifts silently
- **Fix path:** Add Freedom House Global Freedom Score or Fitch Sovereign Rating as fallback. ~3-5 days
- **Honest caveat:** Robustness, not correction. Current WGI data is correct

#### T2-6. Country-material relevance curation
- [x] **Status:** Pending partner input (existing task #77)
- **Affects:** Comtrade trade-volatility attribution + Material concentration
- **Impact:** Per-(country, material) relevance table from #75-76 needs curation pass to flag countries that aren't real market participants
- **Owner:** Partner-driven

---

### Tier 3 — Hygiene + calibration

#### T3-1. FR/GTA tariff double-counting check
- [ ] **Status:** Not started — 1 day audit
- **Affects:** Materials with both FR and GTA tariff events (extreme case: Aluminum, 37 of 38 FR events are TARIFF subtype)
- **Audit work:**
  - [ ] Pull one specific Section 232 action; count how many events it spawned
  - [ ] Determine if FR and GTA dedupe on same underlying action
  - [ ] Decide canonical source for tariff signal

#### T3-2. Federal Register Aluminum noise calibration
- [ ] **Status:** Not started — 30 min sample audit
- **Affects:** 1 material (Aluminum)
- **Audit work:**
  - [ ] Sample 5-10 of the 37 TARIFF FR events
  - [ ] Determine if they're real Section 232 churn or Haiku Q5 denylist gap

#### T3-3. Operational pillar weight question
- [ ] **Status:** Decision needed
- **Question:** Is 11.8% the right weight for a pillar with zero events?
- **Options:**
  - Reduce to ~5% until MSHA/EM-DAT land
  - Keep at 11.8% and accept that the pillar will be noise until then
- **Effort:** Weight change is 5 min; the decision takes longer

#### T3-4. EUR-Lex severity docstring drift
- [ ] **Status:** Not started — 10 min
- **Issue:** Docstring says effective=0.55 / enacted=0.40 / proposed=0.25; DB shows effective=0.7 / enacted=0.4
- **Fix:** Update docstring to match actual `_SEVERITY_BY_STATUS` constants and per-regulation overrides

#### T3-5. IEA Policy thin coverage spot check
- [ ] **Status:** Not started — 1 day
- **Issue:** 1-11 events per material; likely at parser ceiling but worth verifying
- **Audit work:**
  - [ ] Spot-check whether positive_policy routing (task #138) is over-suppressing
  - [ ] Verify three-way scope gate isn't rejecting too aggressively

#### T3-6. HS-mapping confidence normalization
- [x] **Status:** Existing task #98 pending
- **Affects:** Comtrade fractional allocation when one HS code maps to multiple materials
- **Impact:** Can over-allocate trade volume across materials
- **Effort:** 2-3 days

#### T3-7. Per-material noise floors
- [x] **Status:** Existing task #96 pending
- **Affects:** TRADE_CONCENTRATION / EXPORT_DECLINE / IMPORT_DECLINE generation thresholds
- **Impact:** Uniform thresholds across materials; should be per-material since trade volumes vary 3-4 orders of magnitude

---

## Ingestion cadence + new sources

The structural backbone of the scoring engine is almost entirely on the time-series side and most of it updates **once a year**. The event parsers (GTA, FR, IEA, EUR-Lex, OpenSanctions) fill some of the gap but huge categories are dark. This section inventories current cadence and surfaces source candidates that would close it.

### Current cadence map

| Source | Cadence | Parser type | Pillar fed | Status |
|---|---|---|---|---|
| USGS MCS — production shares | annual | time-series | Material, Geo | active |
| USGS MCS — reserves | annual | time-series | Material | active |
| USGS MCS — criticality | annual | time-series | Material | active |
| USGS MCS — capacity | annual | time-series | Material | broken for 9/10 mat (T1-3) |
| USGS MCS — Fig 10 prices | annual | time-series | Financial | active |
| World Bank WGI | annual | time-series | Geopolitical (overlay) | active |
| World Bank Pink Sheet | monthly | time-series | Financial | active (Ni/Cu/Al only) |
| UN Comtrade | monthly | time-series | Material (trade_vol), Geo | active |
| GTA | continuous | event | Geo, Financial | active, dominant |
| Federal Register | continuous | event | Regulatory | active |
| EUR-Lex | quarterly | event (standing) | Regulatory | active, scope-limited |
| IEA Critical Minerals Policy Tracker | ad-hoc | event | Geo, Regulatory | active, thin |
| OpenSanctions | weekly (verify) | event | Geo (sanctions) | active, cadence unverified |
| SEC EDGAR | continuous | event | Financial | A done, B in-progress |
| MRDS facility data | one-time | static | Operational | active |
| MSHA / EM-DAT / GDELT / news APIs | — | event | Operational | **not ingested** |
| Stock exchange disclosures (ASX/LSE/HKEX/SEDAR+) | — | event | Material, Financial, Op | **not ingested** |
| Battery-grade price feeds (Asian Metal / Fastmarkets) | — | time-series | Financial | **not ingested** |

The structural anchors (USGS production_shares, criticality, WGI) are defensible at annual cadence — country-level production concentration and governance scores genuinely don't move month-to-month. The problem is that **we use only annual anchors for non-tariff dynamics**. A mine closure in March doesn't move the score until next year's MCS publication. GTA picks up tariff churn and OpenSanctions picks up designations, but nothing in the current pipeline captures production guidance updates, mine incidents, M&A, or non-US regulatory developments at sub-annual cadence.

### New sources by tier

Ranked by impact × effort × material coverage.

---

#### CT1 — Tier 1: biggest leverage, mostly free

##### CT1-1. Non-SEC stock exchange disclosures (ASX, LSE RNS, HKEX, SEDAR+)
- [ ] **Status:** Not started
- **Why:** ~60-70% of mining/refining companies are listed outside US exchanges. This is the single largest blind spot.
- **What they publish:**
  - Production guidance updates (Material Concentration capacity dynamics)
  - Mine closures, derates, restart announcements (Operational events)
  - M&A and ownership changes (supply chain restructuring, Company graph)
  - Quarterly production reports (would beat annual USGS MCS by 6-9 months)
  - Resource/reserve statement updates
- **Cadence:** Continuous, free, structured
- **Pillars fed:** Material Concentration, Operational, Financial, Company graph
- **Effort:** ~1-2 weeks of parser work per exchange
- **Priority order within this group:**
  - [ ] ASX (largest mining-company concentration: Li, REE, Mn, Co, Ni)
  - [ ] SEDAR+ (Canadian mining + critical minerals)
  - [ ] LSE RNS (major iron ore + diversified miners)
  - [ ] HKEX (Chinese refining + processing)
- **Open questions:**
  - ASX has an RSS feed at asx.com.au/asx/v2/statistics/announcements.do; structured JSON behind it
  - SEDAR+ has a public search API; rate-limited
  - LSE RNS public access is limited; may need a paid feed like Acuity Knowledge Partners
  - HKEX has a structured disclosure portal but bilingual + may need translation layer

##### CT1-2. MSHA Mine Data Retrieval System
- [ ] **Status:** Not started (overlaps with T1-1)
- **Why:** US mining incidents, accidents, fatalities, citations — direct Operational pillar input
- **Cadence:** Weekly publish
- **Pillars fed:** Operational
- **Coverage:** US-only — won't move scores for materials concentrated outside US (Li/Co/REE/Graphite)
- **Effort:** ~2-3 days parser
- **Honest caveat:** US is not the dominant producer for any launch material except possibly Aluminum. MSHA improves Operational pillar quality but doesn't fix the geography gap

##### CT1-3. EM-DAT (CRED disaster database)
- [ ] **Status:** Not started (overlaps with T1-1)
- **Why:** Global disasters at country level — floods, earthquakes, civil unrest, disease outbreaks affecting mining regions
- **Cadence:** Monthly updates
- **Pillars fed:** Operational, Geopolitical
- **Effort:** ~1-2 days parser; data is downloadable CSV
- **Honest caveat:** Country-level granularity only; can't attribute to specific facilities. Useful as broad regional signal

##### CT1-4. Sanctions list refresh cadence audit
- [ ] **Status:** Not started — verification + possible cadence increase
- **Why:** OFAC SDN list updates multiple times per week; BIS Entity List monthly; EU/UK/UN regular updates
- **Cadence:** Daily ideal; verify our current OpenSanctions pull cadence
- **Pillars fed:** Geopolitical (sanctions signal)
- **Effort:** Few hours to audit cadence; small change if daily pull needed
- **Verification work:**
  - [ ] Check OpenSanctions ingester schedule
  - [ ] Compare OpenSanctions dataset publication times vs our pull times
  - [ ] Decide whether to add direct OFAC + BIS feeds as backup (continuous, free, no aggregator dependency)

##### CT1-5. USTR + USITC press releases (GTA redundancy)
- [ ] **Status:** Not started
- **Why:** Section 232/301/337 actions, USITC investigations, Commerce countervailing duty determinations. Currently we get most of this downstream via FR (1-7 day lag) and GTA. Adds redundancy if GTA goes down
- **Cadence:** Daily, RSS-available
- **Pillars fed:** Regulatory, Geopolitical
- **Effort:** ~3-5 days parser (RSS + Haiku classifier)
- **Honest caveat:** Significant overlap with FR + GTA. Value is mostly in redundancy, not new signal

---

#### CT2 — Tier 2: moderate leverage, mostly paid

##### CT2-1. Monthly battery-grade price feeds
- [ ] **Status:** Decision required — paid feed or accept gap
- **Why:** 7 materials without Pink Sheet coverage (Li/Co/Mn/Graphite/Phosphate/Iron Ore LFP/REE) currently have annual USGS MCS prices only
- **Pillars fed:** Financial Pressure
- **Realistic options:**

  | Source | Cost/yr | Coverage | Cadence | Notes |
  |---|---|---|---|---|
  | Asian Metal | $15-30k | Mn, REE, graphite, Li, Co (best for us) | monthly | Best coverage-for-price |
  | Fastmarkets | $30-60k | Li, Co, Ni (no Mn/Graphite/Phosphate) | daily/weekly | Strongest brand, weakest material coverage |
  | Benchmark Mineral Intelligence | $20-40k | Li, Co, Ni only | monthly | Highly cited; narrow material set |
  | LME | free | Co, Ni (no Li/Mn/Graphite/Phosphate) | daily | Spot prices, futures |
  | Shanghai Metal Exchange | free | Cu, Al, Ni, Pb, Zn, Sn | daily | Scrapeable; not battery-grade |
- **Recommendation:** Asian Metal if budget allows; otherwise accept gap until SEC body-text fetch (T1-2) gives partial substitute via 10-K filings
- **Honest caveat:** This is the single biggest material-coverage gap in the Financial pillar. None of the free sources cover the 7 missing materials at monthly cadence

##### CT2-2. Mining-industry news aggregation
- [ ] **Status:** Not started
- **Why:** Mining Weekly, Mining.com, Reuters Mining, Bloomberg Commodities pick up production guidance updates, mine incidents, M&A, exploration results faster than government feeds
- **Cadence:** Daily
- **Pillars fed:** Material, Operational, Financial, Geopolitical
- **Effort:** RSS for the free ones is ~3-5 days; Reuters/Bloomberg APIs are paid + add ~1 week each
- **Requires:** LLM-classifier layer (similar to FR Haiku gate) because volume is high and noise rate is meaningful. Reuses material_classifier infrastructure (built in #3 + #139)
- **Honest caveat:** High noise relative to government sources. Will need calibration time to get the classifier threshold right

---

#### CT3 — Tier 3: narrow value or higher complexity

##### CT3-1. Other government Gazette feeds
- [ ] **Status:** Not started
- **Coverage:** Canada Gazette, Australia Federal Register, UK Gazette, Korean MoTIE
- **Why:** Adds national regulatory signal but coverage is narrow per source
- **Cadence:** Daily/weekly
- **Pillars fed:** Regulatory
- **Effort:** ~3-5 days per parser
- **Recommendation:** Only wire if a launch material has heavy exposure to that jurisdiction (e.g., Australia for Li/REE)

##### CT3-2. USGS Mineral Industry Surveys (monthly)
- [ ] **Status:** Not started
- **Coverage:** Some commodities have monthly USGS surveys beyond the annual MCS — Aluminum, Iron and Steel Scrap, Ilmenite, a few others
- **Cadence:** Monthly publication
- **Pillars fed:** Material, Financial
- **Effort:** ~1-2 days per parser
- **Honest caveat:** Narrow material coverage; doesn't help Li/Co/Ni/Graphite/Phosphate/REE

##### CT3-3. Customs / shipment data (Panjiva, ImportGenius)
- [ ] **Status:** Not started — likely deferred
- **Coverage:** Bill-of-lading level trade data, weekly resolution
- **Cost:** $30-60k/year
- **Pillars fed:** Material (trade_vol), Geopolitical
- **Honest caveat:** Probably not worth the cost until existing Comtrade gaps are addressed first

---

### What this means for the structural-vs-event balance

Currently ~85% of dynamic signal is GTA-driven and the structural inputs are mostly annual. If Tier 1 lands (stock exchange disclosures + MSHA + EM-DAT + sanctions cadence verification + USTR), you'd:

- Unlock Operational pillar from zero events
- Add ~10-50 events/month/material from non-US-listed producers
- Get monthly cadence on production/capacity changes via quarterly reports
- Add redundancy to the GTA single-point-of-failure

You wouldn't need Tier 2 immediately if Tier 1 lands, with one exception: **the pricing decision is its own thing**. Without Asian Metal or Fastmarkets, 7 materials' Financial pillar stays thin regardless of what else you ingest.

**Defensible framing for partner:** the engine's structural anchors (USGS, WGI) are appropriate at annual cadence — country-level production concentration and governance scores genuinely don't move month-to-month. The problem is using only annual anchors for non-tariff dynamics. Stock exchange disclosures + MSHA + EM-DAT close most of that gap. Sources outside US/EU regulatory feeds are where the bulk of free, structured, regularly-updated commodity data actually lives.

---

## Suggested execution order

```
Now (this week):
  T1-2  Finish SEC Workstream B body-text fetch (in-progress)
  T1-4  Sanctions sev=1.0 propagation audit (1 day)
  T3-4  EUR-Lex severity docstring fix (10 min)

Next 2-3 weeks:
  T1-3  Investigate why MCS capacity tables aren't landing for 9 materials
  T2-1  Phosphate facility seed kickoff with partner
  T2-2  Iron Ore regulation seed audit
  T2-4  EUR-Lex Option A: add more CELEX numbers
  T3-1  FR/GTA tariff double-count check

1-3 months:
  T1-1  MSHA + EM-DAT operational sources
  T2-3  Monthly price benchmark decision (paid? accept gap?)
  T2-6  Partner curation pass on country_material_relevance

Defer until forced:
  T2-5  WGI single-source robustness
  T2-4-B/C  EUR-Lex discovery + enforcement
  T3-5  IEA thin coverage (likely at ceiling)
```

**Note:** The execution order above predates the "Ingestion cadence + new sources" section. When prioritizing for next sprint, sequence Tier-1 work items (T1-*) against CT1 sources together — there's significant overlap (T1-1 Operational pillar overlaps with CT1-2 MSHA and CT1-3 EM-DAT; CT1-1 stock exchange disclosures unlocks Material/Operational/Financial signal that would otherwise wait for SEC body-text (T1-2) on US-listed companies only).

---

## Methodology notes

- All event counts and sub-input values pulled from production DB on 2026-06-17
- Material × geography rationale_json data: `as_of_date = 2026-06-16` rescore
- Per-pillar weights are renormalized for the market context: Mat 0.294 · Geo 0.235 · Reg 0.235 · Op 0.118 · Fin 0.118
- Trade-weighting uses `material_production_shares` where available, equal-weight fallback
- The event-sensitivity decomposition for Material Concentration drops the `trade_volatility × 0.30 × 100` contribution from the legacy fallback formula; this approximation undercounts event leakage through the stage-rollup HS-node composites by an estimated 5-10 points

---

## Update log

| Date | Section | Change |
|---|---|---|
| 2026-06-17 | All | Initial document creation from items 1–5 investigation |
| 2026-06-17 | Ingestion cadence + new sources | Added new section: current cadence map + CT1/CT2/CT3 new-source candidates |
