# Deprecation Audit — Post Foundation Phases 1–4

**Last updated:** 2026-04-26
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
- **Status:** `proposed`
- **Action:** `delete` (drop ORM class, remove from `__init__.py`, add a new `017_drop_legacy_material_geography_score_tables.py` migration that drops `material_scores`).
- **Risk:** None known. Append-only table; if rows exist in prod, snapshot before dropping.

### A2. `GeographyScore` ORM model + `geography_scores` table
- **Path:** `app/models/scoring.py:239-269`, baseline migration `alembic/versions/001_baseline.py:606-622`
- **Why dead:** No reader, no writer, no test. Geography rollup is now derived on-demand from `material_geography_risk_scores` (one row per material × geography). Mentioned only in `Automotive Data Solutions/opensanctions_cursor_prompt.md` (a forward-looking design doc, not a live caller).
- **Confidence:** High.
- **Status:** `proposed`
- **Action:** `delete` (same migration as A1).
- **Risk:** None known.

---

## B — Scoring services

### B1. `score_chemistry()` v1.0 path (direct event/criticality scorer)
- **Path:** `app/services/scoring/chemistry_risk.py:238-433` (`score_chemistry`), `:436-444` (`rescore_one_chemistry`), `:447-478` (`rescore_all_chemistries`).
- **Replacement:** v2.0 path on the same file, lines 489–747 — `score_chemistry_from_rollup`, `score_all_chemistries_from_rollup`. Reads `MaterialGlobalRiskScore` and intensity-weights all five pillars (vs. the v1.0 single-composite output).
- **Why partially dead:**
  - The Inngest weekly job (`app/tasks/scoring_jobs.py:165-179`) calls **v2.0** (`score_all_chemistries_from_rollup`).
  - The frontend-facing API (`POST /chemistries/rescore`, `POST /chemistries/{id}/rescore` in `app/api/routes/chemistries.py:106-117, 208-220`) and the `bdi-ingest rescore-chemistry` CLI (`app/cli.py:283-341`) **still call v1.0**.
  - Net effect: prod scheduled scoring uses v2.0, but every manual rescore still runs v1.0 — silently writing rows with `methodology_version="1.0"` that are inconsistent with the rest of `chemistry_risk_scores`.
- **Confidence:** High that v1.0 should be retired; the in-file docstring already says so ("preserved for backward compatibility and can be retired once all chemistries have global rollup scores available").
- **Status:** `proposed`
- **Action:** Two-step:
  1. **Now (`deprecate`)** — Switch `app/api/routes/chemistries.py` and `app/cli.py:rescore-chemistry` to call `score_all_chemistries_from_rollup` / `score_chemistry_from_rollup`. Add a `DeprecationWarning` in `score_chemistry`/`rescore_one_chemistry`/`rescore_all_chemistries`.
  2. **Next release (`delete`)** — Remove `score_chemistry`, `rescore_one_chemistry`, `rescore_all_chemistries`, the v1.0-only constants (`PATENT_TREND_MODIFIERS`, `_HIGH_CONC_GEOS`, `_DEFAULT_GEO_CONCENTRATION`, `METHODOLOGY_VERSION = "1.0"`), and the v1.0 fixture-style tests in `tests/test_chemistry_risk.py`.
- **Risk:** v1.0-shaped rows already in `chemistry_risk_scores` for environments where the cron hasn't yet replaced them. Acceptable — append-only table with `methodology_version` column already differentiates rows.

### B2. `IngestionPipeline.run()` post-ingest auto-rescore loop
- **Path:** `app/services/ingestion/pipeline.py:99-161`
- **Why dead:** Phase 3 explicitly decoupled scoring from ingestion ("ingestion writes events; scheduled jobs rescore"). The Inngest weekly cron (`scoring_jobs.py`) now owns rescoring. Despite that, `IngestionPipeline.run()` still:
  1. Builds a `touched_company_ids: set[uuid.UUID]` from in-memory `matches` (line 522).
  2. Iterates every touched company and calls `rescore_company(self._db, company_id, run_id=str(run.id))` (line 153).
- **Confidence:** High — the design contract is explicit (see `docs/scoring.md`, `docs/ingestion-pipeline.md`). The auto-rescore is a left-over from the pre-Phase-3 architecture.
- **Status:** `proposed`
- **Action:** `delete` the `from app.services.scoring.orchestrator import rescore_company` block (lines 146-161) **and** the `touched_company_ids` plumbing (the parameter is threaded through `_ingest_federal_items`, `_ingest_census_items`, `_ingest_sec_items`, `_ingest_news_items`, `_add_risk_event` — all six signatures get smaller).
  - Update the comment that says "Cache all companies once per run to avoid N+1 queries during entity resolution. Accumulate company IDs (UUID) touched by new risk events for post-run rescoring." — drop the second sentence.
- **Risk:** Test `tests/test_orchestrator.py:248` asserts the loop semantics (`for cid in touched_company_ids`). Update the test to either drop the assertion or move it under a `LINK_EVENTS_TO_COMPANIES` monkeypatch.

### B3. Module-level re-exports in `app/services/scoring/__init__.py`
- **Path:** `app/services/scoring/__init__.py:1-25`
- **Why partially dead:** Module docstring still references "v2 five-pillar framework" and "Removed in v2: macro_context_score". The current scoring engine is **v3.0 six-pillar** (per `app/services/scoring/orchestrator.py:14`). The re-exports themselves are still used (orchestrator imports them via the `__init__`).
- **Confidence:** Medium — the docstring is misleading but the re-exports are live.
- **Status:** `proposed`
- **Action:** `deprecate` (docstring-only). Update the module docstring to "v3.0 six-pillar framework — material, geopolitical, regulatory, operational, financial, supply-chain propagation." Drop the stale "Removed in v2: macro_context_score" line.
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
- **Status:** `confirmed` (already self-marked).
- **Action:** `delete`. Verify no imports remain (`grep -r "from app.api.routes.suppliers" -- :!app/api/routes/suppliers.py` should be empty), drop the file.
- **Risk:** None known — main.py already doesn't register it.

### D2. `POST /companies/{company_id}/rescore`
- **Path:** `app/api/routes/companies.py:930-944`
- **Why considered:** Calls `rescore_company` directly, which is now scheduled by Inngest. However, **per user requirement, company-specific scoring stays.** A manual "rescore one company" admin button is still a valid product affordance (it's how analysts flag-and-rerun after correcting bad source data).
- **Status:** `keep` — kept on purpose.

### D3. `POST /chemistries/rescore` and `POST /chemistries/{id}/rescore`
- **Path:** `app/api/routes/chemistries.py:106-117, 208-220`
- **Status:** `proposed` — see B1; switch the implementation to v2.0 rollup (no API contract change), keep the routes themselves.

---

## E — CLI commands

### E1. `bdi-ingest rescore-all`
- **Path:** `app/cli.py:1352-1489`
- **Status:** `keep` — same reasoning as D2 (manual company rescore is intentional).

### E2. `bdi-ingest rescore-chemistry`
- **Path:** `app/cli.py:283-341`
- **Status:** `proposed` — switch to v2.0 rollup (see B1). The CLI signature stays the same; only the imported function changes.

### E3. `bdi-ingest build-trade-signals`
- **Path:** `app/cli.py:1189-1246`
- **Status:** `keep` — `_link_companies` inside is gated; the rest of the trade-signal builder writes useful events. Active in the pipeline.

### E4. `pyproject.toml` hatch script typo: `igest-mrds`
- **Path:** `pyproject.toml:56`
- **Why:** Typo (`igest-mrds` instead of `ingest-mrds`). Not strictly a deprecation, but a dead alias that no one uses correctly.
- **Status:** `proposed`
- **Action:** `delete` the typo line, add `ingest-mrds = "bdi-ingest ingest-mrds --local-file data/facilities/mrds.csv"`.
- **Risk:** Anyone with muscle memory for `hatch run igest-mrds` (so far: nobody) gets a clean error.

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
- **Status:** `proposed`
- **Action:** When B1 step 2 lands, drop the v1.0-only tests and add (or extend) coverage for `score_chemistry_from_rollup`. v2.0 currently lacks a dedicated unit test that asserts the rollup math.

---

## G — Documentation

### G1. `app/services/scoring/__init__.py` docstring
- See B3.

### G2. `docs/data-sources.md:55, :400, :490` and `docs/database_architecture.md:152, :795`
- **Why:** All five lines reference `score_chemistry()` (v1.0) by name. After B1 step 2 these references should point at `score_chemistry_from_rollup`.
- **Status:** `proposed`
- **Action:** `update`-with-B1 step 2.

### G3. `Automotive Data Solutions/opensanctions_cursor_prompt.md:8` — `GeographyScore`
- **Why:** Forward-looking design doc that names a model we're deleting (A2). Either rewrite the section against `MaterialGeographyRiskScore` or remove the reference.
- **Status:** `proposed`
- **Action:** `update`-with-A2.

---

## H — Out-of-scope and intentionally retained

The following are flagged here so a future reader doesn't think they were missed:

- **`CompanyScore`, `CompanySupplyRelationship`, `RiskEventCompany`** — kept (per user requirement).
- **Company-side `evidence_aggregator.py`, `propagation_risk.py`, `supplier_risk.py`, `orchestrator.py`** — kept.
- **`POST /companies/{id}/rescore`, `bdi-ingest rescore-all`** — kept (manual single-company rescore is intentional).
- **`feature_flags.LINK_EVENTS_TO_COMPANIES`** — kept (Phase 5 will flip it back to `True` for the company overlay).
- **`score_financial_pressure`, `score_geopolitical_trade`, `score_material_exposure`, `score_regulatory_profile`, `score_propagation`, `aggregate_supplier_risk`** — kept (pure-function pillar scorers used by both the company-anchored and market-anchored layers).

---

## Suggested execution order

PRs are sequenced so each one is small and reverts cleanly:

1. **PR 1 — typo fix** (E4): one-line `pyproject.toml` fix. Lowest risk.
2. **PR 2 — delete suppliers.py** (D1): single-file delete.
3. **PR 3 — drop auto-rescore loop** (B2 + F1): `app/services/ingestion/pipeline.py` + `tests/test_orchestrator.py`. Architectural alignment with the documented Phase 3 contract.
4. **PR 4 — switch chemistries API + CLI to v2.0 rollup** (B1 step 1, E2, D3 backing change): no public API change, but stops writing v1.0 rows from interactive paths.
5. **PR 5 — clean up scoring/__init__.py docstring** (B3, G1).
6. **PR 6 — drop legacy material_scores / geography_scores** (A1 + A2): includes the new alembic `017_*.py` migration and `app/models/__init__.py` cleanup.
7. **PR 7 — delete v1.0 chemistry path** (B1 step 2 + F2 + G2): only after PR 4 has shipped and scheduled rollups have produced enough v2.0 rows in prod.
8. **PR 8 — refresh design doc** (G3): align `opensanctions_cursor_prompt.md` against the surviving model names.

Each PR should:
- Run the full test suite locally.
- Include a one-line entry in `docs/README.md`'s changelog.
- Update this file's row from `proposed` → `confirmed` → `removed`.
