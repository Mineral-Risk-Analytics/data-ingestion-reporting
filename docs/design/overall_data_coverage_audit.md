# Data coverage audit — where the engine is thin and what would fill it

**Re-measured 2026-07-30** against production Neon, after the 4.4 rescore
completed. Scope: the **848 rows at `scoring_version = 4.4`,
`as_of_date = 2026-07-29`** — 40 materials across 96 geographies.

Method: for each pillar, the scoring code was read to enumerate every
sub-input, the table it reads, and what happens when that input is absent.
Coverage was then measured from `rationale_json` provenance tags rather than
inferred from score values, because several absent-data paths score a number
rather than a null.

**Denominator correction.** The 2026-07-28 draft of this document measured
against 1,742 rows and quoted every percentage against that base. That was a
union across three scoring versions, so most cells were counted twice and the
denominator was not a grid. Every percentage below is against the 848 rows of
the current single generation. Where a figure moved because the base changed
rather than because the data changed, that is stated.

The table now holds 2,590 rows in five generations: 846 at 4.1/07-20, 26 at
4.1/07-23, 26 at 4.2/07-23, 844 at 4.3/07-26, and 848 at 4.4/07-29.

---

## 0. The headline

Four things are worth more than the rest of this document.

**The version patchwork is fixed for pinned reads, not for the table.** The
4.4 rescore wrote a complete generation — 40 materials, 96 geographies, no
`facility_floor` labels surviving anywhere, positive-event exclusion applied.
But it added a generation rather than replacing the old ones, so anything that
queries `material_geography_risk_scores` without filtering on `as_of_date`
still spans four formula versions and will double-count. That is now a
read-path and retention question rather than a scoring one, and it is the
cheapest remaining correctness item.

**The regulatory obligation gap closed; the regulatory event gap did not.**
Twenty-five of 35 regulations are now verified, 24 of them appear in scope
somewhere in the grid, mean scoped obligations per cell rose from 4.39 to
**6.29**, cells with no obligation at all fell from 256 to **zero**, and the
0.50 uncurated-weight fallback now fires on **zero** regulations. Regulatory
completeness is 1.000 on every cell. The 0–60 event component, however, still
draws on a pool of exactly **six events** linked to six EU instruments —
confirmed in `get_events_for_regulations`, which joins `risk_event_regulations`
and filters on `Regulation.verified`. Every one of the sixteen regulations
verified on 07-29 has zero linked events. Verification was never the
constraint on the event half; links are.

**Bromine and Gold are not in the grid at all.** The 07-28 draft said they
"score 0 in every cell." They have **zero rows**, in 4.3 and 4.4 alike. Of 42
materials in `materials`, 40 are scored. `derive_scoring_geographies` returns
an empty geography set for both, because neither has a production share, a
stage share, or a trade-gate exporter to build a universe from. They are
invisible in the product rather than wrong in it, which is a different problem
and arguably a worse one.

**One publication still underpins three pillars.** USGS Mineral Commodity
Summaries supplies 976 of 1,054 global stage-share rows (93%, counting
`usgs_mcs_propagated`), all 318 `us`-scope rows, all of
`material_production_shares`, and — via the Figure 10 price series — 838 of
848 cells' financial score. Material concentration is 100% share-driven and
geopolitical country concentration is 40% share-driven. A USGS revision or
delay moves three pillars at once. Unchanged from 07-28.

### Pillar summary, 4.4 generation (n = 848)

| pillar | weight | mean | median | max | zero cells | completeness |
| --- | --- | --- | --- | --- | --- | --- |
| material concentration | 0.333 | 9.19 | 0.00 | 100.00 | 461 (54%) | 0.638 |
| geopolitical trade | 0.267 | 22.03 | 17.29 | 81.70 | 0 | 0.639 |
| regulatory compliance | 0.267 | 47.00 | 47.44 | 84.58 | 0 | 1.000 |
| operational | 0.133 | 10.36 | 0.00 | 49.43 | 511 (60%) | 0.199 |
| financial pressure | 0.000 | 49.33 | 47.20 | 75.21 | 0 | 0.500 |
| **overall** | — | **22.85** | 20.70 | 78.76 | 0 | 0.676 |

The top of the grid is defensible on its face: Natural Graphite CN 78.8,
Gallium CN 77.8, Silicon (Anode Grade) CN 77.7, REE CN 76.3, Cobalt CD 72.1,
Nickel ID 68.0. Two pillars have a median of zero, which is the honest
signature of a sparse grid, not a defect.

---

## 1. Pillar-by-pillar coverage

### Material concentration — weight 0.333 (heaviest)

Live formula is `max` over fresh stages of
`hhi_cliff(HHI_stage) × sqrt(share) × 100`, with a multiplicative WGI
governance amplifier. Everything else in `_derive_market_material_inputs` is
retained as rationale context only and no longer scores (4.1).

**Zero cells are now fully explained.** 461 cells score 0, and all 461 report
`stage_rollup_method = "no_share_data"` — the geography holds no share at any
fresh stage of that material. The other 387 report `stage_max`. There is no
residual bucket of cells that had data and lost it, which is what the 07-28
draft claimed for 32 cells and got wrong (see the changelog).

**4.4 changed the `stage_detail` diagnostic, for the better.** It now emits
every stage the *material* has, with `share: null` where the scored geography
is absent from that distribution. So a `no_share_data` cell on a well-covered
material shows the stages it is missing from, and an empty `stage_detail` now
means the material has no stage data at all anywhere. That is a strictly
better signal than 4.3, where both cases looked identical — and it is why the
07-28 diagnosis of the 32 cells was misread.

**The freshness gate is visible and currently harmless.** Exactly 26 cells
report a non-empty `stale_stages`, and every one is Cobalt excluding
`battery_grade` at reference year 2022. No cell loses its score to the gate,
because Cobalt still scores from `ore`, `refined` and `intermediate`. The
two-year gate has approximately zero effect on the current grid — worth
knowing before anyone spends effort tuning `FRESHNESS_YEARS`.

**The pillar is upstream-dominated.** Of the 387 scored cells: `ore` drives
237, `refined` 113, `battery_grade` 23, `intermediate` 14. Sixty-one percent
of all concentration signal in the product comes from where material is dug
up, not where it is processed.

Stage-share coverage is the whole pillar and is **unchanged by the rescore** —
the rescore scored existing shares, it did not add any:

| stage | materials with a global share row | materials with an HS mapping |
| ------------- | --- | --- |
| ore | 24 | 27 |
| refined | 17 | 34 |
| battery_grade | 8 | 21 |
| intermediate | 3 | 31 |
| concentrate | 0 | 3 |

The gap between the two columns is still the finding. Mappings exist; shares
do not. Midstream is effectively unmeasured — three materials at
`intermediate`, zero at `concentrate` — which is precisely where Chinese
processing concentration lives.

**Materials scoring 0 in every cell: Rhenium (13 cells) and Sodium (22
cells)**, down from four. Both have HS mappings at a concentration stage and
no shares, so both are a parser problem rather than a mapping problem.
Bromine and Gold, the other two on the 07-28 list, turn out not to have cells
at all — see §0.

**What the rescore recovered.** Five materials that scored 0 everywhere now
carry real signal: Synthetic Graphite CN 99.68 (battery_grade), Dysprosium and
Terbium CN 99.99 (refined), Neodymium and Praseodymium CN 96.70, Germanium CN
79.62. Malaysia, the US, Canada, Russia, Belgium and Germany pick up non-zero
cells on the same materials. Four cells are new to the grid entirely —
Estonia on Nd and Pr, and two on Silver — because
`derive_scoring_geographies` widens the geography universe when new share
rows appear. Prior to this rescore the product reported *no concentration
risk* for China on dysprosium, terbium, neodymium, praseodymium, germanium and
synthetic graphite.

Governance amplifier coverage remains complete: 129 countries in
`country_governance_signals`, all with ≥4 of 6 WGI dimensions, newest
reference year 2024, and zero scored geographies lacking a usable row.

### Geopolitical trade — weight 0.267

Four sub-inputs; the fourth is universally absent, which changes the
weighting profile on every cell.

`country_concentration` (40% of the pillar) reads
`material_production_shares` and reports `no_data` on **486 of 848 cells
(57%)**. The 07-28 draft quoted 48%, on the inflated base; the underlying
condition did not improve, it was understated. Eight materials have no
production-share row at all: Bromine, Dysprosium, Germanium, Gold, Neodymium,
Praseodymium, Synthetic Graphite, Terbium. Note that six of those eight now
score on *material concentration* via stage shares while still returning
`no_data` here — the two pillars read different tables, and filling
`hs_code_production_shares` did nothing for this sub-input.

**The 07-28 draft combined export restriction and tariff coverage and hid a
large asymmetry.** Measured separately:

| sub-input | both | events only | HS only | neither |
| --- | --- | --- | --- | --- |
| `export_restriction` (0.35) | 215 | 166 | 38 | **429 (51%)** |
| `tariff_exposure` (0.25) | 608 | 230 | 7 | **3 (0.4%)** |

Tariff exposure is effectively fully covered. Export restriction is missing on
half the grid. Only the first is worth spending on, and the OECD inventory
named in §2 targets exactly it.

`production_subsidy_distortion` is absent on **848 of 848 cells** — every row
scored under the `3_component` profile, meaning there is not one
`EXPORT_SUBSIDY` event in the corpus. This is not neutral: the scorer's own
docstring states a country with no subsidy data scores roughly 3.5 points
_higher_ than the same country with subsidy explicitly 0.0, because the weight
redistributes. Every cell in the product carries that inflation. Unchanged
from 07-28, and now the largest untouched item on this pillar.

The `facility_floor` label that 4.1 retired appears on **zero** 4.4 cells,
down from 155.

### Regulatory compliance — weight 0.267

Completeness 1.000 on every cell, and for the obligation half that is now
earned rather than an artefact.

**Obligation component (0–40, saturating) — the curation gap is closed.**
5,335 curated geography weights; **zero** regulations falling to the 0.50
uncurated default, down from 42; mean **6.29** scoped obligations per cell, up
from 4.39; **zero** cells with no obligation, down from 256. Twenty-four
distinct regulations appear in scope somewhere. Four are universal via
`applies_all_materials` and appear in all 848 cells: CA_S211, EU_CSDDD,
EU_FLR_2024, UFLPA. The long tail is where the newly verified non-EU
instruments land — CRMA_2024 on 473 cells, IRA_DOMESTIC 295,
JP_ESPA_CRITICAL_MATERIALS 147, CN_DUAL_USE_EXPORT 98, CN_MINOR_METALS_2025
87, DODD_FRANK_1502 76, CN_REE_MGMT_2024 60, CN_REE_EXPORT_2025 42,
ID_NICKEL_ORE_BAN 28, DRC_COBALT_QUOTA_2025 26, ZW_LITHIUM_EXPORT_BAN 21.
This is a genuinely multi-jurisdiction base for the first time.

**Event component (0–60) — unchanged and now the sole regulatory gap.** Every
cell reports at least one event, which reads like coverage and is not. The
distribution of events per cell is 1 on 322 cells, 2 on 268, 3 on 143, 4 on
89, 5 on 26 — mean 2.09, max 5, and only the top 3 ever score. But the pool
those are drawn from is **six events total**, one each on CRMA_2024,
EU_BATTERY_REG_2023, EU_CBAM, EU_CONFLICT_MINERALS, EU_CSDDD and
EU_REACH_COBALT. Because EU_CSDDD is universally scoped, **322 cells derive
their entire 60-point event component from a single event.** There is still no
non-EU regulatory instrument driving events anywhere in the grid, and 300
regulatory-category events sit in the database with no
`risk_event_regulations` link and are invisible to scoring.

`material_enforcement_weights` is populated on 3 of the 25 verified
regulations; the other 22 fall through to the documented 1.0 default, so
uncurated regulations keep full obligation points.

### Operational — weight 0.133

Thinnest pillar, completeness **0.199** — and structurally unable to exceed
0.5, because V1 hard-disables the structural-dependency half. All 848 cells
report `dep_source = "v1_event_only"`. That ceiling is a code decision, not a
data gap; restoring it requires the vetted facility watchlist with production
weighting, which is why the MRDS tier was retired.

**511 of 848 cells (60%) score 0**, a slightly worse rate than the 07-28
figure once the base is corrected. The entire corpus is still **143 canonical
operational events, newest 2026-06-30** — now a full month stale.

Five materials have zero operational events in every cell: Germanium,
Neodymium, Praseodymium, Dysprosium, Terbium. The operational news ingester is
the intended filler; a 14-day dry run fetched 97 items, filtered 66, and would
create 31 candidates — roughly 15 per week, landing display-only and requiring
manual promotion before they score.

### Financial pressure — weight 0.0

Computed, persisted and displayed, but does not move `overall_risk_score`.

**The SEC EDGAR tier is still entirely dead.** `company_scores` contains zero
rows with a `financial_pressure_score`, so `sec_edgar_coverage_weight` is 0.0
on all 848 cells. Tier 3 contributes nothing anywhere.

**The price tier got worse, not better.** 34 materials have rows in
`commodity_prices` (5,139 rows), but the newest price of any kind is
2026-06-01 — two months stale — and only **6 materials** have any price in the
trailing 180 days. Tier 1 requires two points inside the window, and it now
fires on only **154 cells**, down from 308 on the old base. The feed has not
been refreshed since the last audit and the window is sliding off it.

That leaves **838 of 848 cells (99%) scoring off USGS Figure 10** — an annual,
_material-level_ price series. The consequence is that financial pressure is
essentially constant across every geography of a given material: Lithium in
Chile and Lithium in Australia get the same number, derived from a global
price. The pillar averages 49.33 with **no zero cells at all**, which reads as
confident coverage and is not.

Because the weight is 0.0 this does not corrupt the overall score. It is a
display-integrity problem, not a scoring one.

### Cross-pillar: events with no `primary_category`

New observation, not in the 07-28 draft. **277 non-duplicate events carry
`primary_category = NULL`** and are therefore selected by no scoring query —
`evidence_query`'s module docstring states this is by design, NULL being the
display-only stream. The composition is 144 `sec_filing_signal`, **119
`GEOPOLITICAL_TRADE`**, 7 `sanctions_listing`, 6 `TRADE_POLICY`, 1
`OPERATIONAL_DISRUPTION`; 270 of the 277 are also `verified = false`.

The 119 events typed `GEOPOLITICAL_TRADE` with a null `primary_category` are
worth a decision rather than an assumption. Either they are genuinely
display-only candidates awaiting promotion, in which case the naming is
misleading, or `primary_category` was never backfilled for that ingester, in
which case there is a free 119-event uplift to the pillar whose
`export_restriction` sub-input is missing on half the grid.

---

## 2. What would actually fill each gap

Re-ordered by leverage — pillar weight × cells affected × effort — against the
4.4 state.

**1. Link the 300 orphan regulatory events.** Now the highest-value work in
the system, and it is pure curation against data already in the database. The
event component is a 60-point range on a 0.267-weight pillar currently driven
by six EU events, with a single event carrying 322 cells. The sixteen non-EU
regulations verified on 07-29 are the obvious targets —
CN_REE_EXPORT_2025, ID_NICKEL_ORE_BAN, DRC_COBALT_QUOTA_2025 and UFLPA are all
instruments the existing event corpus almost certainly already covers. Also
populate `material_enforcement_weights` on the 22 verified regulations that
lack it. Verification itself is no longer the constraint.

**2. Decide the `as_of_date` retention and read-path question.** The rescore
left four older generations in place. Either prune them or audit every read
path to confirm it pins to the newest `as_of_date`. Zero data cost, and it is
the difference between a coherent grid and one that silently mixes four
formulas.

**3. Resolve the universally-absent subsidy term.** 848 of 848 cells carry a
~3.5-point inflation because absent subsidy data redistributes weight rather
than scoring an explicit 0.0. This is a one-line scoring decision affecting
every cell in the product, and it is cheaper than any data acquisition on this
list. Either ingest a source (OECD publishes industrial subsidy estimates) or
make the absent case explicit.

**4. Midstream and downstream production shares.** The largest true
acquisition gap, on the heaviest pillar, and unmoved by the rescore. 61% of
concentration signal currently comes from `ore`. USGS does not publish
refining splits for most materials — but it does publish refinery and smelter
production by country in the same chapters the ingester already reads for mine
production, so extending the parser is a code change rather than a licence
purchase and should be tried before anything is bought. Beyond that: IEA
Global Critical Minerals Outlook (free, 40 rows already ingested), IAI for
aluminium, ICSG/ILZSG/INSG/ITA for copper/lead-zinc/nickel/tin, Adamas for
rare earth separation, Benchmark or Fastmarkets or SMM/Antaike if licensed. A
cheaper proxy: derive stage shares from UN Comtrade or CEPII BACI at the HS
level, free and already matching the schema's shape, at the cost of treating
trade as a production proxy.

**5. Production shares for the eight materials with no
`material_production_shares` row** — Gold, Germanium, Synthetic Graphite and
the four rare earths, plus Bromine. This is the sub-input that filling
`hs_code_production_shares` did *not* fix: it unblocks 40% of the geopolitical
pillar on 486 cells, and for Bromine and Gold it is what would put them in the
grid at all. USGS publishes a rare-earths chapter that is not currently
ingested; gold is well covered by the World Gold Council.

**6. Export restriction coverage — and only export restriction.** The OECD
Inventory of Export Restrictions on Industrial Raw Materials is free, updated,
and an exact schema fit for the sub-input that is missing on 429 cells. Global
Trade Alert is already integrated but needs `GTA_API_KEY` set. Tariff exposure
needs nothing — it is covered on 845 of 848 cells. Check the 119 null-category
`GEOPOLITICAL_TRADE` events first; that may be free.

**7. Refresh the commodity price feed, or stop displaying the pillar.** The
newest price is 2026-06-01 and tier 1 now fires on 154 cells, down from 308.
Whatever populates `commodity_prices` has stopped, and at weight 0.0 the
prior question stands: should this pillar be displayed at all in its current
state? If yes, the cheapest real improvement is running the existing SEC EDGAR
company scorer (note the stale `CIK_MAP` entries in `cli.py ingest-sec-edgar`:
Albemarle 915779 vs DB 915913, MP Materials 1820302 vs DB 1801368 — the DB
values look correct).

**8. Operational news, and the V1 structural ceiling.** The ingester produces
roughly 15 candidates a week against the 320-facility watchlist. It will not
target the thin cells — it polls the whole watchlist and returns what the feeds
carry, and the five materials with zero operational events are unlikely to be
served by facility-level news at all. The completeness ceiling of 0.5 is a code
decision and no amount of news moves it.

**9. Add HS mappings, then shares, for Rhenium and Sodium**, the two materials
still scoring 0 on concentration in every cell. Small, bounded, and it retires
the last of that list. `concentrate` needs the same treatment more broadly —
only Fluorspar, Iron Ore and Molybdenum have any mapping at that stage, so
filling it starts with mappings, not shares.

---

## 3. Open decisions

- Prune the older `as_of_date` generations, or keep them as history and audit
  the read paths? Keeping them means every consumer must pin.
- Should the absent-subsidy case redistribute weight (current behaviour,
  inflating all 848 cells) or score an explicit 0.0?
- Are the 119 `GEOPOLITICAL_TRADE` events with null `primary_category` a
  deliberate display-only stream or an un-backfilled column?
- Should Bromine and Gold be seeded with production shares so they enter the
  grid, or explicitly documented as out of scope? Silently absent is the worst
  of the three.
- Is there a budget for licensed price and midstream-share data, or should the
  roadmap assume free sources only? This determines whether the material
  pillar's midstream gap is closeable at all. Extending the USGS parser to
  refinery production should be attempted first either way.
- Should the financial pillar be displayed while it is 99% a material-level
  annual price signal, on a feed that stopped updating two months ago, at
  weight 0.0?
- Is `intermediate`/`concentrate` share coverage worth pursuing, or should the
  concentration pillar be documented as an upstream-only measure? 61% of
  current signal is `ore`-driven, so the honest label today is upstream.

---

## 4. Changelog

**2026-07-30 — re-measured against the completed 4.4 rescore (848 rows).**
Denominator corrected from a 1,742-row cross-version union to the 848-row
current generation; all percentages restated. Two errors in the 07-28 draft
are retired rather than annotated:

- *The 32 unscored concentration cells were attributed to the two-year
  freshness gate. That was wrong.* All 32 reported `no_share_data` with
  `stage_rollup_count: 0` and an empty `stage_detail` — the scorer found no
  share rows at all, so the gate never ran. They were 16 distinct
  (material, geography) cells duplicated across two scoring versions:
  Dysprosium (CN, MY), Terbium (CN, MY), Neodymium (CN, MY, US),
  Praseodymium (CN, MY, US), Germanium (BE, CA, CN, DE, RU, US). The cause was
  a timing gap: the refined-stage share rows for all five materials — 18 rows,
  reference year 2025, sources `benchmark_rare_earth_exchanges_2`,
  `benchmark_mining_com_lynas_heavy`, `benchmark_usgs_mineral_commodity` —
  were inserted at 2026-07-27 14:14 UTC, roughly fourteen hours after the
  scoring run that would have read them finished at 2026-07-26 23:54 UTC. The
  data was correct and fresh; it arrived late. These were unscored data, not
  bad data, and the 4.4 rescore resolved all 32. The misdiagnosis was partly
  an artefact of 4.3's `stage_detail`, which was empty both when a material
  had no stage data and when a geography was merely absent from it; 4.4
  distinguishes the two.
  **Generalisable lesson, still unaddressed:** ingestion that lands after a
  scoring run is invisible until the next one, and nothing in the grid signals
  it. A cheap guard is to compare `max(created_at)` on the share tables
  against the newest `as_of_date` in `material_geography_risk_scores` and warn
  when shares are newer.
- *The draft reported 9 of 35 regulations verified and framed the other 26 as
  a backlog blocking the event component.* The count was accurate at
  measurement and stale within a day — 25 of 35 are now verified, 16 of them
  on 07-29, and the 10 remaining are auto-suggested Federal Register
  candidates awaiting triage rather than curated instruments awaiting
  sign-off. More importantly the framing was wrong: verification gates the
  *obligation* component, which is now fully curated, while the event
  component is gated on `risk_event_regulations` links, which did not change.

**2026-07-28 — original audit** against 1,742 rows spanning versions
4.1/4.2/4.3, with the code at 4.4.

---

## 5. Related documents

- `concentration_coverage_gaps.md` (2026-07-15) — per-node stage-share gap
  queue. Note its stage-weight discussion (battery_grade 0.30 / refined 0.25 /
  intermediate 0.20 / ore 0.10) describes the 3.x weighted rollup, which 4.0
  replaced with max-over-fresh-stages. The gap inventory remains valid; the
  weighting rationale does not.
- `future_features.md` (2026-07-29) — deferred enhancements: buyer-side market
  scope for material concentration, and the ownership/control adjustment. Both
  tabled with measured unblocking conditions.
- `scoring_v1_spec.md` — current pillar definitions.
