# Future features — deferred enhancements and their unblocking conditions

**Created:** 2026-07-29 · **Status:** Roadmap. Nothing here is implemented.

Purpose: capture enhancements that were designed far enough to be costed, then
deliberately deferred, so the reasoning and the measured preconditions are not
re-derived later. Each entry records what the feature is, why it was tabled,
what already exists in the codebase, what would still need building, and the
concrete condition that should trigger picking it back up.

Append new entries as they are deferred. Keep the measurement dates on the
coverage numbers — they go stale.

---

## 1. Buyer-side market scope for material concentration

**Deferred 2026-07-29 (Nicole).** Rationale: the engine is still in flux on
scoring versions and on the ingestion sources needed to fill single-scope gaps.
The persisted grid currently holds three formula versions (872 rows at 4.1, 26
at 4.2, 844 at 4.3) against code at 4.4. Adding a second scope dimension
multiplies the surface area — storage, UI labelling, rescore cost — before the
single-scope grid is stable at one version. Revisit once the grid is coherent.

### What the feature is

Today the material concentration pillar answers *"where is world production of
this material concentrated."* A buyer-side scope would answer a different
question — *"where does **our** supply of this material come from, and how
concentrated is that"* — by scoring against an import-source distribution
instead of a global production distribution.

The two are genuinely different numbers. Under a US scope, large producers that
do not ship to the US drop out of the geography universe entirely, and small
producers that dominate US supply spike. Jamaica would carry roughly 60% on
aluminum ore. That is the correct answer to "where does US bauxite come from"
and the wrong answer to "where is bauxite concentrated." See *Labelling hazard*
below — this is the main reason to ship it as a separate view rather than a
second scope of the same score.

### What already exists

The scorer is already parameterised. `load_share_rows(db, material_id,
market_scope=...)` takes the scope as an argument defaulting to `"global"`, and
`score_material_concentration` passes it straight through
(`app/services/scoring/stage_concentration.py`). No scorer change is required
to compute a US view.

The data is also better than expected. As of 2026-07-29 there are **318
`market_scope='us'` rows across 81 mappings**, all sourced `usgs_mcs`, all
reference year 2026 — these are the USGS MCS "Import Sources (2021–24)" tables.
Critically, they cover several stages that are **empty at global scope**:

| stage | materials with US-scope shares but no global-scope shares |
|---|---|
| refined | Chromium, Tin, Zinc, Tantalum, Zirconium, Rhenium, PGM, REE, Nickel*, Manganese |
| intermediate | Copper, Germanium, Vanadium, Niobium, Zirconium, Manganese, Molybdenum, Rhenium, Selenium |
| concentrate | Molybdenum — **the only concentrate rows anywhere in the table** |
| battery_grade | Sodium |

\* Nickel has global refined shares at 2024; the US-scope row is 2026.

Prior guidance in `concentration_coverage_gaps.md` (2026-07-15) already states
that these rows must never be copied into global scope. That still holds. This
entry proposes using them *as their own scope*, not promoting them.

### Blocker: the shares are not HHI-valid as stored

This is arithmetic, not preference. HHI assumes shares of a single market
summing to 1. Measured on the most specific mapping per material and stage
(2026-07-29):

| material / stage | sum | detail |
|---|---|---|
| Gallium / refined | **0.84** | CA .28, JP .22, CN .18, DE .16 |
| Aluminum / ore | 0.93 | JM .60, TR .16, GY .09, AU .08 |
| PGM / refined | **1.19** | ZA .49, RU .36, DE .10, BE .10, IT .08, CA .06 |
| Selenium / intermediate | **1.54** | KR .78, PH .25, MX .14, CL .12, PL .11, CN .10, DE .04 |
| Aluminum / intermediate | 1.78 | BR .71 ×2, JM .07 ×2, … (duplicate rows) |

Two distinct causes:

* **Sums below 1.0** — USGS publishes the top three or four origins plus
  "Other," and "Other" is dropped on ingest. Gallium is missing 16% of its
  distribution.
* **Sums above 1.0** — several distinct HS lines within one stage each carry
  their own import-source list and are being summed together. PGM refined is
  platinum + palladium + rhodium; Selenium spans two lines.

Fed to `sum(s²)` as-is, Selenium's HHI comes out roughly 2.4× too high and
Gallium's about 30% too low. The parent/child dedupe in
`compute_stage_concentration` catches the straight duplicate rows (Aluminum
intermediate) but not the multi-product case, because those are legitimately
different countries-and-values at the same specificity.

### What would need building

1. **Normalisation, in the USGS MCS parser.** Preference: ingest the "Other"
   row explicitly as an unallocated tail rather than renormalising the top N to
   1.0. A 16% unallocated residual on Gallium should visibly suppress
   confidence, not be silently redistributed onto Canada. This is the piece to
   do first — everything downstream is wrong until it lands.
2. **Dedupe across HS lines within a stage.** The current dedupe is by country
   and prefers the most specific mapping; it has no concept of "two different
   products, both legitimately at this stage." Needs either a product-form
   discriminator on the mapping or an explicit within-stage aggregation rule.
3. **A storage decision.** `material_geography_risk_scores` has no scope
   column. Either add one and extend the uniqueness constraint to
   (material, geography, as_of_date, market_scope), or keep the table
   global-only and compute the buyer view on read. The second is cheaper and
   avoids doubling rescore cost; the first is needed if the buyer view is to
   carry history.

### What would *not* change

The formula. Same HHI, same `hhi_cliff`, same `sqrt(share)`, same governance
amplifier. Amplifying an import-source share by the source country's
instability remains coherent — arguably more so, since a buyer's exposure to an
unstable supplier is exactly the thing being measured.

### Labelling hazard

Two numbers under one label is how someone quotes the wrong one. Recommendation
when this is picked up: ship as a **separately named view** ("US import
dependence" or similar), not as a second scope of "material concentration," and
do **not** let it feed `overall_risk_score`.

### Open question

US only, or EU as well? An EU view needs Eurostat Comext — free, but a new
ingester and a different data shape. Worth deciding before the storage decision,
since a two-buyer world argues for the scope column rather than compute-on-read.

### Unblocking condition

Grid coherent at a single scoring version, **and** the normalisation fix landed
in the USGS parser. The second is independently useful — the `us`-scope rows are
wrong today whether or not anything scores them.

---

## 2. Ownership / control adjustment for material concentration

**Deferred 2026-07-29 (Nicole).** Rationale: the underlying data is not there
yet. To be revisited as part of the companies workstream, when production
numbers are filled in across the board.

### What the feature is

Production shares attribute output to the country the material physically comes
out of. They do not see who controls the asset. Roughly three-quarters of DRC
cobalt output sits under Chinese ownership (CMOC, Zijin, Huayou); most
Indonesian nickel HPAL capacity is Chinese JV. Geographic HHI therefore
systematically **understates** effective concentration wherever one country's
firms own another country's production.

Two defensible positions, answering different questions:

* **Geography-attributed (current).** The tonne is risky because of the ground
  it came out of. Answers: *"if this country becomes unstable, what fraction of
  supply is at risk."* The WGI governance amplifier is built on this reading.
* **Control-attributed.** The tonne is risky because of who controls it.
  Answers: *"if this government decides to restrict, what fraction of supply
  can it reach."*

Neither is wrong. Shipping a number that silently mixes them would be, because
"material concentration" would mean two things at once.

### Candidate mechanics

* **Parallel HHI, take the worse.** Compute a second HHI over control-country
  alongside the existing production-country HHI and use `max`. Preserves the
  existing number intact and adds a second, separately-labelled one. Lower risk.
* **Blended effective share.**
  `effective_share[control_country] += share[producing_country] × owned_fraction`.
  More faithful, but it changes what the number *means* — CN's cobalt share
  stops being "cobalt mined in China" and becomes "cobalt mined under Chinese
  control." That redefinition has to be visible in the UI or it misleads.

Either way the input requirement is the same: per (producing country, stage), a
decomposition of national output by controlling nationality. That requires
facility-level ownership percentage **multiplied by facility capacity**, rolled
up to country.

### Preconditions — measured 2026-07-29

| ingredient | coverage | note |
|---|---|---|
| facilities with any owner link | **327 of 5,817 (5.6%)** | mines: 156 of 4,490 |
| companies with `incorporated_country` | **19 of 100** | |
| `company_jv_parents` rows | **0** | table exists, never populated |
| facility-material links with `annual_capacity_tpy` | **118 of 6,146** | ore: 38 of 4,825; refined: 46 of 108 |

The schema is well designed for this — `company_facilities.ownership_pct` is
populated on 307 of 329 links and all 329 are verified, and `companies` already
carries `is_state_owned_or_influenced` and `has_uflpa_designation`. The gap is
purely coverage.

**Capacity is the binding constraint.** Without it there is no arithmetic path
from "company X owns 60% of facility Y" to "N% of country Z's output." At ore
stage capacity exists on 0.8% of links. Everything else in this entry is moot
until that moves.

**`company_jv_parents` being empty is the sharper issue.** Chinese control of
Indonesian nickel and DRC cobalt is structured almost entirely through JVs.
That table is precisely where the answer would live.

### Definitional trap: incorporation is not control

`incorporated_country` must not be used as the control country. The largest
single owner in the table by facility count is Jersey-incorporated (50 links —
Glencore, which is Swiss-run). Adjusting on incorporation would generate risk
attributed to Jersey and the Cayman Islands. A separately curated
beneficial-control field is required, and it is a judgement call per company,
not an ingest.

### Design constraint for whenever this lands

Adopt the no-op pattern the codebase already uses for the WGI amplifier: absent
data must be a true no-op, not a default. Concretely, the ownership adjustment
should apply to a (country, stage) cell **only where curated capacity covers a
meaningful fraction of that country's share at that stage**, and otherwise leave
the geographic share untouched. A partial adjustment over 5% of facilities is
worse than none — it produces a number that looks adjusted and is not.

### Interim cheap version (not deferred — available now)

An annotation rather than an adjustment. Surface `is_state_owned_or_influenced`
(5 companies flagged) and `has_uflpa_designation` in the evidence drawer, so a
reader sees "foreign-controlled capacity present" without the score having to be
arithmetically defensible. Costs almost nothing, requires no new data, and
captures a large share of the decision value.

### Unblocking condition

The companies workstream, specifically: `annual_capacity_tpy` populated at ore
and refined stage well beyond the current 118 links; owner links materially
above 5.6% of facilities; `company_jv_parents` populated; and a
beneficial-control field distinct from `incorporated_country`.

---

## Related documents

* `concentration_coverage_gaps.md` (2026-07-15) — per-node stage-share gap
  queue. Note its stage-weight discussion (battery_grade 0.30 / refined 0.25 /
  intermediate 0.20 / ore 0.10) describes the 3.x weighted rollup, which 4.0
  replaced with max-over-fresh-stages. The gap inventory remains valid; the
  weighting rationale does not.
* `scoring_v1_spec.md` — current pillar definitions.
