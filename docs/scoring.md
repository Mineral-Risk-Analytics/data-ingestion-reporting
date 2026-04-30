# Scoring (v3.0) — market → rollups → company

> **Last updated: April 2026**

This page is the canonical explanation of **how scores are computed**, at every level:

1. **Market pair score**: material × geography (five pillars) → overall  
   (`material_geography_risk_scores`)
2. **Material rollup**: material × {geographies} (five pillars) → overall  
   (`material_global_risk_scores`)
3. **Chemistry rollup**: chemistry × {materials} (five pillars) → composite  
   (`chemistry_risk_scores`, methodology `2.0`)
4. **Company score**: company (six pillars) → overall  
   (`company_scores`, scoring version `"3.0"`)

For “what data feeds which input”, see [Data sources](data-sources.md).

---

## Source of truth (code locations)

- **Company weights + overall aggregation**: `PILLAR_WEIGHTS` and `aggregate_supplier_risk()` in
  [`app/services/scoring/supplier_risk.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/supplier_risk.py)
- **Market weights**: `MARKET_PILLAR_WEIGHTS` and `score_material_geography()` in
  [`app/services/scoring/market_aggregator.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/market_aggregator.py)
- **Material global rollup**: `score_material_global_rollup()` in
  [`app/services/scoring/global_rollup.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/global_rollup.py)
- **Chemistry rollup**: `score_chemistry_from_rollup()` in
  [`app/services/scoring/chemistry_risk.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/chemistry_risk.py)
- **Company orchestrator**: `rescore_company()` in
  [`app/services/scoring/orchestrator.py`](/Users/nicolebush/dev/battery-data-intelligence-engine/app/services/scoring/orchestrator.py)

---

## Pillars and weights (company vs market)

### Company scoring weights (six pillars, v3.0)

Defined in `PILLAR_WEIGHTS`:

| Pillar key | Pillar | Weight |
|---|---|---:|
| `material` | Material Concentration | 0.25 |
| `geopolitical` | Geopolitical / Trade | 0.20 |
| `regulatory` | Regulatory & Compliance | 0.20 |
| `operational` | Operational | 0.10 |
| `financial` | Financial Pressure | 0.10 |
| `supply_chain_propagation` | Supply-Chain Propagation | 0.15 |
| **Total** |  | **1.00** |

**Renormalization rule:** if `supply_chain_propagation` cannot be computed (`None`), the other five weights are renormalized to sum to 1.0 inside `aggregate_supplier_risk()`.

### Market scoring weights (five pillars, renormalized)

The market layer excludes propagation (no company graph) and keeps financial pressure but reframes it with market inputs. `MARKET_PILLAR_WEIGHTS` renormalizes the five company weights (sum \(0.85\)) by dividing each by \(0.85\):

| Pillar | Company weight | Market weight |
|---|---:|---:|
| Material Concentration | 0.25 | \(0.25/0.85 \approx 0.294\) |
| Geopolitical / Trade | 0.20 | \(0.20/0.85 \approx 0.235\) |
| Regulatory & Compliance | 0.20 | \(0.20/0.85 \approx 0.235\) |
| Operational | 0.10 | \(0.10/0.85 \approx 0.118\) |
| Financial Pressure | 0.10 | \(0.10/0.85 \approx 0.118\) |
| **Total** | 0.85 | **1.00** |

---

## Shared primitives: event impact + decay (used wherever events are inputs)

### Event impact formula

In `event_impact.py`, each event produces an `event_impact` scalar before rollup:

```
effective_confidence = max(confidence, 0.60)  if severity >= 0.80  else confidence
event_impact = severity × effective_confidence × recency_multiplier × relevance_multiplier
```

- `relevance_multiplier` comes from `risk_event_companies.relevance_score` **when company-linking is enabled**. When `LINK_EVENTS_TO_COMPANIES=False`, company scoring has less direct company-event evidence available.
- Theoretical max: \(1.0 \times 1.0 \times 1.20 \times 1.30 = 1.56\).

### Evidence windows + decay schedules

`compute_recency_multiplier(category, event_date, as_of_date)` in `decay.py` returns a value in `[0.60, 1.20]` and is paired with category evidence windows:

| Category | Evidence window | Decay shape (high level) |
|---|---:|---|
| `GEOPOLITICAL_TRADE` | 730 days | linear to 0.70 over 24 months, then 0.60 |
| `REGULATORY_COMPLIANCE` | 365 days | step-up near enforcement; decay otherwise |
| `OPERATIONAL` | 180 days | exponential; 90-day half-life |
| `MATERIAL_CONCENTRATION` | 365 days | linear to 0.60 over 365 days |
| `FINANCIAL_PRESSURE` | None | 1.0 (window controlled by filing selection) |

---

## Pillar math (sub-weights, caps, floors)

These are the pillar-level scoring formulas. Each returns a score on `[0, 100]` unless noted.

### Material Concentration

Pure scorer: `score_material_exposure()` in `material_risk.py`.

```
score = (0.35 × criticality + 0.35 × concentration + 0.30 × trade_volatility) × 100
```

### Geopolitical / Trade

Pure scorer: `score_geopolitical_trade()` in `geopolitical_risk.py`.

```
score = (0.40 × country_concentration + 0.35 × export_restriction_exposure + 0.25 × tariff_exposure) × 100
```

### Regulatory & Compliance

Pure scorer: `score_regulatory_profile()` in `regulatory_risk.py`.

- **Event-driven portion (0–60):** average of top-3 event impacts × `policy_proximity_adjustment` × 60
- **Obligation uplift (0–40):** additive points per obligation × weight multiplier, capped at 40

Obligation base points live in `COMPLIANCE_OBLIGATIONS` (examples include `UFLPA=25`, `EU_BATTERY_REG_2023=20`, `IRA_DOMESTIC=15`, plus additional EU obligations).

### Operational

Company-layer operational is computed in the orchestrator (and mirrored in market scoring):

```
score = (0.40 × structural_dependency + 0.60 × avg(weighted_event_impacts)) × 100
```

### Financial Pressure

Pure scorer: `score_financial_pressure()` in `financial_pressure.py`.

```
raw_score = base_filing_signal (0-40) + leverage_warning_bonus (0-30) + liquidity_stress_bonus (0-30)
```

Sparse-evidence rule: when `filing_count < 2`, `raw_score` is scaled by `filing_count / 2.0`.

### Supply-Chain Propagation (company-only)

Pure scorer: `score_propagation()` in `propagation_risk.py`.

```
share_d_w = clamp01(volume_share) × depth_weight(depth)        # default depth weights (1.00, 0.40)
score    = Σ(supplier_overall_score × share_d_w) / Σ(share_d_w)
```

If no scoreable suppliers exist, the pillar score is `None` and overall renormalization applies.

---

## Level 1 — Market pair scoring (material × geography)

**Entry point:** `score_material_geography()` in `market_aggregator.py`  
**Output:** one `MaterialGeographyRiskScore` row per `(material_id, geography_code, as_of_date)`

At this level:
- each pillar is computed with **market-anchored inputs** (criticality signals + price volatility + scoped events + regulation scopes)
- the five pillar scores are aggregated with **`MARKET_PILLAR_WEIGHTS`**

---

## Level 2 — Material rollup scoring (material global)

**Entry point:** `score_material_global_rollup()` in `global_rollup.py`  
**Output:** one `MaterialGlobalRiskScore` per `(material_id, as_of_date)`

Rollup weights resolve per geography (in order):
1. trade-flow export value (preferred)
2. production-share fallback
3. equal-weight fallback

Each pillar is rolled up independently as a weighted average across geographies, then overall is computed using `MARKET_PILLAR_WEIGHTS`.

---

## Level 3 — Chemistry rollup scoring (methodology 2.0)

**Entry point:** `score_chemistry_from_rollup()` in `chemistry_risk.py`  
**Output:** one `ChemistryRiskScore` per `(chemistry_id, as_of_date, methodology_version="2.0")`

For each active row in `battery_chemistry_materials`:
- pull the latest `MaterialGlobalRiskScore` for that material (≤ as_of date)
- intensity-weight pillar scores
- normalize by total intensity
- compute composite as the `MARKET_PILLAR_WEIGHTS` weighted average of the five pillars

Missing global material scores are excluded (and recorded in `metadata_json.materials_missing_global_score`).

---

## Level 4 — Company scoring (six pillars, v3.0)

**Entry point:** `rescore_company()` in `orchestrator.py`  
**Output:** append-only `CompanyScore` rows (`company_scores`)

Flow:
1. Query evidence (events, exposures, obligations, facilities, relationships) via `evidence_query.py`
2. Aggregate to float inputs via `evidence_aggregator.py`
3. Score each pillar using the pure pillar scorers (+ orchestrator operational)
4. Compute propagation (if not skipped / feasible)
5. Aggregate to overall via `aggregate_supplier_risk()`
6. Persist new row with `rationale_json` (`SupplierScoreRationale`)

---

## Current scoring gaps (explicit)

These are “truthy” gaps in the current system, aligned to code and/or known architecture contracts.

1. **Company event relevance is suppressed by default**  
   Ingestion does not write `risk_event_companies` unless `LINK_EVENTS_TO_COMPANIES=True`. This reduces company-layer ability to use per-event relevance multipliers, and shifts emphasis to non-company-linked evidence (exposures, scopes, facilities, seeded relationships).

2. **`supply_chain_contexts.default_pillar_weights` is still v2 (five-pillar) shaped**  
   The table/model describes five pillar weights, but company scoring in code is v3.0 six pillars (`PILLAR_WEIGHTS`). This is a configuration backlog item: either update the schema/config to represent v3.0 or document that the DB config is not the active source of truth for weights.

3. **Chemistry scoring has multiple methodologies in live codepaths**  
   `chemistry_risk.py` contains a v1-style direct scorer and a v2.0 rollup scorer. Scheduled jobs use the rollup path, but some manual/API paths may still call v1 (see `docs/deprecation-audit.md` for the current audit). This can create mixed `methodology_version` rows in `chemistry_risk_scores`.

4. **Rollup fallbacks can lower fidelity when upstream coverage is incomplete**  
   - material rollup can fall back to equal weighting if trade-flow and production-share data are absent for some geographies
   - chemistry rollup excludes materials that lack global material scores
   
   Both behaviors are intentional safety valves but should be monitored as “coverage gaps.”

---

## Scheduled rescores (Inngest)

The market and chemistry layers are kept fresh by weekly cron jobs; company scoring is on-demand.

| Function | Cron (UTC) | What it runs |
|---|---|---|
| `rescore-market-scores` | `0 2 * * MON` | market pair scoring (material × geography) |
| `rescore-global-rollups` | `0 3 * * MON` | material global rollups (from market pair scores) |
| `rescore-all-chemistries` | `0 4 * * MON` | chemistry rollup scoring (from global rollups) |

Local dev: see the Inngest notes in [Overview](overview.md) (will also be merged into `docs/operations.md`).

---

## Related reading

- [Data sources](data-sources.md) — source→input mapping
- [Overview](overview.md) — architecture and scheduling
- [Database architecture](database_architecture.md) — tables and relationships
