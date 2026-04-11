# Scoring (v2)

The scoring engine is **stateless, pure-function, and transparent**: every component
function returns a raw `float` (0-100) with no database reads. Persistence happens
after computation, never inside the scoring layer. The aggregate result dict includes
a `scoring_version` field so downstream displays can flag score deltas caused by a
methodology change rather than a real-world signal change.

---

## Five-Pillar Architecture

| Pillar | Weight | Module |
|--------|--------|--------|
| Material Concentration | **30%** | `material_risk.py` |
| Geopolitical / Trade | **20%** | `geopolitical_risk.py` |
| Regulatory & Compliance | **20%** | `regulatory_risk.py` |
| Operational | **15%** | `supplier_risk.py` (input) |
| Financial Pressure | **15%** | `financial_pressure.py` |

Previous v1 weights (0.35/0.35/0.15/0.15 across four pillars) are retired.
`scoring_version = "2.0"` is written to every new `supplier_scores` row.

---

## Module layout (`app/services/scoring/`)

| File | Purpose |
|------|---------|
| `types.py` | `ScoreResult` legacy DTO · `SupplierScoreRationale` Pydantic model (rationale_json schema) |
| `event_impact.py` | Core event-impact formula: `compute_event_impact`, `compute_effective_confidence` |
| `decay.py` | Category-specific recency decay → `recency_multiplier` in [0.60, 1.20] |
| `material_risk.py` | Material Concentration Risk: 35/35/30 sub-weights |
| `geopolitical_risk.py` | Geopolitical/Trade Risk: 40/35/25 sub-weights (fifth pillar) |
| `regulatory_risk.py` | Regulatory & Compliance Risk: event rollup + obligation uplift |
| `financial_pressure.py` | Financial Pressure: three bounded sub-components (0-40 / 0-30 / 0-30) |
| `supplier_risk.py` | Five-pillar aggregate → `aggregate_supplier_risk` dict |

---

## Event Impact Formula

All risk events produce a scalar `event_impact` before being aggregated into pillar scores:

```
effective_confidence = max(confidence, 0.60)  if severity >= 0.80  else confidence
event_impact = severity × effective_confidence × recency_multiplier × relevance_multiplier
```

**Confidence floor:** A high-severity signal (`severity ≥ 0.80`) should not be
over-discounted by parser uncertainty. The floor activates only above that threshold
so low-severity noise is not artificially boosted.

All severity and confidence values are on the **0-1.0 scale**. The theoretical
maximum event_impact is `1.0 × 1.0 × 1.20 × 1.30 = 1.56`.

---

## Category-Specific Decay Schedules

`compute_recency_multiplier(category, event_date, as_of_date)` returns a value in
`[0.60, 1.20]`.

| Category | Decay Shape | Window |
|----------|-------------|--------|
| `GEOPOLITICAL_TRADE` | Linear 1.0 → 0.70 over 24 months, then 0.60 | 730 days |
| `REGULATORY_COMPLIANCE` | Step-up 1.10–1.20 within 90 days of enforcement; linear decay otherwise | 365 days |
| `OPERATIONAL` | Exponential, 90-day half-life | 180 days |
| `MATERIAL_CONCENTRATION` | Linear 1.0 → 0.60 over 365 days | 365 days |
| `FINANCIAL_PRESSURE` | Always 1.0 — caller controls window by selecting 4 most recent quarters | None (held) |

Pass `effective_date` for regulatory events approaching their enforcement deadline to
receive the step-up multiplier.

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

Country concentration carries the highest sub-weight because structural geographic
dependency (China ~70-80% of cell production and refining) is the dominant structural
risk for EV battery supply chains.

### Regulatory & Compliance (`score_regulatory_profile`)

Two-part score, capped at 100:

1. **Event-driven rollup (0-60):** average of top-3 `event_impact` values × `policy_proximity_adjustment` × 60.
2. **Obligation uplift (0-40):** additive points for active hard legal obligations:

| Obligation | Uplift points |
|------------|--------------|
| `UFLPA` | 25 |
| `EU_BATTERY_REG` | 20 |
| `IRA_DOMESTIC` | 15 |

UFLPA + EU_BATTERY_REG together would total 45 points, but the obligation uplift
is hard-capped at 40 to keep the two-part structure balanced.

### Financial Pressure (`score_financial_pressure`)

```
raw_score = base_filing_signal (0-40) + leverage_warning_bonus (0-30) + liquidity_stress_bonus (0-30)
```

Components have explicit upper bounds enforced at the function boundary. When
`filing_count < 2`, `raw_score` is scaled by `filing_count / 2.0` to avoid
treating a single filing as a confirmed trend.

---

## `scoring_version` and Score Delta Displays

The `scoring_version` field on `supplier_scores` rows records which methodology
produced each row. When a new version is released:

- New rows will carry the new version string (e.g. `"2.0"`).
- Pre-existing rows retain their original version string (e.g. `"1.0"`, or NULL for
  rows created before versioning was added).
- Presentation-layer score delta displays should suppress or annotate comparisons
  that cross a version boundary — a score change may reflect a methodology change
  rather than a real-world signal change.

---

## `SupplierScoreRationale` (rationale_json contract)

The only permitted writer for `supplier_scores.rationale_json` is:

```python
from app.services.scoring.types import SupplierScoreRationale
row.rationale_json = SupplierScoreRationale(...).model_dump()
```

Never write unstructured dicts to this column. The schema captures:
`inputs` (supplier ID, evidence window, version, run ID), `components` (all five
pillar scores + overall), `top_evidence` (event IDs most influential on the score),
`decay` (eval date + per-pillar windows), and `notes` (human-readable summary).

---

## Relationship to the database

| Table | Intended use |
|-------|--------------|
| `supplier_scores` | Persist `aggregate_supplier_risk` output with `as_of_date` and `scoring_version` |
| `supplier_scores.geopolitical_trade_risk_score` | Fifth pillar added in migration `002_scoring_v2_and_pgvector` |
| `risk_events` | Source of `event_impact` values fed to pillar scorers |
| `regulations` | Source of `active_obligations` keys for `score_regulatory_profile` |
| `document_chunks.embedding` | 1536-dim pgvector column for semantic retrieval (same migration) |

Suggested workflow:

1. Query recent `risk_events` / `regulations` per supplier and geography.
2. Compute `event_impact` values via `compute_event_impact(severity, confidence, recency_multiplier, ...)`.
3. Call each pillar scorer with appropriate inputs.
4. Call `aggregate_supplier_risk(material, geo, regulatory, operational, financial)`.
5. Persist a `supplier_scores` row; write `rationale_json` via `SupplierScoreRationale(...).model_dump()`.

---

## Testing

See `tests/test_scoring.py` for:
- Five-pillar aggregate contract (`test_supplier_aggregate_five_pillars`, `test_supplier_aggregate_all_keys_present`)
- Effective confidence floor (`test_effective_confidence_floor`, `test_effective_confidence_no_floor`)
- Financial pressure sparse-evidence cap (`test_financial_pressure_sparse_evidence`)
- Regulatory obligation uplift (`test_regulatory_obligation_uplift`)

---

## Related reading

- [Overview](overview.md)
- [Parsing & normalization](parsing-and-normalization.md) — event drafts that feed scoring inputs
- [Reports & AI](reports-and-ai.md) — where scored narratives surface
