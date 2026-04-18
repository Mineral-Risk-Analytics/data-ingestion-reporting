Create a company seed and GLEIF LEI enrichment pipeline for the battery-data-intelligence-engine project.

## Context

The `companies` table is empty. It needs to be seeded with the ~40 most strategically important companies across the EV battery supply chain before any company-level scoring, sanctions matching, or supply relationship mapping is possible. These are the companies whose market concentration drives the Geopolitical (20%) and Operational (15%) scoring pillars.

After the manual seed, a GLEIF enrichment step uses the Global Legal Entity Identifier Foundation's free public API (no key required) to verify and backfill `lei`, `legal_name`, and additional aliases for each company. The LEI is critical for future OpenSanctions entity matching — OpenSanctions uses LEI as one of its primary identifiers, which dramatically improves match precision over name-only fuzzy matching.

Two-step design:
1. **Manual seed** — `app/services/ingestion/seed_companies.py` — curated reference data for 40 companies, idempotent upsert on `canonical_name`.
2. **GLEIF enrichment** — `app/services/ingestion/gleif.py` — queries GLEIF API by company name, matches candidates by name similarity, updates `lei`, `legal_name`, and `company_aliases`.

## What to build

### 1. `app/services/ingestion/seed_companies.py` — manual seed module

Create this file. It is static reference data — not ingested, not scraped. Values are manually verified from public sources (company websites, exchange filings, Wikipedia). All Chinese-listed companies are included because automated sources for them are unreliable.

#### Company data structure

Define a module-level list:

```python
_COMPANIES: list[dict] = [...]
```

Each dict maps directly to `Company` model fields:
- `canonical_name` (str) — primary lookup key, used everywhere in the codebase
- `legal_name` (str | None) — official registered name if different from canonical
- `supply_chain_stage` (str) — one of: `miner | refiner | cell_maker | pack_maker | oem | trader | other`
- `headquarters_country` (str) — ISO2 code
- `headquarters_region` (str | None) — free text region (e.g. "Asia-Pacific", "Europe")
- `is_public` (bool)
- `public_ticker` (str | None) — exchange:ticker format (e.g. "NYSE:ALB", "SZSE:300750")
- `data_confidence` (float) — 0.9 for well-documented public companies, 0.7 for private or partially verified
- `data_source` (str) — `"manual"`
- `notes` (str | None) — supply chain role, key materials
- `aliases` (list[dict]) — each dict: `{"alias": str, "alias_type": str}`
  - alias_type: `aka | ticker | former_name | abbreviation`

#### Full `_COMPANIES` list

Use exactly these 40 companies:

```python
_COMPANIES = [
    # ── MINERS ──────────────────────────────────────────────────────────
    {
        "canonical_name": "Albemarle Corporation",
        "legal_name": "Albemarle Corporation",
        "supply_chain_stage": "miner",
        "headquarters_country": "US",
        "headquarters_region": "North America",
        "is_public": True,
        "public_ticker": "NYSE:ALB",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "World's largest lithium producer. Operates in Chile (Atacama), Australia (Greenbushes JV), Nevada.",
        "aliases": [{"alias": "ALB", "alias_type": "ticker"}],
    },
    {
        "canonical_name": "SQM",
        "legal_name": "Sociedad Química y Minera de Chile S.A.",
        "supply_chain_stage": "miner",
        "headquarters_country": "CL",
        "headquarters_region": "South America",
        "is_public": True,
        "public_ticker": "NYSE:SQM",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "Second largest lithium producer globally. Operates Atacama brine operations. Also produces potassium, iodine.",
        "aliases": [
            {"alias": "Sociedad Quimica y Minera", "alias_type": "aka"},
            {"alias": "SQM", "alias_type": "ticker"},
        ],
    },
    {
        "canonical_name": "Glencore",
        "legal_name": "Glencore plc",
        "supply_chain_stage": "miner",
        "headquarters_country": "CH",
        "headquarters_region": "Europe",
        "is_public": True,
        "public_ticker": "LSE:GLEN",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "Largest cobalt producer globally via DRC operations (Katanga, Mutanda). Also major copper trader.",
        "aliases": [
            {"alias": "Glencore International", "alias_type": "former_name"},
            {"alias": "GLEN", "alias_type": "ticker"},
        ],
    },
    {
        "canonical_name": "CMOC Group",
        "legal_name": "CMOC Group Limited",
        "supply_chain_stage": "miner",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "SSE:603993",
        "data_confidence": 0.85,
        "data_source": "manual",
        "notes": "Second largest cobalt producer globally. Operates Tenke Fungurume (DRC) and Kisanfu. Formerly China Molybdenum.",
        "aliases": [
            {"alias": "China Molybdenum", "alias_type": "former_name"},
            {"alias": "CMOC", "alias_type": "abbreviation"},
        ],
    },
    {
        "canonical_name": "Huayou Cobalt",
        "legal_name": "Zhejiang Huayou Cobalt Co., Ltd.",
        "supply_chain_stage": "miner",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "SSE:603799",
        "data_confidence": 0.85,
        "data_source": "manual",
        "notes": "Major cobalt and lithium processor. Operates Kakula mine stake (DRC). Supplies CATL, LG, Samsung SDI.",
        "aliases": [{"alias": "Zhejiang Huayou Cobalt", "alias_type": "aka"}],
    },
    {
        "canonical_name": "Vale",
        "legal_name": "Vale S.A.",
        "supply_chain_stage": "miner",
        "headquarters_country": "BR",
        "headquarters_region": "South America",
        "is_public": True,
        "public_ticker": "NYSE:VALE",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "World's largest nickel producer. Key operations in Canada (Sudbury, Voisey's Bay) and Brazil.",
        "aliases": [
            {"alias": "Vale SA", "alias_type": "aka"},
            {"alias": "VALE", "alias_type": "ticker"},
        ],
    },
    {
        "canonical_name": "Norilsk Nickel",
        "legal_name": "Public Joint-Stock Company MMC Norilsk Nickel",
        "supply_chain_stage": "miner",
        "headquarters_country": "RU",
        "headquarters_region": "Europe",
        "is_public": True,
        "public_ticker": "MOEX:GMKN",
        "data_confidence": 0.80,
        "data_source": "manual",
        "notes": "World's largest nickel and palladium producer. Russian entity — elevated geopolitical risk. Subject to sanctions monitoring.",
        "aliases": [
            {"alias": "Nornickel", "alias_type": "aka"},
            {"alias": "MMC Norilsk Nickel", "alias_type": "aka"},
            {"alias": "GMKN", "alias_type": "ticker"},
        ],
    },
    {
        "canonical_name": "BHP Group",
        "legal_name": "BHP Group Limited",
        "supply_chain_stage": "miner",
        "headquarters_country": "AU",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "ASX:BHP",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "Major nickel and copper producer. Operates Nickel West (WA) and Olympic Dam. Exited nickel in 2024 due to price collapse.",
        "aliases": [
            {"alias": "BHP Billiton", "alias_type": "former_name"},
            {"alias": "BHP", "alias_type": "abbreviation"},
        ],
    },
    {
        "canonical_name": "Freeport-McMoRan",
        "legal_name": "Freeport-McMoRan Inc.",
        "supply_chain_stage": "miner",
        "headquarters_country": "US",
        "headquarters_region": "North America",
        "is_public": True,
        "public_ticker": "NYSE:FCX",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "World's largest publicly traded copper producer. Operates Grasberg (Indonesia) — world's largest gold/copper mine.",
        "aliases": [{"alias": "FCX", "alias_type": "ticker"}],
    },
    {
        "canonical_name": "MP Materials",
        "legal_name": "MP Materials Corp.",
        "supply_chain_stage": "miner",
        "headquarters_country": "US",
        "headquarters_region": "North America",
        "is_public": True,
        "public_ticker": "NYSE:MP",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "Operates Mountain Pass — only active rare earth mining and processing site in the US. IRA-critical supplier.",
        "aliases": [{"alias": "MP", "alias_type": "ticker"}],
    },
    {
        "canonical_name": "Lynas Rare Earths",
        "legal_name": "Lynas Rare Earths Ltd.",
        "supply_chain_stage": "miner",
        "headquarters_country": "AU",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "ASX:LYC",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "Largest rare earth producer outside China. Operates Mt. Weld (AU) mine and Kuantan (Malaysia) processing.",
        "aliases": [{"alias": "Lynas", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Syrah Resources",
        "legal_name": "Syrah Resources Limited",
        "supply_chain_stage": "miner",
        "headquarters_country": "AU",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "ASX:SYR",
        "data_confidence": 0.85,
        "data_source": "manual",
        "notes": "Operates Balama graphite mine (Mozambique) — world's largest graphite deposit. Produces anode material in Louisiana (US).",
        "aliases": [{"alias": "Syrah", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Pilbara Minerals",
        "legal_name": "Pilbara Minerals Limited",
        "supply_chain_stage": "miner",
        "headquarters_country": "AU",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "ASX:PLS",
        "data_confidence": 0.90,
        "data_source": "manual",
        "notes": "Operates Pilgangoora — one of world's largest lithium deposits (WA). Produces spodumene concentrate.",
        "aliases": [{"alias": "Pilbara", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Rio Tinto",
        "legal_name": "Rio Tinto plc",
        "supply_chain_stage": "miner",
        "headquarters_country": "GB",
        "headquarters_region": "Europe",
        "is_public": True,
        "public_ticker": "LSE:RIO",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "Major copper and lithium miner. Acquired Arcadium Lithium in early 2025. Operates Oyu Tolgoi (Mongolia, copper).",
        "aliases": [{"alias": "Rio Tinto Group", "alias_type": "aka"}],
    },

    # ── REFINERS / PROCESSORS ────────────────────────────────────────────
    {
        "canonical_name": "Ganfeng Lithium",
        "legal_name": "Jiangxi Ganfeng Lithium Group Co., Ltd.",
        "supply_chain_stage": "refiner",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "SZSE:002460",
        "data_confidence": 0.85,
        "data_source": "manual",
        "notes": "World's largest lithium compound producer. Supplies most major cell makers. Has mining interests in Argentina and Australia.",
        "aliases": [{"alias": "Jiangxi Ganfeng Lithium", "alias_type": "aka"}],
    },
    {
        "canonical_name": "Tianqi Lithium",
        "legal_name": "Tianqi Lithium Corporation",
        "supply_chain_stage": "refiner",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "SZSE:002466",
        "data_confidence": 0.85,
        "data_source": "manual",
        "notes": "Second largest lithium refiner. 22.16% stake in SQM. Operates Kwinana hydroxide plant (AU).",
        "aliases": [],
    },
    {
        "canonical_name": "Umicore",
        "legal_name": "Umicore NV/SA",
        "supply_chain_stage": "refiner",
        "headquarters_country": "BE",
        "headquarters_region": "Europe",
        "is_public": True,
        "public_ticker": "EBR:UMI",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "Leading cathode active material producer (NMC, NCA). Major cobalt refiner. Battery recycling operations.",
        "aliases": [],
    },
    {
        "canonical_name": "Sumitomo Metal Mining",
        "legal_name": "Sumitomo Metal Mining Co., Ltd.",
        "supply_chain_stage": "refiner",
        "headquarters_country": "JP",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "TYO:5713",
        "data_confidence": 0.90,
        "data_source": "manual",
        "notes": "Major nickel and cobalt refiner. Produces NCA cathode precursor. Supplies Panasonic/Tesla Gigafactory.",
        "aliases": [{"alias": "SMM", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Ecopro BM",
        "legal_name": "EcoPro BM Co., Ltd.",
        "supply_chain_stage": "refiner",
        "headquarters_country": "KR",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "KOSDAQ:247540",
        "data_confidence": 0.90,
        "data_source": "manual",
        "notes": "South Korea's largest cathode active material producer. Supplies Samsung SDI, SK On, and European gigafactories.",
        "aliases": [{"alias": "EcoPro BM", "alias_type": "aka"}],
    },
    {
        "canonical_name": "POSCO Future M",
        "legal_name": "POSCO Future M Co., Ltd.",
        "supply_chain_stage": "refiner",
        "headquarters_country": "KR",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "KRX:003670",
        "data_confidence": 0.85,
        "data_source": "manual",
        "notes": "POSCO subsidiary producing cathode and anode materials. Formerly POSCO Chemical. Expanding into North America.",
        "aliases": [{"alias": "POSCO Chemical", "alias_type": "former_name"}],
    },
    {
        "canonical_name": "BTR New Energy",
        "legal_name": "BTR New Material Group Co., Ltd.",
        "supply_chain_stage": "refiner",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "NEEQ:835185",
        "data_confidence": 0.80,
        "data_source": "manual",
        "notes": "World's largest graphite anode material producer. ~25% global market share. Supplies CATL, LG, Samsung SDI.",
        "aliases": [{"alias": "BTR", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "ShanShan Corporation",
        "legal_name": "Hunan Shanshan Energy Technology Co., Ltd.",
        "supply_chain_stage": "refiner",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "SSE:600884",
        "data_confidence": 0.80,
        "data_source": "manual",
        "notes": "Second largest graphite anode producer globally. Also produces cathode materials. Major CATL supplier.",
        "aliases": [
            {"alias": "Shanshan Energy", "alias_type": "aka"},
            {"alias": "Hunan Shanshan", "alias_type": "aka"},
        ],
    },
    {
        "canonical_name": "BASF",
        "legal_name": "BASF SE",
        "supply_chain_stage": "refiner",
        "headquarters_country": "DE",
        "headquarters_region": "Europe",
        "is_public": True,
        "public_ticker": "ETR:BAS",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "European cathode active material producer (NMC via BASF Catalysts). Battery recycling JV with Nornickel.",
        "aliases": [],
    },

    # ── CELL MAKERS ─────────────────────────────────────────────────────
    {
        "canonical_name": "CATL",
        "legal_name": "Contemporary Amperex Technology Co., Limited",
        "supply_chain_stage": "cell_maker",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "SZSE:300750",
        "data_confidence": 0.90,
        "data_source": "manual",
        "notes": "World's largest battery cell manufacturer (~37% global share). Produces LFP and NMC. Customers include Tesla, VW, BMW, Hyundai.",
        "aliases": [
            {"alias": "Contemporary Amperex Technology", "alias_type": "aka"},
            {"alias": "宁德时代", "alias_type": "aka"},
        ],
    },
    {
        "canonical_name": "BYD",
        "legal_name": "BYD Company Limited",
        "supply_chain_stage": "cell_maker",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "SZSE:002594",
        "data_confidence": 0.90,
        "data_source": "manual",
        "notes": "Second largest battery producer globally. Vertically integrated — also largest EV OEM. Blade battery (LFP) dominant product.",
        "aliases": [{"alias": "比亚迪", "alias_type": "aka"}],
    },
    {
        "canonical_name": "LG Energy Solution",
        "legal_name": "LG Energy Solution, Ltd.",
        "supply_chain_stage": "cell_maker",
        "headquarters_country": "KR",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "KRX:373220",
        "data_confidence": 0.90,
        "data_source": "manual",
        "notes": "Third largest cell maker globally. Supplies Tesla, GM, Hyundai, Stellantis. Joint ventures: Ultium Cells (GM), HES (Hyundai).",
        "aliases": [{"alias": "LGES", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Samsung SDI",
        "legal_name": "Samsung SDI Co., Ltd.",
        "supply_chain_stage": "cell_maker",
        "headquarters_country": "KR",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "KRX:006400",
        "data_confidence": 0.90,
        "data_source": "manual",
        "notes": "Major cell maker. Supplies BMW, Stellantis, Rivian. Joint venture: StarPlus Energy (Stellantis, Indiana).",
        "aliases": [{"alias": "SDI", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "SK On",
        "legal_name": "SK On Co., Ltd.",
        "supply_chain_stage": "cell_maker",
        "headquarters_country": "KR",
        "headquarters_region": "Asia-Pacific",
        "is_public": False,
        "public_ticker": None,
        "data_confidence": 0.80,
        "data_source": "manual",
        "notes": "Battery subsidiary of SK Innovation. Supplies Ford, Hyundai, VW. JVs: BlueOval SK (Ford), SKBA (VW). Financial difficulties 2023–2024.",
        "aliases": [{"alias": "SK Innovation Battery", "alias_type": "aka"}],
    },
    {
        "canonical_name": "Panasonic Energy",
        "legal_name": "Panasonic Energy Co., Ltd.",
        "supply_chain_stage": "cell_maker",
        "headquarters_country": "JP",
        "headquarters_region": "Asia-Pacific",
        "is_public": False,
        "public_ticker": None,
        "data_confidence": 0.85,
        "data_source": "manual",
        "notes": "Panasonic Holdings subsidiary. Exclusive Tesla supplier at Gigafactory Nevada. Produces cylindrical NCA cells.",
        "aliases": [{"alias": "Panasonic Energy of North America", "alias_type": "aka"}],
    },
    {
        "canonical_name": "CALB Group",
        "legal_name": "CALB Co., Ltd.",
        "supply_chain_stage": "cell_maker",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "HKEX:3931",
        "data_confidence": 0.80,
        "data_source": "manual",
        "notes": "China Aviation Lithium Battery. Fourth largest Chinese cell maker. Supplies Li Auto, Neta, Geely.",
        "aliases": [{"alias": "China Aviation Lithium Battery", "alias_type": "aka"}],
    },
    {
        "canonical_name": "Gotion High-tech",
        "legal_name": "Gotion High-tech Co., Ltd.",
        "supply_chain_stage": "cell_maker",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "SZSE:002074",
        "data_confidence": 0.80,
        "data_source": "manual",
        "notes": "VW holds ~26% stake. Expanding globally including US (Michigan). Produces LFP cells.",
        "aliases": [],
    },
    {
        "canonical_name": "Northvolt",
        "legal_name": "Northvolt AB",
        "supply_chain_stage": "cell_maker",
        "headquarters_country": "SE",
        "headquarters_region": "Europe",
        "is_public": False,
        "public_ticker": None,
        "data_confidence": 0.70,
        "data_source": "manual",
        "notes": "European gigafactory (Skellefteå, Sweden). Filed for bankruptcy November 2024; restructuring ongoing. Key customers: BMW, VW, Scania.",
        "aliases": [],
    },

    # ── OEMs ─────────────────────────────────────────────────────────────
    {
        "canonical_name": "Tesla",
        "legal_name": "Tesla, Inc.",
        "supply_chain_stage": "oem",
        "headquarters_country": "US",
        "headquarters_region": "North America",
        "is_public": True,
        "public_ticker": "NASDAQ:TSLA",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "Largest pure-play EV OEM. Produces own 4680 cells. Suppliers: Panasonic Energy, CATL, LG Energy Solution.",
        "aliases": [{"alias": "Tesla Inc", "alias_type": "aka"}],
    },
    {
        "canonical_name": "Volkswagen Group",
        "legal_name": "Volkswagen AG",
        "supply_chain_stage": "oem",
        "headquarters_country": "DE",
        "headquarters_region": "Europe",
        "is_public": True,
        "public_ticker": "ETR:VOW3",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "Largest European OEM by volume. Brands: VW, Audi, Porsche, Skoda, SEAT. Battery JVs: PowerCo (own cells), BlueOval SK.",
        "aliases": [
            {"alias": "Volkswagen AG", "alias_type": "aka"},
            {"alias": "VW", "alias_type": "abbreviation"},
            {"alias": "VOW3", "alias_type": "ticker"},
        ],
    },
    {
        "canonical_name": "BMW Group",
        "legal_name": "Bayerische Motoren Werke AG",
        "supply_chain_stage": "oem",
        "headquarters_country": "DE",
        "headquarters_region": "Europe",
        "is_public": True,
        "public_ticker": "ETR:BMW",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "Premium OEM. Brands: BMW, MINI, Rolls-Royce. Battery suppliers: Samsung SDI, CATL, Northvolt.",
        "aliases": [{"alias": "BMW", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "General Motors",
        "legal_name": "General Motors Company",
        "supply_chain_stage": "oem",
        "headquarters_country": "US",
        "headquarters_region": "North America",
        "is_public": True,
        "public_ticker": "NYSE:GM",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "US OEM. Ultium platform. Battery JV: Ultium Cells LLC (with LG Energy Solution). Brands: Chevy, GMC, Cadillac, Buick.",
        "aliases": [{"alias": "GM", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Ford Motor Company",
        "legal_name": "Ford Motor Company",
        "supply_chain_stage": "oem",
        "headquarters_country": "US",
        "headquarters_region": "North America",
        "is_public": True,
        "public_ticker": "NYSE:F",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "US OEM. Battery JV: BlueOval SK (with SK On). Models: F-150 Lightning, Mustang Mach-E.",
        "aliases": [{"alias": "Ford", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Hyundai Motor Company",
        "legal_name": "Hyundai Motor Company",
        "supply_chain_stage": "oem",
        "headquarters_country": "KR",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "KRX:005380",
        "data_confidence": 0.90,
        "data_source": "manual",
        "notes": "Korean OEM. Ioniq platform. Battery JV: HES (Hyundai Energy Solution with LG Energy Solution). Also owns Kia.",
        "aliases": [{"alias": "Hyundai", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Stellantis",
        "legal_name": "Stellantis N.V.",
        "supply_chain_stage": "oem",
        "headquarters_country": "NL",
        "headquarters_region": "Europe",
        "is_public": True,
        "public_ticker": "NYSE:STLA",
        "data_confidence": 0.90,
        "data_source": "manual",
        "notes": "Multi-brand OEM (Jeep, RAM, Peugeot, Fiat, Chrysler, Dodge, Alfa Romeo, Citroën). Battery JV: StarPlus Energy (with Samsung SDI).",
        "aliases": [],
    },
    {
        "canonical_name": "Mercedes-Benz Group",
        "legal_name": "Mercedes-Benz Group AG",
        "supply_chain_stage": "oem",
        "headquarters_country": "DE",
        "headquarters_region": "Europe",
        "is_public": True,
        "public_ticker": "ETR:MBG",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "Premium German OEM. Battery suppliers: CATL, ACC (JV with Stellantis and TotalEnergies).",
        "aliases": [
            {"alias": "Daimler", "alias_type": "former_name"},
            {"alias": "Mercedes", "alias_type": "abbreviation"},
        ],
    },
]
```

#### Upsert function

```python
def seed_companies(session: Session) -> dict[str, int]:
    """Upsert all curated companies and their aliases. Idempotent.

    Uses canonical_name as the upsert key. Does not overwrite manually
    corrected fields if the row already exists — use --force for that.

    Returns {"inserted": int, "updated": int, "aliases_added": int}
    """
```

For each company dict:
1. Pop the `aliases` list out before constructing the `Company` object.
2. Check if a `Company` with `canonical_name` already exists.
   - If not: insert new `Company` + all `CompanyAlias` rows.
   - If yes: skip (do not overwrite — GLEIF enrichment will update fields later). Increment `updated` counter but make no changes.
3. For aliases: insert each `CompanyAlias` with `ON CONFLICT DO NOTHING` (unique constraint on `company_id + alias`).

`session.commit()` at the end.

### 2. `app/services/ingestion/gleif.py` — GLEIF enrichment module

Create this file.

#### GLEIF API

```python
GLEIF_API_BASE = "https://api.gleif.org/api/v1"
```

No API key required. Use `httpx` (already in deps).

The search endpoint:
```
GET {GLEIF_API_BASE}/lei-records
    ?filter[entity.names]={name}
    &filter[entity.status]=ACTIVE
    &page[size]=5
```

Response structure:
```json
{
  "data": [
    {
      "id": "LEI_STRING",
      "attributes": {
        "lei": "LEI_STRING",
        "entity": {
          "legalName": {"name": "Legal Name", "language": "en"},
          "otherNames": [{"name": "...", "language": "...", "type": "..."}],
          "headquartersAddress": {"country": "US", ...},
          "registeredAddress": {"country": "US", ...},
          "status": "ACTIVE"
        },
        "registration": {"status": "ISSUED"}
      }
    }
  ]
}
```

#### Name similarity scoring

```python
def _name_similarity(a: str, b: str) -> float:
    """Return a similarity score 0–1 between two company name strings.

    Normalises both strings: lowercase, strip legal suffixes
    (Inc., Ltd., Co., AG, plc, N.V., S.A., GmbH, LLC, Corp., Limited),
    collapse whitespace.

    Uses SequenceMatcher from difflib (stdlib). No external deps.
    Returns 1.0 for identical normalised strings.
    """
```

```python
GLEIF_MATCH_THRESHOLD = 0.82  # minimum similarity to accept a GLEIF match
```

This threshold is a balance: too low and you get false positives (e.g. "Ford Motor Company" → "Ford Motor Credit Company"). Too high and you miss legitimate matches with different legal suffix conventions. 0.82 is a reasonable starting point — document it as a named constant so it is easy to tune.

#### Enrichment function

```python
def enrich_company_from_gleif(
    session: Session,
    company: Company,
    timeout: int = 15,
    rate_limit_delay: float = 0.5,
) -> dict[str, Any]:
    """Query GLEIF by company canonical_name, match, and update Company fields.

    Only updates fields that are currently NULL in the DB — does not
    overwrite manually set values.

    Updates:
        company.lei              (if NULL and match found)
        company.legal_name       (if NULL and match found)
        company.headquarters_country (if NULL and match found)

    Inserts CompanyAlias rows for each GLEIF otherName not already present
    (alias_type = "aka").

    Returns {
        "company": canonical_name,
        "matched": bool,
        "lei": str | None,
        "similarity": float | None,
        "aliases_added": int,
    }
    """
```

Logic:
1. Search GLEIF with `canonical_name` as the query.
2. Score each candidate with `_name_similarity(company.canonical_name, candidate_legal_name)`. Also check similarity against each `otherName`.
3. Take the highest-scoring candidate. If score >= `GLEIF_MATCH_THRESHOLD`, proceed.
4. Update NULL fields only. Do not touch fields already populated.
5. Sleep `rate_limit_delay` before returning (GLEIF has no documented rate limit but be polite).

```python
def enrich_all_companies(
    session: Session,
    rate_limit_delay: float = 0.5,
) -> list[dict]:
    """Run GLEIF enrichment for all Company rows where lei IS NULL.

    Returns a list of per-company result dicts from enrich_company_from_gleif().
    Commits after each successful update (not in one bulk transaction) so
    partial runs are not lost on error.
    """
```

Query: `SELECT * FROM companies WHERE lei IS NULL`. Iterate, call `enrich_company_from_gleif()`, log result at INFO.

### 3. `app/cli.py` — add two commands

```python
@app.command("seed-companies")
def seed_companies_cmd() -> None:
    """Seed the companies table from the curated battery supply chain reference list.

    Idempotent: skips companies whose canonical_name already exists.
    Run once after initial setup, then use ingest-gleif to enrich with LEI data.
    Run before: ingest-opensanctions (name matching), seed-hs-mappings (already done).
    """
    from app.services.ingestion.seed_companies import seed_companies

    s = _session()
    try:
        result = seed_companies(s)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()


@app.command("ingest-gleif")
def ingest_gleif_cmd(
    rate_limit_delay: float = typer.Option(
        0.5,
        "--delay",
        help="Seconds to wait between GLEIF API calls.",
    ),
) -> None:
    """Enrich companies with LEI data from the GLEIF public API (no key required).

    Queries GLEIF for each company where lei IS NULL. Updates lei, legal_name,
    headquarters_country (NULL fields only), and adds name aliases.

    Re-run after adding new companies. Safe to run multiple times.
    LEI data improves OpenSanctions entity matching precision significantly.
    """
    from app.services.ingestion.gleif import enrich_all_companies

    s = _session()
    try:
        results = enrich_all_companies(s, rate_limit_delay=rate_limit_delay)
        matched = sum(1 for r in results if r["matched"])
        typer.echo(json.dumps({
            "ok": True,
            "total_queried": len(results),
            "matched": matched,
            "unmatched": len(results) - matched,
        }, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()
```

### 4. `pyproject.toml` — hatch scripts

```toml
seed-companies = "bdi-ingest seed-companies"
ingest-gleif = "bdi-ingest ingest-gleif"
```

### 5. Tests — `tests/test_seed_companies.py` and `tests/test_gleif.py`

**`test_seed_companies.py`:**
- Verify `_COMPANIES` list has no duplicate `canonical_name` values
- Verify all `headquarters_country` values are 2-character strings
- Verify all `supply_chain_stage` values are in the allowed set
- Verify all `data_confidence` values are between 0.0 and 1.0
- Mock a session, call `seed_companies()`, verify inserted count matches len(_COMPANIES)
- Call again, verify inserted=0 (idempotency)

**`test_gleif.py`:**
- Test `_name_similarity`: verify identical strings = 1.0, completely different strings < 0.5, suffix-only differences > 0.82
- Test `enrich_company_from_gleif` with a mocked `httpx.get` returning one GLEIF candidate above threshold — verify `lei` is set on the company
- Test that fields already populated are NOT overwritten
- Test that a response below threshold results in `matched=False` and no DB changes

## Constraints

- No new dependencies — `httpx` (already present) and `difflib` (stdlib) are sufficient
- The `_COMPANIES` seed list is source-of-truth for the initial 40 companies — do not make it configurable or read from CSV. It lives in source code.
- GLEIF updates NULL fields only — never overwrite existing values. Manually set `lei` or `legal_name` values are authoritative.
- Use `structlog` in service modules, `typer.echo` only in CLI
- The seed does not create `CompanyMaterialExposure` or `CompanySupplyRelationship` rows — those are separate ingestion steps (SEC EDGAR, manual)
- Northvolt note: its bankruptcy/restructuring status is in `notes`. Do not add special handling for it — the `data_confidence=0.70` already signals uncertainty. The company still exists as an entity in the supply chain.

## Run order after implementation

```
bdi-ingest seed-companies      # seed 40 companies
bdi-ingest ingest-gleif        # enrich with LEI (run after seed-companies)
bdi-ingest ingest-opensanctions  # re-run — will now match against populated companies table
```
