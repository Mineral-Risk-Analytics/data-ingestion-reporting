# GitHub Issues — Scoring Coverage Extensions (post-2026-05-09 audit)

Four tickets for outstanding items from `docs/scoring-audit-2026-05.md` and `docs/scoring-audit-2026-05-addendum.md` that were not included in the earlier parsing-followups epic. These are structurally larger than the parsing items — new data sources or scoring framework additions, not bug fixes or attribution tweaks.

Copy each `---`-delimited block into its own GitHub issue. Update the epic checklist with actual issue numbers once you create them.

**Correction to my earlier count:** I said "five items not in the issues file" — actually it's four. GEM Iron Ore Mines (G4b) IS in the parsing-followups epic as ticket #6. The four genuinely-not-yet-ticketed items are below.

**Distinct from the parsing followups epic:** Those tickets fix attribution and parser logic against existing data. These tickets add NEW data sources and structural baselines that the scoring engine currently does not consume.

---

## EPIC: Scoring framework coverage extensions

**Labels:** `epic`, `area/scoring`, `area/ingestion`

### Context

The 2026-05-09 scoring audit closed every structural item it identified (G1–G12, N1–N5, G-Cov-1 through G-Cov-3). The scoring math is correct where it runs, and every existing ingester correctly feeds attribution. What's outstanding now is not bugs — it's coverage breadth and one design decision parked with the partner.

These four tickets cover the non-trivial work that remains. They are independent and can be picked up in any order, but two carry dependencies worth knowing:

- **#TBD G-Cov-4** is gated on G4c partner-curated facility data landing first. Don't start until your partner has populated the launch-list materials in `facility_seed_template.xlsx`.
- **#TBD US-dependency tier scoring** is gated on a product-direction decision with your partner before any code lands.

### Checklist

- [ ] #TBD — Build USITC HTS structural tariff baseline ingester
- [ ] #TBD — Build WGI country governance baseline ingester
- [ ] #TBD — US-dependency tier scoring (decision first, then implementation)
- [ ] #TBD — G-Cov-4 event-driven operational signals (gated on G4c data)

### Related

These four are tracked alongside the parsing-followups epic. Both epics together represent the complete outstanding scope from `docs/scoring-audit-2026-05-addendum.md`. Items deliberately deferred or marked watch-list (G10 Inngest dependency chain, G12 cache refresh path verification, G4d paid subscription) are not converted to tickets — see the addendum's P2/P3 sections for context.

### Out of scope for this epic

- G4c partner facility data — waiting on partner content, engineering complete
- G4d paid subscription (Benchmark / S&P / Wood Mackenzie) — business decision
- G10 Inngest dependency chain — watch list, no race observed
- G12 materials.criticality_score cache refresh — verified intact

---

## Build USITC HTS structural tariff baseline ingester

**Labels:** `area/ingestion`, `area/scoring`, `priority/medium`, `effort/large`

### Problem

`hs_node_scorer.tariff_exposure` defaults to 0 unless a RiskEvent (from GTA / Federal Register) adds a tariff. That makes the sub-score a pure "did a tariff event happen recently" signal rather than "what's the baseline structural tariff burden on importing this material from this country."

Two countries with identical event histories but very different MFN rates currently score the same on `tariff_exposure`. That under-represents structural sourcing cost differences. The audit's P2 backlog entry: "**USITC HTS structural tariff baseline** so tariff_exposure isn't zero by default."

USITC publishes the full HTS schedule with all applicable rates (MFN, column 2, special programs like USMCA, AGOA, GSP) at https://hts.usitc.gov/ — bulk download as JSON or CSV. Updates several times per year.

### What to do — data layer

1. Add a new table `hs_tariff_rates` (or extend `hs_code_material_mappings` if cleaner — schema decision below):
   - `hs_code` (10-digit HTS)
   - `partner_country` (ISO2) — for column 1 (MFN), use NULL or 'MFN'; for special programs, use the partner country eligible
   - `rate_type` (mfn | column_2 | special_program)
   - `rate_pct` (numeric, e.g. 2.5 for 2.5% ad valorem)
   - `effective_date`, `expiry_date`
2. Build the parser — USITC publishes structured tariff data; the format is well-documented but parsing the special-program eligibility logic is the non-trivial part.
3. Write `app/services/ingestion/ingest_usitc.py` with `ingest_usitc_hts()` function and a `bdi-ingest ingest-usitc-hts` CLI command. Idempotent on `(hs_code, partner_country, rate_type, effective_date)`.

### What to do — scoring integration

4. Extend `hs_node_scorer.tariff_exposure` to incorporate the structural baseline. Possible shapes:
   - **Additive**: `tariff_exposure = structural_baseline_pct/100 + sum(event_impacts)`, capped at 1.0
   - **Layered**: structural baseline is the floor; events push it higher but never lower
   - **Country-pair vs node**: structural rates are country-of-origin → US importer; nodes are (hs_mapping × producer country) — need to think about how the "importer country" dimension folds in
5. Update `score_method` enum to surface when a node's score includes the structural baseline.

### Decision phase before implementation

The schema question (extend `hs_code_material_mappings` vs new table) and the integration question (additive vs layered, importer dimension) both need a deliberate call before code lands. Worth a design doc round before opening the implementation PR.

### Acceptance

- New ingester module + CLI command, synthetic test passes.
- USITC HTS data populated for all 10-digit codes used by partner-curated mappings.
- `hs_node_scorer.tariff_exposure` reflects structural baseline + event-driven changes; `rationale_json` shows both components.
- Documentation in `docs/scoring-audit-2026-05-addendum.md` describing the formula update.

### Effort

Large — 4-6 days. The bulk is the parser (USITC special programs are gnarly) and the design conversation about how the structural baseline composes with event-driven tariff signals.

---

## Build WGI country governance baseline ingester

**Labels:** `area/ingestion`, `area/scoring`, `priority/medium`, `effort/medium`

### Problem

The Geopolitical pillar's `country_concentration` sub-input measures supply concentration (which countries produce the material) but does not directly measure governance risk in those countries. The audit's P2 backlog entry: "**WGI / structural country governance baseline.**"

World Bank's Worldwide Governance Indicators (https://www.worldbank.org/en/publication/worldwide-governance-indicators) publish six annual per-country dimensions:
- Voice & accountability
- Political stability / absence of violence
- Government effectiveness
- Regulatory quality
- Rule of law
- Control of corruption

Each scored on roughly -2.5 to +2.5. Annual cadence, ~215 countries covered, freely downloadable.

This is structurally different from event-driven signals: it's a stable per-country risk floor that doesn't disappear when there's no recent news.

### What to do — data layer

1. Add table `country_governance_scores`:
   - `country_code` (ISO2)
   - `dimension` (one of the six)
   - `score_raw` (numeric, -2.5 to +2.5)
   - `score_normalized` (0–1, where 0 is best, 1 is worst — easier to compose with other sub-scores)
   - `reference_year`
2. Parser for WGI CSV / Excel. Single annual download; idempotent on `(country_code, dimension, reference_year)`.
3. Write `app/services/ingestion/ingest_wgi.py` with CLI command `bdi-ingest ingest-wgi`.

### What to do — scoring integration

4. Add a `governance_risk` sub-input to `_derive_market_geopolitical_inputs` in `market_aggregator.py`. Aggregation choice:
   - **Composite of all six dimensions** — simple average or weighted average
   - **Subset** — focus on rule_of_law + political_stability for trade-related risk
   - **Selective** — different dimensions for different pillars (regulatory_quality could also feed Regulatory pillar)
5. Decide the integration weight. Current Geopolitical 4-component profile is `0.40 country_concentration + 0.30 export + 0.20 tariff + 0.10 subsidy`. Adding governance as a 5th element means rebalancing. Honest reweighting probably puts governance at 0.10–0.15 since it's a structural floor not a current signal.

### Decision phase

Same shape as USITC — schema and integration are straightforward; the design call is which dimensions feed which pillars and how the weights rebalance. Worth a design doc round.

### Acceptance

- WGI table populated for current year, all six dimensions, ~215 countries.
- `governance_risk` sub-input added to Geopolitical (and optionally Regulatory) pillar.
- Backwards-compatible: existing scores don't change for materials whose producers all have similar WGI scores. Score deltas only appear when governance differs meaningfully between sourcing countries.
- Synthetic test covering weight rebalancing.

### Effort

Medium — 2-3 days. Smaller than USITC because the parser is simpler and the integration is a single new sub-input rather than a structural change to the scoring formula.

---

## US-dependency tier scoring

**Labels:** `area/scoring`, `priority/medium`, `needs-decision`, `blocked-on-partner`

### Problem

Three fields on `materials` are populated by `ingest-usgs` but never read by the scoring engine:
- `us_net_import_reliance_pct` — % of US apparent consumption sourced from imports
- `us_apparent_consumption` — annual US consumption in tonnes
- `us_import_sources` — JSON breakdown of source countries

This is real data sitting in the table doing nothing. The audit deferred this work 2026-05-04 per partner consultation on US-only vs global risk framing.

### Why this is "deferred per partner" not just "open"

The framing question is product-level, not technical. Two valid models:

- **Global risk** (current) — score every material × geography pair symmetrically; user filters/sorts by their geography of interest. Treats US the same as DE the same as JP.
- **US-tier** — explicitly elevate US-import-reliance as a primary signal. Materials with high US net import reliance get higher risk scores by default, regardless of global supply concentration.

The product implication is whether the dashboard is positioned as "global supply chain risk intelligence" (current) or "US-supply-chain risk intelligence with global context" (US-tier). Both are defensible. The choice affects branding, customer targeting, and which materials get flagged as priority.

### What to do — decision phase (BLOCKING)

Confirm with your partner which framing the v1 product targets. Document the decision in `docs/scoring-audit-2026-05-addendum.md` so the deferral status either resolves to "won't fix — global framing confirmed" or "build it — US-tier framing confirmed."

### What to do — implementation phase (only if US-tier framing wins)

1. Add a `us_dependency_score` field to `MaterialGeographyRiskScore` or surface it on `MaterialGlobalRiskScore`.
2. Compute as a weighted blend of NIR + import-source-HHI (concentration among import sources, not global production sources).
3. Decide whether `us_dependency_score` is a separate pillar, a sub-input to the existing Material Concentration pillar, or a presentation-layer overlay on the global composite.
4. Update the rationale_json and API responses to surface the new dimension.

### Acceptance

- Decision documented.
- If implemented: `us_dependency_score` populated and surfaced in scoring outputs; rationale explains how it composes with the existing global view.

### Effort

Decision: low effort, just needs the conversation. Implementation: medium — 2-4 days depending on how deeply it's integrated (separate pillar vs overlay).

---

## G-Cov-4 — event-driven operational signals

**Labels:** `area/ingestion`, `area/scoring`, `priority/medium`, `effort/large`, `blocked-on-G4c-data`

### Problem

The Operational pillar formula today is `0.40 × structural_dependency + 0.60 × weighted_event_impacts`. The `weighted_event_impacts` term feeds from GTA EXPORT_RESTRICTION events (added 2026-05-06 via G-Cov-2). Three more sources can produce operational signals but currently don't:

- **Federal Register** — permit denials, EPA enforcement, ROD vacated, consent decree shutdowns
- **OpenSanctions** — sanctions designations against companies tagged with `supply_chain_stage` in (mine / refiner / processor)
- **SEC EDGAR** — 10-K Item 1A risk-factor mentions of "ceased operations", "indefinitely suspended", "force majeure declaration", "production halted"
- **News** (when a real provider replaces StubNewsProvider) — similar text patterns

This is `G-Cov-4` from `docs/coverage-gap-plan-2026-05.md`. The plan bundles it with `G-Cov-3` (EXPORT_SUBSIDY subtype, since landed 2026-05-09). G-Cov-3 closed independently; G-Cov-4 is still open.

### Why this is blocked on G4c

Per the coverage gap plan: today, `structural_dependency` is mining-heavy because MRDS coverage is mining-heavy. Adding event-driven operational signals NOW would over-weight them against a thin structural baseline. After G4c partner-curated facility data lands (refining-stage facilities populated), `structural_dependency` becomes a stable baseline and event signals layer on top sensibly.

**Do not start this ticket until G4c partner data is in the dev DB for the launch-list materials.** Specifically, every launch-list material needs at least one refining-stage facility row with `annual_capacity_tpy` populated. Phosphate currently has zero facilities of any stage — that's a separate G4c blocker.

### What to do — when unblocked

1. Add `OPERATIONAL_DISRUPTION` event_subtype to the `event_subtype` enum (migration).
2. Per-source detectors:
   - **Federal Register**: regex + (optionally) Haiku for the four patterns above. Tag matched events with `event_subtype='OPERATIONAL_DISRUPTION'` AND keep the existing subtype (e.g. EXPORT_RESTRICTION) — same RiskEvent feeds multiple pillars via `risk_categories_json`.
   - **OpenSanctions**: structural detection. For each sanctioned company, look up `company_material_exposures`. If the company has a `source_geography` and a `supply_chain_stage` in the operational-relevant set, emit OPERATIONAL_DISRUPTION on the material × geography pair.
   - **SEC EDGAR**: text classification of 10-K Item 1A using the existing `MaterialClassifier` + new operational-disruption tool schema. The Tier 3 Haiku pattern from `ingest_sec_edgar.py` already handles this shape.
3. Add `OPERATIONAL_DISRUPTION` to the Operational pillar's event filter in `market_aggregator.py`. Full weight (1.0×) — these are operational-specific, not double-counted in Geopolitical / Regulatory.
4. Run `reingest-all-events` (the orchestrator I built earlier) to re-process all affected sources with the new detectors.

### Acceptance

- New event_subtype landed via migration.
- Three detector code paths landed in their respective ingesters, with synthetic tests for each.
- Operational pillar event-impact term now firing on real (non-GTA) signals against the launch-list materials.
- `rationale_json.operational.event_breakdown` shows the per-source contribution.

### Effort

Large — 3-5 days, mostly text-classification work. Each source has its own pattern and its own ingester to update. Bundle with the next news-provider integration if it lands first (the news detector inherits the same patterns).

### Dependencies

- **Blocks on G4c partner facility data** — see above.
- **Benefits from real news ingester** — once the news provider ticket lands (in the parsing-followups epic), extend the same detector patterns to news.
- **Migration coordination** — adds a new enum value; coordinate with any in-flight migrations.
