# Company Seed Spreadsheet — Tracked Fields Inventory

**File:** `company_seed_expanded.xlsx`
**Generated:** 2026-06-17
**Purpose:** Document every column and tab in the existing seed before extending it with new fields (`battery_grade_relevance`, `revenue_share_by_material`, etc.) so additions are intentional and avoid redundancy.

---

## Sheet 1: `Company Seed` — 66 data rows × 34 columns

The main entity table. Each row becomes one `Company` row in the DB.

### Identity (cols 0–3)

| Col | Field | Purpose | DB target |
|---|---|---|---|
| 0 | `company_name` | Canonical name. FK that joins to facility_seed_template. Loader dedups on this exact string | `Company.canonical_name` |
| 1 | `legal_name` | Full registered name (incl. Inc./Corp./plc/Ltd). Used for fuzzy match against SEC + LEI registries | `Company.legal_name` |
| 2 | `former_names` | Comma-separated prior legal names. CRITICAL for SEC submissions JSON resolution | `CompanyAlias` (alias_type=`former_name`) |
| 3 | `aliases` | Common names, abbreviations, local-language names | `CompanyAlias` (alias_type=`aka`) |

### External Identifiers (cols 4–10)

| Col | Field | Purpose | DB target |
|---|---|---|---|
| 4 | `cik` | SEC Central Index Key (10-digit, zero-padded). Required for any SEC filer | `Company.cik` |
| 5 | `lei` | GLEIF-registered Legal Entity Identifier (20-char). Best cross-system standard | `Company.lei` |
| 6 | `primary_ticker` | Most-traded ticker | `Company.public_ticker` |
| 7 | `additional_tickers` | Dual-class shares, secondary listings, ADRs | `CompanyAlias` (alias_type=`ticker`) |
| 8 | `exchanges` | Comma-separated exchange codes, **primary listing first** (NYSE, NASDAQ, LSE, ASX, etc.) | `Company.exchanges` |
| 9 | `sic_code` | 4-digit SEC SIC code (1040=gold mining, 2812-19=lithium chem, 3690=batteries) | Used as material-bucket prior |
| 10 | `duns` | DUNS number (optional). Useful for supply-chain procurement data integration | `Company.duns_number` |

### Location (cols 11–14)

| Col | Field | Purpose |
|---|---|---|
| 11 | `hq_country` | ISO-2 of legal domicile (where incorporated). Drives jurisdictional risk |
| 12 | `hq_state_region` | State/province/region of HQ. Free text |
| 13 | `hq_city` | HQ city |
| 14 | `incorporated_country` | ISO-2 of incorporation if different from operational HQ. Defaults to `hq_country` |

### Ownership (cols 15–18)

| Col | Field | Purpose |
|---|---|---|
| 15 | `ownership_type` | Enumerated: `public, private, state_owned, jv_vehicle, subsidiary, government_agency, cooperative` |
| 16 | `is_state_owned_or_influenced` | TRUE if ≥30% gov-owned/controlled, national-champion designation, or significant SOE board influence |
| 17 | `parent_company_name` | Canonical name of immediate parent for subsidiaries/jv_vehicles. Must match a row in this sheet |
| 18 | `jv_parent_names` | Comma-separated parent companies for jv_vehicles. Used to roll up risk to actual operating entities |

### Timeline (cols 19–20)

| Col | Field | Purpose |
|---|---|---|
| 19 | `year_founded` | Year company was founded. Optional context — newer companies have less filing history |
| 20 | `year_listed` | Year of IPO. Blank for non-public |

### Operating Model (cols 21–24)

| Col | Field | Purpose |
|---|---|---|
| 21 | `primary_activity_stage` | Single dominant stage from Supply Chain Stages tab. Pick highest bottleneck_weight |
| 22 | `operates_at_activity_stages` | Comma-separated ALL stages the company operates at |
| 23 | `has_facilities` | TRUE if owns/operates physical production sites. Drives three-edge model: TRUE → facility-derived edge; FALSE → partner-curated or supplier-graph |
| 24 | `operates_as_trader` | TRUE if material commodity-trading operations (Glencore, Trafigura, Mercuria, IXM are TRUE) |

### Material Exposure (col 25)

| Col | Field | Purpose |
|---|---|---|
| 25 | `primary_materials` | Comma-separated material canonical names. **Currently used only as a sanity-check for derived exposure, NOT as the primary attribution input** |

### Sanctions (cols 26–27)

| Col | Field | Purpose |
|---|---|---|
| 26 | `is_sanctioned` | TRUE if entity appears on any sanctions list (OFAC SDN, EU, UK, UN). Distinct from operating in sanctioned country |
| 27 | `sanctioning_jurisdictions` | Comma-separated: `US, EU, UK, UN, CH, CA, AU, JP` |

### UFLPA (cols 28–29)

| Col | Field | Purpose |
|---|---|---|
| 28 | `has_uflpa_designation` | TRUE if on US Uyghur Forced Labor Prevention Act Entity List |
| 29 | `uflpa_status` | `presumption` (default), `partial`, `unknown`. Maps to UFLPA scoring overrides |

### Provenance (cols 30–33)

| Col | Field | Purpose |
|---|---|---|
| 30 | `data_source` | Where partner sourced this row (e.g., 'company website', 'SEC 10-K 2024', 'press release') |
| 31 | `source_url` | Primary URL for audit trail |
| 32 | `last_verified_date` | ISO date. Rows older than 12 months get flagged for re-review |
| 33 | `notes` | Free text. Flag pending acquisitions, name changes, JV dissolutions |

---

## Sheet 2: `Supply Chain Stages` — 16 stage codes × 8 columns

Reference tab. Each row defines one stage_code that's foreign-keyed from `Company Seed.primary_activity_stage` and `operates_at_activity_stages`.

| stage_code | bottleneck_weight | Description |
|---|---:|---|
| `mining` | 0.8 | Raw ore/brine extraction |
| `beneficiation` | 1.0 | Physical processing → concentrate |
| `refining` | 1.2 | Chemical conversion → refined metal / battery-grade salts |
| `precursor_production` | 1.3 | pCAM (NCM, NCA precursors) |
| `cathode_active_material` | 1.3 | Finished cathode powder |
| `anode_active_material` | 1.3 | Anode materials (graphite, silicon) |
| `separator_production` | 1.1 | Microporous polymer films |
| `electrolyte_production` | 1.1 | LiPF6 + solvents |
| `cell_making` | 1.4 | Finished battery cells (highest bottleneck) |
| `module_assembly` | 1.1 | Cells → modules with BMS |
| `pack_assembly` | 1.05 | Modules → vehicle-ready packs |
| `vehicle_assembly` | 1.0 | Final EV assembly |
| `recycling` | 1.15 | Black mass production + hydrometallurgy |
| `trading` | 1.0 | Physical commodity trading |
| `financial` | 0.5 | PE / sovereign wealth / project lenders |
| `integrated` | 1.3 | META — multi-stage operator |

Columns: `stage_code, display_name, description, typical_facility_types, bottleneck_weight, hs_chapter_hint, sort_order, notes`

---

## Sheet 3: `Valid Values` — 9 fields with allowed-value enumerations

Reference for loader-side validation.

| Field | Allowed values |
|---|---|
| `ownership_type` | public, private, state_owned, jv_vehicle, subsidiary, government_agency, cooperative |
| `primary_supply_chain_stage` | Any stage_code from Supply Chain Stages tab |
| `operates_at_stages` | Comma-separated list of stage_codes |
| `exchanges` | NYSE, NASDAQ, AMEX, LSE, EURONEXT, XETRA, HKEX, SSE, SZSE, KRX, TSE, ASX, JSE, TSX, BMV |
| `sanctioning_jurisdictions` | US, EU, UK, UN, CH, CA, AU, JP |
| `uflpa_status` | presumption, partial, unknown |
| `relationship_type` (Supply Relationships) | direct, indirect, estimated, framework |
| `agreement_type` (Supply Relationships) | offtake, supply_agreement, joint_development, equity_offtake, framework, spot, unknown |
| Booleans | TRUE/FALSE (uppercase preferred, case-insensitive on load) |

---

## Sheet 4: `Field → Pillar Map` — explains how each seed field enters scoring

Documentation tab. Maps 14 seed fields to which scoring pillar / pipeline uses them:

| Field | Pillar / Pipeline | How it enters scoring |
|---|---|---|
| `company_name` | All | Canonical join key for facility data, supply relationships, sanctions feeds, scoring outputs |
| `former_names + aliases` | All ingest | Resolves SEC submissions / news / external list variants to canonical Company |
| `cik` | Future SEC ingester | Ties SEC filings to Company for Financial Pressure pillar |
| `lei` | Future GLEIF + EU regulatory | Cross-system entity standard |
| `primary_ticker + exchanges` | Future financial data ingester | Drives lookup of price + financials from market-data providers |
| `sic_code` | Material concentration pillar (prior) | Deterministic issuer→material-bucket prior |
| `hq_country + incorporated_country` | Geopolitical/trade | Jurisdictional risk weighting |
| `ownership_type + is_state_owned_or_influenced` | Geopolitical/trade | SOE flag triggers extra geopolitical pillar multiplier |
| `parent_company_name + jv_parent_names` | Supply chain propagation | Risk at TLEA rolls up to Tianqi + IGO. Without this, JV vehicles look isolated |
| `primary_supply_chain_stage + operates_at_stages` | Material concentration + propagation | Stage bottleneck_weight applies as scoring multiplier |
| `has_facilities` | Three-edge company-material model | TRUE → facility-derived edge; FALSE → fallback edges |
| `operates_as_trader` | Material concentration | Triggers supplier-graph-derived exposure path |
| `primary_materials` | **Validation only** | Cross-check derived material exposure against partner-asserted exposure. **Not the primary attribution source** |
| `is_sanctioned + sanctioning_jurisdictions` | Regulatory + geopolitical | Direct entity sanctions trigger maximum-severity event |
| `has_uflpa_designation + uflpa_status` | Regulatory | UFLPA rebuttable-presumption status multiplier |
| `Supply Relationships sheet` | Supply chain propagation | Tier 2/3 risk propagation across CompanySupplyRelationship rows |

---

## Sheet 5: `Recommended Coverage` — 41 prioritized companies × 6 columns

Curated priority list with tier and rationale. **Note: this tab has 41 entries vs. 66 in Company Seed — the tier list is more curated and includes companies not yet in the main seed (LG Energy Solution, Samsung SDI, SK On, BYD, Trafigura, etc.).**

| Col | Field | Purpose |
|---|---|---|
| 0 | `Tier` | T1/T2/T3/T4 priority |
| 1 | `Company name` | Cross-reference to Company Seed |
| 2 | `Type` | Description (Integrated miner+refiner, Cell maker, OEM, Trader, etc.) |
| 3 | `Primary materials` | Same field as Company Seed col 25 |
| 4 | `Why prioritize` | Rationale text |
| 5 | `Has facility data?` | Y / Partial / N |

**Tier breakdown:**
- **T1 (15)**: Pure-play battery-grade producers (Albemarle, SQM, Ganfeng, Tianqi, Glencore, CMOC, Sumitomo, Vale, Norilsk, Indonesia Battery, Tsingshan, Huayou, GEM, BTR, Shanshan)
- **T2 (16)**: Critical-mass refiners and cell makers (MP Materials, Lynas, China Northern REE, JL MAG, Mineral Resources, Pilbara, Arcadium, Liontown, POSCO, LG Energy, Samsung SDI, SK On, BYD, CATL, EVE, Gotion)
- **T3 (6)**: OEMs (Tesla, Ford, GM, Volkswagen, Stellantis, Hyundai/Kia)
- **T4 (5)**: Traders + diversified miners (Trafigura, IXM, BHP, Rio Tinto, Freeport)

---

## Sheet 6: `Supply Relationships` — 10 partner-curated buyer-supplier edges × 12 columns

Captures inter-company supply chains for Tier 2/3 risk propagation.

| Col | Field | Purpose |
|---|---|---|
| 0 | `buyer_company_name` | Must match Company Seed |
| 1 | `supplier_company_name` | Must match Company Seed |
| 2 | `material_canonical` | Material being supplied |
| 3 | `relationship_type` | direct / indirect / estimated / framework |
| 4 | `agreement_type` | offtake / supply_agreement / joint_development / equity_offtake / framework / spot / unknown |
| 5 | `volume_share_pct` | Fraction (0.0–1.0) of buyer's demand. Helps weight propagation impact |
| 6 | `contract_term_years` | Length in years. Use 0 for spot |
| 7 | `announced_date` | ISO date agreement was publicly announced |
| 8 | `effective_from` | ISO date supply starts |
| 9 | `effective_to` | ISO date supply ends. Blank = evergreen |
| 10 | `source_url` | Press release / 8-K / primary URL |
| 11 | `notes` | Free text — terms, conditions, related agreements, expansion options |

**Currently 10 populated rows** covering Talison/Greenbushes JV structure (Tianqi + Albemarle), Wodgina JV (Albemarle + Mineral Resources), Covalent JV (Wesfarmers + SQM), Tenke Fungurume (CMOC + Gécamines), Tamarack (Rio Tinto + Talon), Century Aluminum (Glencore), and the Rio Tinto + Arcadium acquisition.

---

## Coverage summary

```
Companies populated:               66 rows
   With CIK:                       27 (mostly US-listed + ADRs)
   With LEI:                       partial (need audit)
   With primary_ticker:            ~35
   With primary_materials filled:  64/65 (Lithium Argentina blank)
   With facility data:             0/66 in DB right now
   With supply relationships:      10 edges across ~9 companies

Stage taxonomy:                    16 stages, weights 0.5-1.4
Valid value rules:                 9 fields covered
Field-to-pillar mapping:           14 fields mapped
Tier-prioritized coverage:         41 companies (some not in main seed yet)
```

---

## What's NOT tracked yet — surfaced during Vale walkthrough

These are the gaps we identified from working through Vale's 20-F revenue breakdown. None of these exist in the current schema:

### 1. Per-material weighting fields

| Proposed field | Purpose | Example for Vale |
|---|---|---|
| `battery_grade_relevance` (JSON) | 0.0–1.0 per material. Weights Phase 4 SEC body-text attribution so non-battery-grade producers (steel iron ore, fertilizer phosphate, general copper) don't pollute launch-10 signal | `{"Iron Ore": 0.03, "Nickel": 0.75, "Copper": 0.25, "Cobalt": 1.0}` |
| `revenue_share_by_material` (JSON) | Annual revenue % per material from segment reporting | `{"Iron Ore": 0.785, "Nickel": 0.112, "Copper": 0.098, "Other": 0.006}` |
| `revenue_share_year` (int) | Reporting year for `revenue_share_by_material` | `2025` |

### 2. Production scale fields

| Proposed field | Purpose | Example for Vale |
|---|---|---|
| `production_tonnage_by_material` (JSON) | Annual production tonnes per material | `{"Iron Ore": 320e6, "Nickel": 175e3, "Copper": 340e3}` |
| `production_year` (int) | Reporting year for production data | `2025` |
| `production_source` (text) | Where the partner sourced production (10-K Item 5, Sustainability Report, Quarterly Production Report) | `20-F FY2025 Item 4.B + 5.A` |

### 3. Customer / end-use fields

| Proposed field | Purpose | Example for Vale |
|---|---|---|
| `customer_end_use_notes` (text) | Free-text notes on what end-uses the products go to | `Iron ore mostly steel (China-dominated); nickel Class 1 to battery customers; copper concentrate to smelters` |
| `disclosed_battery_grade_pct` (JSON, optional) | If the company breaks out battery-precursor revenue separately (Anglo American, some Rio Tinto), capture the disclosed percentage | `{}` |

### 4. Financial signal fields

| Proposed field | Purpose | Example for Vale |
|---|---|---|
| `annual_revenue_usd` (decimal) | Latest reported total revenue | `38400e6` |
| `revenue_year` (int) | Reporting year | `2025` |
| `revenue_yoy_change_pct` (decimal) | YoY revenue change, useful for Financial Pressure pillar | `0.009` |

### 5. Filing cadence + recency

| Proposed field | Purpose |
|---|---|
| `primary_filing_form` | 10-K / 20-F / ARS / Local-only |
| `most_recent_annual_filing_date` (ISO date) | When was the most recent 10-K / 20-F filed? Helps determine if Phase 2 backfill has it |
| `quarterly_production_disclosure` (bool) | Do they file quarterly production reports? (Vale does; many private companies don't) |

### 6. Material name precision (cross-cutting decision)

`primary_materials` currently uses short-form names that don't match the canonical launch-10. Two options:
- **Option A**: Add an `expand_material_aliases` flag on the loader (e.g., `"Iron Ore"` → `"Iron Ore"`). Cleaner sheet, but loses the distinction between steel-grade and LFP-grade
- **Option B**: Require canonical names in the sheet, with an alias map provided as a reference (`"Iron Ore"`, `"Iron Ore (Steel Grade)"`, `"Phosphate"`, `"Phosphate (Fertilizer Grade)"`)

Option B is more precise but requires the partner to be aware of the canonical names. Worth deciding before extending the schema.

---

## Decisions needed before extending the seed

1. **Where do per-material fields live?** Two structural options:
   - **Inline JSON columns** on Company Seed (e.g., `battery_grade_relevance` as JSON map)
   - **New child tab** `Company × Material Detail` with one row per (company, material) pair carrying battery_grade_relevance + revenue_share + production_tonnage

   Option B is more relational and lets you avoid sparse JSON, but adds a tab partner has to navigate. Option A keeps everything in one row per company.

2. **Material name precision** — Option A vs B above.

3. **What's the minimum viable set for the next research pass?** All 6 categories above are useful but we shouldn't add fields that won't be filled in. Probably:
   - `battery_grade_relevance` (essential — blocks Phase 4 attribution)
   - `revenue_share_by_material` (essential — first sanity check on relevance scoring)
   - `production_year` + `revenue_year` (essential — without these the numbers are stale)
   - Everything else can wait until the seed loader is wired

4. **Should the Recommended Coverage tab be merged into Company Seed?** Currently 41 tier-ranked companies in Recommended Coverage but only 66 in main seed, with overlap. Could collapse into a single sheet with a `tier` column to avoid the two-source-of-truth problem.

---

*Next step: decide on the new-field structure (inline JSON vs. child tab) and material name precision before writing any seed updates.*
