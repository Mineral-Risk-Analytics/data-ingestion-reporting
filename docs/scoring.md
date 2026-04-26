# Scoring

> **Last updated: April 2026**

The platform has three independent scoring systems:

1. **Company risk scoring** (six-pillar, v3.0) — rates a specific supply chain company's risk across material concentration, geopolitical, regulatory, operational, financial, and supply-chain-propagation dimensions. Output: `company_scores`.
2. **Chemistry risk scoring** — rates a battery cell chemistry's supply risk based on its material composition, criticality signals, and geographic concentration of its constituent minerals. Output: `chemistry_risk_scores`.
3. **Market risk scoring** (foundation phase, April 2026) — rates the **material × geography intersection** independent of any specific company. Powers the public intelligence hub. Output: `material_geography_risk_scores`. See [§ Market Risk Scoring](#market-risk-scoring) below.

All three systems are **stateless, pure-function, and transparent**: component functions return raw `float` values with no database reads. Persistence happens after computation, never inside the pure scoring layer.

> **Phase 3 architecture change (April 2026).** Ingestion **no longer auto-rescores companies.** The post-run rescore hook in `IngestionPipeline` was removed; company rescoring is now CLI / API only. In addition, ingestion no longer writes `risk_event_companies` rows by default — that behaviour is gated by `LINK_EVENTS_TO_COMPANIES` in `app/services/ingestion/feature_flags.py`. The two weekly Inngest cron jobs (chemistry + market) take over the "things automatically stay fresh" responsibility for the company-agnostic layers.

> **v3.0 highlights** — see "Scoring v3.0 (April 2026): six pillars, chemistry refinement, ScoringScope" below for the full change log. The seed-staleness reviewers (`docs/seed-review.md`) feed the v3.0 inputs by keeping `CompanyMaterialExposure`, `Facility`, `CompanySupplyRelationship`, `CompanyVehicleModel`, and `CompanyRegulationExposure` rows fresh.

---

## Six-Pillar Architecture (v3.0)

| Pillar | Weight | Module |
|--------|--------|--------|
| Material Concentration | **25%** | `material_risk.py` (chemistry-aware via `derive_material_inputs`) |
| Geopolitical / Trade | **20%** | `geopolitical_risk.py` (folds in facility countries) |
| Regulatory & Compliance | **20%** | `regulatory_risk.py` (folds in `Regulation*Scope` hits) |
| Operational | **10%** | `orchestrator.py` (`_score_operational`; folds in facility status) |
| Financial Pressure | **10%** | `financial_pressure.py` |
| Supply-Chain Propagation | **15%** | `propagation_risk.py` (BFS over `CompanySupplyRelationship`) |

When the propagation pillar can't be computed (no suppliers, or no suppliers with persisted `CompanyScore` rows), `aggregate_supplier_risk()` re-normalises the remaining five weights so the overall score remains comparable across companies. `scoring_version = "3.0"` is written to every new `company_scores` row.

Previous v2 weights (0.30/0.20/0.20/0.15/0.15) and v1 weights (0.35/0.35/0.15/0.15) are retired. Pre-existing rows keep their original version string — see *scoring_version and score delta displays*.

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
| `propagation_risk.py` | Supply-Chain Propagation: volume-weighted, depth-decayed average of supplier `overall_risk_score` (v3.0) |
| `supplier_risk.py` | Six-pillar aggregate → `aggregate_supplier_risk` dict; `SCORING_VERSION` constant; renormalises when propagation is `None` |

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

`structural_dependency` defaults to 0.30 if no `SINGLE_SOURCE` or `CAPACITY_CONSTRAINT` events are present. In v3.0, planned / under-construction `Facility` rows lift `structural_dependency` to `max(event_signal, 0.4 × non_op_share)` — a company with 4 of 5 facilities still in construction has a clear capacity-dependency signal even before any operational event lands.

### Supply-Chain Propagation (`score_propagation`) — v3.0

Sixth pillar. Captures *second-party* risk: the persisted `overall_risk_score` of suppliers reachable via `CompanySupplyRelationship`.

```
share_d_w = clamp01(volume_share) × depth_weight(d)        # depth_weight = (1.00, 0.40, ...)
score    = Σ(supplier_score × share_d_w) / Σ(share_d_w)    # clamped to [0, 100]
```

- The orchestrator runs a BFS over `CompanySupplyRelationship` (`get_supplier_chain`) with cycle detection and a hard `max_visited` cap (default 50). Default `max_depth = 2` (tier-2).
- Suppliers without a persisted `CompanyScore` are dropped; the count is surfaced in `rationale_json.signals_used.supplier_scores_used`.
- Edges with `volume_share_pct = NULL` substitute the conservative default of 0.10.
- When the input list is empty (no chain, no usable scores, or scoped run), the pillar score is `None` and `aggregate_supplier_risk` re-normalises the other five pillar weights.

---

## Scoring v3.0 (April 2026): six pillars, chemistry refinement, ScoringScope

v3.0 keeps the v2 pillar functions intact and adds three orthogonal capabilities:

### 1. Sixth pillar: supply-chain propagation

Migration `006_supply_chain_rollup.py` adds:
- `risk_event_facilities` junction (used by ingestion to fan events out to facility-level relevance).
- `company_scores.supply_chain_propagation_score` and `company_scores.propagation_depth_used` columns.

The pillar reads only persisted `CompanyScore` rows for suppliers — never recurses into rescore. This keeps each rescore O(direct + tier-2 supplier rows) and avoids cascade explosions when many companies share suppliers.

### 2. Chemistry-aware material weighting (no new pillar)

Migration `007_company_vehicle_models.py` adds `company_vehicle_models` and `vehicle_model_chemistries`, time-windowed product-level grain on chemistry exposure.

`derive_material_inputs` re-weights each `CompanyMaterialExposure` by the chemistry-aware intensity of its material. A 100% LFP OEM gets material risk dominated by Li/Fe/P; cobalt exposure (if any) collapses to the `_CHEMISTRY_BASELINE_UNMATCHED = 0.10` floor rather than being averaged at full strength.

Industry-average market shares are *never* substituted when a company has no vehicle models — the aggregator falls back to legacy uniform behaviour so missing data never silently injects industry signal.

`CompanyVehicleModel` rows are seeded from ev-database.org via `bdi-ingest scrape-ev-database` (see [`app/services/ingestion/scrape_ev_database.py`](../app/services/ingestion/scrape_ev_database.py)). Production volume is not exposed by that source, so the company-level chemistry mix is uniformly weighted across an OEM's variants until a manual `production_volume_units` override is applied. The scraper is idempotent on `(company_id, model_name, model_year_start)` and skips brands with no matching `Company` row — no new companies are ever created here.

### 3. ScoringScope hooks (UI-ready scoped views)

`ScoringScope` (in `types.py`) is a frozen dataclass with optional filters:

| Field | Filters |
|-------|---------|
| `material_ids` | Exposures + material-tagged events + chemistry intensities |
| `country_codes` | Source geography + facility country + geo-tagged events |
| `chemistry_ids` | Vehicle-model chemistry mix + intensity rows |
| `regulation_keys` | Active obligations + scope-derived regulations + regulation-tagged events |
| `facility_ids` | Facility roster |
| `supplier_depth_max` | Caps the propagation BFS depth from above |

`ScoringScope.ALL` is the default (no-op) and is regression-tested.

```python
from app.services.scoring.orchestrator import score_company_scoped
from app.services.scoring.types import ScoringScope

# "What is OEM X's profile if we only look at Chinese exposure?"
preview = score_company_scoped(
    db, oem_id, ScoringScope(country_codes=frozenset({"CN"}))
)
# preview.id is None — scoped runs NEVER persist.
```

Two non-negotiable rules:
- **Scoped runs never persist.** `rescore_company` coerces `persist=False` whenever `scope.is_all() is False`. The `company_scores` time-series stays comparable.
- **Scoped runs skip propagation in v1.** Rationale: a scoped propagation rollup would mix scoped-this-company with full-scope-of-supplier signals in a way that's hard to explain. `signals_used.propagation_skipped_due_to_scope == True` flags this in the rationale.

### Rationale block additions

`SupplierScoreRationale` now includes:
- `components.supply_chain_propagation` — pillar score on [0, 100] or `None`.
- `propagation_chain` — list of `(supplier_id, depth, cumulative_volume_share, supplier_overall_score)` actually used.
- `chemistry_mix` — list of `(chemistry_slug, share, persisted_chemistry_composite_or_null)`.
- `signals_used` — count map: facilities, scope_obligations, regulation_events, geo_events_trade, geo_events_operational, material_country_events, supplier_chain_size, supplier_scores_used, chemistries_weighted, plus `propagation_skipped_due_to_scope` for scoped runs.

These power the UI's "why did this score change?" panel without requiring back-computation from raw inputs.

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
3. Calls all six component scorers.
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

Never write unstructured dicts to this column. Schema captures: `inputs` (company ID, evidence window, version, run ID), `components` (all six pillar scores + overall), `top_evidence` (event IDs most influential on the score), `decay` (eval date + per-pillar windows), and `notes` (human-readable summary string).

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

## Data Flow

flowchart TB
    subgraph DB["Database (raw evidence)"]
        ME[(CompanyMaterialExposure<br/>material, geography, exposure_score)]
        RE[(RiskEvent<br/>severity, confidence, event_date,<br/>category, metadata_json)]
        REC[(RiskEventCompany<br/>relevance_score)]
        REM[(RiskEventMaterial)]
        CRE[(CompanyRegulationExposure<br/>compliance_status)]
    end

    subgraph Q["1. Evidence query<br/>(evidence_query.py)"]
        Q1[get_company_material_exposure]
        Q2[get_events_for_company<br/>by RiskCategory]
        Q3[get_filing_signals]
        Q4[get_active_compliance_obligations<br/>status -> weight]
    end

    ME --> Q1
    RE --> Q2
    REC --> Q2
    REM --> Q2
    RE --> Q3
    CRE --> Q4

    subgraph IMP["2a. Per-event impact<br/>(event_impact.py)"]
        IMPF["event_impact =<br/>severity × eff_confidence ×<br/>recency × relevance<br/>(0 - 1.56)"]
    end

    Q2 --> IMPF
    Q3 --> IMPF

    subgraph AGG["2b. Aggregate to component inputs<br/>(evidence_aggregator.py)"]
        DM["derive_material_inputs<br/>criticality | concentration | trade_volatility"]
        DG["derive_geopolitical_inputs<br/>country_conc | export_restr | tariff"]
        DR["derive_regulatory_inputs<br/>top_event_impacts | obligations | proximity"]
        DO["derive_operational_inputs<br/>structural_dep | event_impacts"]
        DF["derive_financial_inputs<br/>base | leverage | liquidity | count"]
    end

    Q1 --> DM
    IMPF --> DM
    Q1 --> DG
    IMPF --> DG
    IMPF --> DR
    Q4 --> DR
    IMPF --> DO
    IMPF --> DF

    subgraph PILLAR["3. Pillar scoring (0-100 each)"]
        P1["Material<br/>0.35·crit + 0.35·conc + 0.30·vol"]
        P2["Geopolitical<br/>0.40·country + 0.35·export + 0.25·tariff"]
        P3["Regulatory<br/>event(top-3)·proximity·60<br/>+ obligation_uplift (cap 40)"]
        P4["Operational<br/>0.40·struct_dep + 0.60·avg_event"]
        P5["Financial<br/>base + leverage + liquidity"]
        P6["Propagation<br/>volume-weighted depth-decayed<br/>avg of supplier overall_risk_score"]
    end

    DM --> P1
    DG --> P2
    DR --> P3
    DO --> P4
    DF --> P5
    P1 --> P6
    P2 --> P6
    P3 --> P6
    P4 --> P6
    P5 --> P6

    subgraph ROLL["4. Aggregate (supplier_risk.py)"]
        OVR["overall_risk_score =<br/>0.25·M + 0.20·G + 0.20·R +<br/>0.10·O + 0.10·F + 0.15·Prop<br/>(Prop=None → renormalise 5)"]
    end

    P1 -- "25%" --> OVR
    P2 -- "20%" --> OVR
    P3 -- "20%" --> OVR
    P4 -- "10%" --> OVR
    P5 -- "10%" --> OVR
    P6 -- "15%" --> OVR

    subgraph PERSIST["5. Persist (orchestrator.py)"]
        ROW[(CompanyScore row<br/>+ rationale_json)]
    end

    P1 --> ROW
    P2 --> ROW
    P3 --> ROW
    P4 --> ROW
    P5 --> ROW
    P6 --> ROW
    OVR --> ROW

---

## Testing

See `tests/test_scoring.py` for:
- Six-pillar aggregate contract with and without propagation (`test_supplier_aggregate_five_pillars_no_propagation`, `test_supplier_aggregate_six_pillars_with_propagation`, `test_supplier_aggregate_all_keys_present`)
- Effective confidence floor
- Financial pressure sparse-evidence cap
- Regulatory obligation uplift (now keyed on `(obligation_key, weight)` tuples)

See `tests/test_orchestrator.py` for:
- Full `rescore_company` integration (fixture supplier + events → `company_scores` row, `scoring_version == "3.0"`)
- Append-only behaviour (two calls → two rows)
- Non-blocking on scoring failure (ingestion run not rolled back)
- Evidence aggregator unit tests

See `tests/scoring/` for the v3.0 surface area:
- `test_evidence_query_supply_chain.py` — BFS depth/cycle/volume math, `get_facilities_for_company`, `get_latest_company_scores`, `get_regulations_scoping_company` UNION + max-weight dedup.
- `test_aggregator_facility_geo.py` — facility countries fold into `country_concentration` and `structural_dependency`; geo-tagged events widen export/tariff pools without double-counting.
- `test_aggregator_scope_regulations.py` — regulation UNION (max weight), regulation-tagged event dedup, `policy_proximity_adjustment` window, `derive_propagation_inputs` shaping (skip-no-score, default-volume).
- `test_chemistry_mix.py` — volume-weighted chemistry aggregation, time-window filtering, scope-driven re-normalisation, chemistry-aware material weighting end-to-end.
- `test_propagation_risk.py` — pure-function pillar math: depth weights, share clamping, [0, 100] clamping.
- `test_orchestrator_propagation.py` — end-to-end propagation through `rescore_company`: tier-1, tier-2, depth caps, suppliers with no persisted score (skipped + signals_used reflect it), no-supplier baseline (renormalisation regression guard).
- `test_scope_and_scoped_orchestrator.py` — `ScoringScope` invariants, `score_company_scoped` never persists, scoped runs skip propagation, scope kwarg is threaded into every `evidence_query` helper.

---

---

## Chemistry Risk Scoring

### Overview

`app/services/scoring/chemistry_risk.py` scores a battery chemistry's supply risk based on its current material composition and criticality data. Unlike company scoring, this scorer reads directly from the database (it is not purely stateless) and is CLI-triggered rather than pipeline-triggered.

**CLI:** `bdi-ingest rescore-chemistry [--slug nmc] [--as-of 2024-01-01]`

**Scheduled:** Mondays 02:00 UTC via the Inngest cron `rescore-all-chemistries` (see [§ Scheduled rescores (Inngest)](#scheduled-rescores-inngest)).

### Inputs

| Data | Source table | Notes |
|------|-------------|-------|
| Material composition | `battery_chemistry_materials` | Filtered by `valid_from ≤ as_of ≤ COALESCE(valid_to, 'infinity')` |
| Criticality signals | `material_criticality_signals` | Resolved by source priority (see below) |
| Fallback criticality | `materials.criticality_score` | Used when no signal rows exist |
| Patent trend | `materials.patent_occurrence_trend` | `rising`/`declining`/`stable` |
| Data availability | `materials.data_availability` | `commercial`/`limited`/`no_benchmark` |
| Geographic concentration | `trade_flows` + `hs_code_material_mappings` | Filtered to high-concentration geos |

### Signal source priority

`_resolve_criticality_signal()` picks the best available signal for each material in this order:

1. `eu_crma` — EU regulatory authority (not yet ingested, planned)
2. `iea_report` — Demand-driven, forward-looking (not yet ingested, planned)
3. `usgs_mcs` — Production-based HHI (current baseline)
4. `manual` — Hand-entered
5. `patstat` — Patent-derived (planned)
6. `materials.criticality_score` — Fallback if no signal rows exist

### Score formula

```
# Per material:
adjusted_criticality = criticality_score × PATENT_TREND_MODIFIERS[patent_occurrence_trend]
material_risk = score_material_exposure(adjusted_criticality, concentration, trade_volatility)

# Aggregated across materials (intensity-weighted):
material_concentration_score = Σ(intensity_i × material_risk_i) / Σ(intensity_i)
geopolitical_score = Σ(intensity_i × geo_concentration_i) / Σ(intensity_i)
composite_risk_score = 0.50 × material_concentration_score + 0.50 × geopolitical_score

# Confidence:
score_confidence = max(0.30, Π DATA_AVAILABILITY_CONFIDENCE[data_availability_i])
```

### Patent trend modifiers (named constants)

These are empirical estimates, not derived from the underlying EPO PATSTAT data directly. They will be replaced with data-driven values once PATSTAT ingestion is live.

| Trend | Modifier |
|-------|---------|
| `rising` | × 1.15 |
| `stable` | × 1.00 |
| `declining` | × 0.85 |
| `None` | × 1.00 (treated as stable) |

### Data availability confidence multipliers

| Tier | Multiplier |
|------|-----------|
| `commercial` | 1.00 — LME/exchange-traded price benchmark exists |
| `limited` | 0.85 — Sporadic or opaque pricing |
| `no_benchmark` | 0.65 — Bilateral contracts only; no public price |

`score_confidence = max(0.30, product of all multipliers)`. A chemistry scoring entirely from `no_benchmark` materials floors at 0.3.

### Output

Appended as a new row in `chemistry_risk_scores`. Never overwrites existing rows.

`metadata_json` records: signal sources used per material, geo coverage gaps, materials missing HS codes, patent modifiers applied, trade flows vintage, no-benchmark material list, final score confidence.

---

## Market Risk Scoring

### Overview

`app/services/scoring/market_aggregator.py` scores **(material, geography)** pairs without any company context. It is the primary intelligence layer powering the public hub at `mineralriskanalytics.com`; the company-overlay scoring (`orchestrator.py`) is built on top of these market signals.

**CLI:**
```bash
bdi-ingest rescore-market                              # all active materials × derived geographies
bdi-ingest rescore-market --material-id 7 --geographies CN,CL,AU
```

**API:**
- `GET /api/v1/materials/{id}/market-scores` — latest score per geography for a material
- `GET /api/v1/materials/{id}/market-scores/{geo}` — full rationale for one pair
- `GET /api/v1/market/scores` — paginated list across all pairs (filterable by material, geography, `min_overall`)
- `POST /api/v1/market/rescore` — synchronously rescore every active pair

**Scheduled:** Mondays 03:00 UTC via the Inngest cron `rescore-market-scores` (one hour after the chemistry rescore so the new chemistry composites are visible).

### Pillar weights (renormalised)

Supply-chain propagation is excluded (no company graph at the market level). Financial pressure is **kept** but reframed with market-level inputs (commodity price volatility + producer-stress events), not company filings. The remaining five v3.0 weights (sum 0.85) are renormalised:

| Pillar | Source weight | Market weight |
|--------|--------------:|--------------:|
| Material Concentration | 0.25 | **≈ 0.294** |
| Geopolitical / Trade | 0.20 | **≈ 0.235** |
| Regulatory & Compliance | 0.20 | **≈ 0.235** |
| Operational | 0.10 | **≈ 0.118** |
| Financial Pressure (reframed) | 0.10 | **≈ 0.118** |

These constants live in `MARKET_PILLAR_WEIGHTS` in `market_aggregator.py`.

### Inputs (where each pillar's signals come from)

| Pillar | Market-level inputs |
|--------|---------------------|
| Material Concentration | `MaterialCriticalitySignal` (HHI + criticality_score, source priority `eu_crma > iea_report > usgs_mcs > manual > patstat`) + High-Concentration Geography uplift for `CN/CD/RU` + average normalised `event_impact` for `GEOPOLITICAL_TRADE` events tagged to the material or geography. |
| Geopolitical / Trade | Binary HCG `country_concentration` for the target geography + classified events: `EXPORT_RESTRICTION` and `TARIFF`/`TRADE_POLICY` subtypes (or keyword-matched titles). |
| Regulatory & Compliance | Regulations linked via `RegulationMaterialScope` or `RegulationGeographyScope` (weight 0.50, "unknown compliance" since there is no company to assess against) + scoped `REGULATORY_COMPLIANCE` events + 90-day proximity adjustment (1.15×) when an event has a near-future `effective_date`. |
| Operational | `SINGLE_SOURCE` / `CAPACITY_CONSTRAINT` events lift `structural_dependency` from the 0.3 baseline; weighted average of `OPERATIONAL` event impacts. |
| Financial Pressure (reframed) | `CommodityPrice` rows over a 180-day window: coefficient of variation → `base_filing_signal` (0–40), price spike → `leverage_warning_bonus` (0–30), price crash → `liquidity_stress_bonus` (0–30). Augmented with `FINANCIAL_PRESSURE` events tagged `PRICE_SURGE`/`MARKET_SQUEEZE` and `PRODUCER_EXIT`/`MINE_CLOSURE`/`BANKRUPTCY`. |

The reused pure-function scorers (`material_risk.score_material_exposure`, `geopolitical_risk.score_geopolitical_trade`, `regulatory_risk.score_regulatory_profile`, `financial_pressure.score_financial_pressure`) are called unchanged. Operational scoring uses a small market-specific helper (`_score_operational_market`) that mirrors the company-layer formula.

### Output: `material_geography_risk_scores`

Migration `011_material_geography_risk_scores.py`. One row per `(material_id, geography_code, as_of_date)` (unique constraint). Columns:

- Five pillar scores: `material_concentration_score`, `geopolitical_trade_score`, `regulatory_compliance_score`, `operational_score`, `financial_pressure_score`.
- `overall_risk_score` (weighted aggregate using `MARKET_PILLAR_WEIGHTS`).
- `event_count` — number of distinct events used.
- `rationale_json` — full sub-input breakdown, pillar scores, weights used, criticality signal source, event counts, and a human-readable `notes` string.
- `scoring_version` — currently `"3.0"` (matches the company-layer version constant).

Rows are appended (never overwritten); the `(material_id, geography_code, as_of_date)` unique constraint prevents duplicate same-day re-runs.

### Transaction contract

`score_material_geography()` calls `db.flush()` but **does not commit** — caller owns the transaction (consistent with `rescore_company`). The batch helper `score_all_active_materials()` does commit per pair, with `db.rollback()` and a structured-log error on per-pair failures so one bad pair never aborts the whole run.

---

## Scheduled rescores (Inngest)

The chemistry and market layers are kept fresh by two weekly cron jobs registered with Inngest:

| Function | Cron (UTC) | What it does |
|----------|-----------|--------------|
| `rescore-all-chemistries` | `0 2 * * MON` | Re-runs `rescore_all_chemistries` for every active battery chemistry. |
| `rescore-market-scores` | `0 3 * * MON` | Re-runs `score_all_active_materials` for every active material across its derived geographies. |

Why one hour apart: market scoring reads chemistry composites indirectly through criticality signals (and downstream API consumers of both layers expect consistent vintages). Running market scoring after chemistry guarantees the new vintages are committed before market reads them.

Architecture:

- `app/core/inngest.py` initialises the shared `inngest_client`. `is_production` is derived from `Settings.app_env`; anything other than `"production"` (or setting `INNGEST_DEV=1`) boots the SDK in dev mode (no signing key required, talks to a local Inngest Dev Server).
- `app/tasks/scoring_jobs.py` defines both functions. Each opens a fresh `Session` via `get_session_factory()` (workers are concurrent — never share sessions), wraps the synchronous scoring function in `asyncio.to_thread`, and always closes the session in `finally`. Run-ids are stable `cron-<YYYY-MM-DD>` strings.
- `app/main.py` calls `inngest.fast_api.serve(app, inngest_client, SCHEDULED_FUNCTIONS)` to expose the discovery endpoint at `/api/inngest`.

Local dev workflow:

```bash
# Terminal 1: FastAPI (app_env=development → dev mode)
uvicorn app.main:app --reload

# Terminal 2: Inngest Dev Server (auto-discovers /api/inngest)
npx --ignore-scripts=false inngest-cli@latest dev \
    -u http://127.0.0.1:8000/api/inngest --no-discovery
```

Company scoring (six pillar) is **not** scheduled — it is on-demand only, via `bdi-ingest rescore-all` / `bdi-ingest rescore-company` or the API rescore endpoint.

---

## Related reading

- [Overview](overview.md)
- [Data sources](data-sources.md) — what feeds the scoring inputs
- [Parsing & normalization](parsing-and-normalization.md) — event drafts and RiskCategory tags
- [Ingestion pipeline](ingestion-pipeline.md) — `LINK_EVENTS_TO_COMPANIES` gate and CLI rescore commands
- [Reports & AI](reports-and-ai.md) — where scored narratives surface (and how `InsightPost` differs from `ReportInsight`)
- [Seed staleness review](seed-review.md) — keeps `CompanyMaterialExposure`, `Facility`, `CompanySupplyRelationship`, and `CompanyVehicleModel` rows fresh, which directly drives the v3.0 chemistry-aware material pillar, geographic fold-in, and supply-chain-propagation pillar.
