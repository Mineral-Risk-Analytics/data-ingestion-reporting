# Seed Staleness Review

The **seed staleness review** is a read-only, run-on-demand tool that
cross-references every seeded subject table (regulations, companies,
material exposures, supply relationships, facilities) against already-ingested
evidence (`source_documents`, `risk_events`, their junction tables), scores
each candidate, and persists findings to `seed_review_findings` so a future
UI can surface them for analyst triage.

It is not an auto-updater. It emits "this seeded row may be stale; here is
the evidence" — a human reads the evidence and decides whether to edit the
seed file.

## Pipeline

```
CLI ── orchestrator ── reviewers ── seed_review_runs
                                └── seed_review_findings
```

Entry points:

- CLI: `bdi-ingest review-seeds` (see [app/cli.py](../app/cli.py)).
- Python: `app.services.review.seed_staleness.run_seed_review(session, ...)`.

The orchestrator lives in [app/services/review/seed_staleness.py](../app/services/review/seed_staleness.py)
and dispatches to one module per seed type in
[app/services/review/reviewers/](../app/services/review/reviewers/).

## CLI

```bash
bdi-ingest review-seeds                                 # all types, last 180 days, persisted, markdown
bdi-ingest review-seeds --seed-type company             # one type
bdi-ingest review-seeds --seed-type regulation,company  # comma list
bdi-ingest review-seeds --since 2025-01-01
bdi-ingest review-seeds --dry-run                       # print only, no DB writes
bdi-ingest review-seeds --format json                   # markdown | json | both
bdi-ingest review-seeds --seed-key IRA_DOMESTIC         # single subject, type inferred
```

Flags:

- `--seed-type` — comma-separated list or `all` (default). Rejected if any
  value is not one of `regulation`, `company`, `material_exposure`,
  `supply_relationship`, `facility`.
- `--since` — lower bound for candidate evidence dates (ISO-8601). Default
  is 180 days ago.
- `--dry-run` — skip all DB writes; print the result in the requested format.
- `--format` — `markdown`, `json`, or `both`.
- `--seed-key` — restrict to one subject. The orchestrator tries to infer
  the type by looking up the key against `regulations.regulation_key` and
  `companies.canonical_name`. Combine with `--seed-type` to be explicit.

## Shared scoring

All reviewers use [app/services/review/scoring.py](../app/services/review/scoring.py):

| Score | Relevance |
| --- | --- |
| ≥ 3 | `high` |
| 2   | `medium` |
| 1   | `low` |
| 0   | `none` (not emitted) |

Weak-signal reviewers (currently `facility`) call `score_to_relevance` with
`max_bucket="medium"` so they can never emit `high`, reflecting that their
evidence is inherently noisy.

## Reviewers

### regulation — [reviewers/regulation.py](../app/services/review/reviewers/regulation.py)

- **Subject**: `regulations` seeded by
  [seed_regulations.py](../app/services/ingestion/seed_regulations.py).
- **Signal source**: Federal Register `source_documents` ingested by
  [ingest_federal_register.py](../app/services/ingestion/ingest_federal_register.py),
  scoped to documents published after the regulation's `updated_at`.
- **Signals** (configured per `regulation_key` in
  [regulation_registry.py](../app/services/review/regulation_registry.py)):
  - **Query name match** (+2): `metadata_json->>'query_name'` matches
    `fr_query_names`.
  - **Keyword match** (+1 per distinct keyword, cap +2): `keywords` scanned
    against `title + raw_text`.
  - **Agency match** (+1): any `agency_hints` substring in
    `metadata_json.agencies`.
- **Evidence type**: `federal_register_document`.

Regulations without a registry entry are skipped with a log warning. Add new
entries to `REGULATION_REVIEW_CONFIG` whenever a new `regulation_key` is
seeded.

### company — [reviewers/company.py](../app/services/review/reviewers/company.py)

- **Subject**: `companies` seeded by
  [seed_companies.py](../app/services/ingestion/seed_companies.py).
- **Signal source**: `risk_event_companies` rows whose linked
  `risk_events.event_date` is after the company's `updated_at`.
- **Signals**:
  - **Severity** (+2 / +1 / 0): `severity_score` ≥ 0.70 / ≥ 0.50 / else.
  - **High-impact event subtype** (+1):
    `metadata_json->>'event_subtype'` ∈ {`SANCTION_ADD`,
    `EXPORT_RESTRICTION`, `ENTITY_LIST_UPDATE`}.
  - **Match confidence** (+1): `risk_event_companies.relevance_score` ≥ 0.80.
    Note: the column is `relevance_score`, not `match_confidence`.
- **Evidence type**: `risk_event_company_link`.

### material_exposure — [reviewers/material_exposure.py](../app/services/review/reviewers/material_exposure.py)

- **Subject**: `company_material_exposures` seeded by
  [seed_material_exposures.py](../app/services/ingestion/seed_material_exposures.py).
- **Signal source**: `risk_event_materials` joined to `risk_events`,
  scoped to events after `max(exposure.updated_at, exposure.as_of_date)`.
- **Signals**:
  - **Material match** (+2, hard gate): always present when a finding is
    emitted; recorded for provenance.
  - **Geography overlap** (+1): `risk_events.geography_json.primary` matches
    `company_material_exposures.source_geography` (case-insensitive).
  - **Severity** (+1): `severity_score` ≥ 0.60.
- **Evidence type**: `risk_event_material_link`.

### supply_relationship — [reviewers/supply_relationship.py](../app/services/review/reviewers/supply_relationship.py)

- **Subject**: `company_supply_relationships` seeded by
  [seed_supply_relationships.py](../app/services/ingestion/seed_supply_relationships.py).
- **Signal source**: SEC-EDGAR + news `source_documents` published after
  `relationships.updated_at`. The reviewer builds an alias set for each
  party from `companies.canonical_name`, `companies.legal_name`, and
  `company_aliases` (types `aka`, `former_name`, `abbreviation`, `ticker`),
  and looks for co-occurrence in `title + raw_text`.
- **Signals**:
  - **Both parties match** (+2, hard gate): required for emission.
  - **SEC filing** (+1): `document_type='filing'` or `source_type='sec_edgar'`.
  - **Contract keyword** (+1): any of
    `supply agreement`, `offtake`, `terminated`, `expanded`, `mou`.
- **Evidence type**: `sec_filing_mention` or `news_mention`.

### facility — [reviewers/facility.py](../app/services/review/reviewers/facility.py)

- **Subject**: `facilities` seeded by
  [seed_facilities.py](../app/services/ingestion/seed_facilities.py).
- **Scope**: only facilities with `status` ∈ {`operating`,
  `under_construction`, `planned`} **and** a non-null `city`.
- **Signal source**: news `source_documents` published after
  `facilities.updated_at`.
- **Signals**:
  - **Company name + city both mentioned** (+1, hard gate).
  - **Status-change keyword** (+1): any of `shutdown`, `closure`,
    `expansion`, `commissioned`, `delay`, `postponed`.
- **Relevance cap**: `max_bucket="medium"`. Text co-occurrence is too noisy
  to ever assert `high`.
- **Evidence type**: `facility_text_mention`.

## Persistence schema

Created by [alembic/versions/005_seed_review_tables.py](../alembic/versions/005_seed_review_tables.py)
and modeled in [app/models/review.py](../app/models/review.py).

### `seed_review_runs`

One row per invocation. Tracks what was asked for (`seed_types`,
`since_date`, `parameters_json`), when it started/completed, and the
aggregate counts.

### `seed_review_findings`

One row per emitted candidate. Polymorphic on `seed_type`:

- `seed_identifier_json` (GIN-indexed) carries typed components so the UI
  can filter "all findings about company CATL" without adding a column per
  seed type.
- `seed_key` is the human-readable form (e.g. `IRA_DOMESTIC`, `CATL`,
  `CATL|Lithium`, `Ford|BlueOvalSK|NMC cells`).
- `evidence_type` + nullable `source_document_id` + nullable `risk_event_id`
  covers all reviewer evidence shapes.
- Triage state lives in `status` (`open` | `acknowledged` | `dismissed`),
  `reviewed_at`, `reviewed_by`, `reviewer_note`.

## UI consumption path

A future UI is expected to:

1. `GET /seed-review/runs/latest` → latest `seed_review_runs` row.
2. `GET /seed-review/runs/{run_id}/findings?seed_type=...&status=open`
   → `seed_review_findings` rows, grouped by `seed_type` → `seed_key`.
3. `PATCH /seed-review/findings/{id}` → update `status`, `reviewed_at`,
   `reviewed_by`, `reviewer_note` when an analyst triages a finding.

Because `seed_identifier_json` is GIN-indexed, the UI can also answer
"show me all historical findings about this subject" with a single JSONB
containment query.

## Out of scope

The following seed types are **not** reviewed because no credible automated
signal exists in the current ingestion pipeline:

- **`materials`** — USGS critical-mineral list changes are rare and manual.
- **`hs_codes` / `hs_code_material_mappings`** — HS codes are static customs
  statistics; the mapping is effectively a lookup table.

Adding either of these would require a new signal source (e.g. a USGS
ingestion job, or an HS-code-list diff feed). When one exists, add a new
reviewer under `reviewers/` and register it in
[`seed_staleness.SEED_TYPES`](../app/services/review/seed_staleness.py) and
[`seed_staleness._dispatch`](../app/services/review/seed_staleness.py).

Also out of scope for v1:

- Automatically editing seed files from findings (that would be "Option B —
  DB-to-seed dumper", a different design).
- Semantic / embedding similarity matching. The registry keyword approach is
  sufficient for v1 and is easy to reason about; semantic search can later
  be wired in as Signal D.
- A live UI. This feature stops at persisted findings and a CLI report.

## Adding a new reviewer

1. Create `app/services/review/reviewers/<type>.py` with a `review(session, *, since_date, ...)` callable that returns `list[Finding]`.
2. Add the new string to `SEED_TYPES` and a branch in `_dispatch` in [seed_staleness.py](../app/services/review/seed_staleness.py).
3. Add a count in `_subject_count` so `total_seeds_checked` is accurate.
4. Extend the CLI validator comment in [app/cli.py](../app/cli.py) if needed — the CLI reads `SEED_TYPES` dynamically.
5. Add a unit test under `tests/review/test_<type>_reviewer.py`. Follow the existing pattern: test the pure `_score_*` helper with hand-built `SimpleNamespace` objects rather than hitting a DB.
6. Document the new reviewer in this file.

## Relationship to company scoring

The seed-review reviewers keep the *inputs* to the v3.0 company scoring
pipeline fresh:

| Reviewer       | Updates table                  | Feeds into v3.0 pillar |
| -------------- | ------------------------------ | ---------------------- |
| regulation     | `regulations` + `*_scope`      | Regulatory & Compliance (`Regulation*Scope` UNION) |
| company        | `companies`                    | All pillars (rationale labelling, supplier-chain BFS roots) |
| material_exposure | `company_material_exposures` | Material Concentration (chemistry-aware reweighting) + Geopolitical (source geography) |
| supply_relationship | `company_supply_relationships` | Supply-Chain Propagation (BFS edges) |
| facility       | `facilities`                   | Geopolitical (facility country) + Operational (planned/under-construction) |

When the reviewers surface a stale row (e.g. a closed facility still flagged
"operating", or a withdrawn `IRA_DOMESTIC` exposure), an analyst's edit to
the seed file flows into the next ingestion run, which then triggers a
rescore — so the company's `overall_risk_score` in `company_scores`
updates the next time the pipeline runs.

See [scoring.md](scoring.md) for the full v3.0 pipeline (six pillars,
chemistry refinement, `ScoringScope` hooks).
