# Coverage-Matrix Gap Fill Plan

> **Date:** 2026-05-06
> **Scope:** The 🔶 gaps surfaced in the coverage matrix at the bottom of
> `scoring-audit-2026-05-addendum.md`.  Each gap has a fix, a scoring-
> integration plan, an effort estimate, and a backfill story.
> **Audience:** Nicole — direct findings only; no partner framing.
> **Predecessors:**
>   - `scoring-audit-2026-05.md` (P0/P1 audit, 2026-05-03)
>   - `scoring-audit-2026-05-addendum.md` (ingester walk-through, 2026-05-05; updated 2026-05-06 with resolution status + coverage-matrix gap legend)

---

## Why this doc exists

After the 2026-05-06 wave of fixes (G2, G3, G4 Half 1, G7, G8, G11, N1,
N2, N3-partial, N4) closed the structural scoring gaps, what remained
was a set of "data sources that COULD feed pillar X but currently
don't."  Those are tracked in the coverage matrix as 🔶 cells.

Filling them isn't free — each requires a parser change, a scoring-
formula decision, and (usually) a re-ingest.  This doc orders them by
impact-vs-effort and explains the integration story for each.

The ❌ (expected) cells in the matrix are NOT in scope here — those
represent by-design non-coverage (a price feed has nothing useful to
say about facility status, etc.).  Only the 🔶 cells are gaps worth
tracking.

---

## Tier 1 — Land first (no dependencies, small + tractable)

### G-Cov-1 — OpenSanctions → Regulatory pillar

**Gap:** Sanctions designations ARE regulatory action.  Today
opensanctions events feed only the Geopolitical pillar via
`risk_categories_json=["geopolitical_trade"]`.  The Regulatory pillar
reads `RiskEvent.risk_categories_json` looking for
`"regulatory_compliance"` — opensanctions never fires there.

**Fix.**
In `app/services/ingestion/opensanctions.py`, change the categories
list to include both:

```python
risk_categories_json=["geopolitical_trade", "regulatory_compliance"]
```

**Scoring integration.**
No formula changes.  The existing `_derive_market_regulatory_inputs()`
in `market_aggregator.py` already filters on
`risk_categories_json @> '["regulatory_compliance"]'`.  Adding the tag
makes existing events visible.

**Effort.** ~5 LOC + comment.

**Backfill.**
Two options:
1. Re-run `bdi-ingest ingest-opensanctions` — the ingester is
   idempotent and rewrites the categories JSON on existing rows when
   the content_hash matches.
2. One-shot SQL (faster):
   ```sql
   UPDATE risk_events
      SET risk_categories_json = jsonb_build_array(
          'geopolitical_trade', 'regulatory_compliance'
      )
    WHERE source_id = (SELECT id FROM sources WHERE name LIKE 'OpenSanctions%')
      AND NOT (risk_categories_json @> '["regulatory_compliance"]');
   ```

Then `bdi-ingest rescore-market`.

**Impact.**
Low-medium.  Sanctioned entities surface in regulatory rationale, with
a modest score lift on materials produced in sanctioned geographies
(mostly CN/RU/IR exposure).

---

### G-Cov-2 — GTA `EXPORT_RESTRICTION` → Operational pillar

**Gap:** Export bans literally curtail supply.  The operational
pillar's `_derive_market_operational_inputs()` reads MRDS
structural_dependency PLUS a narrow event filter
(`{"SINGLE_SOURCE", "CAPACITY_CONSTRAINT"}`).  GTA's
`EXPORT_RESTRICTION` events feed Geopolitical via the HS-node scorer
but never reach the operational pillar.

**Fix.**
Expand the operational event filter in `market_aggregator.py`:

```python
# Was:
if (ew.event.event_subtype or "") in ("SINGLE_SOURCE", "CAPACITY_CONSTRAINT")

# Becomes:
if (ew.event.event_subtype or "") in (
    "SINGLE_SOURCE", "CAPACITY_CONSTRAINT", "EXPORT_RESTRICTION"
)
```

Apply a 0.5× weight on the EXPORT_RESTRICTION events when computing
operational impact — these events ALSO feed Geopolitical (G11 fix
2026-05-06), so full weight here would double-count the same
underlying disruption across two pillars.

**Scoring integration.**
Single change to one filter + a half-weight multiplier in the impact
calculation.  Operational pillar formula stays
`40% × structural_dependency + 60% × event_component`.

**Effort.** ~10 LOC + a synthetic test verifying:
- An EXPORT_RESTRICTION event raises operational pillar by < the same
  event's Geopolitical contribution (proving the half-weight)
- An event with no `RiskEventGeography` row matching the target
  country is excluded (Path B Scope 2 strict-attribution still
  applies)

**Backfill.**
No re-ingest needed — `event_subtype` is already populated by the GTA
post-Scope-2 ingester.  `bdi-ingest rescore-market` picks up the new
operational signal.

**Impact.**
Medium.  Materials with active export bans (CN graphite,
ID nickel, intermittent CL lithium concentrate restrictions) will see
operational pillar scores rise modestly.  Largest deltas where
structural_dependency was 0 because no MRDS data existed for the
material.

---

### G-Cov-3 — `EXPORT_SUBSIDY` event_subtype (closes IEA + GTA Financial gaps)

> **STATUS 2026-05-06: DEFERRED — bundle with G-Cov-4 (Tier 2).**
>
> The producer-vs-consumer-subsidy classification problem
> (e.g. IRA's 30D consumer credit vs 45X manufacturing credit) and the
> sunk-capital persistence problem (manufacturing capacity built under
> repealed subsidies still exists) make this signal nontrivial to
> calibrate.  The plumbing (parse subsidy events, store
> production_subsidy_distortion, surface in rationale) is small, but
> the scoring impact only becomes meaningful after:
>
>   1. Producer-vs-consumer classification at parse time (~1 day work)
>   2. `yearEnded` extraction in iea_policy_tracker.py (~2 hours)
>   3. G4c partner-curated facility seed lands (so sunk capacity is
>      measurable via the operational pillar instead of inferred from
>      subsidy events)
>   4. ~3-6 months of signal observation to calibrate the multiplier
>
> Rather than ship the plumbing in isolation, bundle it with the
> Tier-2 event-driven operational signal work (G-Cov-4).  Both touch
> similar parser changes across GTA / IEA Policy Tracker / Federal
> Register, and both want a partner conversation about pillar weights
> after G4c lands and the operational baseline stabilises.
>
> Original analysis preserved below for context.



**Gap:** Subsidy-type events — state loans, EXIM trade finance, EIB
investment-support — are a real competitive signal but currently get
`event_subtype=NULL` and skip every HS-node sub-score.

Volume in real data:
- GTA `interventions_batteries.csv`: 258 NFI rows + 48 IFI rows = 306
  subsidy events (~18% of the file)
- IEA Policy Tracker: hundreds of `INVESTMENT_PLEDGE` events per pull

**Fix (3 parts).**

**Part A — taxonomy.**  Add `EXPORT_SUBSIDY` to the canonical
event_subtype enum (typed column, migration 040 docstring + comment).
No new migration needed — the column is `String`, not an enum type.

**Part B — ingesters.**

In `gta._INTERVENTION_SUBTYPE_MAP`, add subsidy-side mappings:

```python
"Trade finance":                       "EXPORT_SUBSIDY",
"State loan":                          "EXPORT_SUBSIDY",
"Financial assistance in foreign market": "EXPORT_SUBSIDY",
"Loan guarantee":                      "EXPORT_SUBSIDY",
"Financial grant":                     "EXPORT_SUBSIDY",
"State aid, unspecified":              "EXPORT_SUBSIDY",
"Equity stake":                        "EXPORT_SUBSIDY",
"Financial investment support":        "EXPORT_SUBSIDY",
```

In `iea_policy_tracker.ingest_policy_tracker()`, set
`event_subtype="EXPORT_SUBSIDY"` when `event_type ==
"INVESTMENT_PLEDGE"` AND policy_type contains any of:
`{"investment", "financing", "fund", "grant", "subsid"}`.

**Part C — scoring.**

Add a new sub-input to the Geopolitical pillar (NOT Financial — see
"why Geopolitical not Financial" below).  In
`market_aggregator._derive_market_geopolitical_inputs()`:

```python
# Existing: country_concentration, export_restriction_exposure, tariff_exposure
# New:      production_subsidy_distortion
# Computed as average severity of EXPORT_SUBSIDY events scoped to the
# target geography (implementing country = subsidising country).
# Severity is already calibrated by gta._severity_for() and the new
# Scope-2 multipliers.
```

Modest weight on the new sub-input — subsidies don't increase sourcing
risk for the producer country, but they DO distort downstream
competitive dynamics.  Recommend 0.10 weight (vs 0.40 country
concentration, 0.30 export, 0.20 tariff = sums to 1.00).  Partner
discussion needed.

**Why Geopolitical not Financial.**
The Financial pressure pillar reads commodity-price volatility and
SEC company financial-pressure scores.  Subsidies are government
interventions, not commodity prices or company financials — they
belong with other government-action signals (tariffs, sanctions, export
controls) in Geopolitical.

**Scoring integration details.**
Geopolitical pillar weights need to be re-normalised when the new
sub-input lands.  The current decomposition (in
`geopolitical_risk.score_geopolitical_trade()`):

```python
0.45 × country_concentration
0.35 × export_restriction_exposure
0.20 × tariff_exposure
```

Becomes:

```python
0.40 × country_concentration
0.30 × export_restriction_exposure
0.20 × tariff_exposure
0.10 × production_subsidy_distortion
```

Or alternatively, leave weights frozen and only fire the new sub-input
when subsidies meaningfully exceed a threshold (avoiding score creep
on materials with no meaningful subsidy presence).

**Effort.** Medium.  ~50 LOC across `gta.py`,
`iea_policy_tracker.py`, `market_aggregator.py`, plus partner
conversation about the multiplier value (similar to the
subnational/horizontal calibration in G11 Scope 2).  Plus a synthetic
test exercising the new sub-input.

**Backfill.**
Re-ingest GTA + IEA Policy Tracker so existing events get the new
`event_subtype` value.  Then rescore.

```bash
bdi-ingest ingest-gta --local-file data/global-trade/interventions_batteries.csv
bdi-ingest ingest-iea-policy-tracker --file-path data/iea/iea_policy_tracker.csv
bdi-ingest rescore-market
```

**Impact.**
Medium-high.  Adds a previously-zero signal to ~300+ GTA events and
hundreds of IEA Policy Tracker events.  Most visible on materials
where production is heavily state-financed (REE / silicon / battery-
grade lithium in CN; nickel in ID; cobalt in DRC).

**Removes from backlog.**
- "EXPORT_SUBSIDY / TRADE_FINANCE event_subtype" (P2 backlog)
- "Decode GTA NFI/IFI implementation_level codes" (P2 backlog —
  decoded but multiplier was 1.0 because subtype was NULL; once
  subtype fires, multiplier matters and partner can recalibrate)
- IEA Policy Tracker → Financial gap (closes via Geopolitical instead)
- GTA → Financial gap (closes via Geopolitical instead)

---

## Tier 2 — Bundle after G4c partner facility seed

This tier covers two intertwined pieces of work.  Both are gated on the
same precondition (G4c's partner-curated facility seed must land first
so structural_dependency has fuller coverage to anchor against), and
both touch similar parser changes across multiple ingesters.  Worth
shipping as one batch when the moment comes.

### G-Cov-3 + G-Cov-4 (bundled) — Event-driven operational signals + EXPORT_SUBSIDY subtype

**Gaps closed:**
- Federal Register → Operational
- OpenSanctions → Operational
- SEC EDGAR → Operational
- pipeline.py news → Operational (depends on real news provider)

**Why wait for G4c.**
The operational pillar formula is `40% × structural_dependency + 60%
× weighted_event_impacts`.  Today, structural_dependency is mining-
heavy because MRDS coverage is mining-heavy.  Adding event-driven
signals NOW would weight them more heavily relative to a thin
structural baseline.  After G4c (partner-curated facility seed) lands
and refining-stage coverage is meaningful, structural_dependency
becomes the stable baseline and event signals layer on top sensibly.

**Fix.**
Add a new event_subtype `OPERATIONAL_DISRUPTION`.  Per-source
detectors:

| Source | Detector | Pattern |
|---|---|---|
| Federal Register | regex / LLM | "permit denial", "EPA enforcement action", "ROD vacated", "consent decree shutdown" |
| OpenSanctions | structural | sanctions designation against company tagged with `supply_chain_stage` in (mine/refiner/processor) → operational signal in that company's `source_geography` |
| SEC EDGAR | text | parse 10-K Item 1A risk-factor sections for "ceased operations", "indefinitely suspended", "force majeure declaration", "production halted" |
| news (when real provider) | text | similar patterns |

**Scoring integration.**
Add `OPERATIONAL_DISRUPTION` to the operational pillar event filter.
Apply full weight (1.0×) — these events are operational-specific and
not double-counted in other pillars.

For sources that already feed other pillars (OpenSanctions in
Geopolitical, Federal Register in Regulatory), the SAME RiskEvent row
contributes to multiple pillars via different `risk_categories_json`
tags + `event_subtype` filters.  This is consistent with how
Federal Register events already feed both Geopolitical and Regulatory
today.

**Effort.** High.  ~3-5 days, mostly text-classification work.  Each
source has its own pattern.

**Backfill.**
Re-ingest each source after the parser updates land.  Each ingester is
idempotent so existing events get re-classified rather than
duplicated.

**Impact.**
High after G4c lands.  Today, low-medium and risks distorting scoring
for materials with thin structural data.

---

## Tier 3 — Skip or defer

### G-Cov-5 — IEA Policy Tracker / GTA → Financial (skip; subsumed by G-Cov-3)

These cells in the matrix are flagged 🔶 because subsidy events COULD
in principle feed Financial pressure (cheap state capital implies
cheaper production cost → margin distortion).  But routing them to
Geopolitical (G-Cov-3) is cleaner — Financial pillar is for commodity
prices and company financial filings, not government interventions.

**Decision:** SKIP.  Closed by G-Cov-3.

### G-Cov-6 — MRDS → Material Concentration (skip; redundant with USGS)

MRDS facility distribution by country COULD compute facility-count HHI
as a Material Concentration input.  But USGS MCS already provides
production-volume HHI, which is a more accurate signal — one big mine
contributes more than ten small ones, and production HHI captures
that, facility-count HHI doesn't.

**Decision:** SKIP unless USGS HHI ever becomes unreliable.  Effort
~30 LOC if it ever needs to land; signal is otherwise redundant.

---

## Tier 4 — Future (gated on real news provider)

### G-Cov-7 — pipeline.py news → Operational + Financial

Currently the news adapter is `StubNewsProvider` returning
deterministic test content.  Until a real news provider (NewsAPI /
GDELT / licensed feed) is wired in, there's no real news to apply
detectors to.

**When to revisit:** When a real news provider lands.  Apply the
G-Cov-4 detectors to article text.

**Effort:** Inherits from G-Cov-4 + new ingester wiring.

**Impact:** TBD — depends on news volume + signal quality.

---

## Backfill summary table

| Fix | Status | Re-ingest | Re-score | DB UPDATE option |
|---|---|---|---|---|
| G-Cov-1 (opensanctions categories) | ✅ LANDED 2026-05-06 | optional — re-ingest works | ✅ required | ✅ one-shot SQL |
| G-Cov-2 (GTA EXPORT_RESTRICTION → operational) | ✅ LANDED 2026-05-06 | ❌ not needed | ✅ required | n/a |
| G-Cov-3 (EXPORT_SUBSIDY subtype) | DEFERRED — bundle with G-Cov-4 | ✅ GTA + IEA Policy Tracker | ✅ required | n/a |
| G-Cov-4 (event-driven operational signals) | DEFERRED — gated on G4c | ✅ each source after parser updates | ✅ required | n/a |
| G-Cov-5 | (skipped) | — | — | — |
| G-Cov-6 | (skipped) | — | — | — |
| G-Cov-7 | (waits for news provider; folds into G-Cov-4 bundle) | — | — | — |

---

## Recommended sequencing

### Week 1 — quick wins (Tier 1 small) ✅ LANDED 2026-05-06

1. ~~**G-Cov-1**~~ — OpenSanctions regulatory category.  Done; ingester updated, backfill via SQL UPDATE OR re-ingest after pulling latest.
2. ~~**G-Cov-2**~~ — GTA `EXPORT_RESTRICTION` → Operational pillar.  Done; `_export_restriction_operational_impacts()` helper writes half-weighted impacts, synthetic test passes.

Both verified end-to-end in `Automotive Data Solutions/test_gcov_1_2.py`.

### Future bundle (post-G4c)

3. **G-Cov-3 + G-Cov-4 bundled** — EXPORT_SUBSIDY subtype + event-
   driven operational signals.  Big structural change combining:
     - producer-vs-consumer subsidy classification at parse time
     - `yearEnded` / repealed-status handling in iea_policy_tracker
     - per-source operational-disruption detectors (Federal Register
       permitting actions, OpenSanctions production sanctions, SEC
       EDGAR 10-K risk-factor parsing, news once a real provider
       lands)
     - operational pillar formula stays at 40/60 but event-impacts
       term gets richer
     - geopolitical pillar gets `production_subsidy_distortion` sub-
       input, partner calibrates W after observing signal for 3-6
       months
   Worth doing only after G4c lands and the operational pillar's
   structural_dependency has fuller coverage to serve as stable
   baseline.

### Skipped

- **G-Cov-5** (subsumed by G-Cov-3)
- **G-Cov-6** (redundant with USGS HHI)

### Future-gated

- **G-Cov-7** (waits for real news provider; folds into the G-Cov-4
  bundle when news ingestion goes real)

---

## What this plan does NOT cover

- New scoring pillars or new dimensions beyond the existing five
  market pillars (Material Concentration, Geopolitical, Regulatory,
  Operational, Financial).
- Re-calibration of the existing pillar weights in
  `MARKET_PILLAR_WEIGHTS`.
- Dynamic HCG derivation, WGI / governance baselines, USITC
  structural tariff baseline — these are P2 backlog items in the
  audit addendum, not coverage-matrix gap fills.
- US-dependency tier scoring — explicitly deferred per partner
  consultation.

---

## Verification queries (run after each tier lands)

After Week 1 (G-Cov-1 + G-Cov-2):

```sql
-- G-Cov-1: confirm opensanctions events now carry regulatory_compliance
SELECT count(*) FROM risk_events
 WHERE source_id = (SELECT id FROM sources WHERE name LIKE 'OpenSanctions%')
   AND risk_categories_json @> '["regulatory_compliance"]';
-- Expect: > 0 after backfill

-- G-Cov-2: confirm operational rationale now mentions EXPORT_RESTRICTION events
SELECT material_id, geography_code,
       rationale_json->'sub_inputs'->'operational'->>'event_impact_count' AS op_events
  FROM material_geography_risk_scores
 WHERE rationale_json->'sub_inputs'->'operational'->>'event_impact_count' != '0'
 LIMIT 10;
```

After Week 2-3 (G-Cov-3):

```sql
-- Confirm EXPORT_SUBSIDY events flow through both ingesters
SELECT
  s.name,
  count(*) FILTER (WHERE re.event_subtype = 'EXPORT_SUBSIDY') AS subsidy_events,
  count(*) AS total_events
  FROM risk_events re
  JOIN source_documents sd ON sd.id = re.source_document_id
  JOIN sources s ON s.id = sd.source_id
 WHERE s.name IN ('Global Trade Alert', 'IEA Critical Minerals Policy Tracker')
 GROUP BY 1;
-- Expect: both sources show non-zero subsidy_events
```

After G4c + G-Cov-4:

```sql
-- Confirm OPERATIONAL_DISRUPTION events from each source
SELECT s.name, count(*)
  FROM risk_events re
  JOIN source_documents sd ON sd.id = re.source_document_id
  JOIN sources s ON s.id = sd.source_id
 WHERE re.event_subtype = 'OPERATIONAL_DISRUPTION'
 GROUP BY 1
 ORDER BY 2 DESC;
```
