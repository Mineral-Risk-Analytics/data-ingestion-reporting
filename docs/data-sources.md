# Data Sources

> **Last updated: April 2026**

This document is the single reference for every data source the platform ingests from — current and planned. For each source it records what data it provides, which tables it writes to, and which relationships and scores it contributes to.

---

## Quick-reference table

| Source | Status | CLI command | Primary tables written |
|--------|--------|-------------|----------------------|
| USGS Mineral Commodity Summaries | **Live** | `bdi-ingest ingest-usgs <file>` | `materials`, `material_criticality_signals` |
| World Bank Pink Sheet | **Live** | `bdi-ingest ingest-worldbank` | `commodity_prices` |
| OpenSanctions | **Live** | `bdi-ingest ingest-opensanctions` | `companies`, `company_aliases` |
| UN Comtrade | **Live** | `bdi-ingest ingest-comtrade` | `trade_flows`, `source_documents` |
| Federal Register | **Live** | `bdi-ingest ingest federal-register` | `regulations`, `risk_events`, `source_documents` |
| SEC EDGAR | **Live** | `bdi-ingest ingest sec-edgar` | `risk_events`, `source_documents` |
| U.S. Census Trade | **Live** | `bdi-ingest ingest census-trade` | `trade_flows`, `risk_events`, `source_documents` |
| News (stub) | **Live (stub)** | `bdi-ingest ingest news` | `risk_events`, `source_documents` |
| Manual seed — materials | **Live** | `bdi-ingest seed-materials` | `materials`, `battery_chemistry_materials` |
| Manual seed — HS mappings | **Live** | `bdi-ingest seed-hs-mappings` | `hs_code_material_mappings` |
| EPO PATSTAT | **Planned** | — | `material_criticality_signals` |
| IEA Critical Minerals reports | **Planned** | — | `material_criticality_signals` |
| EU CRM Act assessments | **Planned** | — | `material_criticality_signals`, `materials` |

---

## Current sources

### USGS Mineral Commodity Summaries (MCS)

**Purpose:** Authoritative production and concentration data for critical minerals. The primary source of truth for `materials.criticality_score` and geographic supply concentration.

**File:** `app/services/ingestion/seeds/usgs_mcs_parser.py`
**CLI:** `uv run bdi-ingest ingest-usgs <path/to/MCS2025_World_Data.csv> [--force] [--mcs-year 2025]`

**What it provides:**
- Per-mineral mine/refinery production by country (from `PROD_2023` and `PROD_EST_2024` columns)
- HHI-derived `criticality_score` (Σ(country_share²), 0–1 scale)
- `primary_producing_countries` ranked by production volume
- Static configuration: HS codes, IRA/EU CRM Act flags, `patent_occurrence_trend`, `data_availability`

**Tables written:**

| Table | Operation | Key |
|-------|-----------|-----|
| `materials` | Upsert | `canonical_name` |
| `material_criticality_signals` | Insert/update | `(material_id, source="usgs_mcs", reference_year)` |

**Relationships and scores:**
- `materials.criticality_score` feeds `score_material_exposure()` in `material_risk.py` (Material Concentration pillar, 30% of company score)
- `material_criticality_signals` (source=`usgs_mcs`) is the baseline signal for `score_chemistry()` in `chemistry_risk.py`
- `materials.primary_producing_countries` informs geographic concentration in `geopolitical_risk.py`
- `materials.patent_occurrence_trend` drives `PATENT_TREND_MODIFIERS` (×1.15 rising / ×0.85 declining) in chemistry scoring

**Coverage:** 34 minerals as of MCS 2025. Includes the original 11 core battery minerals plus 23 expansion minerals from the battery chemistry risk layer (Gallium, Germanium, Chromium, Molybdenum, Niobium, Tantalum, Tellurium, Titanium, Zirconium, Iron Ore, Magnesium, Platinum-Group Metals, Tungsten, Indium, Tin, Silver, Fluorspar, Boron, Selenium, Bismuth, Antimony, Zinc, Rhenium).

**Idempotency:** Uses `--force` to upsert. Without `--force`, aborts if `materials` table is non-empty.

**Re-run cadence:** Annually when USGS publishes a new MCS (typically January).

---

### World Bank Pink Sheet

**Purpose:** Monthly commodity price benchmarks for materials with active market pricing.

**File:** `app/services/ingestion/pink_sheet.py`
**CLI:** `uv run bdi-ingest ingest-worldbank [--since-year 1960]`

**What it provides:**
- Monthly spot prices (USD/unit) for lithium, cobalt, nickel, copper, aluminum, manganese, and other traded commodities
- Historical price series back to 1960 (with `--since-year`)

**Tables written:**

| Table | Operation | Key |
|-------|-----------|-----|
| `commodity_prices` | Upsert | `(material_id, price_date, source="world_bank_pink_sheet")` |
| `source_documents` | Upsert | `(source_id, external_id)` |

**Relationships and scores:**
- `commodity_prices` currently informs `trade_volatility` estimates in `derive_material_inputs()` (evidence aggregator)
- Historical price variance is used as a proxy for supply stability in the Material Concentration pillar
- Future: will feed direct price-signal rows to `material_criticality_signals`

**Idempotency:** Unique constraint on `(material_id, price_date, source)` prevents duplicate monthly entries. Re-running with the same file is safe.

**Re-run cadence:** Monthly when the World Bank publishes an updated Pink Sheet.

---

### OpenSanctions

**Purpose:** Sanctions lists, politically exposed persons (PEPs), and adverse media entries. Used to identify sanctioned companies and flag them in entity resolution.

**File:** `app/services/ingestion/opensanctions.py`
**CLI:** `uv run bdi-ingest ingest-opensanctions`

**What it provides:**
- Company names and aliases from OFAC, UN, EU, and other sanctions lists
- Entity type classification (company, person, vessel, etc.)
- Sanctioned status flags

**Tables written:**

| Table | Operation | Key |
|-------|-----------|-----|
| `companies` | Insert (new sanctioned entities) | `canonical_name` |
| `company_aliases` | Insert | `(company_id, alias)` |
| `source_documents` | Upsert | `(source_id, content_hash)` |

**Relationships and scores:**
- Sanctioned company records feed entity resolution — when a `RiskEvent` names a sanctioned entity, the `risk_event_companies` junction is created with an elevated relevance score
- Company `notes` field is populated with sanctions context, surfacing in analyst views
- Future: will contribute to the Regulatory Compliance pillar via `REGULATORY_COMPLIANCE` risk events

**Idempotency:** Deduplication on `content_hash` prevents reprocessing unchanged records.

**Re-run cadence:** Weekly or on-demand for high-risk monitoring periods.

---

### UN Comtrade

**Purpose:** Annual export trade flows by country and HS code. The primary source for geographic concentration data in the chemistry risk scorer.

**File:** `app/services/ingestion/comtrade.py`
**CLI:** `uv run bdi-ingest ingest-comtrade [--years 2021,2022,2023] [--reporters CN,CL,AU] [--hs-prefixes 2604,2602]`

**What it provides:**
- Annual export value (USD) by reporter country × HS code × year
- Partner country breakdown
- Flow direction (export `X` / import `M`)

**Tables written:**

| Table | Operation | Key |
|-------|-----------|-----|
| `trade_flows` | Insert | `(reporter_country, partner_country, hs_code, period, import_export_flag)` |
| `source_documents` | Upsert | `external_id = "comtrade:{reporter}:{hs}:{year}"` |

**Relationships and scores:**
- `trade_flows` is the primary input to `_geo_concentration()` in `chemistry_risk.py` — geographic concentration of exports drives the geopolitical sub-score for each chemistry
- Also feeds `derive_geopolitical_inputs()` in `evidence_aggregator.py` for company-level geopolitical scoring
- `hs_code_material_mappings` bridges raw HS codes in `trade_flows` to `materials.id` for both scoring paths

**HS code scope:** Defaults to all 4-digit prefixes configured in `supply_chain_contexts.relevant_hs_code_prefixes`. Override with `--hs-prefixes`.

**Rate limiting:** `COMTRADE_RATE_LIMIT_DELAY` (default 1.0s) between API calls. Free tier: 500 requests/day.

**Idempotency:** `source_documents.external_id` deduplicates per reporter/HS/year combination.

**Re-run cadence:** Annually. New year data typically available ~3 months after year-end.

---

### Federal Register

**Purpose:** U.S. federal regulations relevant to battery supply chains — UFLPA enforcement, IRA domestic content rules, export controls, trade remedy orders.

**File:** `app/services/ingestion/adapters/federal_register.py` + `app/services/ingestion/pipeline.py`
**CLI:** `uv run bdi-ingest ingest federal-register`

**What it provides:**
- Regulatory document metadata (title, abstract, effective dates, agency, document number)
- Free-text content for embedding and semantic search

**Tables written:**

| Table | Operation | Key |
|-------|-----------|-----|
| `regulations` | Upsert | `external_id = document_number` |
| `risk_events` | Insert | `content_hash` |
| `risk_event_companies` | Insert | `(risk_event_id, company_id)` via entity resolution |
| `source_documents` | Upsert | `(source_id, external_id)` |
| `document_chunks` | Insert | After embedding step |

**Relationships and scores:**
- Produces `REGULATORY_COMPLIANCE` risk events → feeds regulatory scoring pillar (20% of company score)
- `regulation_material_scope` links regulations to materials → used in material exposure queries
- `regulation_geography_scope` links regulations to geographies
- `company_regulation_exposure` rows created by entity resolution → provide obligation uplift points in `score_regulatory_profile()`

**Re-run cadence:** Daily or on API trigger.

---

### SEC EDGAR

**Purpose:** Public company financial filings (10-K, 10-Q, 8-K). Used to detect financial stress, supply chain disclosures, and material risk events.

**File:** `app/services/ingestion/adapters/sec_edgar.py` + pipeline
**CLI:** `uv run bdi-ingest ingest sec-edgar`

**What it provides:**
- Filing metadata per company (type, date, accession number, primary document URL)
- Risk factor and material disclosure text for embedding

**Tables written:**

| Table | Operation | Key |
|-------|-----------|-----|
| `risk_events` | Insert | Per filing, `FINANCIAL_PRESSURE` or `OPERATIONAL` category |
| `risk_event_companies` | Insert | Via entity resolution |
| `source_documents` | Upsert | Accession number as `external_id` |

**Relationships and scores:**
- Produces `FINANCIAL_PRESSURE` events → feeds `derive_financial_inputs()` and `score_financial_pressure()`
- Mentions of supply chain disruption produce `OPERATIONAL` events → `_score_operational()`

---

### U.S. Census Trade Data

**Purpose:** Monthly U.S. import/export trade statistics by HS code and partner country. Used to supplement Comtrade for U.S.-centric analysis and to generate trade concentration risk events.

**File:** `app/services/ingestion/adapters/census_trade.py` + pipeline
**CLI:** `uv run bdi-ingest ingest census-trade`

**Tables written:**

| Table | Operation | Key |
|-------|-----------|-----|
| `trade_flows` | Insert | Monthly grain |
| `risk_events` | Insert | `GEOPOLITICAL_TRADE` or `MATERIAL_CONCENTRATION` |
| `source_documents` | Upsert | Per batch |

**Relationships and scores:**
- `trade_flows` feeds geopolitical concentration analysis
- Trade concentration events feed `score_geopolitical_trade()` (20% of company score)

---

### News (stub)

**Purpose:** News article ingestion. Currently a stub returning synthetic data for testing the full event-resolution-scoring pipeline.

**File:** `app/services/ingestion/adapters/news.py`
**CLI:** `uv run bdi-ingest ingest news`

**Tables written:** `risk_events`, `risk_event_companies`, `source_documents`, `document_chunks`

**Status:** Stub (`StubNewsProvider`). Replace by injecting a `NewsProviderProtocol` implementation (e.g. GDELT, Factiva, Google News API).

---

### Manual seed — materials

**Purpose:** Seed 5 critical materials that are absent from the USGS CSV but are essential for battery chemistry tracking.

**File:** `app/services/ingestion/seed_materials.py`
**CLI:** `uv run bdi-ingest seed-materials`

**Materials seeded:**

| Material | Why not in USGS CSV | Role |
|----------|---------------------|------|
| Neodymium (Nd) | USGS publishes only "Rare Earth Elements" aggregate | NdFeB permanent magnets, EV traction motors |
| Praseodymium (Pr) | Same — co-extracted with Nd | Motor magnets |
| Dysprosium (Dy) | Same | High-temperature magnet performance |
| Terbium (Tb) | Same | High-temperature performance, no public price benchmark |
| Sodium (Na) | Not tracked by USGS MCS | Na-ion battery cathodes |

**Also seeds:** `battery_chemistry_materials` junction table — links all 6 battery chemistries (NMC, LFP, NCA, LFMP, sodium_ion, solid_state) to their constituent materials with intensities and `valid_from` dates.

**Tables written:**

| Table | Operation | Key |
|-------|-----------|-----|
| `materials` | Insert (ON CONFLICT DO NOTHING) | `canonical_name` |
| `battery_chemistry_materials` | Insert (ON CONFLICT DO NOTHING) | `(chemistry_id, material_id, role, valid_from)` |

**Relationships and scores:**
- Neodymium/Dysprosium entries in `battery_chemistry_materials` appear in NMC chemistry scoring
- Sodium entries enable sodium_ion chemistry scoring
- Never overwrites USGS-derived values

---

### Manual seed — HS code mappings

**Purpose:** Seed curated 4-digit HS prefix → material mappings so that Comtrade trade flow data can be attributed to specific materials for concentration scoring.

**File:** `app/services/ingestion/seed_hs_mappings.py`
**CLI:** `uv run bdi-ingest seed-hs-mappings [--force]`

**What it provides:**
- 71 mappings covering 36+ materials × relevant HS chapters
- Three confidence tiers: 1.0 (unambiguous), 0.7–0.9 (primary material, shared prefix), 0.5–0.6 (partial attribution, many-material prefix)
- Source: USGS MCS 2025 + UN Comtrade HS 2022 nomenclature + EU CRM Act 2023 Annex II

**Tables written:**

| Table | Operation | Key |
|-------|-----------|-----|
| `hs_code_material_mappings` | Insert (ON CONFLICT DO NOTHING) | `(hs_code_prefix, material_id)` |

**Relationships and scores:**
- Bridges `trade_flows.hs_code` → `materials.id` in `_geo_concentration()` (chemistry risk scorer)
- Bridges HS prefixes to materials in `MaterialResolver` (entity resolution for trade events)
- `confidence` column gates precision: filter `>= 0.8` for high-precision signals, `>= 0.5` for trend analysis
- Chapter 81 (`8112`) maps to Gallium, Germanium, Indium, Niobium, and Chromium simultaneously — inherent attribution uncertainty until Comtrade ingestion moves to 6-digit codes

**Re-run cadence:** When WCO updates the HS nomenclature (every 5 years) or when new materials are added.

---

## Planned sources

### EPO PATSTAT

**Purpose:** Patent occurrence data for battery-relevant minerals. The primary quantitative source for `patent_occurrence_trend` signals (`rising`/`declining`/`stable`) that adjust criticality scores in the chemistry risk scorer.

**Target table:** `material_criticality_signals` with `source="patstat"`

**What it will provide:**
- Annual patent filings per mineral × technology class
- Country specialization (Revealed Technology Advantage — RTA)
- Trend direction: rising/declining patent occurrence over rolling 5-year windows

**Scoring impact:**
- Writes `trend_direction` to `material_criticality_signals`
- `_sync_patent_trend()` propagates to `materials.patent_occurrence_trend`
- `PATENT_TREND_MODIFIERS` (×1.15 rising / ×0.85 declining) adjust criticality inputs in `score_chemistry()`
- Will upgrade from static config-based trends to data-driven trends per mineral per year

**Implementation path:** EPO PATSTAT offers bulk download (free limited tier) and a paid full-access API. Reference open-source analysis code at `https://github.com/elsanatalia/critical-minerals`.

---

### IEA Critical Minerals Reports

**Purpose:** Annual forward-looking demand projections by mineral and battery technology. Richer than USGS for technology-specific demand curves and supply gap analysis.

**Target table:** `material_criticality_signals` with `source="iea_report"`

**What it will provide:**
- Criticality scores informed by projected demand-supply gaps (more forward-looking than USGS production-based HHI)
- Per-mineral demand projections by technology scenario (Net Zero, Stated Policies)
- Priority ranking for signal resolution — `iea_report` is ranked above `usgs_mcs` in the chemistry risk scorer's source hierarchy

**Scoring impact:**
- Higher-priority signal in `_resolve_criticality_signal()`: `eu_crma` > `iea_report` > `usgs_mcs`
- Will improve accuracy of `material_concentration_score` in `chemistry_risk_scores` for materials where IEA has richer data

**Source:** Free PDF, published annually. IEA 2021, 2023, 2024 editions cover battery minerals. Requires PDF parsing pipeline.

---

### EU Critical Raw Materials Act Assessments

**Purpose:** EU regulatory classification of strategic and critical raw materials. Published every 3 years, with 2023 being the most recent.

**Target tables:** `material_criticality_signals` with `source="eu_crma"`, and updates to `materials.is_eu_crma_critical`

**What it will provide:**
- Highest-priority criticality signals in the scoring hierarchy (`eu_crma` is preferred over all other sources)
- EU-specific supply risk and economic importance scores per material
- List of strategic raw materials triggering EU compliance requirements — directly relevant to customer regulatory exposure

**Scoring impact:**
- Highest-priority signal in `_resolve_criticality_signal()`: `eu_crma` > all others
- `materials.is_eu_crma_critical = True` for listed materials → used in regulatory obligation uplift in `score_regulatory_profile()`

**Source:** EU Regulation 2024/1252 Annex II. PDF and structured data available from European Commission.

---

## How data sources connect to scores

The diagram below shows the full flow from raw data source to final score output:

```mermaid
flowchart TD
    subgraph ingestion [Data ingestion]
        USGS[USGS MCS CSV]
        WB[World Bank Pink Sheet]
        OS[OpenSanctions]
        CT[UN Comtrade API]
        FR[Federal Register API]
        SEC[SEC EDGAR API]
        CEN[U.S. Census Trade API]
        SEED[seed-materials CLI]
        HS[seed-hs-mappings CLI]
    end

    subgraph tables [Database tables]
        MAT[materials]
        MCS[material_criticality_signals]
        CP[commodity_prices]
        TF[trade_flows]
        HSM[hs_code_material_mappings]
        CO[companies / company_aliases]
        RE[risk_events]
        REC[risk_event_companies]
        BCH[battery_chemistries]
        BCM[battery_chemistry_materials]
        CRS[chemistry_risk_scores]
        COMSC[company_scores]
    end

    subgraph scoring [Scoring]
        MATS[score_material_exposure]
        GEOS[score_geopolitical_trade]
        REGS[score_regulatory_profile]
        FINS[score_financial_pressure]
        OPS[_score_operational]
        CHEM[score_chemistry]
        AGG[aggregate_supplier_risk]
    end

    USGS -->|criticality_score, countries| MAT
    USGS -->|source=usgs_mcs| MCS
    WB -->|monthly prices| CP
    OS -->|canonical names, aliases| CO
    CT -->|export values by HS + country| TF
    FR -->|regulations, severity| RE
    SEC -->|filing signals| RE
    CEN -->|trade concentration| TF
    SEED -->|REEs, Sodium| MAT
    SEED -->|intensity weights| BCM
    HS -->|HS prefix → material_id| HSM

    MAT --> MATS
    MCS --> CHEM
    TF --> GEOS
    TF -->|via HSM| CHEM
    HSM --> CHEM
    RE -->|via REC| MATS
    RE -->|via REC| GEOS
    RE -->|via REC| REGS
    RE -->|via REC| FINS
    RE -->|via REC| OPS
    BCM --> CHEM
    BCH --> CHEM

    MATS --> AGG
    GEOS --> AGG
    REGS --> AGG
    FINS --> AGG
    OPS --> AGG
    AGG --> COMSC
    CHEM --> CRS
```

---

## Source hierarchy for criticality signals

The chemistry risk scorer resolves which criticality signal to use for each material using this priority order (highest to lowest):

| Priority | Source | Notes |
|----------|--------|-------|
| 1 | `eu_crma` | EU regulatory assessment — most authoritative for EU compliance context |
| 2 | `iea_report` | Demand-driven, forward-looking |
| 3 | `usgs_mcs` | Production-based HHI — current baseline |
| 4 | `manual` | Hand-entered for materials without other sources |
| 5 | `patstat` | Patent-derived (future) |
| — | `material_column_fallback` | Falls back to `materials.criticality_score` if no signals exist |

Currently only `usgs_mcs` and `manual` signals exist. Higher-priority sources will displace lower-priority ones for covered materials as they are ingested.

---

## Data availability tiers

`materials.data_availability` classifies how reliably each material can be priced and tracked. This directly affects `score_confidence` in chemistry risk scores:

| Tier | Confidence factor | Examples |
|------|------------------|---------|
| `commercial` | 1.00 | Lithium, Nickel, Cobalt, Copper — actively traded on LME or equivalent |
| `limited` | 0.85 | Gallium, Indium, Tellurium — sporadic or opaque pricing |
| `no_benchmark` | 0.65 | Terbium, Rhenium, Germanium — no public price; bilateral contracts only |

`score_confidence = max(0.30, Π confidence_factor_i)` — the product across all materials in the chemistry, floored at 0.3. A chemistry score backed entirely by `no_benchmark` materials is flagged with low confidence.

---

## Related reading

- [Database architecture](database_architecture.md) — full table reference including new tables from migrations 002 and 003
- [Scoring](scoring.md) — five-pillar company scoring and chemistry risk scoring
- [Ingestion pipeline](ingestion-pipeline.md) — step-by-step ingestion execution
- [Overview](overview.md) — system architecture
