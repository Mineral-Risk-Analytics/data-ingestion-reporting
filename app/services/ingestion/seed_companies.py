"""Curated seed data for ~75 strategically important EV battery supply chain
companies.

This module is static reference data — not ingested or scraped. Values are
manually verified from public sources (company websites, exchange filings,
Wikipedia). It is the source-of-truth for the initial companies table population
and is intentionally kept in source code (not a CSV) so that it is version-
controlled and auditable.

Two-pass seeding
----------------
``seed_companies()`` runs in two passes:

Pass 1: Upsert all companies by canonical_name. New rows are flushed
immediately so their UUIDs are available for parent resolution. Non-model
keys (``aliases``, ``gleif_search_name``, ``parent_canonical_name``) are
popped before ``Company(**data)`` and restored afterwards so the module-level
list is never mutated.

Pass 2: Resolve ``parent_canonical_name`` links. For each entry that carries
a ``parent_canonical_name``, query the DB for both child and parent and set
``child.parent_company_id``. Parents must exist in the DB before this pass
runs, which is guaranteed because all companies are inserted/flushed in
pass 1 before pass 2 begins (ordering within ``_COMPANIES`` does not matter
for correctness, though holding companies are placed first for readability).

Run order:
    bdi-ingest seed-companies      # this module (two-pass upsert + parent links)
    bdi-ingest ingest-gleif        # enrich with LEI
    bdi-ingest ingest-opensanctions  # re-run after companies are populated
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company, CompanyAlias

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Curated company data
# ---------------------------------------------------------------------------

_COMPANIES: list[dict] = [
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
        "gleif_search_name": "Sociedad Quimica y Minera de Chile",
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
        "gleif_search_name": "Zhejiang Huayou Cobalt",
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
        "gleif_search_name": "Lynas Rare Earths Ltd",
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
        "gleif_search_name": "POSCO FUTURE M",
        "parent_canonical_name": "POSCO Holdings",
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
        "gleif_search_name": "Shanshan Energy",
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
        "gleif_search_name": "Contemporary Amperex Technology",
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
        "parent_canonical_name": "LG Chem",
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
        "parent_canonical_name": "SK Innovation",
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
        "parent_canonical_name": "Panasonic Holdings",
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
        "gleif_search_name": "China Aviation Lithium Battery",
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
        "lei": "529900HNOAA1KXQJUQ27",
        "gleif_search_name": "Volkswagen AG",
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
        "lei": "VGRQXHF3J8VDLUA7XE92",
        "gleif_search_name": "Bayerische Motoren Werke",
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

    # ── HOLDING / PARENT COMPANIES ───────────────────────────────────────
    {
        "canonical_name": "LG Chem",
        "legal_name": "LG Chem, Ltd.",
        "supply_chain_stage": "holding",
        "headquarters_country": "KR",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "KRX:051910",
        "data_confidence": 0.90,
        "data_source": "manual",
        "notes": "Parent of LG Energy Solution (73.4% stake post-IPO). Also produces petrochemicals, advanced materials.",
        "aliases": [{"alias": "LG화학", "alias_type": "aka"}],
    },
    {
        "canonical_name": "SK Innovation",
        "legal_name": "SK Innovation Co., Ltd.",
        "supply_chain_stage": "holding",
        "headquarters_country": "KR",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "KRX:096770",
        "data_confidence": 0.85,
        "data_source": "manual",
        "notes": "Parent of SK On (battery) and SK IE Technology (separators). Energy and chemicals conglomerate.",
        "gleif_search_name": "SK Innovation",
        "aliases": [],
    },
    {
        "canonical_name": "Panasonic Holdings",
        "legal_name": "Panasonic Holdings Corporation",
        "supply_chain_stage": "holding",
        "headquarters_country": "JP",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "TYO:6752",
        "data_confidence": 0.90,
        "data_source": "manual",
        "notes": "Parent of Panasonic Energy Co., Ltd. Reorganised into holding structure in 2022.",
        "gleif_search_name": "Panasonic Holdings",
        "aliases": [{"alias": "Panasonic Corporation", "alias_type": "former_name"}],
    },
    {
        "canonical_name": "POSCO Holdings",
        "legal_name": "POSCO Holdings Inc.",
        "supply_chain_stage": "holding",
        "headquarters_country": "KR",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "KRX:005490",
        "data_confidence": 0.90,
        "data_source": "manual",
        "notes": "South Korean steel and materials conglomerate. Parent of POSCO Future M (battery materials). Formerly POSCO.",
        "gleif_search_name": "POSCO Holdings",
        "aliases": [{"alias": "POSCO", "alias_type": "former_name"}],
    },
    {
        "canonical_name": "Zhejiang Geely Holding Group",
        "legal_name": "Zhejiang Geely Holding Group Co., Ltd.",
        "supply_chain_stage": "holding",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": False,
        "data_confidence": 0.85,
        "data_source": "manual",
        "notes": "Private Chinese holding company. Controls Geely Auto Group, Volvo Cars, Polestar, Zeekr, Lotus, LEVC. Founded by Li Shufu.",
        "gleif_search_name": "Zhejiang Geely Holding",
        "aliases": [
            {"alias": "Geely Holding", "alias_type": "abbreviation"},
            {"alias": "吉利控股", "alias_type": "aka"},
        ],
    },

    # ── MINING SUBSIDIARIES ──────────────────────────────────────────────
    {
        "canonical_name": "Tenke Fungurume Mining",
        "legal_name": "Tenke Fungurume Mining SARL",
        "supply_chain_stage": "miner",
        "headquarters_country": "CD",
        "headquarters_region": "Africa",
        "is_public": False,
        "data_confidence": 0.85,
        "data_source": "manual",
        "parent_canonical_name": "CMOC Group",
        "notes": "CMOC 80% owned. One of world's largest cobalt and copper mines. Located in Lualaba Province, DRC. Former Freeport-McMoRan asset.",
        "aliases": [{"alias": "TFM", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Kisanfu Mining",
        "legal_name": "Kisanfu Mining SARL",
        "supply_chain_stage": "miner",
        "headquarters_country": "CD",
        "headquarters_region": "Africa",
        "is_public": False,
        "data_confidence": 0.80,
        "data_source": "manual",
        "parent_canonical_name": "CMOC Group",
        "notes": "CMOC 100% owned since 2021 (acquired from Freeport). High-grade cobalt/copper resource in DRC. Ramp-up ongoing.",
        "aliases": [{"alias": "KFM", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Mutanda Mining",
        "legal_name": "Mutanda Mining SARL",
        "supply_chain_stage": "miner",
        "headquarters_country": "CD",
        "headquarters_region": "Africa",
        "is_public": False,
        "data_confidence": 0.85,
        "data_source": "manual",
        "parent_canonical_name": "Glencore",
        "notes": "Glencore 100%. World's single largest cobalt mine by output. Located in Lualaba Province, DRC. Placed on care and maintenance 2019–2021.",
        "aliases": [{"alias": "MUMI", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Kamoto Copper Company",
        "legal_name": "Kamoto Copper Company SARL",
        "supply_chain_stage": "miner",
        "headquarters_country": "CD",
        "headquarters_region": "Africa",
        "is_public": False,
        "data_confidence": 0.80,
        "data_source": "manual",
        "parent_canonical_name": "Glencore",
        "notes": "Glencore 75%, DRC state (Gécamines) 25%. Copper and cobalt mine in Kolwezi, DRC. Significant historical DRC royalty dispute.",
        "aliases": [{"alias": "KCC", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Congo DPR Huayou Cobalt",
        "legal_name": "CDM S.A.",
        "supply_chain_stage": "miner",
        "headquarters_country": "CD",
        "headquarters_region": "Africa",
        "is_public": False,
        "data_confidence": 0.75,
        "data_source": "manual",
        "parent_canonical_name": "Huayou Cobalt",
        "notes": "Huayou-controlled cobalt mining operations in DRC. Multiple concessions in Katanga/Lualaba. Subject to ASM (artisanal mining) risk and human rights scrutiny.",
        "aliases": [{"alias": "CDM", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "PT Freeport Indonesia",
        "legal_name": "PT Freeport Indonesia",
        "supply_chain_stage": "miner",
        "headquarters_country": "ID",
        "headquarters_region": "Asia-Pacific",
        "is_public": False,
        "data_confidence": 0.85,
        "data_source": "manual",
        "parent_canonical_name": "Freeport-McMoRan",
        "notes": "Freeport 48.76%, Indonesian state (PT Inalum) 51.24%. Operates Grasberg — world's largest gold and second largest copper mine. Papua, Indonesia.",
        "aliases": [{"alias": "PTFI", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Cerro Verde",
        "legal_name": "Sociedad Minera Cerro Verde S.A.A.",
        "supply_chain_stage": "miner",
        "headquarters_country": "PE",
        "headquarters_region": "South America",
        "is_public": False,
        "data_confidence": 0.85,
        "data_source": "manual",
        "parent_canonical_name": "Freeport-McMoRan",
        "notes": "Freeport 53.56%, SMM 21%, Buenaventura 19.58%. Major copper producer in Arequipa, Peru.",
        "aliases": [],
    },
    {
        "canonical_name": "Vale Base Metals",
        "legal_name": "Vale Base Metals Limited",
        "supply_chain_stage": "miner",
        "headquarters_country": "CA",
        "headquarters_region": "North America",
        "is_public": False,
        "data_confidence": 0.85,
        "data_source": "manual",
        "parent_canonical_name": "Vale",
        "notes": "Vale's nickel, copper and cobalt division, carved out in 2022. Operations: Sudbury and Voisey's Bay (Canada), PT Vale Indonesia, VBM Brazil. Saudi Aramco and Engine No. 1 hold minority stakes.",
        "aliases": [{"alias": "VBM", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "BHP Nickel West",
        "legal_name": "BHP Nickel West Pty Ltd",
        "supply_chain_stage": "miner",
        "headquarters_country": "AU",
        "headquarters_region": "Asia-Pacific",
        "is_public": False,
        "data_confidence": 0.75,
        "data_source": "manual",
        "parent_canonical_name": "BHP Group",
        "notes": "BHP 100%. Nickel mining and refining in Western Australia (Kambalda, Mt Keith, Leinster). Placed on care and maintenance May 2024 due to nickel price collapse. Future uncertain.",
        "aliases": [],
    },
    {
        "canonical_name": "Escondida",
        "legal_name": "Minera Escondida Limitada",
        "supply_chain_stage": "miner",
        "headquarters_country": "CL",
        "headquarters_region": "South America",
        "is_public": False,
        "data_confidence": 0.90,
        "data_source": "manual",
        "parent_canonical_name": "BHP Group",
        "notes": "BHP 57.5%, Rio Tinto 30%, JECO consortium 12.5%. World's largest copper mine by output. Atacama Desert, Chile.",
        "aliases": [],
    },
    {
        "canonical_name": "Lynas Malaysia",
        "legal_name": "Lynas Malaysia Sdn Bhd",
        "supply_chain_stage": "refiner",
        "headquarters_country": "MY",
        "headquarters_region": "Asia-Pacific",
        "is_public": False,
        "data_confidence": 0.85,
        "data_source": "manual",
        "parent_canonical_name": "Lynas Rare Earths",
        "notes": "Lynas 100%. Advanced Materials Plant (LAMP) in Gebeng, Pahang — processes ore from Mt Weld (Australia). Largest REE processing plant outside China. Operating licence subject to periodic Malaysian government review.",
        "aliases": [{"alias": "LAMP", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Balama Graphite",
        "legal_name": "Twigg Exploration and Mining Limitada",
        "supply_chain_stage": "miner",
        "headquarters_country": "MZ",
        "headquarters_region": "Africa",
        "is_public": False,
        "data_confidence": 0.80,
        "data_source": "manual",
        "parent_canonical_name": "Syrah Resources",
        "notes": "Syrah 100%. Balama, Cabo Delgado Province, Mozambique. World's largest graphite deposit by resource. Output feeds Vidalia active anode material plant (Louisiana, US). Subject to regional insurgency risk in Cabo Delgado.",
        "aliases": [{"alias": "Balama", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Ivanhoe Mines",
        "legal_name": "Ivanhoe Mines Ltd.",
        "supply_chain_stage": "miner",
        "headquarters_country": "CA",
        "headquarters_region": "North America",
        "is_public": True,
        "public_ticker": "TSX:IVN",
        "data_confidence": 0.85,
        "data_source": "manual",
        "notes": "Operates Kamoa-Kakula (DRC, 39.6% stake) — world's second largest copper mine. Also Platreef (South Africa, PGMs) and Kipushi (DRC, zinc). Key DRC copper producer not controlled by Chinese or US parent.",
        "aliases": [{"alias": "IVN", "alias_type": "ticker"}],
    },

    # ── CELL MANUFACTURER SUBSIDIARIES ───────────────────────────────────
    {
        "canonical_name": "FinDreams Battery",
        "legal_name": "FinDreams Battery Co., Ltd.",
        "supply_chain_stage": "cell_maker",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": False,
        "data_confidence": 0.80,
        "data_source": "manual",
        "parent_canonical_name": "BYD",
        "notes": "BYD's wholly owned battery manufacturing subsidiary. Produces Blade Battery (LFP). Supplies BYD vehicles and select external OEMs including Toyota.",
        "gleif_search_name": "FinDreams Battery",
        "aliases": [],
    },
    {
        "canonical_name": "PowerCo SE",
        "legal_name": "PowerCo SE",
        "supply_chain_stage": "cell_maker",
        "headquarters_country": "DE",
        "headquarters_region": "Europe",
        "is_public": False,
        "data_confidence": 0.75,
        "data_source": "manual",
        "parent_canonical_name": "Volkswagen Group",
        "notes": "VW Group's battery manufacturing subsidiary. Planned gigafactories in Salzgitter (DE), Valencia (ES), St. Thomas (CA). Also developing unified prismatic cell format.",
        "aliases": [],
    },
    {
        "canonical_name": "Envision AESC",
        "legal_name": "Envision AESC Group Ltd.",
        "supply_chain_stage": "cell_maker",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": False,
        "data_confidence": 0.75,
        "data_source": "manual",
        "notes": "Formerly Nissan's battery manufacturing arm (AESC). Acquired by Envision Group (China) in 2018. Operates gigafactories in UK (Sunderland), US (Tennessee), Japan, France. Primary supplier to Nissan and Renault.",
        "aliases": [
            {"alias": "AESC", "alias_type": "former_name"},
            {"alias": "Automotive Energy Supply Corporation", "alias_type": "former_name"},
        ],
    },
    {
        "canonical_name": "Prime Planet and Energy Solutions",
        "legal_name": "Prime Planet and Energy & Solutions, Inc.",
        "supply_chain_stage": "cell_maker",
        "headquarters_country": "JP",
        "headquarters_region": "Asia-Pacific",
        "is_public": False,
        "data_confidence": 0.80,
        "data_source": "manual",
        "parent_canonical_name": "Toyota Motor Corporation",
        "notes": "Toyota 51%, Panasonic 49% joint venture. Primary cell supplier for Toyota and Lexus hybrids and EVs. Produces prismatic NMC and LFP cells. Formerly Prime Earth EV Energy (PEVE).",
        "gleif_search_name": "Prime Planet and Energy",
        "aliases": [{"alias": "PPES", "alias_type": "abbreviation"}],
    },

    # ── NEW OEMs ─────────────────────────────────────────────────────────
    {
        "canonical_name": "Toyota Motor Corporation",
        "legal_name": "Toyota Motor Corporation",
        "supply_chain_stage": "oem",
        "headquarters_country": "JP",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "TYO:7203",
        "data_confidence": 0.95,
        "data_source": "manual",
        "notes": "World's largest OEM by volume. Dominant hybrid (HEV) platform globally. Battery EV ramp slower than peers. Invests in solid-state battery development. ~20% stake in Subaru. Battery JV: PPES (with Panasonic).",
        "aliases": [{"alias": "Toyota", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Honda Motor Company",
        "legal_name": "Honda Motor Co., Ltd.",
        "supply_chain_stage": "oem",
        "headquarters_country": "JP",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "TYO:7267",
        "data_confidence": 0.90,
        "data_source": "manual",
        "notes": "Major Japanese OEM. Battery JV with LG Energy Solution for US cell manufacturing (L-H Battery Company). Announced merger with Nissan (2024, pending).",
        "aliases": [{"alias": "Honda", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Nissan Motor Company",
        "legal_name": "Nissan Motor Co., Ltd.",
        "supply_chain_stage": "oem",
        "headquarters_country": "JP",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "TYO:7201",
        "data_confidence": 0.85,
        "data_source": "manual",
        "notes": "Part of Renault-Nissan-Mitsubishi Alliance (cross-shareholdings, not parent-subsidiary). Pioneer EV OEM (Leaf). Sold AESC battery arm to Envision in 2018. Announced merger discussions with Honda (2024).",
        "aliases": [{"alias": "Nissan", "alias_type": "abbreviation"}],
    },
    {
        "canonical_name": "Subaru Corporation",
        "legal_name": "Subaru Corporation",
        "supply_chain_stage": "oem",
        "headquarters_country": "JP",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "TYO:7270",
        "data_confidence": 0.85,
        "data_source": "manual",
        "notes": "Toyota holds ~20% equity stake but Subaru operates independently. EV platform (Solterra) co-developed with Toyota. Battery sourcing: Panasonic, Samsung SDI.",
        "aliases": [],
    },
    {
        "canonical_name": "Rivian Automotive",
        "legal_name": "Rivian Automotive, Inc.",
        "supply_chain_stage": "oem",
        "headquarters_country": "US",
        "headquarters_region": "North America",
        "is_public": True,
        "public_ticker": "NASDAQ:RIVN",
        "data_confidence": 0.80,
        "data_source": "manual",
        "notes": "US EV startup. Products: R1T truck, R1S SUV, delivery vans (Amazon). Battery cells sourced from Samsung SDI. Single factory (Normal, IL). Volkswagen strategic investment announced 2024.",
        "aliases": [{"alias": "RIVN", "alias_type": "ticker"}],
    },
    {
        "canonical_name": "Lucid Group",
        "legal_name": "Lucid Group, Inc.",
        "supply_chain_stage": "oem",
        "headquarters_country": "US",
        "headquarters_region": "North America",
        "is_public": True,
        "public_ticker": "NASDAQ:LCID",
        "data_confidence": 0.75,
        "data_source": "manual",
        "notes": "Saudi PIF owns ~65%. Premium EV OEM (Lucid Air sedan). Operates AMP-1 factory (Arizona) and AMP-2 (Saudi Arabia, Jeddah). Produces own drivetrain and battery pack. Cells sourced externally.",
        "aliases": [{"alias": "LCID", "alias_type": "ticker"}],
    },
    {
        "canonical_name": "Geely Auto Group",
        "legal_name": "Geely Automobile Holdings Limited",
        "supply_chain_stage": "oem",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "HKEX:0175",
        "data_confidence": 0.85,
        "data_source": "manual",
        "parent_canonical_name": "Zhejiang Geely Holding Group",
        "notes": "Listed OEM arm of Geely Holding. Brands: Geely, Lynk & Co. Owns ~17% of Volvo Cars separately from parent's stake. Battery supplier: CATL, CALB.",
        "gleif_search_name": "Geely Automobile Holdings",
        "aliases": [
            {"alias": "Geely", "alias_type": "abbreviation"},
            {"alias": "吉利汽车", "alias_type": "aka"},
        ],
    },
    {
        "canonical_name": "Volvo Car Group",
        "legal_name": "Volvo Car AB",
        "supply_chain_stage": "oem",
        "headquarters_country": "SE",
        "headquarters_region": "Europe",
        "is_public": True,
        "public_ticker": "STO:VOLCAR B",
        "data_confidence": 0.85,
        "data_source": "manual",
        "parent_canonical_name": "Zhejiang Geely Holding Group",
        "notes": "Geely Holding majority owner (~82%). Listed on Nasdaq Stockholm. EV models: EX30, EX40, EX90. Battery supplier: CATL. Parent of Polestar.",
        "gleif_search_name": "Volvo Car AB",
        "aliases": [
            {"alias": "Volvo Cars", "alias_type": "aka"},
            {"alias": "Volvo", "alias_type": "abbreviation"},
        ],
    },
    {
        "canonical_name": "Polestar Automotive",
        "legal_name": "Polestar Automotive Holding UK PLC",
        "supply_chain_stage": "oem",
        "headquarters_country": "SE",
        "headquarters_region": "Europe",
        "is_public": True,
        "public_ticker": "NASDAQ:PSNY",
        "data_confidence": 0.75,
        "data_source": "manual",
        "parent_canonical_name": "Volvo Car Group",
        "notes": "Performance EV brand. Volvo Cars and Geely collectively hold majority. Manufacturing in China (Chengdu) and South Korea (Renault Samsung). Battery supplier: CATL.",
        "gleif_search_name": "Polestar Automotive",
        "aliases": [
            {"alias": "Polestar", "alias_type": "abbreviation"},
            {"alias": "PSNY", "alias_type": "ticker"},
        ],
    },
    {
        "canonical_name": "Zeekr",
        "legal_name": "Zeekr Intelligent Technology Holding Limited",
        "supply_chain_stage": "oem",
        "headquarters_country": "CN",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "NYSE:ZK",
        "data_confidence": 0.75,
        "data_source": "manual",
        "parent_canonical_name": "Zhejiang Geely Holding Group",
        "notes": "Premium EV brand of Geely Holding. NYSE listed 2024. Produces 001, 007, X models. Battery supplier: CATL. Also supplies Mobileye with autonomous test fleets.",
        "gleif_search_name": "Zeekr Intelligent Technology",
        "aliases": [{"alias": "ZK", "alias_type": "ticker"}],
    },
    {
        "canonical_name": "Kia Corporation",
        "legal_name": "Kia Corporation",
        "supply_chain_stage": "oem",
        "headquarters_country": "KR",
        "headquarters_region": "Asia-Pacific",
        "is_public": True,
        "public_ticker": "KRX:000270",
        "data_confidence": 0.90,
        "data_source": "manual",
        "parent_canonical_name": "Hyundai Motor Company",
        "notes": "Hyundai owns ~33.9%. Operates as independent brand under Hyundai Motor Group. EV6, EV9 flagship EVs. Battery suppliers: SK On, LG Energy Solution, Samsung SDI.",
        "aliases": [{"alias": "Kia", "alias_type": "abbreviation"}],
    },

    # ── RECYCLERS ────────────────────────────────────────────────────────
    {
        "canonical_name": "Redwood Materials",
        "legal_name": "Redwood Materials, Inc.",
        "supply_chain_stage": "recycler",
        "headquarters_country": "US",
        "headquarters_region": "North America",
        "is_public": False,
        "data_confidence": 0.80,
        "data_source": "manual",
        "notes": "Founded by JB Straubel (Tesla co-founder). Recycling and remanufacturing of lithium-ion batteries. Produces recycled cathode active material and anode copper foil. Partners: Ford, Panasonic Energy, Amazon, Volkswagen. Factory in Nevada.",
        "aliases": [],
    },
    {
        "canonical_name": "Cirba Solutions",
        "legal_name": "Cirba Solutions Inc.",
        "supply_chain_stage": "recycler",
        "headquarters_country": "US",
        "headquarters_region": "North America",
        "is_public": False,
        "data_confidence": 0.75,
        "data_source": "manual",
        "notes": "Formerly Retriev Technologies and RSB Logistics. Largest US battery recycler by volume. Processes lithium-ion, NiMH, and lead-acid batteries. Black mass processing facilities in Ohio and South Carolina.",
        "aliases": [
            {"alias": "Retriev Technologies", "alias_type": "former_name"},
        ],
    },
]

# ---------------------------------------------------------------------------
# Seed function
# ---------------------------------------------------------------------------

_ALLOWED_STAGES = {
    "miner", "refiner", "cell_maker", "pack_maker",
    "oem", "trader", "recycler", "holding", "other",
}


def seed_companies(session: Session) -> dict[str, int]:
    """Upsert all curated companies and their aliases, then resolve parent links.

    Uses canonical_name as the upsert key. Existing rows are partially updated
    for selected mutable fields only; immutable identity fields are not modified.

    Two-pass approach:
      Pass 1: Insert or update each company. session.flush() after each new
              insert so IDs are available for pass 2 parent resolution.
      Pass 2: For entries with parent_canonical_name, set
              child.parent_company_id if the parent is found in the DB and
              child.parent_company_id is currently NULL. Logs a warning if
              the parent is not found (does not raise).

    Returns {"inserted": int, "updated": int, "aliases_added": int,
             "parents_linked": int}
    """
    inserted = 0
    updated = 0
    aliases_added = 0

    # ── Pass 1: upsert ───────────────────────────────────────────────────
    for data in _COMPANIES:
        # Pop non-Company-model fields before constructing the ORM object.
        aliases = data.pop("aliases", [])
        gleif_search_name = data.pop("gleif_search_name", None)
        parent_canonical_name = data.pop("parent_canonical_name", None)
        canonical_name = data["canonical_name"]

        existing = session.scalar(
            select(Company).where(Company.canonical_name == canonical_name)
        )

        if existing is None:
            company = Company(**data)
            session.add(company)
            session.flush()
            log.info("seed_companies.inserted", canonical_name=canonical_name)
            inserted += 1
        else:
            company = existing
            changed_fields: list[str] = []
            mutable_seed_values = {
                "notes": data.get("notes"),
                "data_confidence": data.get("data_confidence"),
                "supply_chain_stage": data.get("supply_chain_stage"),
                "headquarters_region": data.get("headquarters_region"),
            }
            for field, seed_value in mutable_seed_values.items():
                if getattr(company, field) != seed_value:
                    setattr(company, field, seed_value)
                    changed_fields.append(field)

            if changed_fields:
                updated += 1
                log.info(
                    "seed_companies.updated",
                    canonical_name=canonical_name,
                    changed_fields=changed_fields,
                )
            else:
                log.debug("seed_companies.skip_existing", canonical_name=canonical_name)

        # Insert seed aliases — check-before-insert to honour the unique constraint.
        existing_aliases = {a.alias for a in company.aliases}
        for alias_data in aliases:
            if alias_data["alias"] not in existing_aliases:
                session.add(
                    CompanyAlias(
                        company_id=company.id,
                        alias=alias_data["alias"],
                        alias_type=alias_data.get("alias_type", "aka"),
                    )
                )
                existing_aliases.add(alias_data["alias"])
                aliases_added += 1

        # Add the LEI itself as a dedicated alias so entity resolution can match on it.
        seed_lei: str | None = data.get("lei")
        if seed_lei and seed_lei not in existing_aliases:
            session.add(
                CompanyAlias(
                    company_id=company.id,
                    alias=seed_lei,
                    alias_type="lei",
                )
            )
            aliases_added += 1

        # Restore non-model keys so the module-level list is not mutated.
        data["aliases"] = aliases
        if gleif_search_name is not None:
            data["gleif_search_name"] = gleif_search_name
        if parent_canonical_name is not None:
            data["parent_canonical_name"] = parent_canonical_name

    # ── Pass 2: resolve parent links ────────────────────────────────────
    parents_linked = 0
    for entry in _COMPANIES:
        pcn = entry.get("parent_canonical_name")
        if not pcn:
            continue

        child = session.scalar(
            select(Company).where(Company.canonical_name == entry["canonical_name"])
        )
        parent = session.scalar(
            select(Company).where(Company.canonical_name == pcn)
        )

        if parent is None:
            log.warning(
                "seed_companies.parent_not_found",
                canonical_name=entry["canonical_name"],
                parent_canonical_name=pcn,
            )
            continue

        if child is not None and child.parent_company_id is None:
            child.parent_company_id = parent.id
            log.info(
                "seed_companies.parent_linked",
                canonical_name=entry["canonical_name"],
                parent_canonical_name=pcn,
            )
            parents_linked += 1

    session.commit()

    log.info(
        "seed_companies.done",
        inserted=inserted,
        updated=updated,
        aliases_added=aliases_added,
        parents_linked=parents_linked,
    )
    return {
        "inserted": inserted,
        "updated": updated,
        "aliases_added": aliases_added,
        "parents_linked": parents_linked,
    }
