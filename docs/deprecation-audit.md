# Deprecation Audit — Post Foundation Phases 1–4

**Last updated:** 2026-04-30
**Scope:** Post-Phase-4 audit of code that was used by older scoring methodologies but is no longer reached by the live architecture.
**Out of scope (explicitly preserved):** company-specific scoring (`CompanyScore`, `orchestrator.rescore_company`, the six-pillar engine), company relationships (`CompanySupplyRelationship`, supplier chain traversal, propagation pillar), and the company overlay layer that Phase 5 will build on top of these.

This file is the running tracker for the cleanup. It captures **what** is dead, **why** it's dead, **how confident** we are, and the **proposed action** (delete vs. mark deprecated vs. leave). Rows graduate from `proposed` → `confirmed` → `removed` as we work through them.

---

## Architectural shifts that triggered this audit

Three architectural shifts make a chunk of older code unreachable:

1. **Market-level scoring layer** (Phase 2) — `MaterialGeographyRiskScore`, `MaterialGlobalRiskScore`, and the `chemistry_risk_scores` v2.0 rollup replaced the older `material_scores` / `geography_scores` rollup and the v1.0 chemistry path.
2. **Decoupling of company scoring from ingestion** (Phase 3) — the new contract is "ingestion writes events; scheduled Inngest jobs rescore." Auto-rescore-on-ingest is dead architecture.
3. **`LINK_EVENTS_TO_COMPANIES` feature flag** (Phase 3) — gates `risk_event_companies` writes during ingestion. The matching logic still runs (intentionally — Phase 5 reuses it), but most of its current side-effects in the live pipeline are no-ops.

---

## Legend

| Status | Meaning |
| --- | --- |
| `proposed` | Identified by this audit; needs reviewer sign-off before action |
| `confirmed` | Agreed for removal/deprecation; PR open or queued |
| `deprecated` | Marked deprecated in code (decorator, comment, or docstring) — still callable |
| `removed` | Deleted from the repo |

| Action | Meaning |
| --- | --- |
| `delete` | Hard-delete code + drop migration |
| `deprecate` | Add `# DEPRECATED:` header + `DeprecationWarning` (or docstring), keep callable |
| `keep-but-gate` | Keep code, ensure call site is gated by feature flag |
| `leave` | Code is intentionally retained for future re-enable |

---

## A — Models & migrations (highest-impact)

### A1. `MaterialScore` ORM model + `material_scores` table
- **Path:** `app/models/scoring.py:26-61`, baseline migration `alembic/versions/001_baseline.py:584-603`
- **Why dead:** Superseded by `MaterialGeographyRiskScore` (geo-anchored, Phase 2) and `MaterialGlobalRiskScore` (trade-weighted rollup, Phase 2). Grep shows **zero callers** outside the `app.models` re-export and the docstring in the same file.
- **Confidence:** High — only references are in `app/models/__init__.py` (re-export) and docs.
- **Status:** `removed` — ORM class deleted, `__init__.py` import/re-export removed, migration `021_drop_legacy_scores.py` added, `database_architecture.md` sections updated 2026-04-30.
- **Risk:** None known. Append-only table; if rows exist in prod, snapshot before dropping.

### A2. `GeographyScore` ORM model + `geography_scores` table
- **Path:** `app/models/scoring.py:239-269`, baseline migration `alembic/versions/001_baseline.py:606-622`
- **Why dead:** No reader, no writer, no test. Geography rollup is now derived on-demand from `material_geography_risk_scores` (one row per material × geography). Mentioned only in `Automotive Data Solutions/opensanctions_cursor_prompt.md` (a forward-looking design doc, not a live caller).
- **Confidence:** High.
- **Status:** `removed` — same migration as A1, same `__init__.py` cleanup 2026-04-30.
- **Risk:** None known.

---

## B — Scoring services

### B1. `score_chemistry()` v1.0 path (direct event/criticality scorer)
- **Path:** `app/services/scoring/chemistry_risk.py` — `score_chemistry`, `rescore_one_chemistry`, `rescore_all_chemistries`.
- **Replacement:** `score_chemistry_from_rollup`, `score_all_chemistries_from_rollup` (v2.0).
- **Status:** `removed` — step 1 (deprecation warnings, route/CLI migration) complete 2026-04-30. Step 2 (deletion) complete 2026-04-30:
  - `score_chemistry`, `rescore_one_chemistry`, `rescore_all_chemistries`, `PATENT_TREND_MODIFIERS`, `_HIGH_CONC_GEOS`, `_DEFAULT_GEO_CONCENTRATION`, `_SIGNAL_PRIORITY`, `METHODOLOGY_VERSION = "1.0"`, and all v1.0-only helper functions deleted.
  - Unused imports (`warnings`, `HsCodeMaterialMapping`, `TradeFlow`, `EventWithRelevance`, `get_events_for_material`, `score_material_exposure`, `RiskCategory`) removed.
  - `tests/test_chemistry_risk.py` v1.0 test classes removed; v2.0 coverage added.
  - `app/models/criticality_signal.py` docstring updated (`score_chemistry()` → `score_chemistry_from_rollup()`).
- **Risk:** v1.0-shaped rows already in `chemistry_risk_scores` are differentiated by `methodology_version` column — no action needed on existing data.

### B2. `IngestionPipeline.run()` post-ingest auto-rescore loop
- **Path:** `app/services/ingestion/pipeline.py:99-161`
- **Why dead:** Phase 3 explicitly decoupled scoring from ingestion ("ingestion writes events; scheduled jobs rescore"). The Inngest weekly cron (`scoring_jobs.py`) now owns rescoring.
- **Confidence:** High.
- **Status:** `removed` — deleted 2026-04-30. Removed `touched_company_ids` set and all six method parameters that threaded it through (`_ingest_federal_items`, `_ingest_census_items`, `_ingest_sec_items`, `_ingest_news_items`, `_add_risk_event`, `run`). Removed `import uuid`. Added explanatory comment at the commit point directing readers to `scoring_jobs.py`.
- **Test:** `tests/test_orchestrator.py:test_scoring_failure_does_not_block_ingestion` removed; replaced with a comment explaining the removal (check 3 section preserved as documentation note).

### B3. Module-level re-exports in `app/services/scoring/__init__.py`
- **Path:** `app/services/scoring/__init__.py:1-25`
- **Why partially dead:** Module docstring still references "v2 five-pillar framework" and "Removed in v2: macro_context_score". The current scoring engine is **v3.0 six-pillar** (per `app/services/scoring/orchestrator.py:14`). The re-exports themselves are still used (orchestrator imports them via the `__init__`).
- **Confidence:** Medium — the docstring is misleading but the re-exports are live.
- **Status:** `removed` (docstring-only) — updated to "v3.0 six-pillar framework" with pillar list 2026-04-30.
- **Risk:** None — pure documentation fix.

---

## C — Ingestion services & adapters

### C1. `_link_companies()` in `trade_signal_builder.py`
- **Path:** `app/services/ingestion/trade_signal_builder.py:166-219`
- **Why partially dead:** Already gated by `LINK_EVENTS_TO_COMPANIES` (returns 0 when flag is False). The function is **kept on purpose** — the function-level docstring and `feature_flags.py` both explicitly say so. Phase 5's company overlay will reuse the matching logic.
- **Confidence:** High — this is intentionally retained.
- **Status:** `keep-but-gate` (no action; flagged here for completeness so a future reader doesn't think this row was missed).

### C2. `persist_company_links()` in `entity_resolution.py`
- **Path:** `app/services/ingestion/entity_resolution.py:161-215`
- **Status:** Same as C1 — already gated, intentionally retained.
- **Action:** `keep-but-gate`.

### C3. `RiskEventCompany` ORM + `risk_event_companies` table
- **Path:** `app/models/regulatory.py` (RiskEventCompany), baseline migration.
- **Status:** `keep` — Phase 5 company overlay writes here.

---

## D — API routes

### D1. `app/api/routes/suppliers.py`
- **Path:** entire file (5 lines).
- **Why dead:** Already a stub with header `# DEPRECATED: this route module is superseded by app.api.routes.companies. It is no longer registered in main.py. Delete when tests are updated.`
- **Confidence:** High — the file says it itself.
- **Status:** `removed` — deleted 2026-04-30.
- **Risk:** None — main.py didn't register it; no external imports found.

### D2. `POST /companies/{company_id}/rescore`
- **Path:** `app/api/routes/companies.py:930-944`
- **Why considered:** Calls `rescore_company` directly, which is now scheduled by Inngest. However, **per user requirement, company-specific scoring stays.** A manual "rescore one company" admin button is still a valid product affordance (it's how analysts flag-and-rerun after correcting bad source data).
- **Status:** `keep` — kept on purpose.

### D3. `POST /chemistries/rescore` and `POST /chemistries/{id}/rescore`
- **Path:** `app/api/routes/chemistries.py:106-117, 208-220`
- **Status:** `removed` (v1.0 call site) — switched to v2.0 rollup 2026-04-30. Routes kept; no API contract change. Added explicit `db.commit()` to single-chemistry route.

---

## E — CLI commands

### E1. `bdi-ingest rescore-all`
- **Path:** `app/cli.py:1352-1489`
- **Status:** `keep` — same reasoning as D2 (manual company rescore is intentional).

### E2. `bdi-ingest rescore-chemistry`
- **Path:** `app/cli.py:283-341`
- **Status:** `removed` (v1.0 call site) — switched to v2.0 rollup 2026-04-30. CLI signature unchanged; output now includes `methodology_version`, `materials_scored`, `materials_missing`.

### E3. `bdi-ingest build-trade-signals`
- **Path:** `app/cli.py:1189-1246`
- **Status:** `keep` — `_link_companies` inside is gated; the rest of the trade-signal builder writes useful events. Active in the pipeline.

### E4. `pyproject.toml` hatch script typo: `igest-mrds`
- **Path:** `pyproject.toml:56`
- **Why:** Typo (`igest-mrds` instead of `ingest-mrds`). Not strictly a deprecation, but a dead alias that no one uses correctly.
- **Status:** `removed` — typo was already gone; `ingest-mrds` was the correct entry. No action taken.
- **Action:** n/a.

---

## F — Tests

### F1. `tests/test_orchestrator.py:248-262` — `touched_company_ids` assertion
- **Why:** Tied to B2 (the orphaned auto-rescore loop). When B2 is deleted, this assertion needs to either:
  - go away, or
  - move under an explicit `monkeypatch.setattr("app.services.ingestion.feature_flags.LINK_EVENTS_TO_COMPANIES", True)` (the same pattern `tests/test_entity_resolution.py` uses).
- **Status:** `proposed`
- **Action:** `update`-with-B2.

### F2. `tests/test_chemistry_risk.py` — v1.0 paths
- **Why:** Tests `score_chemistry`, `rescore_one_chemistry`, `rescore_all_chemistries` directly. Tied to B1.
- **Status:** `removed` — v1.0 test classes (`TestPatentTrendModifiers`, `TestResolveCriticalitySignal`, `TestGeoConcentration`, `TestScoreChemistry`) and all v1.0 helper fixtures deleted 2026-04-30. v2.0 tests added: `TestScoreChemistryFromRollup` with three cases including the "all materials missing global scores" error path.

---

## G — Documentation

### G1. `app/services/scoring/__init__.py` docstring
- See B3.

### G2. `docs/data-sources.md:55, :400, :490` and `docs/database_architecture.md:152, :795`
- **Why:** All five lines reference `score_chemistry()` (v1.0) by name. After B1 step 2 these references should point at `score_chemistry_from_rollup`.
- **Status:** `removed` — `database_architecture.md` lines updated 2026-04-30: `data_availability` column description now references `score_chemistry_from_rollup()`; "Source priority" note decoupled from function name; `app/models/criticality_signal.py` docstring updated.

### G3. `Automotive Data Solutions/opensanctions_cursor_prompt.md:8` — `GeographyScore`
- **Why:** Forward-looking design doc that names a model we're deleting (A2). Either rewrite the section against `MaterialGeographyRiskScore` or remove the reference.
- **Status:** `removed` — line updated 2026-04-30 to reference `RiskEventGeography` → `MaterialGeographyRiskScore.geopolitical_trade_score` pipeline.

---

## H — Out-of-scope and intentionally retained

The following are flagged here so a future reader doesn't think they were missed:

- **`CompanyScore`, `CompanySupplyRelationship`, `RiskEventCompany`** — kept (per user requirement).
- **Company-side `evidence_aggregator.py`, `propagation_risk.py`, `supplier_risk.py`, `orchestrator.py`** — kept.
- **`POST /companies/{id}/rescore`, `bdi-ingest rescore-all`** — kept (manual single-company rescore is intentional).
- **`feature_flags.LINK_EVENTS_TO_COMPANIES`** — kept (Phase 5 will flip it back to `True` for the company overlay).
- **`score_financial_pressure`, `score_geopolitical_trade`, `score_material_exposure`, `score_regulatory_profile`, `score_propagation`, `aggregate_supplier_risk`** — kept (pure-function pillar scorers used by both the company-anchored and market-anchored layers).

---

---

## I — Post-April-26 findings: country refactor + new seed architecture

*Added 2026-04-30. These items were not in scope for the original audit.*

### I1. `app/models/country_org.py` tombstone — ready to delete
- **Path:** entire file.
- **Why:** The file already says "REMOVED: Country and Organization models have been dropped." A new `app/models/country.py` (migration `020_countries`) was created with a different design.
- **Confidence:** High.
- **Status:** `removed` — deleted 2026-04-30.
- **Risk:** None.

### I2. `app/db/seed.py` — company seed rows superseded by `seed_companies.py`
- **Path:** `app/db/seed.py:20-44` (Tesla + Albemarle + 2 aliases).
- **Why:** `seed_companies.py` is now the authoritative company seed. `seed_if_empty` demo inserts created a stale Tesla stub after `seed-companies` ran.
- **Confidence:** High.
- **Status:** `removed` — deleted 2026-04-30. Removed `Company`/`CompanyAlias` imports and the entire `if session.scalar(select(Company)...) is None` block. Updated `"time": "2024-11"` → `"time": "latest"` in Census Trade config. Source rows retained.
- **Risk:** No tests called `seed_if_empty` for company assertions (confirmed by grep).

### I3. Phase 2/3 adapter stubs — organizational noise in `adapters/`
- **Path:** `app/services/ingestion/adapters/future/` (moved 2026-04-30).
- **Status:** `removed` from top-level — 6 stub files moved to `adapters/future/`. New `future/__init__.py` created with activation instructions. Parent `__init__.py` now imports from `adapters.future` with a Phase 1 / Phase 2-3 comment split in `ADAPTER_BY_TYPE`. No tests imported these directly (confirmed by grep).
- **Risk:** None — routing unchanged.

### I4. AI services layer — built but never connected
- **Path:** `app/services/ai/classify_event.py`, `cluster_duplicates.py`, `summarize_document.py`; `app/services/ingestion/document_embedder.py`; `app/services/ingestion/chunker.py`.
- **Status:** `leave` with comments — `# PHASE 2 — NOT YET WIRED` headers added to `document_embedder.py` and `chunker.py` 2026-04-30. No functional change.

### I5. `normalizers/supplier_resolver.py` — exported but never imported by pipeline
- **Path:** `app/services/ingestion/normalizers/supplier_resolver.py`.
- **Status:** `leave` with updated docstring — module docstring updated to "Reserved for Phase 5 company overlay — not called by the current pipeline." Class docstring updated to remove misleading "backward compatibility" language 2026-04-30.

### I6. `services/reports/` — all raise `NotImplementedError`
- **Path:** `app/services/reports/build_investor_report.py`, `build_oem_report.py`, `build_supplier_report.py`.
- **Status:** `leave` with comments — `# FUTURE: Phase 3 reporting` header added to all three files 2026-04-30.

### I7. `app/models/platform.py` — `Tenant`, `User`, `UsageEvent` — zero live callers
- **Path:** `app/models/platform.py` (3 classes: `Tenant`, `User`, `UsageEvent`).
- **Why:** Created in the baseline for SaaS multi-tenancy. The file's own docstring says "remain unused until the SaaS transition." No API routes, no queries, no route imports these models.
- **Confidence:** High — confirmed zero usage outside `models/__init__.py`.
- **Status:** `leave` (future SaaS transition). Well-documented in the file.
- **Action:** None. Tables exist in DB schema, keeping them is correct.

### I8. `schemas/note.py` — `CompanyNoteCreate` alias is dead
- **Path:** `app/schemas/note.py:55` — `CompanyNoteCreate = AnalystNoteCreate`.
- **Why:** Zero callers anywhere outside the alias line itself.
- **Confidence:** High.
- **Status:** `removed` — deleted 2026-04-30. One-line delete.
- **Risk:** None.

---

## J — Documentation staleness

### J1. `docs/database_architecture.md` — multiple stale sections
- **Lines 63, 516–532, 533–548:** References `material_scores` and `geography_scores` tables as live (described as "append-only", part of the scoring flow). Both are superseded by `MaterialGeographyRiskScore` / `MaterialGlobalRiskScore` (migrations 011 and 015).
- **Lines 717–718:** Diagram still shows `material_scores` and `geography_scores` as output tables.
- **Lines 795, 878:** Reference `score_chemistry()` (v1.0) and "append-only scores" including the legacy tables.
- **Status:** `removed` — all stale sections updated 2026-04-30: `material_scores`/`geography_scores` table entries deleted; diagram updated; "9. Scoring" layer updated; "Append-only scores" design decision note updated.

### J2. `app/services/scoring/__init__.py` docstring — "v2 five-pillar"
- See existing item B3. `removed` — see B3.

### J3. Deleted docs referenced in `docs/README.md`
- Early commits created `docs/ingestion-pipeline.md`, `docs/parsing-and-normalization.md`, and `docs/data-model-and-api.md` — these were likely removed or merged but should be confirmed absent and removed from any README index that still links them.
- **Status:** `verified` — `docs/README.md` links only the six current docs: `overview.md`, `database_architecture.md`, `data-sources.md`, `scoring.md`, `operations.md`, `reports-and-ai.md`. All six confirmed present. No ghost links.

---

## Suggested execution order (updated 2026-04-30)

> **Note on migration numbering:** Migrations 017–020 have been added since the original audit. The "drop legacy scores" migration referenced in PR 6 below should now be `021_drop_legacy_scores.py`, not `017_*.py`.

PRs are sequenced so each one is small and reverts cleanly:

1. **~~PR 1 — typo fix~~ (E4):** `removed` — typo was already gone. ✓
2. **~~PR 2 — quick deletes~~ (D1, I1, I8):** `removed` — `suppliers.py`, `country_org.py`, `CompanyNoteCreate` all deleted 2026-04-30. ✓
3. **~~PR 3 — strip company seeds~~ (I2):** `removed` — demo company inserts deleted, Census config updated to `"time": "latest"` 2026-04-30. ✓
4. **~~PR 4 — drop auto-rescore loop~~ (B2 + F1):** `removed` — `touched_company_ids` plumbing and rescore loop deleted from `pipeline.py`; test removed from `test_orchestrator.py` 2026-04-30. ✓
5. **~~PR 5 — switch chemistries API + CLI to v2.0 rollup~~ (B1 step 1, E2, D3):** `deprecated` — routes and CLI now call v2.0; DeprecationWarning added to v1.0 functions 2026-04-30. ✓
6. **~~PR 6 — add Phase 2/3 stub comments~~ (I4, I5, I6):** `leave` with comments — `PHASE 2 — NOT YET WIRED` headers added to `document_embedder.py`, `chunker.py`; `FUTURE: Phase 3 reporting` added to all three report builders; `supplier_resolver.py` docstring updated to remove misleading "backward compatibility" language 2026-04-30. ✓
7. **~~PR 7 — move Phase 2/3 adapter stubs~~ (I3):** `leave` in `adapters/future/` — 6 stub files moved 2026-04-30. `adapters/future/__init__.py` created with activation instructions. Parent `__init__.py` updated; `ADAPTER_BY_TYPE` routing unchanged. ✓
8. **~~PR 8 — clean up scoring/__init__.py docstring~~ (B3, J2):** `removed` — docstring updated to "v3.0 six-pillar framework" 2026-04-30. ✓
9. **~~PR 9 — drop legacy material_scores / geography_scores~~ (A1 + A2 + J1):** `removed` — migration `021_drop_legacy_scores.py` added, ORM classes deleted, `models/__init__.py` cleaned, `database_architecture.md` updated 2026-04-30. ✓
10. **~~PR 10 — delete v1.0 chemistry path~~ (B1 step 2 + F2 + G2):** `removed` — v1.0 functions and constants deleted from `chemistry_risk.py`, v1.0 test classes removed from `test_chemistry_risk.py`, v2.0 tests added, doc references updated 2026-04-30. ✓
11. **~~PR 11 — refresh design docs~~ (G3, J3):** `removed` — `opensanctions_cursor_prompt.md` updated; `docs/README.md` verified ghost-link-free 2026-04-30. ✓

Each PR should:
- Run the full test suite locally.
- Include a one-line entry in `docs/README.md`'s changelog.
- Update this file's row from `proposed` → `confirmed` → `removed`.
