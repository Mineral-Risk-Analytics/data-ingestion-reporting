# Database Architecture

## Non-Technical Summary
> **Last updated: April 2026 — reflects migrations 001 (baseline) through 012 (insight posts).** Notable additions: 002 (battery chemistry risk layer), 003 (HS code mappings), 006 (supply-chain rollup tables), 007 (vehicle-model chemistries), 011 (market-level risk scores), 012 (insight posts).
> This section is for anyone who wants to understand what the platform stores and why, without needing to know SQL or software engineering.

The battery supply chain intelligence platform is built around a single question: **how risky is it to rely on a particular company for a critical component of an EV battery?**

To answer that, the database stores and connects five types of information:

**1. Who the companies are.**
The platform tracks companies across the EV battery supply chain — miners, refiners, cell makers, pack assemblers, and OEMs. For each company it stores where they are headquartered, what stage of the supply chain they operate in, whether they are publicly traded, and any alternative names they go by. Companies can also own other companies (parent-subsidiary relationships).

**2. What materials they use.**
Critical minerals like lithium, cobalt, nickel, and graphite power EV batteries. The database tracks which materials each company depends on, how much they depend on them, and where those materials come from. Materials flagged as critical under the US Inflation Reduction Act or the EU Critical Raw Materials Act are specifically tagged.

**3. What risks are happening.**
Every day the system ingests news, government filings, trade data, and regulatory updates. These get turned into structured "risk events" — records that say something like "on this date, in this country, something happened that poses a material supply risk, with this severity." Events are automatically linked to the materials and geographies they affect; linking events directly to specific companies is currently disabled (gated by `LINK_EVENTS_TO_COMPANIES`) and runs through a customer's configured exposure profile instead.

**4. What the rules are.**
The platform tracks major regulations — UFLPA, the EU Battery Regulation, IRA domestic content requirements — and maps which companies are exposed to each one and whether they appear to be compliant.

**5. What the risk scores are.**
All of the above feeds three scoring engines:

- **Company risk scoring** produces a single risk score per company, broken into six pillars: material concentration risk, geopolitical/trade risk, regulatory compliance risk, operational risk, financial pressure, and supply-chain propagation. Scores are append-only so you can track how a company's risk profile changes over time.
- **Battery-chemistry risk scoring** rates each cell chemistry (NMC, LFP, NCA, sodium-ion, etc.) on a composite of material concentration and geopolitical exposure.
- **Market risk scoring** (added April 2026) rates the **material × geography** intersection without any company context. This is the layer that powers the public intelligence hub at `mineralriskanalytics.com`. Output: `material_geography_risk_scores`.

The platform is multi-tenant, meaning different organisations each see their own data and reports. Users belong to a tenant (an organisation account), and usage is tracked so the platform knows who is doing what. Public-facing intelligence-hub articles are stored in the `insight_posts` table — a separate, manually-authored content surface from the per-customer `report_insights` rows.

---

## Technical Overview

The database runs on **PostgreSQL 16** with two extensions:
- **`pgvector`** — enables vector similarity search on document embeddings (1536-dimensional, OpenAI `text-embedding-3-small` compatible)
- **`pgcrypto`** — provides `gen_random_uuid()` for server-side UUID generation

Primary keys are `SERIAL INTEGER` for high-volume append tables (risk events, scores, trade flows) and `UUID` for entity tables where stable global IDs are needed across systems (companies, tenants, users).

All timestamps are stored with timezone (`TIMESTAMPTZ`). Dates without time components use `DATE`. JSONB columns are used for semi-structured payloads (pillar weights, geography lists, scoring rationale) to avoid premature schema rigidity.

---

## Schema Layers

The schema is organised into eleven dependency layers. Tables in later layers reference tables in earlier ones.

```
1. Extensions          pgvector, pgcrypto
2. Platform            tenants
3. Domain config       supply_chain_contexts
4. Entity tables       materials, companies, company_aliases, facilities
                       +  company_vehicle_models         [migration 007]
                       +  vehicle_model_chemistries      [migration 007]
5. Ingestion pipeline  sources, ingestion_runs, raw_api_payloads
6. Document storage    source_documents, document_chunks
7. Risk & regulatory   regulations, risk_events
8. Relationship layer  (10 junction / bridge tables)
                       +  hs_code_material_mappings      [migration 003]
                       +  risk_event_facilities          [migration 006]
9. Scoring             company_scores, material_scores, geography_scores
                       +  material_criticality_signals   [migration 002]
                       +  battery_chemistries            [migration 002]
                       +  battery_chemistry_materials    [migration 002]
                       +  chemistry_risk_scores          [migration 002]
                       +  material_geography_risk_scores [migration 011]
10. Reports & content  report_templates, report_template_focus_entities,
                       report_runs, report_insights, analyst_notes
                       +  insight_posts                  [migration 012]
11. Platform users     users, usage_events
```

---

## Layer 2 — Platform

### `tenants`

One row per customer organisation. All multi-tenant data traces back here.

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | Server-generated via `gen_random_uuid()` |
| `clerk_org_id` | VARCHAR(128) UNIQUE NOT NULL | External ID from Clerk auth provider |
| `name` | VARCHAR(512) NOT NULL | Display name of the organisation |
| `plan` | VARCHAR(64) DEFAULT `'starter'` | Billing plan tier |
| `metadata_json` | JSONB | Arbitrary tenant metadata |
| `created_at` | TIMESTAMPTZ | — |
| `updated_at` | TIMESTAMPTZ | — |

---

## Layer 3 — Domain Configuration

### `supply_chain_contexts`

Externalises constants that govern how scoring and entity resolution behave for a specific supply chain domain. The initial seed row is the EV battery domain. Adding a new row here would allow the platform to support a different domain (e.g. rare earth magnets, solar panels) without code changes.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `slug` | VARCHAR(64) UNIQUE NOT NULL | Machine-readable identifier, e.g. `ev_battery` |
| `name` | VARCHAR(255) NOT NULL | Human-readable name |
| `description` | TEXT | — |
| `default_pillar_weights` | JSONB NOT NULL | Scoring weights by pillar, e.g. `{"material_concentration": 0.30, "geopolitical_trade": 0.20, ...}` |
| `high_concentration_geos` | JSONB NOT NULL | ISO2 country codes treated as high-concentration geographies, e.g. `["CN", "CD", "RU"]` |
| `relevant_hs_code_prefixes` | JSONB NOT NULL | HS code prefixes relevant to this domain, e.g. `["8507", "2825"]` |
| `supply_chain_stages` | JSONB NOT NULL | Valid stage values for `companies.supply_chain_stage`, e.g. `["miner", "refiner", ...]` |
| `is_active` | BOOLEAN DEFAULT true | — |

**Seeded at migration time:**

```json
{
  "slug": "ev_battery",
  "default_pillar_weights": {
    "material_concentration": 0.30,
    "geopolitical_trade": 0.20,
    "regulatory": 0.20,
    "operational": 0.15,
    "financial": 0.15
  },
  "high_concentration_geos": ["CN", "CD", "RU"],
  "relevant_hs_code_prefixes": ["8507", "2825", "2836", "2604", "2602", "2501"],
  "supply_chain_stages": ["miner", "refiner", "cell_maker", "pack_maker", "oem", "trader"]
}
```

---

## Layer 4 — Entity Tables

### `materials`

The critical minerals and compounds tracked by the platform. As of migration 002, covers 39 materials (34 from USGS MCS + 5 seeded manually: Neodymium, Praseodymium, Dysprosium, Terbium, Sodium).

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `canonical_name` | VARCHAR(255) UNIQUE NOT NULL | e.g. `Lithium`, `Cobalt`, `Natural Graphite` |
| `category` | VARCHAR(128) | e.g. `cathode_active`, `anode`, `structural`, `component` |
| `symbol_or_code` | VARCHAR(64) | Chemical symbol or commodity code |
| `hs_codes` | JSONB | 4-digit HS code prefix strings for trade flow matching |
| `criticality_score` | FLOAT | 0–1 normalised HHI from USGS mine production data. Denormalised convenience column — authoritative source is `material_criticality_signals`. |
| `primary_producing_countries` | JSONB | ISO2 country codes ranked by production volume |
| `price_unit` | VARCHAR(20) | e.g. `per_mt`, `per_kg` |
| `is_ira_critical_mineral` | BOOLEAN DEFAULT false | Flagged under US Inflation Reduction Act |
| `is_eu_crma_critical` | BOOLEAN DEFAULT false | Flagged under EU Critical Raw Materials Act 2023 |
| `patent_occurrence_trend` | VARCHAR(16) | `rising` \| `declining` \| `stable` \| NULL. **Denormalized cache** — authoritative source is `material_criticality_signals`. Refreshed by `_sync_patent_trend()` after any signal write. *Added in migration 002.* |
| `data_availability` | VARCHAR(32) | `commercial` \| `limited` \| `no_benchmark`. Used by `score_chemistry()` to compute `score_confidence`. *Added in migration 002.* |
| `notes` | TEXT | Source methodology and production notes |

**Relationships:** one `material` → many `material_criticality_signals`, many `battery_chemistry_materials` (via junction), many `hs_code_material_mappings`, many `trade_flows`, many `commodity_prices`, many `company_material_exposures`.

### `companies`

Every entity in the supply chain — miners, refiners, cell makers, pack assemblers, OEMs, traders. Self-references for parent/subsidiary hierarchy.

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | — |
| `canonical_name` | VARCHAR(512) UNIQUE NOT NULL | Normalised company name for deduplication |
| `legal_name` | VARCHAR(512) | Full registered legal name |
| `supply_chain_stage` | VARCHAR(64) | One of the stages defined in `supply_chain_contexts` |
| `headquarters_country` | CHAR(2) | ISO2 country code |
| `headquarters_region` | VARCHAR(128) | Sub-national region |
| `public_ticker` | VARCHAR(32) | Stock ticker if publicly traded |
| `is_public` | BOOLEAN DEFAULT false | — |
| `duns_number` | VARCHAR(32) | Dun & Bradstreet identifier |
| `lei` | VARCHAR(20) | Legal Entity Identifier (ISO 17442) |
| `parent_company_id` | UUID FK → `companies.id` ON DELETE SET NULL | Self-reference for subsidiary tracking |
| `data_confidence` | FLOAT | 0–1 confidence in entity resolution accuracy |
| `data_source` | VARCHAR(128) | Where this company record originated |
| `notes` | TEXT | — |

### `company_aliases`

Alternative names, ticker symbols, and legacy names for companies. Used by entity resolution to match incoming raw text to a canonical company.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `company_id` | UUID FK → `companies.id` ON DELETE CASCADE | — |
| `alias` | VARCHAR(512) NOT NULL | The alternative name |
| `alias_type` | VARCHAR(64) DEFAULT `'aka'` | e.g. `aka`, `ticker`, `former_name`, `subsidiary` |

**Unique constraint:** `(company_id, alias)`

### `facilities`

Physical locations associated with a company (mines, refineries, gigafactories). Geographic coordinates enable map-based views.

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | — |
| `company_id` | UUID FK → `companies.id` ON DELETE CASCADE | — |
| `facility_type` | VARCHAR(64) NOT NULL | e.g. `mine`, `refinery`, `cell_factory`, `pack_assembly` |
| `country` | CHAR(2) NOT NULL | ISO2 country code |
| `region` | VARCHAR(128) | — |
| `city` | VARCHAR(128) | — |
| `status` | VARCHAR(64) DEFAULT `'operating'` | `operating`, `under_construction`, `suspended`, `closed` |
| `capacity_notes` | TEXT | Production capacity in human-readable form |
| `latitude` / `longitude` | FLOAT | Optional precise coordinates |
| `metadata_json` | JSONB | Additional facility attributes |

---

## Layer 5 — Ingestion Pipeline

### `sources`

Registry of every external data source the platform can ingest from.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `name` | VARCHAR(255) UNIQUE NOT NULL | e.g. `Federal Register`, `SEC EDGAR`, `Census Trade` |
| `source_type` | VARCHAR(64) NOT NULL | `federal_register`, `census_trade`, `sec_edgar`, `news` |
| `phase` | VARCHAR(8) NOT NULL | Implementation phase, e.g. `p1` |
| `is_active` | BOOLEAN DEFAULT true | Inactive sources are skipped by the scheduler |
| `config_json` | JSONB | Adapter-specific configuration (API keys, URL templates, etc.) |

### `ingestion_runs`

Execution log for every ingestion job. Tracks status, duration, statistics, and any error messages. One row per source per run attempt.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `source_id` | INTEGER FK → `sources.id` ON DELETE CASCADE | — |
| `org_id` | UUID FK → `tenants.id` ON DELETE SET NULL | Optional tenant scoping |
| `status` | VARCHAR(32) NOT NULL | `running`, `success`, `failed` |
| `started_at` | TIMESTAMPTZ | — |
| `completed_at` | TIMESTAMPTZ | NULL while running |
| `parameters_json` | JSONB | Runtime parameters passed to the adapter |
| `error_message` | TEXT | Populated on failure |
| `stats_json` | JSONB | Counters: items fetched, written, skipped, etc. |

### `raw_api_payloads`

Immutable record of every raw HTTP response received during ingestion. Enables reprocessing without re-fetching and provides an audit trail. Large response bodies are stored on object storage (R2); the path is stored here.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `ingestion_run_id` | INTEGER FK → `ingestion_runs.id` ON DELETE CASCADE | — |
| `source_id` | INTEGER FK → `sources.id` ON DELETE CASCADE | — |
| `endpoint` | VARCHAR(1024) NOT NULL | Full URL that was called |
| `http_status` | INTEGER | HTTP response code |
| `request_params_json` | JSONB | Query params / request body sent |
| `response_body_path` | VARCHAR(1024) | Path to the raw body in object storage |
| `response_body_text` | TEXT | Inline copy for small payloads |
| `checksum` | VARCHAR(64) | SHA-256 of the response body for deduplication |

---

## Layer 6 — Document Storage

### `source_documents`

One row per unique document ingested — a Federal Register notice, an SEC filing, a news article. Deduplication is enforced by `(source_id, external_id)`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `source_id` | INTEGER FK → `sources.id` ON DELETE CASCADE | — |
| `external_id` | VARCHAR(512) NOT NULL | The document's ID within the source (e.g. Federal Register document number) |
| `title` | VARCHAR(1024) | — |
| `url` | TEXT | Canonical URL |
| `published_at` | TIMESTAMPTZ | Publication/filing date |
| `document_type` | VARCHAR(64) DEFAULT `'unknown'` | `regulation`, `sec_filing`, `news_article`, `trade_record` |
| `raw_storage_path` | VARCHAR(1024) | Path to raw file in object storage |
| `raw_text` | TEXT | Extracted plain text |
| `checksum` | VARCHAR(64) | SHA-256 for deduplication |
| `metadata_json` | JSONB | Source-specific metadata |

**Unique constraint:** `(source_id, external_id)`

### `document_chunks`

Documents are split into overlapping text chunks for vector search. Each chunk stores a 1536-dimensional embedding generated by OpenAI `text-embedding-3-small`. An IVFFlat index (`lists=100`) enables fast approximate nearest-neighbour search.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `source_document_id` | INTEGER FK → `source_documents.id` ON DELETE CASCADE | — |
| `chunk_index` | INTEGER NOT NULL | Sequential position within the document |
| `text` | TEXT NOT NULL | The text content of this chunk |
| `embedding` | VECTOR(1536) | pgvector embedding; indexed with IVFFlat (cosine distance) |
| `embedding_id` | VARCHAR(128) | Optional external embedding ID from an embedding provider |
| `metadata_json` | JSONB | Chunk-level metadata (page number, section, etc.) |

---

## Layer 7 — Regulatory & Risk Events

### `regulations`

Master registry of laws, executive orders, and standards that affect the supply chain. Examples: UFLPA, EU Battery Regulation 2023/1542, IRA Section 45X.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `source_document_id` | INTEGER FK → `source_documents.id` ON DELETE SET NULL | The Federal Register or EUR-Lex document this came from |
| `regulation_key` | VARCHAR(256) UNIQUE NOT NULL | Stable machine-readable key, e.g. `UFLPA`, `EU_BATTERY_REG_2023` |
| `title` | VARCHAR(1024) | Full title |
| `issuing_body` | VARCHAR(512) | e.g. `US CBP`, `European Commission` |
| `geography` | VARCHAR(256) | Primary jurisdiction |
| `policy_theme` | VARCHAR(256) | e.g. `forced_labour`, `domestic_content`, `battery_passport` |
| `status` | VARCHAR(128) | `active`, `proposed`, `superseded` |
| `publication_date` | DATE | — |
| `effective_date` | DATE | When obligations take effect |
| `summary` | TEXT | Human-readable description |
| `metadata_json` | JSONB | — |

### `risk_events`

The core signal table. Each row represents a discrete event that could affect supply chain risk — an export restriction announcement, a mine closure, an SEC going-concern warning, a tariff escalation. Created by the normaliser/classifier during ingestion.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `source_document_id` | INTEGER FK → `source_documents.id` ON DELETE SET NULL | Originating document (nullable — events can be manually created) |
| `event_type` | VARCHAR(128) NOT NULL | Broad category, e.g. `supply_disruption`, `policy_change`, `financial_stress` |
| `event_date` | TIMESTAMPTZ | When the event occurred (may differ from fetch date) |
| `title` | VARCHAR(1024) NOT NULL | — |
| `summary` | TEXT | — |
| `severity_score` | FLOAT | 0–1 signal of how severe this event is |
| `confidence_score` | FLOAT | 0–1 confidence that the classification is correct |
| `risk_categories_json` | JSONB | List of pillar category tags, e.g. `["geopolitical_trade", "material_concentration"]` |
| `geography_json` | JSONB | ISO2 country codes affected |
| `content_hash` | VARCHAR(64) | Deduplication hash of title + summary |
| `metadata_json` | JSONB | Subtype flags, effective dates, HS codes, etc. |

---

## Layer 8 — Relationship Layer

This layer contains ten junction tables that connect the entity and event tables. Rather than embedding foreign keys everywhere, all many-to-many relationships are expressed here with explicit confidence/relevance scores.

### `company_material_exposures`

Which materials does each company depend on, at which stage, and from which country?

| Column | Type | Notes |
|--------|------|-------|
| `company_id` | UUID FK → `companies.id` ON DELETE CASCADE | — |
| `material_id` | INTEGER FK → `materials.id` ON DELETE CASCADE | — |
| `supply_chain_stage` | VARCHAR(64) NOT NULL | The stage at which this company uses the material |
| `exposure_score` | FLOAT NOT NULL | 0–100 dependency intensity |
| `source_geography` | CHAR(2) | ISO2 country where the material is sourced |
| `data_confidence` | FLOAT | 0–1 confidence in this exposure record |
| `as_of_date` | DATE | When this exposure was assessed |

**Unique constraint:** `(company_id, material_id, supply_chain_stage)`

### `company_supply_relationships`

Direct supply relationships between companies — who buys from whom, for which material.

| Column | Type | Notes |
|--------|------|-------|
| `buyer_id` | UUID FK → `companies.id` ON DELETE CASCADE | — |
| `supplier_id` | UUID FK → `companies.id` ON DELETE CASCADE | — |
| `material_id` | INTEGER FK → `materials.id` ON DELETE SET NULL | Optional: which material this relationship is for |
| `relationship_type` | VARCHAR(64) DEFAULT `'direct'` | `direct`, `indirect`, `inferred` |
| `data_confidence` | FLOAT | 0–1 confidence |
| `valid_from` / `valid_to` | DATE | Known validity period of the relationship |

**Unique constraint:** `(buyer_id, supplier_id, material_id)`

### `regulation_material_scope`

Which regulations apply to which materials?

| Columns | Notes |
|---------|-------|
| `regulation_id → regulations.id` | — |
| `material_id → materials.id` | — |
| `scope_type` | `covered`, `exempt`, `proposed` |

### `regulation_geography_scope`

Which countries does each regulation apply to or originate from?

| Columns | Notes |
|---------|-------|
| `regulation_id → regulations.id` | — |
| `country_code` | ISO2 |
| `scope_type` | `jurisdiction`, `targeted`, `origin` |

### `company_regulation_exposure`

Is a given company exposed to a given regulation, and what is its compliance status?

| Columns | Notes |
|---------|-------|
| `company_id → companies.id` | — |
| `regulation_id → regulations.id` | — |
| `compliance_status` | `compliant`, `non_compliant`, `at_risk`, `unknown` |
| `exposure_reason` | Free text explanation |
| `assessed_at` | DATE |

### `hs_code_material_mappings`

Lookup table mapping HS code prefixes to materials. Used during trade flow ingestion to automatically identify which material a shipment relates to.

| Columns | Notes |
|---------|-------|
| `hs_code_prefix → materials.id` | e.g. `8507` → Lithium-ion batteries |
| `confidence` | 0–1, defaults to 1.0 for exact matches |

### `risk_event_companies`

Which companies is a given risk event relevant to, and how relevant?

| Columns | Notes |
|---------|-------|
| `risk_event_id → risk_events.id` ON DELETE CASCADE | — |
| `company_id → companies.id` ON DELETE CASCADE | — |
| `relevance_score` | FLOAT — fed into `relevance_multiplier` in the scoring formula |
| `match_reason` | `named`, `geography`, `material`, `category_broad` |

**Unique constraint:** `(risk_event_id, company_id)`

> **Phase 3 (April 2026):** Writes to this table are gated by `LINK_EVENTS_TO_COMPANIES` in `app/services/ingestion/feature_flags.py` (default `False`). The ingestion pipeline still resolves event ↔ company relevance but no longer materialises the rows; suppressed counts are emitted as a structured WARNING. The table, ORM model, and `persist_company_links` helper are intentionally retained for Phase 5 when company exposure profiles will drive relevance.

### `risk_event_materials`

Which materials is a given risk event relevant to?

| Columns | Notes |
|---------|-------|
| `risk_event_id → risk_events.id` | — |
| `material_id → materials.id` | — |
| `relevance_score` | FLOAT |
| `match_reason` | VARCHAR(64) |

### `risk_event_regulations`

Links risk events that discuss a specific regulation to that regulation's master record.

| Columns | Notes |
|---------|-------|
| `risk_event_id → risk_events.id` | — |
| `regulation_id → regulations.id` | — |
| `relevance_score` | FLOAT |

### `risk_event_geographies`

Which countries is a risk event relevant to, beyond the broad `geography_json` JSONB field? Enables country-level risk aggregation.

| Columns | Notes |
|---------|-------|
| `risk_event_id → risk_events.id` | — |
| `country_code` | CHAR(2) |
| `geography_context` | `primary`, `secondary`, `supply_origin` |

### `trade_flows`

Raw international trade statistics ingested from sources like US Census trade data. One row per `(period, reporter, partner, hs_code, import/export)` observation.

| Column | Type | Notes |
|--------|------|-------|
| `source_document_id` | INTEGER FK → `source_documents.id` | — |
| `period` | VARCHAR(16) | e.g. `2024-Q3`, `2024-11` |
| `reporter_country` | VARCHAR(8) | Reporting country (ISO2/ISO3) |
| `partner_country` | VARCHAR(8) | Trade partner (ISO2/ISO3) |
| `hs_code` | VARCHAR(32) | Harmonised System code |
| `material_id` | INTEGER FK → `materials.id` ON DELETE SET NULL | Set by HS code lookup |
| `import_export_flag` | VARCHAR(16) | `import` or `export` |
| `trade_value_usd` | FLOAT | USD value |

### `commodity_prices`

Historical spot prices for tracked materials. One row per `(material, date, source)`.

| Column | Type | Notes |
|--------|------|-------|
| `material_id` | INTEGER FK → `materials.id` ON DELETE CASCADE | — |
| `price_date` | DATE NOT NULL | — |
| `price_usd` | FLOAT NOT NULL | — |
| `price_unit` | VARCHAR(20) NOT NULL | e.g. `USD/t`, `USD/lb` |
| `source` | VARCHAR(128) NOT NULL | Data provider name |

**Unique constraint:** `(material_id, price_date, source)`

---

## Layer 9 — Scoring

Scores are always appended — never updated in place. This preserves a full time-series of how risk profiles evolve. Scores exist at three levels of granularity.

### `company_scores`

Risk score for a single company at a single point in time. Generated by the scoring orchestrator on demand (CLI / API). Phase 3 (April 2026) removed the post-ingestion auto-rescore — refresh now happens via `bdi-ingest rescore-all`, `rescore-company`, or `POST /api/v1/companies/{id}/rescore`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `company_id` | UUID FK → `companies.id` ON DELETE CASCADE | — |
| `as_of_date` | DATE NOT NULL | The date this score reflects |
| `material_concentration_risk_score` | FLOAT | 0–100, weight **25%** (v3.0) |
| `geopolitical_trade_risk_score` | FLOAT | 0–100, weight **20%** |
| `regulatory_risk_score` | FLOAT | 0–100, weight **20%** |
| `operational_risk_score` | FLOAT | 0–100, weight **10%** |
| `financial_pressure_score` | FLOAT | 0–100, weight **10%** |
| `supply_chain_propagation_score` | FLOAT NULL | 0–100, weight **15%** — sixth pillar added in migration 006. Nullable; when null, weights renormalise. |
| `propagation_depth_used` | INTEGER NULL | BFS depth that produced the propagation pillar |
| `overall_risk_score` | FLOAT | Weighted aggregate |
| `rationale_json` | JSONB | Full `SupplierScoreRationale` — inputs, component scores, top evidence event IDs, decay parameters, propagation chain, chemistry mix, signals_used, notes |
| `scoring_version` | VARCHAR(32) DEFAULT `'3.0'` | Formula version for reproducibility |

### `material_scores`

Aggregate risk score for a critical material across all companies that handle it. Used for portfolio-level views.

| Column | Type | Notes |
|--------|------|-------|
| `material_id` | INTEGER FK → `materials.id` ON DELETE CASCADE | — |
| `as_of_date` | DATE NOT NULL | — |
| `material_concentration_score` | FLOAT | — |
| `geopolitical_trade_score` | FLOAT | — |
| `regulatory_compliance_score` | FLOAT | — |
| `operational_score` | FLOAT | — |
| `financial_pressure_score` | FLOAT | — |
| `overall_risk_score` | FLOAT | — |
| `company_count` | INTEGER | Number of companies contributing to this score |
| `event_count` | INTEGER | Number of risk events factored in |

### `geography_scores`

Country-level risk roll-up. Supports heat-map views and geopolitical exposure summaries.

| Column | Type | Notes |
|--------|------|-------|
| `geography_code` | CHAR(2) NOT NULL | ISO2 country code |
| `as_of_date` | DATE NOT NULL | — |
| `geopolitical_trade_score` | FLOAT | — |
| `regulatory_compliance_score` | FLOAT | — |
| `operational_score` | FLOAT | — |
| `overall_risk_score` | FLOAT | — |
| `company_count` | INTEGER | — |
| `event_count` | INTEGER | — |

---

## Layer 10 — Reports

### `report_templates`

Configures how a report should be structured. Can be a platform-level template (shared) or an org-specific override.

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | — |
| `org_id` | UUID FK → `tenants.id` ON DELETE CASCADE | NULL for platform templates |
| `name` | VARCHAR(255) NOT NULL | — |
| `focus_type` | VARCHAR(64) NOT NULL | `company`, `material`, `geography`, `portfolio` |
| `audience_type` | VARCHAR(64) NOT NULL | `executive`, `analyst`, `procurement` |
| `is_platform_template` | BOOLEAN | Shared across all tenants if true |
| `weight_overrides` | JSONB | Optional pillar weight overrides for this template |
| `sections_config` | JSONB | Ordered list of sections to include |

### `report_template_focus_entities`

Which specific entities (companies, materials, geographies) a report template is focused on.

| Column | Type | Notes |
|--------|------|-------|
| `template_id` | UUID FK → `report_templates.id` ON DELETE CASCADE | — |
| `entity_type` | VARCHAR(64) | `company`, `material`, `geography` |
| `entity_id` | VARCHAR(128) | The UUID or integer ID of the entity |

### `report_runs`

Each time a report is generated, a row is inserted here. Tracks status and links back to the template.

| Column | Type | Notes |
|--------|------|-------|
| `org_id` | UUID FK → `tenants.id` ON DELETE SET NULL | Which tenant triggered this |
| `template_id` | UUID FK → `report_templates.id` ON DELETE SET NULL | — |
| `report_type` | VARCHAR(64) | Mirrors `focus_type` from the template |
| `audience_type` | VARCHAR(64) | — |
| `as_of_date` | DATE | The date the report reflects |
| `status` | VARCHAR(32) DEFAULT `'queued'` | `queued`, `running`, `complete`, `failed` |

### `report_insights`

Individual findings written into the report. Each insight optionally links to a company, material, regulation, or geography.

| Column | Type | Notes |
|--------|------|-------|
| `report_run_id` | INTEGER FK → `report_runs.id` ON DELETE CASCADE | — |
| `insight_type` | VARCHAR(64) | e.g. `risk_alert`, `trend`, `recommendation` |
| `title` | VARCHAR(512) | — |
| `body` | TEXT | The written insight content |
| `related_company_id` | UUID FK → `companies.id` ON DELETE SET NULL | — |
| `related_material_id` | INTEGER FK → `materials.id` ON DELETE SET NULL | — |
| `related_regulation_id` | INTEGER FK → `regulations.id` ON DELETE SET NULL | — |
| `related_geography_code` | CHAR(2) | — |
| `sort_order` | INTEGER DEFAULT 0 | Display order within the report |

### `analyst_notes`

Free-text annotations that analysts can attach to any entity (company, material, regulation, geography, HS-mapping row, risk event, facility, chemistry) using a generic `(entity_type, entity_id)` composite reference rather than hard foreign keys. Phase 2 widened the `entity_type` enum to support reference-data flag-issue dialogs across the full admin UI.

| Column | Type | Notes |
|--------|------|-------|
| `entity_type` | VARCHAR(64) | `company` \| `material` \| `hs_material_mapping` \| `regulation` \| `risk_event` \| `facility` \| `battery_chemistry` |
| `entity_id` | VARCHAR(128) | The ID of the target entity (string to accommodate both UUIDs and integers) |
| `note_type` | VARCHAR(64) | `data_error` \| `missing_data` \| `outdated` \| `other` |
| `note_text` | TEXT | — |

### `insight_posts` (migration 012)

Manually authored content for the public-facing intelligence hub at `mineralriskanalytics.com`. **Distinct from `report_insights`**, which are auto-generated findings inside per-customer report runs. The taxonomy mirrors the scoring engine dimensions exactly so future "related intelligence" surfacing alongside company and market scores is structurally possible without schema changes.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `slug` | VARCHAR(255) UNIQUE NOT NULL | URL-safe identifier, e.g. `ira-feoc-rules-cobalt-2025` |
| `title` | VARCHAR(512) NOT NULL | — |
| `content_type` | VARCHAR(16) NOT NULL | `analysis` \| `signal` \| `report` \| `news` |
| `pillar` | VARCHAR(64) | `material_concentration` \| `geopolitical_trade` \| `regulatory_compliance` \| `operational` \| `financial_pressure`. NULL when a post spans pillars. |
| `materials` | TEXT[] | Plain string array of material canonical names, e.g. `['Lithium','Cobalt']`. Not an FK so authors can tag content before a referenced material is fully seeded. |
| `geographies` | TEXT[] | Plain string array of ISO2 country codes |
| `summary` | TEXT | Short description shown in the feed list (1–3 sentences) |
| `body` | TEXT | Full article content in Markdown |
| `pdf_url` | VARCHAR(1024) | Cloudflare R2 URL for `report`-type PDFs |
| `read_time_minutes` | INTEGER | Estimated read time, shown in feed. NULL for `signal`/`news` types. |
| `author` | VARCHAR(255) | — |
| `status` | VARCHAR(16) NOT NULL DEFAULT `'draft'` | `draft` → `published` → `archived`. Soft-delete via `archived` so published URLs remain resolvable (301 redirect). |
| `published_at` | TIMESTAMPTZ | Set when status transitions to `published`. Used for feed ordering. |
| `metadata_json` | JSONB | Freeform metadata: `external_url` for `news` type, featured flag, etc. |
| `created_at` / `updated_at` | TIMESTAMPTZ | — |

**Indexes:** `slug` (unique), `content_type`, `pillar`, `status`, `published_at`.

---

## Layer 11 — Platform Users

### `users`

Platform users authenticated via Clerk. Each user belongs to exactly one tenant.

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | — |
| `tenant_id` | UUID FK → `tenants.id` ON DELETE CASCADE | — |
| `clerk_user_id` | VARCHAR(128) UNIQUE NOT NULL | External Clerk user ID |
| `email` | VARCHAR(512) | — |
| `role` | VARCHAR(64) DEFAULT `'member'` | `owner`, `admin`, `member`, `viewer` |

### `usage_events`

Append-only event log of user actions within the platform. Used for billing, analytics, and audit.

| Column | Type | Notes |
|--------|------|-------|
| `tenant_id` | UUID FK → `tenants.id` ON DELETE CASCADE | — |
| `user_id` | UUID FK → `users.id` ON DELETE SET NULL | NULL for system-initiated events |
| `event_type` | VARCHAR(128) | e.g. `report_generated`, `score_requested`, `export_downloaded` |
| `metadata_json` | JSONB | Event-specific context |

---

## Entity Relationship Summary

The diagram below shows the primary relationships between logical groups. Arrows represent foreign key direction (child → parent).

```
tenants ──────────────────────────────────────────────┐
  │                                                    │
  ├── users ──→ usage_events                           │
  │                                                    │
  └── report_templates ──→ report_template_focus_entities
           │
           └── report_runs ──→ report_insights
                                    │
                          ┌─────────┼──────────┐
                          ▼         ▼          ▼
                      companies  materials  regulations

supply_chain_contexts (domain config — referenced by scoring logic)

materials ◄──── company_material_exposures ────► companies
                       │
                   source_geography (ISO2)

companies ◄──── company_supply_relationships ────► companies
                       │
                    materials (optional)

regulations ◄── regulation_material_scope ──► materials
regulations ◄── regulation_geography_scope ── (country_code)
companies   ◄── company_regulation_exposure ──► regulations

sources ──→ ingestion_runs ──→ raw_api_payloads
               │
               └──→ source_documents ──→ document_chunks (+ embedding vector)
                           │
                    ┌──────┴────────┐
                    ▼               ▼
               risk_events      regulations
                    │
         ┌──────────┼───────────────┐────────────┐
         ▼          ▼               ▼            ▼
  risk_event_    risk_event_    risk_event_  risk_event_
  companies      materials      regulations  geographies
         │
         ▼
    companies ──→ company_scores (append-only)
    materials  ──→ material_scores (append-only)
    (country)  ──→ geography_scores (append-only)

materials ──→ hs_code_material_mappings ← trade_flows
materials ──→ commodity_prices
materials ──→ material_criticality_signals  (source=usgs_mcs|eu_crma|iea_report|patstat|manual)
materials ──→ material_geography_risk_scores  (geography_code, append-only — migration 011)

battery_chemistries ──→ battery_chemistry_materials ─────► materials
                                                            (valid_from/valid_to versioning)
battery_chemistries ──→ chemistry_risk_scores (append-only)

companies ──→ company_aliases
companies ──→ facilities
companies ──→ company_vehicle_models ──→ vehicle_model_chemistries ──→ battery_chemistries

insight_posts (no FKs — public intelligence hub content; tagged via materials/geographies arrays — migration 012)
```

---

## Battery Chemistry Risk Layer (migration 002)

Added April 2026. Introduces four new tables for chemistry-level supply chain risk scoring.

### `battery_chemistries`

One row per battery cell chemistry. NMC variants (111/622/811) are collapsed into a single `nmc` slug for v1.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `slug` | VARCHAR(64) UNIQUE | `nmc`, `lfp`, `nca`, `lfmp`, `sodium_ion`, `solid_state` |
| `name` | VARCHAR(255) | Human-readable name |
| `description` | TEXT | — |
| `status` | VARCHAR(32) | `commercial` \| `emerging` \| `research` |
| `current_market_share_pct` | FLOAT | Approximate global share 0.0–1.0 |
| `market_share_as_of_date` | DATE | **Required when share is set** — LFP went from ~6% to 40%+ in 3 years; without this date the figure is uninterpretable |
| `is_active` | BOOLEAN | — |

**Seeded:** 6 rows at migration time (NMC 0.38, LFP 0.40, NCA 0.08, LFMP 0.04, sodium_ion 0.03, solid_state 0.01 — 2024-12-31 vintage).

### `battery_chemistry_materials`

Versioned junction linking each chemistry to its constituent materials with intensity weights. Temporal versioning enables score auditability as compositions evolve.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `battery_chemistry_id` | FK → `battery_chemistries` | — |
| `material_id` | FK → `materials` | — |
| `role` | VARCHAR(64) | `cathode_active` \| `anode` \| `electrolyte` \| `current_collector` \| `other` |
| `intensity` | FLOAT | 0–1: relative material intensity in this chemistry |
| `is_substitutable` | BOOLEAN | Whether another material can substitute |
| `valid_from` | DATE NOT NULL | When this intensity value became applicable |
| `valid_to` | DATE | NULL = currently active. Point-in-time: `valid_from ≤ as_of ≤ COALESCE(valid_to, 'infinity')` |

**Unique constraint:** `(battery_chemistry_id, material_id, role, valid_from)`

**Seeded:** ~48 rows via `seed-materials` CLI covering all 6 chemistries.

### `material_criticality_signals`

Authoritative timeseries of per-material criticality from multiple sources. Replaces the single static `materials.criticality_score` float for multi-source, multi-year tracking.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `material_id` | FK → `materials` | — |
| `source` | VARCHAR(32) | `usgs_mcs` \| `eu_crma` \| `iea_report` \| `patstat` \| `manual` |
| `reference_year` | INTEGER | Publication year of the assessment |
| `criticality_score` | FLOAT | 0.0–1.0 normalised criticality |
| `trend_direction` | VARCHAR(16) | `rising` \| `declining` \| `stable` |
| `hhi_score` | FLOAT | Raw HHI Σ(share_i²), 0–1 scale |
| `metadata_json` | JSONB | Source methodology notes |

**Unique constraint:** `(material_id, source, reference_year)`

**Source priority** in `score_chemistry()`: `eu_crma` > `iea_report` > `usgs_mcs` > `manual` > `patstat` > `materials.criticality_score` (fallback)

### `chemistry_risk_scores`

Pre-computed, append-only chemistry-level risk scores. One row per scoring run.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `battery_chemistry_id` | FK → `battery_chemistries` | — |
| `computed_at` | TIMESTAMPTZ | When scored |
| `as_of_date` | DATE | Point-in-time for active composition lookup |
| `methodology_version` | VARCHAR(16) | `"1.0"` |
| `material_concentration_score` | FLOAT | 0–100 |
| `geopolitical_score` | FLOAT | 0–100 |
| `composite_risk_score` | FLOAT | 0–100 |
| `score_confidence` | FLOAT | 0–1. Penalised by `data_availability`. Floored at 0.3. |
| `metadata_json` | JSONB | `signal_sources`, `geo_coverage`, `materials_missing_hs`, `patent_modifiers_applied`, `trade_flows_vintage`, `no_benchmark_materials` |

**CLI:** `bdi-ingest rescore-chemistry [--slug nmc] [--as-of 2024-01-01]`. Also runs Mondays 02:00 UTC via the Inngest cron job `rescore-all-chemistries`.

---

## Market Risk Scores (migration 011)

### `material_geography_risk_scores`

Append-only market-level risk scores at the **(material, geography)** intersection. This is the company-agnostic intelligence layer that powers the public hub at `mineralriskanalytics.com`. See [`docs/scoring.md` § Market Risk Scoring](scoring.md#market-risk-scoring) for formula details.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `material_id` | INTEGER FK → `materials.id` ON DELETE CASCADE | — |
| `geography_code` | CHAR(2) NOT NULL | ISO2 country code |
| `as_of_date` | DATE NOT NULL | Point-in-time of evidence cutoff |
| `material_concentration_score` | FLOAT | 0–100 — material pillar (criticality + HHI + HCG uplift) |
| `geopolitical_trade_score` | FLOAT | 0–100 — geopolitical pillar (binary HCG + classified trade events) |
| `regulatory_compliance_score` | FLOAT | 0–100 — regulatory pillar (regulation scope unions + scoped events) |
| `operational_score` | FLOAT | 0–100 — operational pillar |
| `financial_pressure_score` | FLOAT | 0–100 — **reframed**: commodity price volatility + producer-stress events |
| `overall_risk_score` | FLOAT | Weighted aggregate using `MARKET_PILLAR_WEIGHTS` (renormalised across the five active pillars; supply-chain propagation excluded) |
| `event_count` | INTEGER | Number of distinct events used |
| `rationale_json` | JSONB | Sub-input breakdown, pillar scores, weights used, criticality-signal source, event counts, human-readable `notes` |
| `scoring_version` | VARCHAR(32) | `"3.0"` (matches the company-layer `SCORING_VERSION`) |
| `computed_at` | TIMESTAMPTZ | When the row was written |

**Unique constraint:** `(material_id, geography_code, as_of_date)` — prevents duplicate same-day rescore rows; re-runs upsert into the same slot.

**CLI:** `bdi-ingest rescore-market [--material-id <id>] [--geographies CN,CL,...] [--as-of YYYY-MM-DD]`. Also runs Mondays 03:00 UTC via the Inngest cron job `rescore-market-scores` (one hour after the chemistry cron so the new chemistry composites are visible).

**API:** `GET /api/v1/materials/{id}/market-scores`, `GET /api/v1/materials/{id}/market-scores/{geo}`, `GET /api/v1/market/scores`, `POST /api/v1/market/rescore`.

---

## HS Code Mappings (migration 003)

### `hs_code_material_mappings`

Maps 4-digit HS code prefixes to `materials.id`. Bridges trade flow data (stored at 4-digit level by Comtrade ingestion) to specific materials for concentration scoring.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | — |
| `hs_code_prefix` | VARCHAR(10) NOT NULL | 4-digit string e.g. `"2604"` |
| `material_id` | FK → `materials` | — |
| `description` | TEXT | What this HS chapter covers |
| `confidence` | FLOAT | 0.0–1.0 specificity tier (not data quality) |

**Unique constraint:** `(hs_code_prefix, material_id)`

**Confidence tiers:**
- `1.0` — unambiguous: prefix maps to exactly one material (e.g. `2504` = Natural Graphite)
- `0.7–0.9` — primary material, prefix covers 2–3 commodities
- `0.5–0.6` — partial attribution: prefix covers many materials (e.g. `2615` = Vanadium + Niobium + Tantalum + Zirconium)

**Chapter 81 note:** HS 8112 covers Gallium, Germanium, Indium, Niobium, and Chromium at the 4-digit level. Individual 6-digit codes are specific (8112.21 = Chromium, 8112.31 = Germanium, etc.) but Comtrade ingestion queries at 4-digit. These mappings are intentionally low-confidence (0.6–0.7) until ingestion is updated.

**Seeded:** 71 rows via `seed-hs-mappings` CLI. Source: USGS MCS 2025, UN Comtrade HS 2022, EU CRM Act 2023 Annex II.

---

## Key Design Decisions

**Append-only scores.** `company_scores`, `material_scores`, and `geography_scores` never have rows updated in place. Each scoring run inserts a new row. This gives a full historical time series for trend analysis and score-delta tracking, at the cost of more storage.

**UUID vs integer PKs.** Entity tables (`companies`, `tenants`, `users`, `facilities`, `report_templates`) use UUID primary keys because they need to be stable identifiers that can be created client-side or referenced across external systems. High-volume write tables (`risk_events`, `source_documents`, `ingestion_runs`, scores) use auto-increment integers for index efficiency.

**JSONB for semi-structured fields.** `risk_categories_json`, `geography_json`, `rationale_json`, pillar weights, and HS code lists all use JSONB to avoid premature normalisation. JSONB supports indexed containment queries (`@>`) which power the evidence query layer's category and geography filtering.

**pgvector for semantic search.** `document_chunks.embedding` stores 1536-dimensional vectors with an IVFFlat index (100 lists, cosine distance). This enables "find documents semantically similar to this query" without an external vector database.

**Multi-tenancy via `tenant_id`.** Reports and ingestion runs carry `org_id` (a UUID FK to `tenants`). Entity data (companies, materials, events, scores) is intentionally shared across tenants — the intelligence layer is a shared resource. Only reports, templates, and usage events are tenant-scoped.

**Cascade vs SET NULL.** `ON DELETE CASCADE` is used when child rows have no meaning without their parent (e.g. a `document_chunk` without its `source_document`). `ON DELETE SET NULL` is used when the child row retains independent value even if the referenced parent is deleted (e.g. a `risk_event` that outlives the deletion of its originating document).
