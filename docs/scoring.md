# Scoring

Phase 1 scoring is **transparent and rule-based**: every function returns a **`ScoreResult`** with a numeric **`score`** (clamped 0–100 where applicable) and a **`rationale`** list of human-readable strings. There is **no** automatic call from **`IngestionPipeline`** into scoring today; you invoke these modules from jobs, notebooks, or future report builders when you are ready to populate **`supplier_scores`**.

## Module layout (`app/services/scoring/`)

| File | Purpose |
|------|---------|
| `types.py` | **`ScoreResult`** dataclass |
| `material_risk.py` | Exposure + breadth + critical-mineral bump |
| `regulatory_risk.py` | Recent high-severity events + active regulation count |
| `supplier_risk.py` | Weighted blend of sub-scores (material/regulatory/financial/operational) |
| `macro_context.py` | Placeholder average of caller-supplied indices (demand, infrastructure stress) |

## `ScoreResult`

Defined in **`app/services/scoring/types.py`**:

- **`score: float`** — post-init clamped to **[0, 100]**.
- **`rationale: list[str]`** — bullet-style explanations suitable for analyst review or `supplier_scores.rationale_json`.

## Individual scorers (high level)

### Material risk (`score_material_exposure`)

Inputs include **`exposure_score`**, **`num_distinct_sources`**, **`is_critical_mineral`**.

Logic (simplified):

- Base term scales with exposure (60% weight metaphor).
- **Breadth penalty**: fewer distinct sources → higher risk addition.
- **Critical mineral** adds a fixed bump when relevant.

Use when you have **`supplier_material_exposure`** rows or equivalent estimates.

### Regulatory risk (`score_regulatory_profile`)

Inputs: **`recent_high_severity_events`**, **`active_regulation_count`**.

Caps contributions so scores stay interpretable. Tune multipliers when you calibrate against analyst labels.

### Supplier aggregate (`aggregate_supplier_risk`)

Fixed weights today: **0.35** material, **0.35** regulatory, **0.15** financial, **0.15** operational. Produces a single blended score and explains weights in **`rationale`**.

Swap weights or read them from **`sources.config_json`** / `report_runs.parameters_json` when you formalize report profiles.

### Macro context (`macro_context_score`)

**Phase 1** simply averages **`demand_index`** and **`infrastructure_stress_index`** supplied by the caller. Later, ingest **charging density**, OEM build rates, or grid backlog and compute these indices inside a dedicated service.

## Relationship to the database

| Table | Intended use |
|-------|----------------|
| **`supplier_scores`** | Persist outputs of **`aggregate_supplier_risk`** (and components) with **`as_of_date`** |
| **`risk_events`** | Feeds counts/labels for regulatory/material narrative; not scored automatically yet |
| **`regulations`** | Source for “active regulation” counts |

Suggested workflow (not implemented as a single command):

1. Query recent **`risk_events`** / **`regulations`** per supplier or geography.
2. Derive inputs for **`score_regulatory_profile`** / **`score_material_exposure`**.
3. Call **`aggregate_supplier_risk`**.
4. Insert **`supplier_scores`** with **`rationale_json`** from each **`ScoreResult.rationale`**.

## Testing

See **`tests/test_scoring.py`** for clamping and weight behavior.

## Related reading

- [Overview](overview.md)
- [Parsing & normalization](parsing-and-normalization.md) — event drafts that could feed scoring features
- [Reports & AI](reports-and-ai.md) — where scored narratives may surface
