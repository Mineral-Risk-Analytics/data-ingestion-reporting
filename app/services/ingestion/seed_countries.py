"""Seed the ``countries`` reference table.

Covers all countries currently referenced across the ingestion pipeline:
  - All entries in the former ``REPORTER_COUNTRIES`` / ``CONSUMER_COUNTRIES``
    dicts (comtrade.py) — now stored as ``is_major_producer`` / ``is_major_consumer``
  - All entries in the former ``GTA_COUNTRY_MAP`` (gta.py) — now stored as
    ``common_names`` for name→ISO2 resolution
  - Additional countries that commonly appear in trade and risk data

Run via:
    bdi-ingest seed-countries

Idempotent: uses INSERT ... ON CONFLICT (iso2) DO UPDATE so re-running
refreshes ``common_names``, ``comtrade_code``, and flag columns without
touching ``created_at``.
"""

from __future__ import annotations

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.country import Country

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Country data
# ---------------------------------------------------------------------------
# Each entry:
#   iso2             str   ISO 3166-1 alpha-2 (PK)
#   name             str   canonical display name
#   iso3             str?  ISO 3166-1 alpha-3
#   region           str?  broad geographic region
#   comtrade_code    int?  UN Comtrade M49 numeric reporter code
#   common_names     list  full-name variants used by GTA/IEA/etc for resolution
#   is_major_producer bool  → export reporter in Comtrade runs
#   is_major_consumer bool  → import reporter in Comtrade runs

_COUNTRIES: list[dict] = [
    # ── Major battery material producers (export reporters) ──────────────────
    {
        "iso2": "CN", "name": "China", "iso3": "CHN", "region": "Asia",
        "comtrade_code": 156,
        "common_names": ["China", "People's Republic of China", "PRC", "Chine"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's largest producer of graphite, lithium processing, cell manufacturing.",
    },
    {
        "iso2": "CL", "name": "Chile", "iso3": "CHL", "region": "South America",
        "comtrade_code": 152,
        "common_names": ["Chile"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's largest lithium reserves; copper production.",
    },
    {
        "iso2": "AU", "name": "Australia", "iso3": "AUS", "region": "Oceania",
        "comtrade_code": 36,
        "common_names": ["Australia"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Major lithium (spodumene), nickel, and rare earth producer.",
    },
    {
        "iso2": "CD", "name": "Democratic Republic of the Congo", "iso3": "COD", "region": "Africa",
        "comtrade_code": 180,
        "common_names": [
            "Democratic Republic of the Congo", "DRC",
            "Congo, Democratic Republic", "Congo, Dem. Rep.", "DR Congo",
        ],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "~70% of global cobalt production.",
    },
    {
        "iso2": "ID", "name": "Indonesia", "iso3": "IDN", "region": "Asia",
        "comtrade_code": 360,
        "common_names": ["Indonesia"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's largest nickel producer; export restrictions enacted 2019–2023.",
    },
    {
        "iso2": "RU", "name": "Russia", "iso3": "RUS", "region": "Europe",
        "comtrade_code": 643,
        "common_names": ["Russia", "Russian Federation"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Major nickel and PGM producer (Norilsk Nickel).",
    },
    {
        "iso2": "ZA", "name": "South Africa", "iso3": "ZAF", "region": "Africa",
        "comtrade_code": 710,
        "common_names": ["South Africa"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's largest platinum-group metals producer; manganese.",
    },
    {
        "iso2": "PH", "name": "Philippines", "iso3": "PHL", "region": "Asia",
        "comtrade_code": 608,
        "common_names": ["Philippines"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Significant nickel laterite producer.",
    },
    {
        "iso2": "MZ", "name": "Mozambique", "iso3": "MOZ", "region": "Africa",
        "comtrade_code": 508,
        "common_names": ["Mozambique"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Emerging graphite producer (Syrah Resources, Volt Resources).",
    },
    {
        "iso2": "ZW", "name": "Zimbabwe", "iso3": "ZWE", "region": "Africa",
        "comtrade_code": 716,
        "common_names": ["Zimbabwe"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Lithium (Bikita Minerals); chromite.",
    },
    {
        "iso2": "BO", "name": "Bolivia", "iso3": "BOL", "region": "South America",
        "comtrade_code": 68,
        "common_names": ["Bolivia", "Bolivia, Plurinational State of", "Plurinational State of Bolivia"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Large lithium salt-flat reserves (Salar de Uyuni); limited production to date.",
    },
    {
        "iso2": "AR", "name": "Argentina", "iso3": "ARG", "region": "South America",
        "comtrade_code": 32,
        "common_names": ["Argentina"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Lithium triangle; growing lithium carbonate production.",
    },
    {
        "iso2": "PE", "name": "Peru", "iso3": "PER", "region": "South America",
        "comtrade_code": 604,
        "common_names": ["Peru"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Major copper and silver producer.",
    },
    {
        "iso2": "BR", "name": "Brazil", "iso3": "BRA", "region": "South America",
        "comtrade_code": 76,
        "common_names": ["Brazil", "Brasil"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Niobium (world's largest), rare earths, nickel.",
    },
    {
        "iso2": "ZM", "name": "Zambia", "iso3": "ZMB", "region": "Africa",
        "comtrade_code": 894,
        "common_names": ["Zambia"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Cobalt and copper (Copperbelt; second only to DRC).",
    },
    {
        "iso2": "MA", "name": "Morocco", "iso3": "MAR", "region": "Africa",
        "comtrade_code": 504,
        "common_names": ["Morocco", "Maroc"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's largest phosphate rock reserves.",
    },
    {
        "iso2": "MG", "name": "Madagascar", "iso3": "MDG", "region": "Africa",
        "comtrade_code": 450,
        "common_names": ["Madagascar"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Graphite and nickel (QMM/Rio Tinto).",
    },
    {
        "iso2": "KZ", "name": "Kazakhstan", "iso3": "KAZ", "region": "Asia",
        "comtrade_code": 398,
        "common_names": ["Kazakhstan"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Chromite, manganese, uranium; significant rare earth deposits.",
    },
    {
        "iso2": "MM", "name": "Myanmar", "iso3": "MMR", "region": "Asia",
        "comtrade_code": 104,
        "common_names": ["Myanmar", "Burma"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Major rare earth (heavy REE) and tin producer.",
    },
    {
        "iso2": "GN", "name": "Guinea", "iso3": "GIN", "region": "Africa",
        "comtrade_code": 324,
        "common_names": ["Guinea"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's largest bauxite reserves; cobalt.",
    },
    {
        "iso2": "GH", "name": "Ghana", "iso3": "GHA", "region": "Africa",
        "comtrade_code": 288,
        "common_names": ["Ghana"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Manganese (Ghana Manganese Company); bauxite.",
    },
    {
        "iso2": "GA", "name": "Gabon", "iso3": "GAB", "region": "Africa",
        "comtrade_code": 266,
        "common_names": ["Gabon"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's second-largest manganese producer.",
    },
    {
        "iso2": "NA", "name": "Namibia", "iso3": "NAM", "region": "Africa",
        "comtrade_code": 516,
        "common_names": ["Namibia"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Emerging lithium and rare earth producer; uranium.",
    },
    {
        "iso2": "PG", "name": "Papua New Guinea", "iso3": "PNG", "region": "Oceania",
        "comtrade_code": 598,
        "common_names": ["Papua New Guinea", "PNG"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Nickel and cobalt (Ramu NiCo).",
    },
    {
        "iso2": "LA", "name": "Lao PDR", "iso3": "LAO", "region": "Asia",
        "comtrade_code": 418,
        "common_names": ["Laos", "Lao People's Democratic Republic", "Lao PDR"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Potash, copper, gold.",
    },
    {
        "iso2": "VN", "name": "Vietnam", "iso3": "VNM", "region": "Asia",
        "comtrade_code": 704,
        "common_names": ["Vietnam", "Viet Nam"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Rare earths; titanium (ilmenite); tungsten.",
    },
    {
        "iso2": "MY", "name": "Malaysia", "iso3": "MYS", "region": "Asia",
        "comtrade_code": 458,
        "common_names": ["Malaysia"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Rare earth processing (Lynas Gebeng plant); tin.",
    },
    {
        "iso2": "ET", "name": "Ethiopia", "iso3": "ETH", "region": "Africa",
        "comtrade_code": 231,
        "common_names": ["Ethiopia"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Tantalum (coltan); emerging critical mineral sector.",
    },
    {
        "iso2": "TZ", "name": "Tanzania", "iso3": "TZA", "region": "Africa",
        "comtrade_code": 834,
        "common_names": ["Tanzania", "Tanzania, United Republic of"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Graphite; nickel; emerging lithium.",
    },
    {
        "iso2": "NG", "name": "Nigeria", "iso3": "NGA", "region": "Africa",
        "comtrade_code": 566,
        "common_names": ["Nigeria"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Lithium; tin; columbite-tantalite.",
    },
    {
        "iso2": "CI", "name": "Côte d'Ivoire", "iso3": "CIV", "region": "Africa",
        "comtrade_code": 384,
        "common_names": ["Côte d'Ivoire", "Ivory Coast", "Cote d'Ivoire"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Manganese; cobalt (minor).",
    },
    # ── Major consumer / manufacturing economies (import reporters) ──────────
    {
        "iso2": "US", "name": "United States", "iso3": "USA", "region": "North America",
        "comtrade_code": 842,
        "common_names": ["United States", "United States of America", "USA", "US"],
        "is_major_producer": True, "is_major_consumer": True,
        "notes": "Major lithium producer (Nevada); IRA critical mineral designation authority.",
    },
    {
        "iso2": "JP", "name": "Japan", "iso3": "JPN", "region": "Asia",
        "comtrade_code": 392,
        "common_names": ["Japan"],
        "is_major_producer": True, "is_major_consumer": True,
        "notes": "Major cell and battery pack manufacturer; imports most raw materials.",
    },
    {
        "iso2": "KR", "name": "South Korea", "iso3": "KOR", "region": "Asia",
        "comtrade_code": 410,
        "common_names": ["South Korea", "Korea, Republic of", "Korea"],
        "is_major_producer": True, "is_major_consumer": True,
        "notes": "Samsung SDI, LG Energy Solution, SK On — major cell manufacturers.",
    },
    {
        "iso2": "DE", "name": "Germany", "iso3": "DEU", "region": "Europe",
        "comtrade_code": 276,
        "common_names": ["Germany", "Deutschland"],
        "is_major_producer": True, "is_major_consumer": True,
        "notes": "Major EV and battery pack manufacturer; BASF cathode production.",
    },
    {
        "iso2": "CA", "name": "Canada", "iso3": "CAN", "region": "North America",
        "comtrade_code": 124,
        "common_names": ["Canada"],
        "is_major_producer": True, "is_major_consumer": True,
        "notes": "Nickel, cobalt, lithium production; IRA-aligned trade partner.",
    },
    {
        "iso2": "FR", "name": "France", "iso3": "FRA", "region": "Europe",
        "comtrade_code": 251,
        "common_names": ["France"],
        "is_major_producer": False, "is_major_consumer": True,
        "notes": "Renault, Stellantis; Dunkirk gigafactory (ACC).",
    },
    {
        "iso2": "GB", "name": "United Kingdom", "iso3": "GBR", "region": "Europe",
        "comtrade_code": 826,
        "common_names": ["United Kingdom", "UK", "Great Britain", "Britain"],
        "is_major_producer": False, "is_major_consumer": True,
        "notes": "Britishvolt (failed); AESC Sunderland; Arrival.",
    },
    {
        "iso2": "BE", "name": "Belgium", "iso3": "BEL", "region": "Europe",
        "comtrade_code": 56,
        "common_names": ["Belgium", "Belgique", "België"],
        "is_major_producer": False, "is_major_consumer": True,
        "notes": "Umicore — major cobalt refining and cathode precursor hub.",
    },
    {
        "iso2": "IN", "name": "India", "iso3": "IND", "region": "Asia",
        "comtrade_code": 699,
        "common_names": ["India"],
        "is_major_producer": False, "is_major_consumer": True,
        "notes": "Fast-growing EV market; emerging domestic battery manufacturing.",
    },
    # ── Other significant trade-policy actors ────────────────────────────────
    {
        "iso2": "TH", "name": "Thailand", "iso3": "THA", "region": "Asia",
        "comtrade_code": 764,
        "common_names": ["Thailand"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Regional EV assembly hub (Toyota, BYD).",
    },
    {
        "iso2": "MX", "name": "Mexico", "iso3": "MEX", "region": "North America",
        "comtrade_code": 484,
        "common_names": ["Mexico"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Top-5 copper producer; significant silver, zinc, manganese; USMCA EV supply chain.",
    },
    {
        "iso2": "TR", "name": "Turkey", "iso3": "TUR", "region": "Europe",
        "comtrade_code": 792,
        "common_names": ["Turkey", "Türkiye"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's largest boron producer (~40% of global supply); chromite.",
    },
    {
        "iso2": "NO", "name": "Norway", "iso3": "NOR", "region": "Europe",
        "comtrade_code": 578,
        "common_names": ["Norway"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "World's highest EV adoption rate; Freyr gigafactory.",
    },
    {
        "iso2": "FI", "name": "Finland", "iso3": "FIN", "region": "Europe",
        "comtrade_code": 246,
        "common_names": ["Finland"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Boliden nickel/cobalt refining (Harjavalta); Keliber lithium mine; Northvolt cell plant.",
    },
    {
        "iso2": "SE", "name": "Sweden", "iso3": "SWE", "region": "Europe",
        "comtrade_code": 752,
        "common_names": ["Sweden"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Northvolt HQ and Skellefteå gigafactory.",
    },
    {
        "iso2": "NL", "name": "Netherlands", "iso3": "NLD", "region": "Europe",
        "comtrade_code": 528,
        "common_names": ["Netherlands", "Holland"],
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "PL", "name": "Poland", "iso3": "POL", "region": "Europe",
        "comtrade_code": 616,
        "common_names": ["Poland", "Polska"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "LG Energy Solution Wrocław — largest cell plant outside Asia.",
    },
    {
        "iso2": "HU", "name": "Hungary", "iso3": "HUN", "region": "Europe",
        "comtrade_code": 348,
        "common_names": ["Hungary"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Samsung SDI and CATL gigafactories under construction.",
    },
    {
        "iso2": "CZ", "name": "Czechia", "iso3": "CZE", "region": "Europe",
        "comtrade_code": 203,
        "common_names": ["Czechia", "Czech Republic"],
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "ES", "name": "Spain", "iso3": "ESP", "region": "Europe",
        "comtrade_code": 724,
        "common_names": ["Spain", "España"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Volkswagen Sagunto gigafactory; cobalt refining.",
    },
    {
        "iso2": "IT", "name": "Italy", "iso3": "ITA", "region": "Europe",
        "comtrade_code": 380,
        "common_names": ["Italy", "Italia"],
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "PT", "name": "Portugal", "iso3": "PRT", "region": "Europe",
        "comtrade_code": 620,
        "common_names": ["Portugal"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Lithium (world's largest hard rock deposits); tungsten.",
    },
    {
        "iso2": "SG", "name": "Singapore", "iso3": "SGP", "region": "Asia",
        "comtrade_code": 702,
        "common_names": ["Singapore"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Regional trading hub; cobalt spot market.",
    },
    {
        "iso2": "TW", "name": "Taiwan", "iso3": "TWN", "region": "Asia",
        "comtrade_code": 490,
        "common_names": ["Taiwan", "Taiwan, Province of China", "Chinese Taipei"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "CATL Taiwan; minor cell manufacturing.",
    },
    {
        "iso2": "NZ", "name": "New Zealand", "iso3": "NZL", "region": "Oceania",
        "comtrade_code": 554,
        "common_names": ["New Zealand"],
        "is_major_producer": False, "is_major_consumer": False,
    },
    # ── Additional producers: nickel / cobalt / REE ─────────────────────────
    {
        "iso2": "NC", "name": "New Caledonia", "iso3": "NCL", "region": "Oceania",
        "comtrade_code": 540,
        "common_names": ["New Caledonia", "Nouvelle-Calédonie"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "~8% of global nickel production; Glencore Koniambo, SLN, Prony Resources.",
    },
    {
        "iso2": "CU", "name": "Cuba", "iso3": "CUB", "region": "North America",
        "comtrade_code": 192,
        "common_names": ["Cuba"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Nickel and cobalt (Moa laterite deposit); ~4% of world nickel output.",
    },
    {
        "iso2": "UA", "name": "Ukraine", "iso3": "UKR", "region": "Europe",
        "comtrade_code": 804,
        "common_names": ["Ukraine"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Titanium (CRMA-listed; ~5% global ilmenite); graphite; neon gas (~50% of global supply pre-2022).",
    },
    {
        "iso2": "MN", "name": "Mongolia", "iso3": "MNG", "region": "Asia",
        "comtrade_code": 496,
        "common_names": ["Mongolia"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Copper and gold (Oyu Tolgoi, Rio Tinto); coal; emerging rare earth deposits.",
    },
    {
        "iso2": "LK", "name": "Sri Lanka", "iso3": "LKA", "region": "Asia",
        "comtrade_code": 144,
        "common_names": ["Sri Lanka", "Sri Lanka, Democratic Socialist Republic of"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Natural vein graphite (highest purity globally); minor but high-value.",
    },
    {
        "iso2": "UZ", "name": "Uzbekistan", "iso3": "UZB", "region": "Asia",
        "comtrade_code": 860,
        "common_names": ["Uzbekistan"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Uranium; rare earth elements; copper (Almalyk).",
    },
    # ── Additional producers: copper / lithium ───────────────────────────────
    {
        "iso2": "EC", "name": "Ecuador", "iso3": "ECU", "region": "South America",
        "comtrade_code": 218,
        "common_names": ["Ecuador"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Emerging copper producer (Mirador mine); gold.",
    },
    {
        "iso2": "RS", "name": "Serbia", "iso3": "SRB", "region": "Europe",
        "comtrade_code": 688,
        "common_names": ["Serbia"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Jadar lithium-boron deposit (Rio Tinto); one of Europe's largest lithium resources.",
    },
    {
        "iso2": "AT", "name": "Austria", "iso3": "AUT", "region": "Europe",
        "comtrade_code": 40,
        "common_names": ["Austria", "Österreich"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Tungsten (Wolfram Bergbau, ~10% of EU supply); lithium (European Lithium Wolfsberg).",
    },
    # ── Additional producers: tantalum / coltan / REE ────────────────────────
    {
        "iso2": "RW", "name": "Rwanda", "iso3": "RWA", "region": "Africa",
        "comtrade_code": 646,
        "common_names": ["Rwanda"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Tantalum/coltan (~15% of global tantalum); tin; tungsten (3TG minerals).",
    },
    {
        "iso2": "BI", "name": "Burundi", "iso3": "BDI", "region": "Africa",
        "comtrade_code": 108,
        "common_names": ["Burundi"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Coltan and tantalum; nickel (Musongati deposit — large but undeveloped).",
    },
    {
        "iso2": "UG", "name": "Uganda", "iso3": "UGA", "region": "Africa",
        "comtrade_code": 800,
        "common_names": ["Uganda"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Cobalt; rare earths (Makuutu deposit); coltan.",
    },
    {
        "iso2": "AO", "name": "Angola", "iso3": "AGO", "region": "Africa",
        "comtrade_code": 24,
        "common_names": ["Angola"],
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Cobalt and manganese; phosphate (emerging); oil-revenue-funded battery-mineral exploration.",
    },
    # ── Trade-policy actors and refining hubs ────────────────────────────────
    {
        "iso2": "SA", "name": "Saudi Arabia", "iso3": "SAU", "region": "Asia",
        "comtrade_code": 682,
        "common_names": ["Saudi Arabia", "Saudi Arabia, Kingdom of", "Kingdom of Saudi Arabia"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Vision 2030 battery investment (SABIC, ARAMCO ventures); significant GTA trade-policy actor.",
    },
    {
        "iso2": "AE", "name": "United Arab Emirates", "iso3": "ARE", "region": "Asia",
        "comtrade_code": 784,
        "common_names": ["United Arab Emirates", "UAE", "U.A.E."],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Metals trading and re-export hub; EV investment (Masdar, TAQA); GTA-active.",
    },
    {
        "iso2": "CH", "name": "Switzerland", "iso3": "CHE", "region": "Europe",
        "comtrade_code": 756,
        "common_names": ["Switzerland", "Suisse", "Schweiz"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Glencore HQ (cobalt/nickel/copper trading); commodity price-setting hub.",
    },
    {
        "iso2": "EG", "name": "Egypt", "iso3": "EGY", "region": "Africa",
        "comtrade_code": 818,
        "common_names": ["Egypt", "Egypt, Arab Republic of"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Phosphate rock (significant producer); regional trade hub.",
    },
    {
        "iso2": "PK", "name": "Pakistan", "iso3": "PAK", "region": "Asia",
        "comtrade_code": 586,
        "common_names": ["Pakistan"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "GTA-active trade-policy actor; minor chromite.",
    },
    {
        "iso2": "BD", "name": "Bangladesh", "iso3": "BGD", "region": "Asia",
        "comtrade_code": 50,
        "common_names": ["Bangladesh"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "GTA-active; growing EV two-wheeler market.",
    },
    # ── EU manufacturing nations not previously covered ───────────────────────
    {
        "iso2": "SK", "name": "Slovakia", "iso3": "SVK", "region": "Europe",
        "comtrade_code": 703,
        "common_names": ["Slovakia", "Slovak Republic"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Volkswagen battery and EV production (Bratislava); Inobat gigafactory.",
    },
    {
        "iso2": "RO", "name": "Romania", "iso3": "ROU", "region": "Europe",
        "comtrade_code": 642,
        "common_names": ["Romania"],
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "EU coverage; Ford EV assembly; emerging battery supply chain.",
    },
    # ── Bloc identifiers (not ISO 3166 countries) ────────────────────────────
    {
        "iso2": "EU", "name": "European Union", "iso3": None, "region": "Europe",
        "comtrade_code": None,
        "common_names": ["European Union", "EU"],
        "is_major_producer": False, "is_major_consumer": True,
        "notes": "Bloc identifier used in regulation_geography_scope and risk_event_geography.",
    },
]


# ---------------------------------------------------------------------------
# Seed function
# ---------------------------------------------------------------------------

def seed_countries(session: Session) -> dict[str, int]:
    """Upsert all country rows. Idempotent.

    Uses INSERT ... ON CONFLICT (iso2) DO UPDATE so re-running refreshes
    ``common_names``, ``comtrade_code``, and flag columns without touching
    ``created_at``.

    Returns ``{"inserted": int, "updated": int}``.
    """
    inserted = updated = 0

    for c in _COUNTRIES:
        stmt = (
            pg_insert(Country)
            .values(**c)
            .on_conflict_do_update(
                index_elements=["iso2"],
                set_={
                    "name":               c["name"],
                    "iso3":               c.get("iso3"),
                    "region":             c.get("region"),
                    "comtrade_code":      c.get("comtrade_code"),
                    "common_names":       c.get("common_names"),
                    "is_major_producer":  c["is_major_producer"],
                    "is_major_consumer":  c["is_major_consumer"],
                    "notes":              c.get("notes"),
                },
            )
        )
        result = session.execute(stmt)
        # rowcount == 1 for both insert and update in pg; use matched_rows
        if result.rowcount == 1:
            # Distinguish insert vs update via a pre-check is expensive; just
            # count all upserted rows and report as inserted for simplicity.
            inserted += 1

    session.flush()
    log.info("seed_countries.done", total=len(_COUNTRIES))
    return {"inserted": inserted, "updated": updated}
