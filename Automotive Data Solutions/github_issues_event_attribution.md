# GitHub Issues — Event Attribution & Per-Country Score Differentiation (2026-05-12)

Three tickets arising from an investigation into why `event_count` on `material_geography_risk_scores` was nearly uniform (117–158) across all 113 countries for Natural Graphite — a symptom that surfaced via the Country Scores tab redesign.

The investigation found the column stores the **union** of material-anchored events (all events for the material, any country) and geography-anchored events (all events in the country, any material). The material-anchored half dominates, leaving each country with the same ~115-event floor plus a thin per-country sliver. The **intersection** — events tagged to both this material AND this country — is what an analyst expects the column to mean, and it's 3–30× smaller with sensibly differentiated per-country counts.

Step 1 (data structure) has already shipped — recorded below for traceability. Steps 2 and 3 are open.

Copy each `---`-delimited block into its own GitHub issue. Update the epic checklist with actual issue numbers once you create them.

---

## EPIC: Event attribution correctness and per-country score differentiation

**Labels:** `epic`, `area/scoring`, `area/ingestion`

### Context

When redesigning the Country Scores tab to filter to countries with material exposure (default-on, ≥1% production share OR ≥5 events OR ≥1 facility), every one of Natural Graphite's 113 scored countries passed the filter — the events branch was admitting everything because all 113 rows had event_count between 117 and 158.

Diagnosis confirmed:
- `market_aggregator._score_material_for_geography` computes `event_count = len(dedup(material_anchored_events ∪ geo_anchored_events))`.
- For Natural Graphite, the material-anchored half is 115 events (any country, geopolitical_trade category, within the 730-day window). That's the floor.
- The geo-anchored half adds 2–60 country-specific events on top.
- Intersection (events tagged to BOTH this material AND this country) drops the numbers dramatically: CN=66, JP=23, UA=9, AU=8, NZ=3, CD=2. That's the supply-chain-salience-ordered signal we actually want.

Two adjacent findings surfaced in the same investigation and both deserve their own tickets:

1. The pillar sub-input derivation also consumes the union. Whether downstream sub-input math is geography-aware enough to wash out the union floor is unverified — that's Step 2.
2. Average attribution density per Graphite event: **22.3 materials per event (max 34 — the Tier 1.3 Top-N cap is binding)** and **1.76 countries/event (max 75 — long-tail mass-tagged events)**. Whether these are legitimate (e.g., "OECD critical-minerals policy" genuinely touches many materials) or ingester defaults that need tightening is unverified — that's Step 3.

### Checklist

- [x] #TBD — Step 1: Store `event_count_geo_specific` on MaterialGeographyRiskScore *(merged 2026-05-12, migration 042 — record below)*
- [ ] #TBD — Step 2: Audit pillar sub-input derivation for genuine country differentiation
- [x] #TBD — Step 3: Audit attribution density *(closed 2026-05-12 — investigated, no attribution-logic changes; reset-events --source flag + structural-metadata capture retained from the investigation, see below)*

### Why these three are separable

Step 1 is a display + filter fix and was non-blocking — the UI no longer misleads. Step 2 is a scoring-correctness question: even with the display fixed, if pillar sub-inputs are over-driven by the union floor then per-country scores are less differentiated than they appear. Step 3 is an upstream attribution question: even with sub-input derivation fixed, if events are wrongly attributed to 30+ materials each, the downstream math is fed bad input.

If forced to pick, **Step 3 first** — fixing upstream attribution propagates value into both Step 2 and Step 1's stored counts (no point auditing sub-input math against bad inputs). Step 2 second. Step 1 done.

### Related

- Migration 042: `alembic/versions/042_event_count_geo_specific.py`
- Originating UI surface: Country Scores tab on `/data/materials/[id]` — the filter that exposed the symptom
- Tier 1.3 (in `github_issues_parsing_followups.md`) capped per-event material attribution at 32 — that cap is the source of the 22.3-materials/event average and is binding for many events; Step 3 may revisit it

### Out of scope

- The cross-material `/market/scores` browse endpoint — that one uses the union deliberately for cross-material comparison; this epic only touches the per-material × per-geography display
- Recalibrating NFI/IFI severity multipliers (parked, waiting on partner input)
- Confidence threading changes (already done in the parsing-followups epic)

---

## Step 1: Store `event_count_geo_specific` on MaterialGeographyRiskScore *(merged 2026-05-12)*

**Status:** Merged. Recorded for traceability — do not open as a fresh ticket.

**Labels:** `area/scoring`, `area/db`, `effort/small`

### What landed

Migration 042 adds a nullable `event_count_geo_specific` integer column to `material_geography_risk_scores`. `market_aggregator._score_material_for_geography` now computes the intersection alongside the existing union and writes both:

- **`event_count`** (existing, unchanged semantics) — UNION of material-anchored and geo-anchored events consumed by the pillar sub-input derivation. Preserves audit trail of what was fed into the score.
- **`event_count_geo_specific`** (new, nullable) — INTERSECTION of `RiskEventMaterial ∩ RiskEventGeography`, computed per category, then unioned across `geopolitical_trade` and `operational`. Cheap to compute (set intersection of in-memory event id sets — no extra DB round-trip).

`rationale_json["event_counts"]` now also exposes per-category geo-specific subtotals (`trade_events_geo_specific`, `operational_events_geo_specific`) so the score-breakdown panel can show "of N trade events consumed, K were geo-specific."

### What changed on the frontend

- `MaterialGeographyScoreRead` type extended with `event_count_geo_specific?: number | null`
- `buildMarketRiskScoreColumns` Events column now displays the geo-specific count; null renders as `—` with a "rescore to populate" hover tooltip rather than silently falling back to the misleading union value
- Country-scores `hasMaterialExposure` filter now keys off `event_count_geo_specific` so the events branch is meaningful again (Graphite intersection counts CN=66, JP=23, UA=9, AU=8 pass; NZ=3, CD=2 fail — discriminating)

### Migration / deploy notes

- Pre-042 rows have NULL `event_count_geo_specific`. They render as `—` in the Events column and cannot trigger the events branch of the exposure filter (share and facility branches still work).
- Re-running `POST /market/rescore` populates the field for every (material, geography) pair touched. No data backfill SQL was written because the per-category lookback windows are easier to honour from the aggregator than from raw SQL.

### Acceptance (already met)

- Migration runs cleanly against dev DB.
- `tests/scoring/` 84/84 pass.
- Frontend `npm run typecheck` clean.
- After a rescore, Country Scores tab for Natural Graphite shows discriminating Events column values.

### Files

- `alembic/versions/042_event_count_geo_specific.py`
- `app/models/scoring.py` — added Mapped field
- `app/services/scoring/market_aggregator.py` — intersection computation, ORM + upsert writes, rationale_json update
- `app/schemas/market_scores.py` — added field to `MaterialGeographyScoreRead`
- `lib/types/reference-data.ts` (frontend) — type extension
- `lib/table/market-risk-score-columns.tsx` — Events column accessor change + null handling
- `app/(dashboard)/data/materials/[id]/page.tsx` — exposure filter keys off geo_specific

---

## Step 2: Audit pillar sub-input derivation for genuine country differentiation

**Labels:** `area/scoring`, `priority/medium`, `effort/medium`

### Problem

The market aggregator passes `all_trade_events` (the union of material-anchored + geo-anchored events) into `_derive_market_material_inputs` and `_derive_market_operational_inputs`. It passes `geo_trade_events` (the geo-anchored half only) into `_derive_market_geopolitical_inputs`. The asymmetry is undocumented.

If material-input and operational-input derivation consume the union without re-filtering by `geography_code` internally, then the per-country pillar scores would be partially driven by the global material event pool — meaning the actual differentiation between, say, Graphite-in-China and Graphite-in-Brazil pillar scores is smaller than it could be. The Step 1 display fix doesn't address this; it only fixes what's *shown* in the Events column.

Visually, the rendered pillar scores already differ across countries (CN Geopolitical=55, JP=32, CD=70 for Graphite), so there IS some country-aware computation downstream. But "differs by a few points" vs "differs by enough to be analytically useful" hasn't been verified.

### What to do

1. **Read each `_derive_*_inputs` function carefully** and document for each:
   - Does it accept the geography_code parameter?
   - Does it re-filter the event list by `geography_code` (via `RiskEventGeography` membership) before computing sub-inputs, or does it use every event in the input list regardless of geography?
   - For each sub-input it produces, is the computation geography-aware (uses HS-node × country aggregates, country-specific signals, etc.) or geography-blind (uses material-level features only)?

2. **Measure the variance.** For Natural Graphite, pick 5 different countries (CN, JP, BR, CD, NZ — high-share + low-share mix) and log per-sub-input values for each. Specifically:
   - How much do `criticality`, `concentration`, `trade_volatility` (material pillar) vary across these 5? They probably shouldn't — they're material-level features. Confirm.
   - How much do `country_concentration`, `export_restriction_exposure`, `tariff_exposure`, `subsidy_distortion` (geopolitical pillar) vary? They should vary significantly.
   - Same question for `structural_dependency` and `event_impact_count` (operational pillar).
3. **Decide for each pillar:** is the current derivation correct, or does it need either (a) re-filtering the event list inside the derivation, or (b) splitting the event list into "global material signal" + "country-specific signal" upstream and letting the derivation weight them explicitly?
4. **Document the contract.** Whatever the decision, add inline comments in `market_aggregator.py` describing for each `_derive_*` call WHY it gets `all_*_events` vs `geo_*_events` so future maintainers don't accidentally pass the wrong list.

### Acceptance

- Written audit (added to `docs/scoring-audit-2026-05-addendum.md` or a new doc) describing, per pillar and per sub-input, whether derivation is geography-aware and what list it consumes.
- If any derivation is incorrectly geography-blind, ship the fix with a synthetic test that exercises two different countries for the same material and asserts the affected sub-inputs differ.
- Inline contract comments at every `_derive_market_*_inputs` call site explaining the event list choice.

### Effort

Medium — 1–2 days. The reading is fast; the variance measurement requires a small script and access to scored data; the design conversation if a fix is needed could expand it.

### Dependencies

None hard, but Step 3 should ideally land first — auditing sub-input math against potentially-bad attribution input is wasted effort if attribution itself is over-broad.

---

## Step 3: Audit attribution density *(closed 2026-05-12 — no-op outcome)*

**Status:** Investigated and closed without code changes to attribution logic. Two adjacent pieces of work landed during the investigation and are recorded separately below. Do not open as a fresh ticket — this entry is for the audit trail.

### What was investigated

Diagnostic on Natural Graphite events showed:
- **22.3 materials per event on average, max 34** (initially interpreted as a Tier 1.3 cap leak).
- **1.76 countries per event on average, max 75** (capped correctly by `_AFFECTED_COUNTRY_CAP`).
- 86 events with >32 materials, all GTA, all with 204–294 HS codes in the CSV.

### Hypothesis (wrong) and remediation attempt (reverted)

The first hypothesis was that the curated CSV's `Affected Products` column carried a near-universal HS-code blob, and 86 over-cap events were the symptom. A structural classifier was built in `gta.py` to gate HS-based material attribution when an event's `mast_chapter` was `L: Subsidies`, `F: Price-control`, `M: Procurement`, `FDI measures`, `Capital control`, or `G: Finance`, or when `eligible_firm` indicated a targeted financial program, or when the intervention_type mapped to `EXPORT_SUBSIDY`. That classifier was reverted the same day.

### Why the classifier was reverted

A follow-up distribution audit across all 3,787 rows in the three GTA CSVs falsified the universal-blob premise:

- **The 251–300 HS-code bucket the original diagnostic latched onto is only 1.3–2.4% of rows.** The actual distribution is bimodal: ~30% genuinely narrow (≤20 codes), ~30–40% genuinely wide (≥300 codes — sanctions packages, GSP overhauls, Section 232-style schedules), ~30% in between.
- **Pairwise Jaccard similarity across rows from different mast_chapters: mean 0.022, median 0.000.** Rows have essentially disjoint HS code sets — there is no shared blob to filter against.
- **Sample inspection of "wide" rows confirms they are legitimately wide.** EU GSP overhauls (3,015 codes) really do affect thousands of tariff lines; EU sanctions on Belarus (1,379 codes) really do enumerate hundreds of HS lines; US Section 232 (208 codes) really does span HS 73xx.
- **Sample inspection of "narrow" rows confirms they are policy-targeted.** Indonesia's export-duty changes (5 codes covering iron/copper/aluminum ores), US brass-drains tariff reclassification (1 code = 741820), etc.
- The classifier would have gated ~63% of GTA events (1,457+ out of 2,299 in the batteries CSV) — most of which have legitimate, per-intervention HS lists. That's throwing away real signal to fix a problem that doesn't exist at the claimed scale.

### What actually remains useful from the investigation

1. **Step 1 (the `event_count_geo_specific` column)** — already shipped, addresses the original UI symptom that started this audit. The "117–158 events per country" pattern in the dashboard was a display issue (event_count was a *union* of material-anchored ∪ geography-anchored), not a data-quality issue. The intersection count fixes the display.

2. **`reset-events --source` CLI flag** — useful infrastructure for any future per-source reset, kept after the revert.

3. **Capture of `mast_chapter`, `eligible_firm`, `affected_sectors_raw` into `RiskEvent.metadata_json`** — kept after the revert. Useful for future ad-hoc analysis (e.g., "show me all Subsidy events for Brazil") even though we don't act on these fields at ingest time.

### Methodology lesson recorded

The original 10-event sample (Singapore COVID stimulus, Jordan e-commerce fees, Brazil BNDES) all happened to have ~294 HS codes and was treated as representative. It wasn't — those were outliers from a bimodal distribution. The right ordering would have been: distribution audit FIRST, sample inspection SECOND. Sample-driven inference without a distribution test produced a misdiagnosis and ~1.5 hours of code that had to be reverted.

### Real open questions surfaced (separate tickets if pursued)

- **Should subsidies move a material's risk score the same direction as restrictions do?** A Brazil BNDES credit line and a Russia metals embargo currently both contribute to the Geopolitical pillar via different sub-inputs (`production_subsidy_distortion` vs `export_restriction_exposure`). Whether those weightings are calibrated correctly is a partner-product-direction question.
- **Should the Events column on Country Scores exclude subsidy events?** Different question — even if subsidies should feed scoring, an analyst counting "events about graphite in Brazil" might want them surfaced separately.
- **Step 2 (pillar sub-input derivation audit)** remains the more important open ticket. If sub-inputs are dominated by the material-wide event floor rather than the per-country event set, country differentiation in the Geopolitical pillar is weaker than it looks.
