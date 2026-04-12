# Scoring

> **Last updated: April 2026**

The scoring engine is **stateless, pure-function, and transparent**: every component function returns a raw `float` (0-100) with no database reads. Persistence happens after computation, never inside the scoring layer. The aggregate result dict includes a `scoring_version` field so downstream displays can flag score deltas caused by a methodology change rather than a real-world signal change.

---

## Five-Pillar Architecture

| Pillar | Weight | Module |
|--------|--------|--------|
| Material Concentration | **30%** | `material_risk.py` |
| Geopolitical / Trade | **20%** | `geopolitical_risk.py` |
| Regulatory & Compliance | **20%** | `regulatory_risk.py` |
| Operational | **15%** | `orchestrator.py` (`_score_operational`) |
| Financial Pressure | **15%** | `financial_pressure.py` |

Previous v1 weights (0.35/0.35/0.15/0.15 across four pillars) are retired. `scoring_version = "2.0"` is written to every new `company_scores` row.

---

## Module layout (`app/services/scoring/`)

### Pure scoring functions (no DB access)

| File | Purpose |
|------|---------|
| `types.py` | `SupplierScoreRationale` Pydantic model (rationale_json contract); `ScoringInputs`, `ComponentScores`, `DecayParameters` |
| `event_impact.py` | Core event-impact formula: `compute_event_impact`, `compute_effective_confidence` |
| `decay.py` | Category-specific recency decay → `recency_multiplier` in [0.60, 1.20]; `EVIDENCE_WINDOWS` dict |
| `material_risk.py` | Material Concentration Risk (35/35/30 sub-weights) |
| `geopolitical_risk.py` | Geopolitical/Trade Risk (40/35/25 sub-weights) |
| `regulatory_risk.py` | Regulatory & Compliance Risk: event rollup + obligation uplift |
| `financial_pressure.py` | Financial Pressure: three bounded sub-components (0-40 / 0-30 / 0-30) |
| `supplier_risk.py` | Five-pillar aggregate → `aggregate_supplier_risk` dict; `SCORING_VERSION` constant |

### Orchestration layer (DB reads + persistence)

| File | Purpose |
|------|---------|
| `evidence_query.py` | DB read layer: returns `EventWithRelevance` objects per category per company |
| `evidence_aggregator.py` | Translates `EventWithRelevance` records into the float inputs each scorer expects |
| `orchestrator.py` | Entry point: `rescore_company(db, company_id, run_id)` — coordinates evidence → aggregation → scoring → persistence |

**Rule:** `aggregate_supplier_risk()` must only be called from `orchestrator.py`. Never call it from pipeline code, tests, or routes directly.

---

## Event Impact Formula

All risk events produce a scalar `event_impact` before being aggregated into pillar scores:

```
effective_confidence = max(confidence, 0.60)  if severity >= 0.80  else confidence
event_impact = severity × effective_confidence × recency_multiplier × relevance_multiplier
```

- `relevance_multiplier` comes from `risk_event_companies.relevance_score` (set by entity resolution), not a hardcoded default.
- Theoretical maximum: `1.0 × 1.0 × 1.20 × 1.30 = 1.56`.
- All severity/confidence values are on the **0-1.0 scale**.

---

## Category-Specific Decay Schedules

`compute_recency_multiplier(category, event_date, as_of_date)` returns a value in `[0.60, 1.20]`.

| Category | Decay Shape | Evidence Window |
|----------|-------------|-----------------|
| `GEOPOLITICAL_TRADE` | Linear 1.0 → 0.70 over 24 months, then 0.60 | 730 days |
| `REGULATORY_COMPLIANCE` | Step-up 1.10–1.20 within 90 days of enforcement; linear decay otherwise | 365 days |
| `OPERATIONAL` | Exponential, 90-day half-life | 180 days |
| `MATERIAL_CONCENTRATION` | Linear 1.0 → 0.60 over 365 days | 365 days |
| `FINANCIAL_PRESSURE` | Always 1.0 — caller controls window by selecting 4 most recent quarters | None (held) |

---

## Component Scoring Functions

### Material Concentration (`score_material_exposure`)

```
score = (0.35 × criticality + 0.35 × concentration + 0.30 × trade_volatility) × 100
```

All inputs on [0, 1]. Returns [0, 100].

### Geopolitical / Trade (`score_geopolitical_trade`)

```
score = (0.40 × country_concentration + 0.35 × export_restriction_exposure + 0.25 × tariff_exposure) × 100
```

Country concentration carries the highest sub-weight because geographic dependency (China ~70-80% of cell production and refining) is the dominant structural risk for EV battery supply chains.

### Regulatory & Compliance (`score_regulatory_profile`)

Two-part score, capped at 100:

1. **Event-driven rollup (0-60):** average of top-3 `event_impact` values × `policy_proximity_adjustment` × 60.
2. **Obligation uplift (0-40):** additive points for active hard legal obligations:

| Obligation key | Uplift points |
|----------------|--------------|
| `UFLPA` | 25 |
| `EU_BATTERY_REG` | 20 |
| `IRA_DOMESTIC` | 15 |

Obligation uplift is hard-capped at 40. Active obligations are sourced from `company_regulation_exposure` or derived from `REGULATORY_COMPLIANCE` events with matching `event_subtype`.

### Financial Pressure (`score_financial_pressure`)

```
raw_score = base_filing_signal (0-40) + leverage_warning_bonus (0-30) + liquidity_stress_bonus (0-30)
```

When `filing_count < 2`, `raw_score` is scaled by `filing_count / 2.0` to avoid treating a single filing as a confirmed trend.

### Operational

Computed in `orchestrator._score_operational` (not a standalone module):

```
score = (0.40 × structural_dependency + 0.60 × avg(weighted_event_impacts)) × 100
```

`structural_dependency` defaults to 0.30 if no `SINGLE_SOURCE` or `CAPACITY_CONSTRAINT` events are present.

---

## Orchestration layer

### Evidence query (`evidence_query.py`)

Returns `EventWithRelevance` objects — a dataclass wrapping `(event: RiskEvent, relevance_score: float)`. The `relevance_score` comes from `risk_event_companies.relevance_score` and is used as `relevance_multiplier` in `compute_event_impact`.

Key functions:
- `get_events_for_company(db, company_id, category, as_of_date)` — uses `EVIDENCE_WINDOWS` from `decay.py` for the lookback period.
- `get_company_material_exposure(db, company_id)` — active `company_material_exposures` records.
- `get_active_compliance_obligations(db, company_id)` — obligation keys from `company_regulation_exposure` or regulatory events.
- `get_filing_signals(db, company_id, max_quarters=4)` — most recent N distinct-quarter financial filing events.

### Evidence aggregation (`evidence_aggregator.py`)

Translates raw `EventWithRelevance` records into float inputs for each scoring function:

- `derive_material_inputs` → `(criticality, concentration, trade_volatility)`
- `derive_geopolitical_inputs` → `(country_concentration, export_restriction_exposure, tariff_exposure)`
- `derive_regulatory_inputs` → `(top_event_impacts, active_obligations, policy_proximity_adjustment)`
- `derive_operational_inputs` → `(structural_dependency, weighted_event_impacts)`
- `derive_financial_inputs` → `(base_filing_signal, leverage_warning_bonus, liquidity_stress_bonus, filing_count)`

High-concentration geographies default: `['CN', 'CD', 'RU']`. Conservative defaults apply when evidence is absent (e.g. 0.5 for criticality/concentration, 0.3 for structural dependency).

### Orchestrator (`orchestrator.py`)

```python
from app.services.scoring.orchestrator import rescore_company

score_row = rescore_company(db, company_id, run_id="manual")
db.commit()
```

`rescore_company` is the **single permitted entry point** for scoring. It:
1. Queries all evidence by category.
2. Aggregates into float inputs.
3. Calls all five component scorers.
4. Calls `aggregate_supplier_risk()`.
5. Builds a `SupplierScoreRationale` model.
6. Appends a **new** `company_scores` row (never overwrites existing rows).
7. Returns the ORM object without committing — caller commits the transaction.

---

## `scoring_version` and score delta displays

The `scoring_version` field on `company_scores` rows records which methodology produced each row. When a new version is released:

- New rows carry the new version string (e.g. `"2.0"`).
- Pre-existing rows retain their original version string.
- Score delta displays should suppress or annotate comparisons that cross a version boundary.

---

## `SupplierScoreRationale` (rationale_json contract)

The only permitted writer for `company_scores.rationale_json` is:

```python
from app.services.scoring.types import SupplierScoreRationale
row.rationale_json = SupplierScoreRationale(...).model_dump()
```

Never write unstructured dicts to this column. Schema captures: `inputs` (company ID, evidence window, version, run ID), `components` (all five pillar scores + overall), `top_evidence` (event IDs most influential on the score), `decay` (eval date + per-pillar windows), and `notes` (human-readable summary string).

---

## Database relationships

| Table | Intended use |
|-------|--------------|
| `company_scores` | Append-only rows from `aggregate_supplier_risk`; one per company per `as_of_date` |
| `risk_events` | Source of `event_impact` values fed to all pillar scorers |
| `risk_event_companies` | Links events to companies with `relevance_score` (entity resolution output) |
| `company_material_exposures` | Source of criticality and concentration inputs for material scoring |
| `company_regulation_exposure` | Source of active obligation keys for regulatory scoring |
| `document_chunks.embedding` | 1536-dim pgvector column; used for semantic evidence retrieval (future) |

Schema DDL is in `alembic/versions/001_baseline.py`.

---

## Testing

See `tests/test_scoring.py` for:
- Five-pillar aggregate contract (`test_supplier_aggregate_five_pillars`, `test_supplier_aggregate_all_keys_present`)
- Effective confidence floor
- Financial pressure sparse-evidence cap
- Regulatory obligation uplift

See `tests/test_orchestrator.py` for:
- Full `rescore_company` integration (fixture supplier + events → `company_scores` row)
- Append-only behaviour (two calls → two rows)
- Non-blocking on scoring failure (ingestion run not rolled back)
- Evidence aggregator unit tests

---

## Related reading

- [Overview](overview.md)
- [Parsing & normalization](parsing-and-normalization.md) — event drafts and RiskCategory tags
- [Ingestion pipeline](ingestion-pipeline.md) — how post-ingestion rescoring is triggered
- [Reports & AI](reports-and-ai.md) — where scored narratives surface
