# HS Code Mapping — Stage-Aware Redesign

> **Status: Draft — pending implementation**  
> **Last updated: May 2026**  
> **Depends on: migrations 001–021 (deprecation audit complete)**  
> **Migrations required: 022–027**  
> **Changelog:** Corrected `primary_producing_countries` column name; fixed PDF import-sources section header; added clarification on global vs US production data; added configurable weights decision; added existing `material_production_shares` context; added guidance on clearing existing scores; added `trade_signal_builder.py` HS stage attribution fix (migration 027, PR 16d); added `material_resolver.py` `_HS_PREFIX_RULES` removal (PR 16e); added Phase 2.5 — migrations 029 (`facility_material_links` stage attribution) and 030 (`commodity_prices` form attribution) — identified as structural gaps during Phase 2 review. **Numbering note (2026-05):** Phase 2.5 uses migrations 029 and 030 as originally planned. Migration 031 (`country_detection_patterns`) was created with `down_revision="028"`; its `down_revision` has been updated to `"030"` so the linear chain is 028→029→030→031.
>
> **Migration 035 (2026-05):** Dropped the `hs_code_material_mappings.{hhi_score, hhi_reference_year, hhi_source}` cache columns originally added by migration 022. The runtime never adopted the cache — `hs_node_scorer` recomputes HHI directly from `hs_code_production_shares` and persists it on `hs_code_geography_risk_scores.hhi_at_stage`. The cache columns were written-only. References to those columns elsewhere in this document describe the original design intent and are retained for historical context; the *current* schema does not include them.

This document describes the architectural redesign of the HS code mapping system. It covers
the motivation, schema changes, new scoring flow, implementation plan, and explicit decisions
made about the existing `materials` table and scoring stack.

---

## Background and Motivation

The current `hs_code_material_mappings` table maps 4-digit HS prefixes to materials with a
confidence score. This was sufficient to bootstrap geographic concentration scoring but has
three structural problems that prevent accurate risk attribution:

**Problem 1 — Supply chain stage is invisible.**  
HS prefix `2606` is bauxite ore. HS prefix `7601` is unwrought aluminum. Both map to
"Aluminum" in the current schema with no distinction. China controls roughly 60% of global
unwrought aluminum production but a much smaller share of bauxite mining. The current model
cannot express this. A concentration score for "Aluminum" is meaningless without knowing
which stage of the supply chain it represents.

**Problem 2 — Country shares are flat on the material row.**  
`materials.primary_producing_countries` is a JSONB array of country codes
(e.g. `["CN", "IN", "RU", "CA"]`) with no per-stage attribution, no production share
percentages, and no year. The `material_production_shares` table (migration 014) adds
share fractions and reference years but is still at the material level — it cannot
express "6% of *ore* is mined in China" versus "60% of *unwrought aluminum* is produced
there." Saying a country contributes X% of a material is ambiguous without knowing which
stage of the supply chain that refers to.

**Problem 3 — HHI and criticality are computed at the wrong granularity.**  
`MaterialCriticalitySignal.hhi_score` is a single HHI value per (material, source, year).
It conflates the production concentration of ore mining with refining and battery-grade
processing into one number. Cobalt has very different HHI profiles at the ore stage
(DRC-dominated, HHI ≈ 0.50+) versus hydroxide/precursor (China-dominated, different
concentration). The scoring engine cannot distinguish them.

**Problem 4 — US HTS codes cannot safely coexist with global HS codes.**  
10-digit HTS codes are US-specific extensions. If they are added alongside 4/6-digit
international HS codes without a `market_scope` field, trade flow queries against UN Comtrade
(which uses international HS nomenclature) will silently fail to match or, worse, apply
US-market data to a global risk score.

---

## Architectural Decisions

### Decision 1: `materials` stays, but is demoted to an identity anchor

The `materials` table is kept but stripped of all fields that vary by trade stage, year, or
processing geography. Its only purpose after this redesign is:

- Canonical name, symbol, CAS number, category
- IRA/EU CRM Act criticality flags
- Battery chemistry membership (via `battery_chemistry_materials`)
- A stable integer primary key for FK relationships throughout the schema

Fields to remove or demote:
- `primary_producing_countries` — **removed**; country share data already lives in
  `material_production_shares` (migration 014) at the material level, and will move to
  `hs_code_production_shares` at the stage level in migration 023.
- `criticality_score` — **retained as a denormalized cache only**, explicitly labeled in the
  schema comment as a derived field updated by `ingest-usgs`. Not used directly by any scorer.
- `patent_occurrence_trend` — **retained as a denormalized cache** (already managed by
  `_sync_patent_trend()` in `chemistry_risk.py`)

Without the identity anchor, the join path `chemistry → material → HS code → country` breaks.
The `materials` table is load-bearing for FK integrity; demoting it is not the same as
removing it.

### Decision 2: Supply chain stage belongs on `hs_code_material_mappings`

Each HS code implicitly encodes a processing stage. This mapping is stable and should be
explicit. A new `supply_chain_stage` enum column is added:

| Stage | Description | Example HS codes |
|---|---|---|
| `ore` | Raw mined ore or concentrate | 2605 (cobalt ore), 2606 (bauxite), 2610 (chromite) |
| `concentrate` | Processed concentrate, before smelting | 2603 (copper concentrate) |
| `intermediate` | Intermediate metallurgical product | 7501 (nickel matte), 8105 (cobalt mattes) |
| `refined` | Unwrought refined metal | 7502 (unwrought nickel), 7601 (unwrought aluminum) |
| `battery_grade` | Battery-grade chemical compound | 2825 (LiOH), 2836.91 (Li carbonate), 2822 (cobalt hydroxide) |
| `fabricated` | Finished or semi-finished product | (future use) |
| `scrap` | Recycled/secondary material | (future use) |

A `stage_sequence` integer (1–6 matching the order above) is added to enable ordered queries
and rollup weighting.

### Decision 3: HHI must be per HS code, not per material

The `hs_code_production_shares` table (new, see below) stores country shares per HS code
mapping per year. HHI is computed from these shares at query time (or pre-computed and cached
on the `hs_code_material_mappings` row as `hhi_score`). The existing
`MaterialCriticalitySignal.hhi_score` becomes a material-level aggregate derived from
stage-level HHIs, not the source of truth.

### Decision 4: Market scope is a first-class column, not a convention

A `market_scope` column is added to `hs_code_material_mappings` with values:
- `global` — 4 or 6-digit international HS codes (WCO/UN Comtrade compatible)
- `us` — 8 or 10-digit US HTS codes (US CBP / USITC)
- `eu` — 8-digit EU Combined Nomenclature codes

**Scoring rule:** Comtrade trade flow queries **only** match `market_scope = 'global'` rows.
US HTS codes are used for tariff exposure scoring and regulatory compliance checks within
the US market scope, not for global geographic concentration calculations. This prevents
US-market data from skewing global HHI values.

### Decision 5: PDF is the authoritative data source; the Excel/CSV is not needed

The uploaded CSV was hand-extracted from MCS 2026 by the business partner. Since we can
parse the PDF directly (see Phase 2), the CSV is only useful as a QA validation dataset to
verify parser output for the materials it covers. It should not be a maintained ingestion
source. The PDF parser becomes the sole ingestion path for USGS-sourced HTS codes and
production share data.

**What the PDF parser covers vs. the manual `_MAPPINGS` list:**

The PDF provides two different datasets that are complementary, not duplicates:

- **Tariff tables** (US HTS codes, 10-digit): one per commodity. These become
  `market_scope = 'us'` rows in `hs_code_material_mappings`. The parser handles this.
- **World mine production and reserves**: country tonnage tables. These feed
  `hs_code_production_shares` at the `ore` or `refined` stage depending on the commodity.
  Note: the MCS production table does not distinguish between HS code stages — it reports
  total world production of a commodity, not by processing stage. Stage assignment is
  inferred from the commodity's typical traded form (e.g. cobalt → ore + mattes).
- **Import sources**: see Decision 6 below for detail.

The manual `_MAPPINGS` list remains required for global 4/6-digit HS codes (Comtrade
compatible), stage assignments, and materials not covered in MCS. The PDF parser is
additive, not a replacement.

### Decision 6: Global production shares and US import sources are separate datasets

These measure fundamentally different things and are stored separately using `market_scope`.

**World mine production shares** (from the MCS "World Mine Production and Reserves" table)
answer the question: what fraction of total *global physical production* of this material
originates in each country? DRC produces ~72% of world cobalt ore. This is a supply-side
geographic concentration measure and is what drives the global HHI. Stored as
`market_scope = 'global'` in `hs_code_production_shares`.

**U.S. import sources** (from the MCS "Import Sources (YYYY–YY):" inline text, where the
year range reflects the data period, e.g. "Import Sources (2021–24):") answer a different
question: of all the cobalt that the *United States imports*, what fraction comes from each
country? This can diverge significantly from global production. The US may import 40% of its
refined cobalt from Finland because Finland processes DRC ore — even though Finland mines
essentially no cobalt. This measures US trade dependency, not global supply concentration.
Stored as `market_scope = 'us'` in `hs_code_production_shares`.

**Scoring rule:** global risk scores (HHI, `material_geography_risk_scores`) only use
`market_scope = 'global'` rows. US import sources feed US-specific tariff exposure and IRA
domestic content analysis. The two are never mixed in the same HHI calculation.

**PDF parser note:** The import sources section header format is
`"Import Sources (YYYY–YY):"` — the year range varies by material and edition. The parser
must match this pattern with a regex (e.g. `r"Import Sources \(\d{4}[–-]\d{2,4}\):"`)
rather than a literal string match.

### Decision 7: Stage rollup weights are configurable per tenant, but persisted scores always use system defaults

Different viewers of the data have legitimate reasons to weight supply chain stages
differently. A battery cell manufacturer buying lithium hydroxide directly cares most about
the `battery_grade` and `refined` stages — the ore stage is multiple procurement steps
removed from their exposure. An OEM is one step further removed. A mining-sector investor
cares primarily about `ore` and `concentrate`. A government policy analyst working on
domestic content rules may care about all stages within their jurisdiction.

**The constraint:** if persisted score rows use different weights per tenant, scores become
incomparable over time. A score of 72 under battery-maker weights is not the same as 72
under default weights. The time series becomes meaningless for tracking "did risk actually
change."

**The rule:** all persisted scores (`hs_code_geography_risk_scores`,
`material_geography_risk_scores`, `material_global_risk_scores`, `chemistry_risk_scores`)
are always computed using system default weights. `methodology_version` on each row encodes
which weight configuration produced it and must stay stable across persisted rows.

**Configurable weights are scoped views only** — never written to the score tables. The
pattern follows the existing `ScoringScope` / `score_company_scoped` never-persist rule.
Implementation:
- Named weight profiles stored in `supply_chain_contexts` per tenant:
  `battery_maker`, `oem`, `miner`, `policy`, `default`
- A request specifying a profile calls the scorer with those weights and returns
  an in-memory result
- The UI toggle "view as battery maker / OEM / miner" is powered by this mechanism

This is a product feature that can be built in Phase 4 without any schema changes —
`ScoringScope` gains an optional `stage_weight_profile: str | None` field.

---

## Relationship to Existing `material_production_shares` Table

Migration 014 (`014_material_production_shares.py`) already created a
`material_production_shares` table that stores country-level production shares at the
**material level** (one row per material × country × year). This table is actively used by
`global_rollup.py` as a fallback weighting source when trade flow coverage is incomplete.

The new `hs_code_production_shares` table (migration 023) is a **stage-level extension**,
not a replacement. The two tables coexist:

| Table | Granularity | Source | Used for |
|---|---|---|---|
| `material_production_shares` | material × country × year | USGS MCS CSV | Fallback weighting in `global_rollup.py`; remains as-is |
| `hs_code_production_shares` | HS code (stage) × country × year | MCS PDF parser | Level 0 stage-specific HHI; new scorer only |

`material_production_shares` is **not removed** in this redesign. It stays as the fallback
path for the existing scoring pipeline. `hs_code_production_shares` is additive. When both
are populated, the stage-level table is preferred for HHI computation; when stage data is
absent, the scorer falls back to the material-level table.

---

## Existing Score Data — What to Keep and What to Clear

Before implementing migrations 022–025, here is the factual state of each affected table
and the recommended action:

| Table | Current state | Recommendation |
|---|---|---|
| `hs_code_material_mappings` | Populated with 4-digit global codes, no stage/scope metadata | Keep rows; migration 022 adds columns; run `seed-hs-mappings --force` after to backfill stage/scope |
| `material_production_shares` | Populated by `ingest-usgs` from USGS MCS CSV | **Keep as-is.** Still used as fallback. Do not clear. |
| `material_criticality_signals` | HHI values at material level, from USGS | **Keep as-is.** Used as fallback. These are not wrong, just imprecise. |
| `material_geography_risk_scores` | Derived scores, computed from above | Keep; append-only pattern means new rescore runs produce new rows that supersede old ones. Old rows remain in time series but are not served as "latest." |
| `material_global_risk_scores` | Derived scores | Same as above — keep, rescore will append newer rows. |
| `chemistry_risk_scores` | Derived scores | Same. |
| `company_scores` | Derived scores | Same. |
| `risk_events` | Source data from ingest | **Do not clear.** These are raw ingested data, not derived. Clearing them would destroy evidence. |
| `trade_flows` | Source data from Comtrade ingest | **Do not clear.** Keep all rows; migration 027 adds `hs_mapping_id` column (NULL for existing rows). Re-running Comtrade ingest after migration 027 will backfill `hs_mapping_id` on new ingest runs but will not update historical rows — this is acceptable. Historical trade flow rows without `hs_mapping_id` continue to produce `risk_event_materials` links without stage attribution, same as current behavior. |

**Recommended sequence before Phase 3 (scoring engine work):**

1. Run migrations 022–027 (schema only; migrations 022–025 add columns/tables, 026 adds
   `risk_event_hs_mappings` and `keywords`, 027 adds `hs_mapping_id` to `trade_flows`)
2. Run `bdi-ingest seed-hs-mappings --force` — repopulates with stage/scope metadata and
   seeds `keywords` per mapping row
3. Run a Comtrade re-ingest (or at minimum the next scheduled Comtrade pull) — this will
   start populating `trade_flows.hs_mapping_id` on new rows; historical rows remain NULL
4. Run MCS PDF parser (Phase 2) — populates `hs_code_production_shares`
5. Run `bdi-ingest full-score` — triggers a full rescore from Level 0 up; new rows append
   to all scoring tables

At this point, all scoring tables contain both pre-redesign rows and post-redesign rows.
The "latest" query pattern (`ORDER BY as_of_date DESC LIMIT 1`) will naturally serve the
new scores. The old rows are retained for time-series continuity.

**If you want a clean slate for a demo or initial launch:** after step 4 above, it is safe
to `TRUNCATE` the four derived score tables (`hs_code_geography_risk_scores`,
`material_geography_risk_scores`, `material_global_risk_scores`, `chemistry_risk_scores`)
and rerun `full-score` once more. This eliminates pre-redesign rows from the time series.
Do not truncate `risk_events`, `material_production_shares`, or `material_criticality_signals`.
Company scores should only be truncated if you have a reason to discard the historical
company risk history.

---

## Schema Changes

### Migration 022 — `hs_code_material_mappings` expansion

Adds columns to the existing table. No rows are dropped; existing data remains valid as
`market_scope = 'global'`, `digit_count = 4`, with `supply_chain_stage` inferred from the
HS prefix and backfilled via a data migration.

```sql
ALTER TABLE hs_code_material_mappings
  ADD COLUMN digit_count        SMALLINT     NOT NULL DEFAULT 4
                                CHECK (digit_count IN (4, 6, 8, 10)),
  ADD COLUMN market_scope       VARCHAR(8)   NOT NULL DEFAULT 'global'
                                CHECK (market_scope IN ('global', 'us', 'eu')),
  ADD COLUMN supply_chain_stage VARCHAR(16)  NULL
                                CHECK (supply_chain_stage IN (
                                  'ore', 'concentrate', 'intermediate',
                                  'refined', 'battery_grade', 'fabricated', 'scrap'
                                )),
  ADD COLUMN stage_sequence     SMALLINT     NULL,    -- 1=ore ... 6=fabricated
  ADD COLUMN hhi_score          FLOAT        NULL,    -- cached Σ(share²) for this HS node
  ADD COLUMN hhi_reference_year SMALLINT     NULL,
  ADD COLUMN hhi_source         VARCHAR(32)  NULL;    -- usgs_mcs | comtrade | manual

-- Drop the existing 2-column unique constraint and replace with one that
-- accounts for market scope (same HS code can exist under 'global' and 'us'
-- with different digit counts).
ALTER TABLE hs_code_material_mappings
  DROP CONSTRAINT uq_hs_material;

ALTER TABLE hs_code_material_mappings
  ADD CONSTRAINT uq_hs_material_scope
  UNIQUE (hs_code_prefix, material_id, market_scope);

-- Index for the most common query pattern: fetch all nodes for a material
-- filtered to a specific market scope.
CREATE INDEX idx_hs_material_scope
  ON hs_code_material_mappings (material_id, market_scope, supply_chain_stage);
```

**ORM changes** (`app/models/supply.py`):

```python
class HsCodeMaterialMapping(Base):
    __tablename__ = "hs_code_material_mappings"
    __table_args__ = (
        UniqueConstraint(
            "hs_code_prefix", "material_id", "market_scope",
            name="uq_hs_material_scope",
        ),
    )

    id: Mapped[int]
    hs_code_prefix: Mapped[str]              # "2604", "260400", "2604000000"
    material_id: Mapped[int]                 # FK → materials.id
    description: Mapped[Optional[str]]
    confidence: Mapped[float]                # 0.0–1.0 prefix specificity
    digit_count: Mapped[int]                 # 4 | 6 | 8 | 10
    market_scope: Mapped[str]               # global | us | eu
    supply_chain_stage: Mapped[Optional[str]]
    stage_sequence: Mapped[Optional[int]]
    hhi_score: Mapped[Optional[float]]       # cached, updated by ingest pipeline
    hhi_reference_year: Mapped[Optional[int]]
    hhi_source: Mapped[Optional[str]]
    created_at: Mapped[datetime]

    # Relationships
    production_shares: Mapped[list["HsCodeProductionShare"]] = relationship(
        back_populates="hs_mapping"
    )
```

### Migration 023 — new `hs_code_production_shares` table

Replaces `materials.top_countries` (which is removed in this migration). Stores country-level
production shares per HS code node, per year, per source.

```sql
CREATE TABLE hs_code_production_shares (
    id                  SERIAL          PRIMARY KEY,
    hs_mapping_id       INTEGER         NOT NULL
                        REFERENCES hs_code_material_mappings (id) ON DELETE CASCADE,
    country_code        CHAR(2)         NOT NULL
                        REFERENCES countries (iso2) ON DELETE RESTRICT,
    production_share    FLOAT           NOT NULL
                        CHECK (production_share >= 0 AND production_share <= 1),
    reference_year      SMALLINT        NOT NULL,
    source              VARCHAR(32)     NOT NULL,  -- usgs_mcs | comtrade | manual
    notes               TEXT            NULL,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    CONSTRAINT uq_hs_production_share
        UNIQUE (hs_mapping_id, country_code, reference_year, source)
);

CREATE INDEX idx_hs_prod_share_mapping
  ON hs_code_production_shares (hs_mapping_id, reference_year);

CREATE INDEX idx_hs_prod_share_country
  ON hs_code_production_shares (country_code, reference_year);

-- Remove the denormalized column from materials.
ALTER TABLE materials DROP COLUMN IF EXISTS top_countries;
```

**ORM** (`app/models/supply.py`):

```python
class HsCodeProductionShare(Base):
    __tablename__ = "hs_code_production_shares"
    __table_args__ = (
        UniqueConstraint(
            "hs_mapping_id", "country_code", "reference_year", "source",
            name="uq_hs_production_share",
        ),
    )

    id: Mapped[int]
    hs_mapping_id: Mapped[int]              # FK → hs_code_material_mappings
    country_code: Mapped[str]               # ISO-2 e.g. "CN"
    production_share: Mapped[float]         # 0–1; shares per hs_mapping_id + year should sum to ≤ 1
    reference_year: Mapped[int]
    source: Mapped[str]
    notes: Mapped[Optional[str]]
    created_at: Mapped[datetime]

    hs_mapping: Mapped["HsCodeMaterialMapping"] = relationship(
        back_populates="production_shares"
    )
```

**HHI computation** is derived on demand from `hs_code_production_shares`:

```python
hhi = sum(share ** 2 for share in shares_for_node_year)
```

The computed value is cached back to `hs_code_material_mappings.hhi_score` after each ingest
run so scorers can read it without re-aggregating every time.

### Migration 024 — new `hs_code_geography_risk_scores` table

This is the new bottom level of the scoring stack. One row per
`(hs_mapping_id, country_code, as_of_date)`, storing stage-specific and country-specific
risk sub-scores. These aggregate up to `material_geography_risk_scores`.

```sql
CREATE TABLE hs_code_geography_risk_scores (
    id                      SERIAL          PRIMARY KEY,
    hs_mapping_id           INTEGER         NOT NULL
                            REFERENCES hs_code_material_mappings (id) ON DELETE CASCADE,
    country_code            CHAR(2)         NOT NULL
                            REFERENCES countries (iso2) ON DELETE RESTRICT,
    as_of_date              DATE            NOT NULL,
    -- Core sub-scores (0–1 each)
    production_share        FLOAT           NULL,    -- share of world production at this stage
    hhi_at_stage            FLOAT           NULL,    -- Σ(share²) for this stage node
    tariff_exposure         FLOAT           NULL,    -- 0–1, from tariff event scoring
    export_restriction      FLOAT           NULL,    -- 0–1, from regulatory event scoring
    composite_node_score    FLOAT           NULL,    -- 0–100, weighted combination
    market_scope            VARCHAR(8)      NOT NULL DEFAULT 'global',
    methodology_version     VARCHAR(8)      NOT NULL DEFAULT '1.0',
    metadata_json           JSONB           NULL,    -- event IDs, weight breakdown, etc.
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT now(),
    CONSTRAINT uq_hs_geo_score
        UNIQUE (hs_mapping_id, country_code, as_of_date, market_scope)
);

CREATE INDEX idx_hs_geo_score_mapping
  ON hs_code_geography_risk_scores (hs_mapping_id, as_of_date DESC);

CREATE INDEX idx_hs_geo_score_country
  ON hs_code_geography_risk_scores (country_code, as_of_date DESC);
```

### Migration 025 — `material_geography_risk_scores` rollup source annotation

Add a column to track how the material-level score was computed when stage-level data is
available versus when the scorer fell back to material-level signals.

```sql
ALTER TABLE material_geography_risk_scores
  ADD COLUMN stage_rollup_count   SMALLINT  NULL,  -- number of HS nodes rolled up
  ADD COLUMN stage_rollup_method  VARCHAR(32) NULL; -- 'stage_weighted' | 'material_fallback'
```

---

## New Scoring Flow

The complete scoring chain from raw trade data to chemistry composite, with the new
HS-code stage layer inserted at the bottom.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Data Sources                                                                  │
│   USGS MCS PDF parser → hs_code_production_shares (per HS node × country)   │
│   UN Comtrade ingest  → trade_flows (existing, 4/6-digit global HS codes)   │
│   Risk event ingest   → risk_events → risk_event_geographies                 │
│   Tariff/regulatory   → regulations, risk_events (tariff subtype)            │
└───────────────────────────────────────┬──────────────────────────────────────┘
                                        │
                    ┌───────────────────▼────────────────────┐
                    │  LEVEL 0 (NEW)                          │
                    │  hs_code_geography_risk_scores          │
                    │  per (hs_mapping_id × country × date)  │
                    │                                         │
                    │  Inputs per node:                       │
                    │  • production_share (from shares table) │
                    │  • hhi_at_stage (Σshare²)               │
                    │  • tariff events scoped to this HS code │
                    │  • export restriction events            │
                    │  • market_scope filter enforced here    │
                    └───────────────────┬────────────────────┘
                                        │ weighted avg across
                                        │ HS nodes (by stage_sequence
                                        │ and trade volume)
                    ┌───────────────────▼────────────────────┐
                    │  LEVEL 1 (EXISTS — feeds from above)   │
                    │  material_geography_risk_scores         │
                    │  per (material × country × date)        │
                    │                                         │
                    │  Five pillars; Material Concentration   │
                    │  pillar now sourced from stage rollup   │
                    │  when available.                        │
                    └───────────────────┬────────────────────┘
                                        │ trade-flow weighted avg
                                        │ across countries
                    ┌───────────────────▼────────────────────┐
                    │  LEVEL 2 (EXISTS)                       │
                    │  material_global_risk_scores            │
                    │  per (material × date)                  │
                    └───────────────────┬────────────────────┘
                                        │ intensity-weighted avg
                                        │ across materials
                    ┌───────────────────▼────────────────────┐
                    │  LEVEL 3 (EXISTS)                       │
                    │  chemistry_risk_scores                  │
                    │  per (chemistry × date)                 │
                    └───────────────────┬────────────────────┘
                                        │ pillar-weighted avg
                                        │ across chemistries
                    ┌───────────────────▼────────────────────┐
                    │  LEVEL 4 (EXISTS)                       │
                    │  company_scores                         │
                    │  per (company × date), six pillars      │
                    └────────────────────────────────────────┘
```

### Rollup weighting at Level 0 → Level 1

When rolling up from `hs_code_geography_risk_scores` into `material_geography_risk_scores`
for the Material Concentration pillar, the stage weights are:

| Stage | Default weight | Rationale |
|---|---:|---|
| `ore` | 0.10 | Least processed; most geographically distributed |
| `concentrate` | 0.15 | Closer to value capture but still commodity |
| `intermediate` | 0.20 | Smelting / metallurgical — significant concentration step |
| `refined` | 0.25 | Dominant traded form; most Comtrade data here |
| `battery_grade` | 0.30 | Most directly relevant to battery supply chain |

**Weight justification:** These weights reflect where supply chain disruptions at each stage
have the most direct impact on battery production. An ore shortage takes years to propagate;
a battery-grade chemical shortage is immediately felt. These weights are stored in a new
`STAGE_ROLLUP_WEIGHTS` dict in `market_aggregator.py` and are configurable.

**Fallback rule:** When fewer than 2 HS nodes exist with stage-level data for a given
`(material × country)`, the scorer falls back to the current `material_geography_risk_scores`
logic (using `MaterialCriticalitySignal.hhi_score`). The fallback is logged and
`stage_rollup_method = 'material_fallback'` is written to the output row.

### Scoring attribution example

For a chemistry risk score on NMC532, the attribution chain is:

```
NMC532 chemistry risk: 58.2
  └─ Cobalt material global score: 71.4  (intensity weight: 0.18)
       └─ Cobalt × DRC geography score: 88.1  (trade-flow weight: 0.72)
            ├─ Cobalt ore (2605) × DRC: prod_share=0.72, hhi=0.54, export_restriction=0.82
            ├─ Cobalt hydroxide (2822) × DRC: prod_share=0.12, hhi=0.31
            └─ Cobalt mattes (8105) × DRC: prod_share=0.09, hhi=0.28
       └─ Cobalt × ZM geography score: 44.2  (trade-flow weight: 0.19)
  └─ Nickel material global score: 52.1   (intensity weight: 0.33)
       └─ ...
```

This chain is storable in `rationale_json` at each level and surfaceable through the API
without recomputing.

---

## MCS PDF Parser Plan

The USGS Mineral Commodity Summaries PDF is the authoritative annual source for:
1. **Tariff tables** — 10-digit HTS codes with descriptions and trade rates per material
2. **Production leaders** — "World Mine Production and Reserves" tables (top countries + share)
3. **Import sources** — "U.S. Import Sources" text or table (top 4 countries + percentages)
4. **Events/trends** — "Salient Statistics", "Events and Trends" narrative text

### Parser structure (`app/services/ingestion/mcs_pdf_parser.py`)

```python
class MCSPdfParser:
    """
    Parses USGS Mineral Commodity Summaries annual PDF.

    Page layout: each commodity occupies 2 pages in consistent order.
    Page offset: document page = PDF page - 4.

    Extraction targets per commodity:
      - tariff_table: list[TariffEntry(description, hts_code, trade_rate)]
      - production_leaders: list[ProductionShare(country_code, share, reference_year)]
      - import_sources: list[ImportSource(country_code, share, reference_year)]
      - salient_notes: str  (raw text from "Salient Statistics" section)
    """
```

**Library:** `pdfplumber` (already a candidate; no new dependencies if added to
`pyproject.toml`). Install: `pip install pdfplumber`.

**On HS digit counts — what the PDF actually contains:**

The MCS tariff tables contain **only 10-digit US HTS codes** in `HHHH.SS.XXXX` format
(e.g. `2605.00.0000`, `2822.00.0010`, `8105.20.3000`). There are no 6-digit international
codes in the PDF.

The parser inserts **two rows per tariff line**: the 10-digit US row and a derived 6-digit
global row. This is not an approximation — the US HTS was built on the international HS by
treaty, so the first 6 numerical digits of any US HTS code *are* the WCO international
subheading. Truncating `2605.00.0000` to `260500` gives the actual WCO code for cobalt ores.

Multiple 10-digit codes can map to the same 6-digit prefix (e.g. `2822.00.0010` and
`2822.00.0090` both truncate to `282200`). The parser uses `ON CONFLICT DO NOTHING` on the
`(hs_code_prefix, material_id, market_scope)` unique constraint, so only the first one
inserts the global row; duplicates are silently skipped.

**The parser does NOT derive 4-digit rows.** The manual `_MAPPINGS` list covers 4-digit codes
with hand-assigned stage and confidence values. Generating 4-digit rows from the parser
would create duplicates and override verified manual metadata.

**Confidence on derived 6-digit rows** is set to the *maximum* confidence of the 10-digit
codes that truncate to it. If any 10-digit code is unambiguous (confidence 1.0), the derived
6-digit row inherits 1.0. This reflects the ceiling of specificity — the 6-digit code is
at least as clean as its cleanest sub-code.

**10-digit codes have equal or higher confidence than their 6-digit counterparts.** Where the
US HTS distinguishes sub-types that the international HS does not (e.g. `2836.91.0010` =
lithium carbonate ≥99.5% purity vs `2836.91.0050` = <99.5% purity, both under international
`283691`), the 10-digit code resolves a grade ambiguity that the 6-digit code leaves open.
This should be reflected in the `confidence` value: if a 6-digit code covers both battery-grade
and technical-grade forms of the same compound, its confidence toward a single material is
lower than the specific 10-digit code for battery-grade alone.

**Parsing strategy per section:**

- *Tariff table:* Located by searching for `"Tariff:"` text anchor then extracting the
  following lines until a `"Depletion Allowance:"` or `"Government Stockpile:"` anchor is
  hit. Format is consistent across all commodities: description text, 10-digit HTS code
  (`XXXX.XX.XXXX`), and rate of duty, tab- or space-separated. Use a regex to extract the
  code rather than column-position parsing, as description text wraps unpredictably across
  lines: `r'\b(\d{4}\.\d{2}\.\d{4})\b'`.

- *Production leaders:* Located by `"World Mine Production and Reserves"` section header.
  Extract the two-column country/quantity table. Derive production shares by dividing each
  country's tonnage by the world total in the same table.

- *Import sources:* Located by the regex pattern `r"Import Sources \(\d{4}[–-]\d{2,4}\):"`.
  The section header format is `"Import Sources (2021–24):"` — the year range is embedded
  and varies by material and edition. Do **not** use a literal string match. Extract the
  inline country/percentage list that follows on the same or next line. Note: these are US
  import fractions, `market_scope = 'us'`. See Decision 6 for context on why these differ
  from world production shares.

- *Events text:* Full text extraction from the "Salient Statistics" and "Events, Trends, and
  Issues" named sections via bounding box detection.

**Material linkage — how HS codes get associated to materials:**

The MCS PDF organizes content by commodity section (e.g. "COBALT", "LITHIUM"). The parser
resolves each commodity name to a `material_id` before processing any tariff or production
data for that section. This is the only mechanism that links extracted HS codes to materials —
there is no inference from the HS codes themselves.

A commodity name normalization map is required at the top of `mcs_pdf_parser.py` because PDF
headings are all-caps and do not always match `materials.canonical_name`:

```python
_MCS_COMMODITY_MAP: dict[str, str] = {
    "COBALT":                   "Cobalt",
    "LITHIUM":                  "Lithium",
    "NICKEL":                   "Nickel",
    "MANGANESE":                "Manganese",
    "GRAPHITE (NATURAL)":       "Natural Graphite",
    "RARE EARTHS":              "Rare Earth Elements",
    "PLATINUM-GROUP METALS":    "Platinum-Group Metals",
    "ALUMINUM":                 "Aluminum",
    "COPPER":                   "Copper",
    "SILICON":                  "Silicon (Anode Grade)",
    "TITANIUM":                 "Titanium",
    "CHROMIUM":                 "Chromium",
    "TUNGSTEN":                 "Tungsten",
    "MOLYBDENUM":               "Molybdenum",
    "VANADIUM":                 "Vanadium",
    "NIOBIUM":                  "Niobium",
    "TANTALUM":                 "Tantalum",
    "TIN":                      "Tin",
    "ZINC":                     "Zinc",
    "BORON":                    "Boron",
    "FLUORSPAR":                "Fluorspar",
    # Add entries as new commodities are covered by MCS
}
```

Commodities not present in `_MCS_COMMODITY_MAP` are skipped with a warning — do not attempt
to infer a material from the HS codes themselves, as the same prefix can appear across multiple
commodities. For multi-material commodities like "RARE EARTHS", write all HS codes to the
parent aggregate material ("Rare Earth Elements"), not to individual REE materials — those
finer-grained mappings are already handled by `seed_hs_mappings.py`.

**Insert order within each commodity section (must follow this sequence):**

1. Insert 10-digit US HTS rows → `hs_code_material_mappings` (`market_scope='us'`, `digit_count=10`)
2. Insert derived 6-digit global rows → `hs_code_material_mappings` (`market_scope='global'`,
   `digit_count=6`, `ON CONFLICT DO NOTHING`)
3. SELECT back `hs_mapping_id` for every inserted or pre-existing row — do NOT assume the ID
   from step 1/2; an `ON CONFLICT DO NOTHING` row that already exists must still have its ID
   retrieved via a follow-up query
4. Insert `hs_code_production_shares` using those IDs (world production → `market_scope='global'`;
   US import sources → `market_scope='us'`)

Steps 1–2 must complete before step 4 because `hs_code_production_shares.hs_mapping_id` is a
non-nullable FK. If the parser attempts to write production shares before the mapping rows
exist, the FK constraint will fail.

**Output seeding targets:**

| Parser output | → Seeds into | Notes |
|---|---|---|
| `tariff_table` (10-digit) | `hs_code_material_mappings`, `market_scope='us'`, `digit_count=10` | One row per tariff line; `material_id` from `_MCS_COMMODITY_MAP` lookup |
| `tariff_table` (derived 6-digit) | `hs_code_material_mappings`, `market_scope='global'`, `digit_count=6` | One row per unique 6-digit prefix; `ON CONFLICT DO NOTHING`; confidence = max of source 10-digit rows |
| `production_leaders` | `hs_code_production_shares`, `market_scope='global'` | Requires step 3 (SELECT back IDs) before insert; linked to the 6-digit global row |
| `import_sources` | `hs_code_production_shares`, `market_scope='us'` | Requires step 3 before insert; linked to the 10-digit US rows; year range from section header |
| `salient_notes` | `MaterialCriticalitySignal.metadata_json` | Appended; does not overwrite existing signals |

---

## Implementation Plan

### Phase 1 — Schema migrations (migrations 022–025)

**PR 12:** Migration 022 — expand `hs_code_material_mappings`

- `digit_count`, `market_scope`, `supply_chain_stage`, `stage_sequence` columns added
- `hhi_score`, `hhi_reference_year`, `hhi_source` columns added
- Unique constraint updated to include `market_scope`
- ORM updated, `__init__.py` exports updated
- Data migration: backfill `market_scope = 'global'`, `digit_count = 4` for all existing rows
- Backfill `supply_chain_stage` and `stage_sequence` for all existing `_MAPPINGS` entries
  via a one-time data migration script

**PR 13:** Migration 023 — `hs_code_production_shares`

- Create new table with FK to `hs_code_material_mappings`
- ORM added to `supply.py`, exported from `__init__.py`
- `ALTER TABLE materials DROP COLUMN primary_producing_countries` (the JSONB array;
  country data now lives in `material_production_shares` and `hs_code_production_shares`)
- `material_production_shares` is **not removed** — it remains as the material-level
  fallback; see "Relationship to Existing `material_production_shares` Table" above
- Update `seed_hs_mappings.py` to populate stage metadata on the new columns

**PR 14:** Migration 024 — `hs_code_geography_risk_scores`

- Create new scoring table
- ORM in `app/models/scoring.py`
- Update `docs/database_architecture.md` schema layers section

**PR 15:** Migration 025 — `material_geography_risk_scores` rollup annotations

- Add `stage_rollup_count` and `stage_rollup_method` columns
- Update `docs/database_architecture.md`

---

### Phase 1.5 — Risk event attribution fixes

These must land before the scoring engine work in Phase 3. The current
`risk_event_materials` table links events to materials only; it has no path to
a specific supply chain stage. These PRs fix both the attribution granularity
and the confidence-weighting gap.

**PR 16a:** Migration 028 — `risk_event_hs_mappings` junction + `hs_code_material_mappings.keywords`

> **Implementation note (2026-05):** This migration landed as **028** (not 026 as originally planned).
> Migrations 026 and 027 were used for earlier schema work (`risk_event_hs_mappings` on 026 was
> superseded; 027 added `hs_mapping_id` to `trade_flows`).  The actual file is
> `alembic/versions/028_hs_event_attribution.py`.

Two changes in one migration:

*New junction table* `risk_event_hs_mappings`:
```sql
CREATE TABLE risk_event_hs_mappings (
    id              SERIAL          PRIMARY KEY,
    risk_event_id   INTEGER         NOT NULL
                    REFERENCES risk_events (id) ON DELETE CASCADE,
    hs_mapping_id   INTEGER         NOT NULL
                    REFERENCES hs_code_material_mappings (id) ON DELETE CASCADE,
    relevance_score FLOAT           NOT NULL DEFAULT 1.0,
    match_reason    VARCHAR(64),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    CONSTRAINT uq_risk_event_hs_mapping
        UNIQUE (risk_event_id, hs_mapping_id)
);
CREATE INDEX idx_rem_event ON risk_event_hs_mappings (risk_event_id);
CREATE INDEX idx_rem_mapping ON risk_event_hs_mappings (hs_mapping_id);
```

This is a separate junction rather than adding a nullable FK to `risk_event_materials`
because one event can affect multiple HS codes for the same material simultaneously
(e.g. a broad export restriction covering both cobalt ore and cobalt hydroxide).
The existing `(risk_event_id, material_id)` unique constraint on `risk_event_materials`
would prevent a second row for the same material; the separate table has no such
constraint between the two material rows.

*New column* on `hs_code_material_mappings`:
```sql
ALTER TABLE hs_code_material_mappings
    ADD COLUMN keywords JSONB NULL
    COMMENT 'Array of compound/trade-name keywords that unambiguously identify
             this specific HS node in free text. E.g. ["lithium hydroxide", "LiOH"]
             for hs_prefix=282520. Used by MaterialCache to build event attribution
             lookups. This IS the source of truth — do not maintain a parallel
             hard-coded list anywhere in the application code.';
```

Example values seeded with this migration:

| hs_prefix | material | keywords |
|---|---|---|
| `282520` | Lithium | `["lithium hydroxide", "LiOH", "lithium hydrate"]` |
| `283691` | Lithium | `["lithium carbonate", "Li2CO3", "battery-grade carbonate"]` |
| `260400` | Lithium | `["spodumene", "lithium brine", "lithium ore"]` |
| `282200` | Cobalt | `["cobalt hydroxide", "cobalt oxide", "cobaltous oxide"]` |
| `283329` | Cobalt | `["cobalt sulfate", "cobaltous sulfate"]` |
| `283699` | Cobalt | `["cobalt carbonate"]` |
| `810520` | Cobalt | `["cobalt metal", "unwrought cobalt", "cobalt mattes"]` |
| `750200` | Nickel | `["unwrought nickel", "nickel metal"]` |
| `750100` | Nickel | `["nickel matte", "nickel oxide sinter"]` |

Keywords should only be added where the term **unambiguously** identifies the specific
HS node — not the parent material broadly. "Lithium" is not a keyword for `282520`
because it matches the material, not the stage. "Lithium hydroxide" IS a keyword for
`282520` because it can only mean that compound.

**PR 16b:** Remove `_MATERIAL_ALIASES` hard-code; replace with DB lookup

`_MATERIAL_ALIASES` in `ingest_federal_register.py` is a hard-coded dict that duplicates
information that will now live in `hs_code_material_mappings.keywords`. It must be
replaced so there is one source of truth.

Changes to `MaterialCache.build(cls, session)` in `ingest_federal_register.py`:

```python
@classmethod
def build(cls, session: Session) -> "MaterialCache":
    """
    Build keyword → (material_id, hs_mapping_id | None, relevance) lookup
    from the database. Single source of truth is hs_code_material_mappings.keywords.

    Precedence (highest relevance wins per material):
      1. hs_code_material_mappings.keywords entries  → relevance 0.90, carries hs_mapping_id
      2. materials.canonical_name                    → relevance 0.85, no hs_mapping_id
      3. materials.symbol_or_code (len >= 2)         → relevance 0.50, no hs_mapping_id
    """
```

The return type of `detect()` changes from `list[tuple[int, float, str]]` to
`list[tuple[int, float, str, int | None]]` — adding `hs_mapping_id` as the fourth
element. Callers that previously ignored the HS mapping ID continue to work; callers
that need stage attribution use the fourth element.

When `detect()` returns a match with a non-None `hs_mapping_id`, the ingester:
1. Inserts `risk_event_materials` as before (material-level link, backward compatible)
2. Also inserts `risk_event_hs_mappings` (stage-level link, new)

The hard-coded `_MATERIAL_ALIASES` dict is deleted entirely.

**PR 16d:** Add `hs_mapping_id` to `trade_flows`; fix `trade_signal_builder.py` stage attribution

`TradeFlow` currently stores `hs_code` (raw string from Comtrade) and `material_id` (FK →
`materials`, resolved at ingest time via `hs_code_material_mappings`). The `hs_mapping_id`
that was found during that lookup is never persisted — it is discarded after `material_id`
is set. When `trade_signal_builder.py` runs later, it aggregates by `material_id` only:

```python
# Current — in _get_annual_totals():
.group_by(TradeFlow.material_id, TradeFlow.reporter_country, TradeFlow.period)
```

This drops all stage and HS specificity. The resulting `RiskEventMaterial` rows carry
`match_reason="hs_code"`, which is technically correct (the attribution used HS code
resolution), but the specific HS node — and with it the supply chain stage — is gone.
Events end up attributed to "Cobalt" rather than "Cobalt hydroxide (2822), battery_grade."

**Migration 027** — add `hs_mapping_id` FK to `trade_flows`:

```sql
ALTER TABLE trade_flows
    ADD COLUMN hs_mapping_id INTEGER NULL
    REFERENCES hs_code_material_mappings (id) ON DELETE SET NULL;

CREATE INDEX idx_trade_flows_hs_mapping
    ON trade_flows (hs_mapping_id)
    WHERE hs_mapping_id IS NOT NULL;
```

`NULL` on existing rows is intentional — historical trade flows resolved before this
column existed remain valid; they just have no stage attribution. New Comtrade ingest
runs will populate `hs_mapping_id` alongside `material_id`.

**ORM change** (`app/models/supply.py`):

```python
class TradeFlow(Base):
    # ... existing columns ...
    hs_mapping_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("hs_code_material_mappings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    hs_mapping: Mapped[Optional["HsCodeMaterialMapping"]] = relationship()
```

**Comtrade ingest change** — when resolving `material_id` at Comtrade ingest time, the
resolver already has the `hs_code_material_mappings` row in hand. Persist its `id` to
`TradeFlow.hs_mapping_id` in the same write:

```python
# In comtrade.py (or wherever trade_flows are inserted):
mapping = session.execute(
    select(HsCodeMaterialMapping)
    .where(HsCodeMaterialMapping.hs_code_prefix == hs_prefix)
    .where(HsCodeMaterialMapping.market_scope == "global")
).scalar_one_or_none()
trade_flow.material_id = mapping.material_id if mapping else None
trade_flow.hs_mapping_id = mapping.id if mapping else None
```

**`trade_signal_builder.py` change** — update `_get_annual_totals()` to group by
`hs_mapping_id` as well, and populate `risk_event_hs_mappings` alongside `risk_event_materials`:

```python
# New — in _get_annual_totals():
.group_by(
    TradeFlow.material_id,
    TradeFlow.hs_mapping_id,      # added
    TradeFlow.reporter_country,
    TradeFlow.period,
)
```

When `hs_mapping_id` is non-NULL on the aggregated row, `_link_material()` additionally
inserts into `risk_event_hs_mappings`:

```python
def _link_material(
    self,
    session: Session,
    risk_event_id: int,
    material_id: int,
    hs_mapping_id: int | None,
    relevance: float,
) -> None:
    # Existing: material-level link (backward compatible)
    session.merge(RiskEventMaterial(
        risk_event_id=risk_event_id,
        material_id=material_id,
        relevance_score=relevance,
        match_reason="hs_code",
    ))
    # New: stage-level link when we have hs_mapping_id
    if hs_mapping_id is not None:
        session.merge(RiskEventHsMapping(
            risk_event_id=risk_event_id,
            hs_mapping_id=hs_mapping_id,
            relevance_score=relevance,
            match_reason="trade_signal",
        ))
```

Trade flows where `hs_mapping_id IS NULL` (historical rows or unresolved codes) continue
to produce only `risk_event_materials` rows, which is the existing behavior.

**PR 16e:** Remove `material_resolver.py` `_HS_PREFIX_RULES` hard-code; replace with DB lookup

`MaterialResolver._HS_PREFIX_RULES` in `app/services/ingestion/normalizers/material_resolver.py`
is another hard-coded list of HS-prefix-to-material-name mappings outside the DB:

```python
_HS_PREFIX_RULES: list[tuple[str, str]] = [
    ("8507", "Lithium-ion battery cells"),
    ("850760", "Lithium-ion battery cells"),
    ("2805", "Lithium chemicals"),
    ("284390", "Rare earth compounds"),
    ("810820", "Unwrought lithium"),
]
```

This is the same structural problem as `_MATERIAL_ALIASES`: it is a second source of truth
that can drift out of sync with `hs_code_material_mappings`. It also returns no `hs_mapping_id`,
so any material resolved via this path cannot be attributed to a stage node.

Replace `resolve_by_hs_code()` with a DB query against `hs_code_material_mappings`, returning
both `material_id` and `hs_mapping_id`:

```python
class MaterialResolver:
    def resolve_by_hs_code(
        self, hs_code: str | None
    ) -> tuple[int | None, int | None]:
        """
        Returns (material_id, hs_mapping_id). Both None if unresolved.
        Queries hs_code_material_mappings using longest-prefix match across
        digit counts (10 → 6 → 4). market_scope='global' only.
        """
        if not hs_code:
            return None, None
        code = str(hs_code).strip().replace(".", "")  # normalize HTS dots
        # Try longest prefix first (10-digit → 6-digit → 4-digit)
        for length in (10, 8, 6, 4):
            prefix = code[:length]
            row = self._db.execute(
                select(HsCodeMaterialMapping)
                .where(HsCodeMaterialMapping.hs_code_prefix == prefix)
                .where(HsCodeMaterialMapping.market_scope == "global")
                .order_by(HsCodeMaterialMapping.confidence.desc())
            ).scalar_one_or_none()
            if row:
                return row.material_id, row.id
        return None, None
```

The hard-coded `_HS_PREFIX_RULES` list is deleted. All HS-to-material resolution paths
now use `hs_code_material_mappings` as the single source of truth.

**PR 16c:** Apply HS mapping `confidence` as relevance multiplier in event impact

Currently `risk_event_materials.relevance_score` is stored at ingestion time and used
directly in scoring. For events where the attribution was via an HS code with
`confidence < 1.0` (e.g. prefix `2615` maps to Nb/Ta/Va/Zr with confidence 0.6),
the impact should be proportionally reduced — a broad prefix match is weaker evidence
than an unambiguous one.

Change in `app/services/scoring/evidence_query.py` — `get_events_for_material()`:

```python
# Current: returns events with raw relevance_score from risk_event_materials
# New: when the event has a risk_event_hs_mappings row for this material's HS nodes,
# effective_relevance = risk_event_materials.relevance_score
#                       × hs_code_material_mappings.confidence
# When no hs_mapping row exists (material-level attribution only), effective_relevance
# = risk_event_materials.relevance_score × 0.85 (discount for unresolved stage)
```

The `0.85` fallback discount for unresolved-stage events is a deliberate conservative
choice: an event attributed to "Cobalt" broadly but not to any specific stage is
probably real but noisier than a stage-specific attribution. The discount prevents
generic material-level events from scoring identically to precise compound-specific ones.

This does not change the `event_impact()` formula structure — `relevance_score` is
still the input. It changes what value is passed in as `relevance_score` when the
query layer resolves it.

---

### Phase 2 — MCS PDF parser

**PR 16:** `app/services/ingestion/mcs_pdf_parser.py`

- Implement `MCSPdfParser` with `pdfplumber`
- Parse tariff tables → seed `hs_code_material_mappings` (US HTS rows)
- Parse production leaders → seed `hs_code_production_shares`
- Parse import sources → seed `hs_code_production_shares` (`market_scope='us'`)
- CLI command: `bdi-ingest ingest-mcs-pdf --path mcs2026.pdf --year 2026`
- Tests: `tests/test_mcs_pdf_parser.py` with sample page extracts as fixtures

**PR 17:** `app/services/ingestion/seed_hs_mappings.py` — update

- Expand `_MAPPINGS` with missing entries identified from MCS 2026:
  - Cobalt: add `2822` (hydroxide), `2833.29` (sulfate)
  - Chromium: add `7202.41/49` (ferrochromium)
  - Lithium: verify 6-digit breakdowns `2825.20`, `2836.91`
- Update `upsert_hs_mappings()` to accept and write `supply_chain_stage`,
  `market_scope`, `digit_count` from the new `_MAPPINGS` tuple shape:
  `(hs_prefix, canonical_name, description, confidence, stage, digit_count, market_scope)`

---

### Phase 2.5 — Stage attribution for facility and price data

These two migrations were identified as structural gaps during Phase 2 review.
Neither is blocking Phase 2, but migration 029 is a prerequisite for accurate
Phase 3 operational risk scoring and should land before `hs_node_scorer.py` is
written. Migration 030 is lower urgency (no automated price scraping exists yet)
but should precede any price-based scoring work.

**PR 21:** Migration 029 — `facility_material_links` stage attribution

`FacilityMaterialLink` currently records only which material a facility handles.
It has no record of which processing stage that facility operates at. The
operational scoring pillar computes `structural_dependency` as
`at_risk_capacity / total_capacity` for a `(material × geography)` pair. Without
stage awareness, a cobalt ore mine shutdown and a Chinese cobalt hydroxide plant
shutdown are treated identically — they both reduce "Cobalt" capacity. In
practice these are different supply chain disruptions with different downstream
timelines and impacts.

```sql
ALTER TABLE facility_material_links
    ADD COLUMN supply_chain_stage  VARCHAR(16)  NULL
        CHECK (supply_chain_stage IN (
            'ore', 'concentrate', 'intermediate',
            'refined', 'battery_grade', 'fabricated', 'scrap'
        )),
    ADD COLUMN hs_mapping_id       INTEGER      NULL
        REFERENCES hs_code_material_mappings (id) ON DELETE SET NULL;

CREATE INDEX idx_fml_hs_mapping
    ON facility_material_links (hs_mapping_id)
    WHERE hs_mapping_id IS NOT NULL;

CREATE INDEX idx_fml_stage
    ON facility_material_links (material_id, supply_chain_stage)
    WHERE supply_chain_stage IS NOT NULL;
```

Both columns are nullable so all GEM-sourced rows that predate this migration
remain valid. New facility records and any backfill work can populate them
incrementally.

**ORM change** (`app/models/facility.py`):

```python
class FacilityMaterialLink(Base):
    # ... existing columns ...
    supply_chain_stage: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
        comment=(
            "ore | concentrate | intermediate | refined | battery_grade. "
            "NULL for GEM rows predating migration 029. Stage determines which "
            "hs_code_geography_risk_scores node this facility's capacity "
            "contributes to in the operational scoring pillar."
        ),
    )
    hs_mapping_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("hs_code_material_mappings.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "FK to hs_code_material_mappings. NULL for historical rows. "
            "When set, links this facility's capacity directly to a stage node "
            "for stage-weighted structural_dependency calculation."
        ),
    )
```

**GEM ingester update** — GEM tags facilities with a facility type field
(`mine`, `smelter/refinery`, `processing plant`, `chemical plant`, etc.) that
maps reasonably cleanly to the stage enum. The ingester should populate
`supply_chain_stage` from this field at ingest time:

| GEM facility type | `supply_chain_stage` |
|---|---|
| `mine` | `ore` |
| `concentration plant` | `concentrate` |
| `smelter` / `refinery` | `intermediate` or `refined` |
| `processing plant` (hydroxide/sulfate/carbonate) | `battery_grade` |
| `recycling` | `scrap` |

GEM does not always provide enough detail to distinguish `intermediate` from
`refined` — when ambiguous, prefer the lower stage (`intermediate`) and note
`hs_mapping_id = NULL` until manually confirmed.

**Scoring engine impact** — `hs_node_scorer.py` (Phase 3, PR 18) should be
written to read `FacilityMaterialLink.supply_chain_stage` when computing
`structural_dependency` for an HS node. Specifically:

```
at_risk_tpy_for_node = Σ capacity WHERE
    facility_material_links.material_id   = this material
    AND facility_material_links.supply_chain_stage = this node's supply_chain_stage
    AND facilities.status IN ('mothballed', 'closed', 'care_maintenance')
    AND facilities.country_code           = this country
```

This makes the operational disruption signal stage-specific rather than
material-aggregate.

---

**PR 22:** Migration 030 — `commodity_prices` form attribution

`CommodityPrice` currently links a price record only to a material. For materials
that trade in multiple chemically distinct forms — lithium (spodumene concentrate,
lithium carbonate, lithium hydroxide monohydrate) and cobalt (LME metal, hydroxide
20.5%, sulfate) being the most relevant examples — a single material-level price
row is ambiguous. LME cobalt and cobalt hydroxide can diverge by 30–40% at times.
Spodumene concentrate and battery-grade lithium hydroxide are different price
benchmarks that drive completely different margin analyses.

```sql
ALTER TABLE commodity_prices
    ADD COLUMN hs_mapping_id  INTEGER      NULL
        REFERENCES hs_code_material_mappings (id) ON DELETE SET NULL,
    ADD COLUMN price_form     VARCHAR(128) NULL;

CREATE INDEX idx_commodity_price_hs_mapping
    ON commodity_prices (hs_mapping_id)
    WHERE hs_mapping_id IS NOT NULL;
```

`hs_mapping_id` is nullable — all historical USGS annual average prices (which
are material-level aggregates) remain valid without a stage assignment.
`price_form` is a free-text benchmark descriptor for display and filtering:
`"LiOH·H2O 56.5% min"`, `"spodumene 6% Li₂O SC"`, `"LME cobalt"`,
`"cobalt hydroxide 20.5% Co"`.

**ORM change** (`app/models/supply.py`):

```python
class CommodityPrice(Base):
    # ... existing columns ...
    hs_mapping_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("hs_code_material_mappings.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "FK to hs_code_material_mappings. NULL for USGS annual averages "
            "and any price record where the specific traded form is not known. "
            "When set, scopes this price to a specific supply chain stage node."
        ),
    )
    price_form: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        comment=(
            "Free-text benchmark descriptor, e.g. 'LiOH·H2O 56.5% min', "
            "'spodumene 6% Li2O', 'LME cobalt', 'cobalt hydroxide 20.5% Co'. "
            "For display and filtering; not a controlled vocabulary."
        ),
    )
```

The unique constraint `uq_commodity_price` is currently `(material_id, price_date, source)`.
Once multiple forms of the same material are tracked, this constraint is too broad —
two LME cobalt prices on the same date from different sources are distinct records.
The constraint should be extended to include `hs_mapping_id` and `price_form`:

```sql
ALTER TABLE commodity_prices
    DROP CONSTRAINT uq_commodity_price;

ALTER TABLE commodity_prices
    ADD CONSTRAINT uq_commodity_price
    UNIQUE (material_id, price_date, source, hs_mapping_id, price_form);
```

Note: because `hs_mapping_id` and `price_form` are nullable, PostgreSQL treats
two rows with `NULL` in either column as distinct for unique constraint purposes
(NULL ≠ NULL). This means historical rows without `hs_mapping_id` do not conflict
with each other, which is the desired behavior.

---

### Phase 3 — Scoring engine updates

**PR 18:** `app/services/scoring/hs_node_scorer.py` (new file)

- `score_hs_node_geography(session, hs_mapping_id, country_code, as_of_date)` — pure scorer
- Computes `hhi_at_stage`, `tariff_exposure`, `export_restriction`, `composite_node_score`
- Writes to `hs_code_geography_risk_scores`
- Unit tests with mock `hs_code_production_shares` data

**PR 19:** `app/services/scoring/market_aggregator.py` — update rollup

- Add `STAGE_ROLLUP_WEIGHTS` dict
- Update `score_material_geography()` to:
  1. Check for `hs_code_geography_risk_scores` rows for this `(material × country)`
  2. If ≥ 2 stage nodes exist: compute stage-weighted rollup for the Material Concentration
     pillar; write `stage_rollup_method = 'stage_weighted'`
  3. If < 2: fall back to existing `MaterialCriticalitySignal`-based path; write
     `stage_rollup_method = 'material_fallback'`
- Update `docs/scoring.md` Material Concentration sub-formula section

**PR 20:** Scheduled job update + `docs/scoring.md`

- Add `rescore-hs-nodes` Inngest function (runs before `rescore-market-scores`)
- Update cron table in `docs/scoring.md`
- Add "Stage-level scoring" section to `docs/scoring.md`

---

### Phase 4 — API exposure (future)

These are not part of the current implementation plan but define what the schema redesign
enables. Tracked here so API design doesn't contradict the schema choices.

- `GET /materials/{id}/supply-chain` — return all HS nodes with production shares by country
  and stage, ordered by `stage_sequence`
- `GET /materials/{id}/risk-by-stage` — return `hs_code_geography_risk_scores` aggregated
  by stage, showing which stage is driving the material's overall risk
- `GET /chemistries/{id}/drill-down` — full attribution chain from chemistry score to
  material → stage → country → risk events
- `ScoringScope` already supports `market_scope` filtering; the UI toggle
  "Global / US / EU view" maps directly to filtering `hs_code_material_mappings.market_scope`

---

## Open Questions

These are unresolved as of this draft and need a decision before Phase 3 work starts:

**OQ-1: Stage rollup weights — are the defaults defensible?**  
The weights (ore=0.10, concentrate=0.15, intermediate=0.20, refined=0.25,
battery_grade=0.30) were chosen to reflect proximity to battery production. They have not
been validated against any empirical disruption data. An alternative is equal weighting
(0.20 each) until there is data to justify differentiation. Equal weighting is less likely
to produce systematically wrong scores.

**OQ-2: What happens to `material_criticality_signals` HHI values once stage HHI exists?**  
Currently `ingest-usgs` writes a single `hhi_score` per material per year. Once
`hs_code_production_shares` is populated, the authoritative HHI lives at the stage level.
Should `material_criticality_signals.hhi_score` become a derived aggregate (e.g. the
production-weighted mean of stage HHIs), or should it remain an independently-sourced value
that can diverge from the stage-level picture? The current plan leaves it independent and
treats it as a fallback only. If they diverge significantly, it is a data quality signal
worth surfacing.

**OQ-3: How are multi-country intermediate processing stages handled?**  
For cobalt: DRC mines ~70% of ore, but ~70% of hydroxide processing happens in China. These
are different countries at different stages. The `hs_code_production_shares` model handles
this correctly — one set of shares for `2605` (ore, DRC-weighted), a different set for `2822`
(hydroxide, China-weighted). However, the rollup from stage scores into a single
material-geography score is non-trivial when the "geography" in question (DRC vs China) varies
by stage. The current design aggregates by `material × country` at Level 1, which means both
DRC and China get separate rows, and their relative weights are determined by trade flow
volume. This is probably correct but needs explicit test coverage.

**OQ-4: What is the minimum viable stage data to replace the current flat HHI?**  
Not every material will have production share data available at every stage from the MCS PDF.
The parser may only recover stage data for the 25–30 materials covered by MCS. Remaining
materials will stay at `material_fallback`. Is this acceptable for v1, or do we need a
minimum coverage threshold before switching the rollup method?

---

## Affected Files Summary

| File | Change |
|---|---|
| `alembic/versions/022_hs_expand.py` | New migration — DDL changes to `hs_code_material_mappings` |
| `alembic/versions/023_hs_production_shares.py` | New migration — `hs_code_production_shares` table |
| `alembic/versions/024_hs_geo_scores.py` | New migration — `hs_code_geography_risk_scores` table |
| `alembic/versions/025_material_geo_rollup_annotations.py` | New migration — rollup columns on `material_geography_risk_scores` |
| `app/models/supply.py` | `HsCodeMaterialMapping` expanded; `HsCodeProductionShare` added |
| `app/models/scoring.py` | `HsCodeGeographyRiskScore` added |
| `app/models/__init__.py` | New ORM exports |
| `alembic/versions/026_risk_event_hs_mappings.py` | New migration — `risk_event_hs_mappings` junction + `keywords` column on `hs_code_material_mappings` |
| `app/models/regulatory.py` | `RiskEventHsMapping` ORM added |
| `app/services/ingestion/seed_hs_mappings.py` | Expanded `_MAPPINGS` tuple shape; `keywords` array seeded per mapping |
| `app/services/ingestion/ingest_federal_register.py` | `_MATERIAL_ALIASES` deleted; `MaterialCache.build()` queries DB; `detect()` returns `hs_mapping_id` |
| `app/services/scoring/evidence_query.py` | `get_events_for_material()` applies confidence multiplier |
| `alembic/versions/027_trade_flows_hs_mapping.py` | New migration — `hs_mapping_id` FK added to `trade_flows` |
| `app/models/supply.py` | `TradeFlow.hs_mapping_id` added |
| `app/services/ingestion/comtrade.py` | Persist `hs_mapping_id` alongside `material_id` at Comtrade ingest |
| `app/services/ingestion/trade_signal_builder.py` | Group by `hs_mapping_id`; populate `risk_event_hs_mappings` when mapping ID is available |
| `app/services/ingestion/normalizers/material_resolver.py` | `_HS_PREFIX_RULES` deleted; `resolve_by_hs_code()` queries DB; returns `(material_id, hs_mapping_id)` |
| `app/services/ingestion/mcs_pdf_parser.py` | New file — PDF parser |
| `app/cli.py` | `ingest-mcs-pdf` command added |
| `tests/test_mcs_pdf_parser.py` | New file — PDF parser unit tests |
| `alembic/versions/029_facility_stage_attribution.py` | New migration — `supply_chain_stage` + `hs_mapping_id` on `facility_material_links` |
| `app/models/facility.py` | `FacilityMaterialLink.supply_chain_stage` + `hs_mapping_id` added |
| `app/services/ingestion/gem.py` | Populate `supply_chain_stage` from GEM facility type at ingest time |
| `alembic/versions/030_commodity_price_form.py` | New migration — `hs_mapping_id` + `price_form` on `commodity_prices`; unique constraint extended |
| `app/models/supply.py` | `CommodityPrice.hs_mapping_id` + `price_form` added |
| `app/services/scoring/hs_node_scorer.py` | New file — Level 0 scorer (reads `supply_chain_stage` from `facility_material_links`) |
| `app/services/scoring/market_aggregator.py` | Stage rollup added; fallback logic |
| `app/cli.py` | `ingest-mcs-pdf` command added (already present — listed for completeness) |
| `docs/scoring.md` | Stage layer documented; cron table updated |
| `docs/database_architecture.md` | Schema layers updated through migration 025 |

---

## Related documents

- [Scoring](scoring.md) — current scoring methodology and weights
- [Database architecture](database_architecture.md) — full schema reference
- [Deprecation audit](deprecation-audit.md) — migrations 001–021 history
- [Data sources](data-sources.md) — source → table ingestion map
