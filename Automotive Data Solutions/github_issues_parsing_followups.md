# GitHub Issues — Parsing & Attribution Follow-Ups

Eight tickets. Copy each `---`-delimited block into its own GitHub issue. The first one is the epic; the remaining seven are the children. Update the epic's checklist with the actual issue numbers once you create them.

The priority ranking and "Why this matters" framing reflect the audit done 2026-05-09. Adjust before posting if your partner's priorities differ.

---

## EPIC: Parsing & attribution follow-ups (post-2026-05-09 audit)

**Labels:** `epic`, `area/ingestion`, `area/scoring`

### Context

The 2026-05-09 audit cycle closed eight parsing / attribution issues (Tier 1.1–1.5, Tier 3 Haiku for SEC EDGAR, GTA confidence threading, EUR-Lex material junctions). See `docs/scoring-audit-2026-05-addendum.md` for the closed items.

These seven tickets capture what remained outstanding after that audit. They are independent — none blocks any other — and can be picked up in any order. The recommended order based on impact + effort is:

1. Update broken `tests/test_comtrade.py` (5 min, low risk)
2. Backfill historical `TradeFlow.hs_mapping_id` (low effort, closes a real coverage gap)
3. Decide N5 — 10-digit HTS coverage strategy (design decision, then implement)
4. Decide on mutable-title content_hash for `trade_signal_builder` / OpenSanctions geo events (design decision)
5. Wire real news ingester (only if a provider is selected; otherwise stalled)
6. GEM Iron Ore Mines ingester for LFP coverage
7. Recalibrate GTA NFI/IFI multipliers
8. Dynamic HCG (high-concentration geos) derivation

### Checklist

- [ ] #TBD — Update `tests/test_comtrade.py` to match `_resolve_material_id` 3-tuple return
- [ ] #TBD — Backfill historical `TradeFlow.hs_mapping_id` for pre-Phase-1.5 rows
- [ ] #TBD — Decide 10-digit HTS keyword coverage strategy (N5)
- [ ] #TBD — Decide on parameter-stable titles for trade_signal_builder + OpenSanctions geo events
- [ ] #TBD — Build real news ingester (replace `StubNewsProvider`)
- [ ] #TBD — Build GEM Iron Ore Mines ingester (G4b)
- [ ] #TBD — Recalibrate GTA NFI/IFI implementation_level multipliers
- [ ] #TBD — Replace hardcoded HCG list with dynamic derivation

### Out of scope for this epic

- G4c partner-curated facility data — engineering done; waiting on partner input
- G4d paid subscription evaluation — business decision
- USITC HTS structural tariff baseline — separate ingester epic
- WGI structural country governance baseline — separate ingester epic
- US-dependency tier scoring — deferred per partner consultation
- G10 Inngest dependency chain — watch list, no scoping yet

---

## Update `tests/test_comtrade.py` to match `_resolve_material_id` 3-tuple return

**Labels:** `area/tests`, `priority/low`, `good-first-issue`

### Problem

`tests/test_comtrade.py` predates the Phase 1.5 HS-mapping refactor. It expects `_resolve_material_id` to return a single `int` and uses a `dict[str, int]` mock map, but the actual signature has changed twice since:

- Phase 1.5: now returns `tuple[Optional[int], Optional[int]]` and takes `dict[str, list[tuple[int, float, int]]]`
- 2026-05-09: extended to `tuple[Optional[int], Optional[int], Optional[float]]` (confidence threading)

These tests would have been failing for some time; nobody appears to have noticed.

### What to do

Update the test cases in `tests/test_comtrade.py` (currently around lines 176–192) so:

1. The mock map matches the real shape: `dict[str, list[tuple[int, float, int]]]` (material_id, confidence, hs_mapping_id).
2. The assertions unpack the 3-tuple return: `(material_id, hs_mapping_id, confidence)`.
3. Cover at least: exact 6-digit match, 4-digit prefix fallback, unmapped code (returns all-None), multi-entry prefix with tie-breaking by confidence.

Reference for the new shape: `app/services/ingestion/comtrade.py` `_resolve_material_id` (around lines 477–530) and the synthetic test that does this correctly: `Automotive Data Solutions/test_confidence_and_eurlex_attribution.py` test group CT.1.

### Acceptance

- `pytest tests/test_comtrade.py::TestResolveMatertialId -v` passes.
- Tests cover the 3-tuple return shape including the confidence element.
- No production code changes.

---

## Backfill historical `TradeFlow.hs_mapping_id` for pre-Phase-1.5 rows

**Labels:** `area/ingestion`, `area/scoring`, `priority/medium`

### Problem

`TradeFlow.hs_mapping_id` was added in migration 027 (Phase 1.5). Rows ingested before that date have `material_id` set but `hs_mapping_id IS NULL`.

After the 2026-05-09 confidence-weighting changes, `trade_signal_builder._get_annual_totals` and `global_rollup._aggregate_trade_values_for_material` apply `COALESCE(HsCodeMaterialMapping.confidence, 1.0)` as a per-row weight on `TradeFlow.trade_value_usd`. This means historical rows fall through at confidence 1.0 — which is fine for backwards-compat but masks the very signal the confidence weighting is supposed to produce. A 4-digit broad-prefix row from 2021 will be treated identically to a 6-digit precise-prefix row from 2024.

### What to do

Write a one-shot backfill that, for every `TradeFlow` row where `hs_mapping_id IS NULL` and `material_id IS NOT NULL`:

1. Re-runs `_resolve_material_id(row.hs_code, hs_material_map)` (or the equivalent `MaterialResolver.resolve_by_hs_code` path) against the current `hs_code_material_mappings` table.
2. If the resolved `material_id` matches the row's existing `material_id`, update `hs_mapping_id` to the resolved value.
3. If the resolved `material_id` differs, leave the row untouched and log a warning — that's a data-quality signal (the prefix-to-material mapping shifted), not something the backfill should silently fix.

Suggested entry point: a new `bdi-ingest backfill-trade-flow-hs-mappings` CLI command alongside the other ingest commands in `app/cli.py`. Print before/after counts and a sample of mismatches.

### Acceptance

- New CLI command runs to completion against the dev DB.
- Reports counts: examined, updated, mismatched (left alone), missing-mapping-still.
- Re-running is a no-op (idempotent).
- Spot-check: pick 5 affected rows and verify `HsCodeMaterialMapping.confidence` is now reachable through the FK.

### Out of scope

Recomputing scoring after the backfill is the caller's problem — they should run `rescore-market` and `rescore-global-rollups` afterward.

---

## Decide 10-digit HTS keyword coverage strategy (N5)

**Labels:** `area/ingestion`, `priority/medium`, `needs-decision`

### Problem

`hs_code_material_mappings` supports 4 / 6 / 8 / 10-digit prefixes with `market_scope='global'|'us'|'eu'`. The current resolver (`MaterialResolver.resolve_by_hs_code` and the legacy `_resolve_material_id`) does longest-prefix match — 10 → 8 → 6 → 4 digits.

US HTS rows from the USGS MCS PDF parser carry 10-digit codes. These currently resolve via the 6-digit international fallback. That's acceptable when the 6-digit code maps unambiguously to one material, but it loses information for codes where multiple materials share the 6-digit but diverge at the 10-digit (e.g. specific product-form distinctions in cathode chemistry).

The audit (N5) flagged this as an open question with no committed direction: keep HS-only longest-prefix routing, or add 10-digit keyword/alias coverage?

### What to do — decision phase

Pull the actual US HTS rows from the dev DB and survey them against the launch-list materials. Specific questions:

1. How many 10-digit US HTS codes exist in `hs_code_material_mappings`?
2. Of those, how many resolve to a material that is *different* from what the 6-digit prefix would resolve to?
3. For those cases, is the 10-digit resolution actually more accurate, or is it spurious (the 6-digit was already correct)?

Decide explicitly: (a) keep HS-only longest-prefix routing — close ticket as "won't fix" — or (b) extend the resolver to also use keyword/alias data on the 10-digit rows.

### What to do — implementation phase (if option b)

Wire the resolver to consult `HsCodeMaterialMapping.keywords` (already a column, added migration 026) when the 10-digit prefix has multiple candidates. Match keyword against the event/filing text using the same word-bounded regex pattern as `MaterialCache`.

### Acceptance

- Decision documented in `docs/scoring-audit-2026-05-addendum.md` under N5 with the survey results.
- If option (b): resolver covers 10-digit HTS prefixes with keyword disambiguation, regression-tested against current behaviour for 6-digit and 4-digit cases.

---

## Decide on parameter-stable titles for `trade_signal_builder` + OpenSanctions geo events

**Labels:** `area/ingestion`, `priority/medium`, `needs-decision`

### Problem

Two ingesters compute `content_hash` from titles that encode mutable values:

- `trade_signal_builder._insert_event` titles: `"{country} accounts for {share:.0%} of tracked {mat_name} exports ({year})"`. The percentage is now affected by the 2026-05-09 confidence-weighting change.
- `opensanctions._ingest_country_geo_event` titles: `"{country} — {count} Sanctioned Entities (OpenSanctions)"`. The count dropped sharply after the 2026-05-09 Tier 1.2 topic filter.

For both ingesters, a re-run against the same underlying source data produces a different `content_hash` than the previously-ingested row — so re-running on top of existing data creates duplicates rather than skipping. The current operational workflow is "use `bdi-ingest reset-events --yes` before any logic change re-ingest" which works but is brittle.

This is a design choice, not a bug. The title is the human-readable identifier and arguably *should* change when the underlying value changes. But it does mean these two ingesters have weaker idempotency than the others.

### What to do — decision phase

Decide which behaviour you want:

- **Option A (status quo):** keep human-readable titles with embedded values. Idempotency means "same source data → same hash". Logic changes require `reset-events` to re-ingest cleanly. Document that this is the contract.
- **Option B (parameter-stable hash):** keep human-readable titles for display, but compute `content_hash` from a stable identifier (e.g., `f"{country}|{material_id}|{year}|trade_concentration"`). Re-runs after logic changes update the same row's title/severity rather than creating a new one.

Option B is more forgiving but means scoring sees the *current* values, never a historical trace. That's actually what you want for these signals (a trade-concentration event is a state, not a point event), so Option B is probably correct — but worth checking against how the scoring pillars consume these events.

### What to do — implementation phase (if option B)

For `trade_signal_builder._content_hash`: switch to `sha256(f"{material_id}|{country}|{year}|{event_subtype}")`. On match, UPDATE the existing row's title/severity/summary instead of skipping.

For OpenSanctions `_ingest_country_geo_event`: switch to `sha256(f"opensanctions_geo|{country}")`. Same UPDATE pattern.

### Acceptance

- Decision documented in `docs/scoring-audit-2026-05-addendum.md`.
- If option B: re-running an ingester after a logic change updates rows in place; verify via a synthetic test (re-run twice with different mock inputs, confirm 1 event with the latest title, not 2).

---

## Build real news ingester (replace `StubNewsProvider`)

**Labels:** `area/ingestion`, `priority/medium`, `blocked-on-product-decision`

### Problem

`app/services/ingestion/adapters/news.py` ships with `StubNewsProvider`, which produces nothing. The pipeline routes news through `IngestionPipeline._ingest_news_items` (line 399 of `pipeline.py`), which now has working dedup (2026-05-09 fix) but no real input.

Until a real news source is wired, the news path contributes zero events to scoring. The audit doc has flagged this as zero-impact-today.

### What to do — decision phase

Pick a provider. Options to evaluate:

- **GDELT 2.0** — free, broad coverage, noisy, requires aggressive filtering. Material attribution would lean heavily on the keyword + Haiku hybrid pattern landed for SEC EDGAR.
- **NewsAPI / Newsdata.io** — paid, cleaner, more focused. Per-call cost.
- **A purpose-built RSS aggregator** — curate ~20 industry feeds (Reuters Metals, Mining.com, S&P Platts, etc.). Highest signal, hardest to maintain.
- **Continue with stub** — accept zero news coverage at launch. Scoring works fine without it (all five pillars have other inputs).

The Tier 3 audit recommended Haiku attribution for any free-text source. For GDELT specifically, the keyword pre-filter from `MaterialCache` becomes essential — GDELT volume is too high for Haiku-on-everything.

### What to do — implementation phase (after provider selected)

1. Replace `StubNewsProvider` with the chosen adapter. Honor the existing `NewsAdapter` interface in `app/services/ingestion/adapters/news.py`.
2. Run `MaterialCache.detect` over `article.title + article.summary` per the Tier 1.3 cap (top-3 materials, drops >5).
3. When `ANTHROPIC_API_KEY` is set, run results through `MaterialClassifier.classify` per the Tier 3 pattern (see `app/services/ingestion/ingest_sec_edgar.py` around lines 382–460 for the reference implementation).
4. Persist via `pipeline._add_risk_event` — dedup is now handled there.

### Acceptance

- Decision documented in `docs/scoring-audit-2026-05-addendum.md`.
- If implemented: end-to-end synthetic test (mock provider response → events written → material junctions written → idempotent re-run).
- Cost ceiling documented (if paid provider).

---

## Build GEM Iron Ore Mines ingester (G4b)

**Labels:** `area/ingestion`, `area/coverage`, `priority/low-medium`

### Problem

LFP cathode chemistry depends on iron ore. The current facility coverage for iron ore is gappy because MRDS (USGS Minerals Resource Data System) under-represents active iron ore mines globally. GEM (Global Energy Monitor) publishes a curated dataset specifically for iron ore mining operations.

This was logged as G4b in the original audit and remains open. Bounded scope — LFP/iron-ore only.

### What to do

1. Investigate GEM's data publication format (likely CSV/JSON; check `https://globalenergymonitor.org/projects/global-iron-ore-mine-tracker/`).
2. Build a parser that maps GEM rows to `Facility` + `FacilityMaterialLink` rows with:
   - `material_id` → Iron Ore (LFP Grade)
   - `supply_chain_stage` → `ore`
   - `annual_capacity_tpy` if available
   - Country, region, operator
3. Wire idempotency via `(source_id, external_id)` upsert on `Facility`.
4. Register via a new CLI command: `bdi-ingest ingest-gem-iron-ore`.

The `seed_facilities_partner.py` loader and its template can serve as a reference for the persistence shape — same target tables.

### Acceptance

- New ingester module under `app/services/ingestion/` with synthetic test.
- CLI command registered.
- Re-running the ingester is idempotent.
- After ingest, `Iron Ore (LFP Grade)` has non-trivial facility coverage in the dev DB (sanity check the row count).

---

## Recalibrate GTA `NFI` / `IFI` implementation_level multipliers

**Labels:** `area/ingestion`, `area/scoring`, `priority/low`

### Problem

GTA's `implementation_level` field has 4 documented values: `National`, `Subnational`, `Supranational`, plus two financial-institution codes — `NFI` (National Financial Institution: EXIM banks, state development banks) and `IFI` (International Financial Institution: EIB, World Bank, EBRD, ADB).

`gta.py` currently maps NFI and IFI to a 1.0× severity multiplier — i.e. treats them as equivalent to National. This was a deliberate placeholder set 2026-05-06: at the time these codes were attached to subsidy-class interventions that hit `event_subtype=NULL` and skipped HS-node sub-scores entirely, so the multiplier didn't matter.

The 2026-05-09 G-Cov-3 work landed `EXPORT_SUBSIDY` subtype and a 4-component Geopolitical profile that includes `production_subsidy_distortion`. Now NFI/IFI events DO feed scoring — through the subsidy sub-input, not the tariff/export ones — and the 1.0× multiplier is no longer a no-op.

### What to do

1. Survey actual GTA NFI/IFI events in the dev DB: count, severity distribution, country distribution, material attribution.
2. Decide multipliers based on impact magnitude:
   - NFI (one national EXIM bank) — probably 0.7–0.9× of a national tariff
   - IFI (multilateral institution) — probably 1.0–1.2× because the policy carries broader reach
3. Update `_apply_severity_modifiers` in `gta.py` and/or the implementation-level branch.
4. Run a before/after on subsidy_distortion values for the launch-10 materials.

### Acceptance

- Updated multipliers committed with calibration notes in code comments.
- Documented in `docs/scoring-audit-2026-05-addendum.md` (the "Decode GTA NFI/IFI" line item).
- A synthetic test covers the new multiplier values.

---

## Replace hardcoded high-concentration-geos list with dynamic derivation

**Labels:** `area/scoring`, `priority/low`, `needs-decision`

### Problem

`app/services/scoring/evidence_query.py:81` defines:

```python
_HIGH_CONCENTRATION_GEOS = frozenset({"CN", "CD", "RU"})
```

This frozenset feeds several evidence queries — e.g., flagging supply chain exposure when a material has production concentrated in these countries.

There is already a `supply_chain_contexts.high_concentration_geos` column intended to hold per-material HCG lists derived from production share data, but nothing populates or reads it. The hardcoded frozenset short-circuits the design.

### What to do — decision phase

Decide which materials should have HCG and at what threshold. Options:

- **Per-material, top-N producers** — for each material, the top 3 producers from `MaterialProductionShare` are its HCG. Dynamic; reflects actual concentration.
- **Per-material, share-threshold** — for each material, any country with >25% production share is HCG. Asymmetric (some materials have many, some have none).
- **Keep global list** — frozenset stays, document it as a deliberate "always-watch" list rather than a concentration measure.

The global list is currently doing two things at once: flagging supply-chain countries-of-concern (CN, CD, RU regardless of material) AND substituting for per-material concentration data. Splitting those two semantics would clarify the scoring.

### What to do — implementation phase

After decision: build a derivation pass that populates `supply_chain_contexts.high_concentration_geos` from `MaterialProductionShare`. Update `evidence_query.py` to read from the column instead of the frozenset.

### Acceptance

- Decision documented in `docs/scoring-audit-2026-05-addendum.md`.
- `supply_chain_contexts.high_concentration_geos` populated for all launch-list materials.
- `_HIGH_CONCENTRATION_GEOS` frozenset deleted (or repurposed with a different name if you want to keep a separate "always-watch" list).
- Regression-tested: scoring outputs for launch-list materials before/after the change are comparable, not radically different.
