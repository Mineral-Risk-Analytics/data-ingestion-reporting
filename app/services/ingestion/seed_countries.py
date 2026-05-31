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
#   iso2                str   ISO 3166-1 alpha-2 (PK)
#   name                str   canonical display name
#   iso3                str?  ISO 3166-1 alpha-3
#   region              str?  broad geographic region
#   comtrade_code       int?  UN Comtrade M49 numeric reporter code
#   common_names        list  full-name variants used by GTA/IEA/etc for resolution
#   detection_patterns  list  [{pattern, context}] for GeographyCache free-text detection
#                             context "primary" → relevance 0.9 (direct reference)
#                             context "mentioned" → relevance 0.6 (adjectival/contextual)
#   is_sanctions_risk   bool  high concentration of sanctioned entities (OpenSanctions)
#   is_major_producer   bool  → export reporter in Comtrade runs
#   is_major_consumer   bool  → import reporter in Comtrade runs

_COUNTRIES: list[dict] = [
    # ── Major battery material producers (export reporters) ──────────────────
    {
        "iso2": "CN", "name": "China", "iso3": "CHN", "region": "Asia",
        "comtrade_code": 156,
        "common_names": ["China", "People's Republic of China", "PRC", "Chine"],
        "detection_patterns": [
            {"pattern": "xinjiang",  "context": "primary"},
            {"pattern": "china",     "context": "primary"},
            {"pattern": "chinese",   "context": "mentioned"},
            {"pattern": "prc",       "context": "primary"},
        ],
        "is_sanctions_risk": True,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's largest producer of graphite, lithium processing, cell manufacturing.",
    },
    {
        "iso2": "CL", "name": "Chile", "iso3": "CHL", "region": "South America",
        "comtrade_code": 152,
        "common_names": ["Chile"],
        "detection_patterns": [
            {"pattern": "chile",    "context": "primary"},
            {"pattern": "chilean",  "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's largest lithium reserves; copper production.",
    },
    {
        "iso2": "AU", "name": "Australia", "iso3": "AUS", "region": "Oceania",
        "comtrade_code": 36,
        "common_names": ["Australia"],
        "detection_patterns": [
            {"pattern": "australia",   "context": "primary"},
            {"pattern": "australian",  "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Major lithium (spodumene), nickel, and rare earth producer.",
    },
    {
        "iso2": "CD", "name": "Democratic Republic of the Congo", "iso3": "COD", "region": "Africa",
        "comtrade_code": 180,
        "common_names": [
            "Democratic Republic of the Congo", "DRC",
            "Congo, Democratic Republic", "Congo, Dem. Rep.", "DR Congo",
            "Congo (Kinshasa)",
        ],
        "detection_patterns": [
            {"pattern": "democratic republic of congo", "context": "primary"},
            {"pattern": "drc",                          "context": "primary"},
            {"pattern": "congo (kinshasa)",             "context": "primary"},
            {"pattern": "congo",                        "context": "mentioned"},
        ],
        "is_sanctions_risk": True,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "~70% of global cobalt production.",
    },
    {
        "iso2": "ID", "name": "Indonesia", "iso3": "IDN", "region": "Asia",
        "comtrade_code": 360,
        "common_names": ["Indonesia"],
        "detection_patterns": [
            {"pattern": "indonesia",   "context": "primary"},
            {"pattern": "indonesian",  "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's largest nickel producer; export restrictions enacted 2019–2023.",
    },
    {
        "iso2": "RU", "name": "Russia", "iso3": "RUS", "region": "Europe",
        "comtrade_code": 643,
        "common_names": ["Russia", "Russian Federation"],
        "detection_patterns": [
            {"pattern": "russia",   "context": "primary"},
            {"pattern": "russian",  "context": "mentioned"},
        ],
        "is_sanctions_risk": True,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Major nickel and PGM producer (Norilsk Nickel).",
    },
    {
        "iso2": "ZA", "name": "South Africa", "iso3": "ZAF", "region": "Africa",
        "comtrade_code": 710,
        "common_names": ["South Africa"],
        "detection_patterns": [
            {"pattern": "south africa",   "context": "primary"},
            {"pattern": "south african",  "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's largest platinum-group metals producer; manganese.",
    },
    {
        "iso2": "PH", "name": "Philippines", "iso3": "PHL", "region": "Asia",
        "comtrade_code": 608,
        "common_names": ["Philippines"],
        "detection_patterns": [
            {"pattern": "philippines",  "context": "primary"},
            {"pattern": "philippine",   "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Significant nickel laterite producer.",
    },
    {
        "iso2": "MZ", "name": "Mozambique", "iso3": "MOZ", "region": "Africa",
        "comtrade_code": 508,
        "common_names": ["Mozambique"],
        "detection_patterns": [{"pattern": "mozambique", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Emerging graphite producer (Syrah Resources, Volt Resources).",
    },
    {
        "iso2": "ZW", "name": "Zimbabwe", "iso3": "ZWE", "region": "Africa",
        "comtrade_code": 716,
        "common_names": ["Zimbabwe"],
        "detection_patterns": [
            {"pattern": "zimbabwe",   "context": "primary"},
            {"pattern": "zimbabwean", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Lithium (Bikita Minerals); chromite.",
    },
    {
        "iso2": "BO", "name": "Bolivia", "iso3": "BOL", "region": "South America",
        "comtrade_code": 68,
        "common_names": ["Bolivia", "Bolivia, Plurinational State of", "Plurinational State of Bolivia"],
        "detection_patterns": [{"pattern": "bolivia", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Large lithium salt-flat reserves (Salar de Uyuni); limited production to date.",
    },
    {
        "iso2": "AR", "name": "Argentina", "iso3": "ARG", "region": "South America",
        "comtrade_code": 32,
        "common_names": ["Argentina"],
        "detection_patterns": [
            {"pattern": "argentina",   "context": "primary"},
            {"pattern": "argentinian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Lithium triangle; growing lithium carbonate production.",
    },
    {
        "iso2": "PE", "name": "Peru", "iso3": "PER", "region": "South America",
        "comtrade_code": 604,
        "common_names": ["Peru"],
        "detection_patterns": [
            {"pattern": "peru",    "context": "primary"},
            {"pattern": "peruvian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Major copper and silver producer.",
    },
    {
        "iso2": "BR", "name": "Brazil", "iso3": "BRA", "region": "South America",
        "comtrade_code": 76,
        "common_names": ["Brazil", "Brasil"],
        "detection_patterns": [
            {"pattern": "brazil",    "context": "primary"},
            {"pattern": "brazilian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Niobium (world's largest), rare earths, nickel.",
    },
    {
        "iso2": "ZM", "name": "Zambia", "iso3": "ZMB", "region": "Africa",
        "comtrade_code": 894,
        "common_names": ["Zambia"],
        "detection_patterns": [
            {"pattern": "zambia",   "context": "primary"},
            {"pattern": "zambian",  "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Cobalt and copper (Copperbelt; second only to DRC).",
    },
    {
        "iso2": "MA", "name": "Morocco", "iso3": "MAR", "region": "Africa",
        "comtrade_code": 504,
        "common_names": ["Morocco", "Maroc"],
        "detection_patterns": [
            {"pattern": "morocco",  "context": "primary"},
            {"pattern": "moroccan", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's largest phosphate rock reserves.",
    },
    {
        "iso2": "MG", "name": "Madagascar", "iso3": "MDG", "region": "Africa",
        "comtrade_code": 450,
        "common_names": ["Madagascar"],
        "detection_patterns": [{"pattern": "madagascar", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Graphite and nickel (QMM/Rio Tinto).",
    },
    {
        "iso2": "KZ", "name": "Kazakhstan", "iso3": "KAZ", "region": "Asia",
        "comtrade_code": 398,
        "common_names": ["Kazakhstan"],
        "detection_patterns": [
            {"pattern": "kazakhstan",   "context": "primary"},
            {"pattern": "kazakhstani",  "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Chromite, manganese, uranium; significant rare earth deposits.",
    },
    {
        "iso2": "MM", "name": "Myanmar", "iso3": "MMR", "region": "Asia",
        "comtrade_code": 104,
        "common_names": ["Myanmar", "Burma"],
        "detection_patterns": [
            {"pattern": "myanmar", "context": "primary"},
            {"pattern": "burma",   "context": "primary"},
            {"pattern": "burmese", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Major rare earth (heavy REE) and tin producer.",
    },
    {
        "iso2": "GN", "name": "Guinea", "iso3": "GIN", "region": "Africa",
        "comtrade_code": 324,
        "common_names": ["Guinea"],
        "detection_patterns": [{"pattern": "guinea", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's largest bauxite reserves; cobalt.",
    },
    {
        "iso2": "GH", "name": "Ghana", "iso3": "GHA", "region": "Africa",
        "comtrade_code": 288,
        "common_names": ["Ghana"],
        "detection_patterns": [{"pattern": "ghana", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Manganese (Ghana Manganese Company); bauxite.",
    },
    {
        "iso2": "GA", "name": "Gabon", "iso3": "GAB", "region": "Africa",
        "comtrade_code": 266,
        "common_names": ["Gabon"],
        "detection_patterns": [{"pattern": "gabon", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's second-largest manganese producer.",
    },
    {
        "iso2": "NA", "name": "Namibia", "iso3": "NAM", "region": "Africa",
        "comtrade_code": 516,
        "common_names": ["Namibia"],
        "detection_patterns": [{"pattern": "namibia", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Emerging lithium and rare earth producer; uranium.",
    },
    {
        "iso2": "PG", "name": "Papua New Guinea", "iso3": "PNG", "region": "Oceania",
        "comtrade_code": 598,
        "common_names": ["Papua New Guinea", "PNG"],
        "detection_patterns": [{"pattern": "papua new guinea", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Nickel and cobalt (Ramu NiCo).",
    },
    {
        "iso2": "LA", "name": "Lao PDR", "iso3": "LAO", "region": "Asia",
        "comtrade_code": 418,
        "common_names": ["Laos", "Lao People's Democratic Republic", "Lao PDR"],
        "detection_patterns": [{"pattern": "laos", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Potash, copper, gold.",
    },
    {
        "iso2": "VN", "name": "Vietnam", "iso3": "VNM", "region": "Asia",
        "comtrade_code": 704,
        "common_names": ["Vietnam", "Viet Nam"],
        "detection_patterns": [{"pattern": "vietnam", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Rare earths; titanium (ilmenite); tungsten.",
    },
    {
        "iso2": "MY", "name": "Malaysia", "iso3": "MYS", "region": "Asia",
        "comtrade_code": 458,
        "common_names": ["Malaysia"],
        "detection_patterns": [{"pattern": "malaysia", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Rare earth processing (Lynas Gebeng plant); tin.",
    },
    {
        "iso2": "ET", "name": "Ethiopia", "iso3": "ETH", "region": "Africa",
        "comtrade_code": 231,
        "common_names": ["Ethiopia"],
        "detection_patterns": [{"pattern": "ethiopia", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Tantalum (coltan); emerging critical mineral sector.",
    },
    {
        "iso2": "TZ", "name": "Tanzania", "iso3": "TZA", "region": "Africa",
        "comtrade_code": 834,
        "common_names": ["Tanzania", "Tanzania, United Republic of"],
        "detection_patterns": [{"pattern": "tanzania", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Graphite; nickel; emerging lithium.",
    },
    {
        "iso2": "NG", "name": "Nigeria", "iso3": "NGA", "region": "Africa",
        "comtrade_code": 566,
        "common_names": ["Nigeria"],
        "detection_patterns": [{"pattern": "nigeria", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Lithium; tin; columbite-tantalite.",
    },
    {
        "iso2": "CI", "name": "Côte d'Ivoire", "iso3": "CIV", "region": "Africa",
        "comtrade_code": 384,
        "common_names": ["Côte d'Ivoire", "Ivory Coast", "Cote d'Ivoire"],
        "detection_patterns": [
            {"pattern": "côte d'ivoire", "context": "primary"},
            {"pattern": "ivory coast",   "context": "primary"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Manganese; cobalt (minor).",
    },
    # ── Major consumer / manufacturing economies (import reporters) ──────────
    {
        "iso2": "US", "name": "United States", "iso3": "USA", "region": "North America",
        "comtrade_code": 842,
        "common_names": ["United States", "United States of America", "USA", "US"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": True,
        "notes": "Major lithium producer (Nevada); IRA critical mineral designation authority.",
    },
    {
        "iso2": "JP", "name": "Japan", "iso3": "JPN", "region": "Asia",
        "comtrade_code": 392,
        "common_names": ["Japan"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": True,
        "notes": "Major cell and battery pack manufacturer; imports most raw materials.",
    },
    {
        "iso2": "KR", "name": "South Korea", "iso3": "KOR", "region": "Asia",
        "comtrade_code": 410,
        "common_names": ["South Korea", "Korea, Republic of", "Korea"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": True,
        "notes": "Samsung SDI, LG Energy Solution, SK On — major cell manufacturers.",
    },
    {
        "iso2": "DE", "name": "Germany", "iso3": "DEU", "region": "Europe",
        "comtrade_code": 276,
        "common_names": ["Germany", "Deutschland"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": True,
        "notes": "Major EV and battery pack manufacturer; BASF cathode production.",
    },
    {
        "iso2": "CA", "name": "Canada", "iso3": "CAN", "region": "North America",
        "comtrade_code": 124,
        "common_names": ["Canada"],
        "detection_patterns": [
            {"pattern": "canada",   "context": "primary"},
            {"pattern": "canadian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": True,
        "notes": "Nickel, cobalt, lithium production; IRA-aligned trade partner.",
    },
    {
        "iso2": "FR", "name": "France", "iso3": "FRA", "region": "Europe",
        "comtrade_code": 251,
        "common_names": ["France"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": True,
        "notes": "Renault, Stellantis; Dunkirk gigafactory (ACC).",
    },
    {
        "iso2": "GB", "name": "United Kingdom", "iso3": "GBR", "region": "Europe",
        "comtrade_code": 826,
        "common_names": ["United Kingdom", "UK", "Great Britain", "Britain"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": True,
        "notes": "Britishvolt (failed); AESC Sunderland; Arrival.",
    },
    {
        "iso2": "BE", "name": "Belgium", "iso3": "BEL", "region": "Europe",
        "comtrade_code": 56,
        "common_names": ["Belgium", "Belgique", "België"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": True,
        "notes": "Umicore — major cobalt refining and cathode precursor hub.",
    },
    {
        "iso2": "IN", "name": "India", "iso3": "IND", "region": "Asia",
        "comtrade_code": 699,
        "common_names": ["India"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": True,
        "notes": "Fast-growing EV market; emerging domestic battery manufacturing.",
    },
    # ── Other significant trade-policy actors ────────────────────────────────
    {
        "iso2": "TH", "name": "Thailand", "iso3": "THA", "region": "Asia",
        "comtrade_code": 764,
        "common_names": ["Thailand"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Regional EV assembly hub (Toyota, BYD).",
    },
    {
        "iso2": "MX", "name": "Mexico", "iso3": "MEX", "region": "North America",
        "comtrade_code": 484,
        "common_names": ["Mexico"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Top-5 copper producer; significant silver, zinc, manganese; USMCA EV supply chain.",
    },
    {
        "iso2": "TR", "name": "Turkey", "iso3": "TUR", "region": "Europe",
        "comtrade_code": 792,
        "common_names": ["Turkey", "Türkiye"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "World's largest boron producer (~40% of global supply); chromite.",
    },
    {
        "iso2": "NO", "name": "Norway", "iso3": "NOR", "region": "Europe",
        "comtrade_code": 578,
        "common_names": ["Norway"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "World's highest EV adoption rate; Freyr gigafactory.",
    },
    {
        "iso2": "FI", "name": "Finland", "iso3": "FIN", "region": "Europe",
        "comtrade_code": 246,
        "common_names": ["Finland"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Boliden nickel/cobalt refining (Harjavalta); Keliber lithium mine; Northvolt cell plant.",
    },
    {
        "iso2": "SE", "name": "Sweden", "iso3": "SWE", "region": "Europe",
        "comtrade_code": 752,
        "common_names": ["Sweden"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Northvolt HQ and Skellefteå gigafactory.",
    },
    {
        "iso2": "NL", "name": "Netherlands", "iso3": "NLD", "region": "Europe",
        "comtrade_code": 528,
        "common_names": ["Netherlands", "Holland"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "PL", "name": "Poland", "iso3": "POL", "region": "Europe",
        "comtrade_code": 616,
        "common_names": ["Poland", "Polska"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "LG Energy Solution Wrocław — largest cell plant outside Asia.",
    },
    {
        "iso2": "HU", "name": "Hungary", "iso3": "HUN", "region": "Europe",
        "comtrade_code": 348,
        "common_names": ["Hungary"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Samsung SDI and CATL gigafactories under construction.",
    },
    {
        "iso2": "CZ", "name": "Czechia", "iso3": "CZE", "region": "Europe",
        "comtrade_code": 203,
        "common_names": ["Czechia", "Czech Republic"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "ES", "name": "Spain", "iso3": "ESP", "region": "Europe",
        "comtrade_code": 724,
        "common_names": ["Spain", "España"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Volkswagen Sagunto gigafactory; cobalt refining.",
    },
    {
        "iso2": "IT", "name": "Italy", "iso3": "ITA", "region": "Europe",
        "comtrade_code": 380,
        "common_names": ["Italy", "Italia"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "PT", "name": "Portugal", "iso3": "PRT", "region": "Europe",
        "comtrade_code": 620,
        "common_names": ["Portugal"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Lithium (world's largest hard rock deposits); tungsten.",
    },
    {
        "iso2": "SG", "name": "Singapore", "iso3": "SGP", "region": "Asia",
        "comtrade_code": 702,
        "common_names": ["Singapore"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Regional trading hub; cobalt spot market.",
    },
    {
        "iso2": "TW", "name": "Taiwan", "iso3": "TWN", "region": "Asia",
        "comtrade_code": 490,
        "common_names": ["Taiwan", "Taiwan, Province of China", "Chinese Taipei"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "CATL Taiwan; minor cell manufacturing.",
    },
    {
        "iso2": "NZ", "name": "New Zealand", "iso3": "NZL", "region": "Oceania",
        "comtrade_code": 554,
        "common_names": ["New Zealand"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    # ── Additional producers: nickel / cobalt / REE ─────────────────────────
    {
        "iso2": "NC", "name": "New Caledonia", "iso3": "NCL", "region": "Oceania",
        "comtrade_code": 540,
        "common_names": ["New Caledonia", "Nouvelle-Calédonie"],
        "detection_patterns": [{"pattern": "new caledonia", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "~8% of global nickel production; Glencore Koniambo, SLN, Prony Resources.",
    },
    {
        "iso2": "CU", "name": "Cuba", "iso3": "CUB", "region": "North America",
        "comtrade_code": 192,
        "common_names": ["Cuba"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Nickel and cobalt (Moa laterite deposit); ~4% of world nickel output.",
    },
    {
        "iso2": "UA", "name": "Ukraine", "iso3": "UKR", "region": "Europe",
        "comtrade_code": 804,
        "common_names": ["Ukraine"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Titanium (CRMA-listed; ~5% global ilmenite); graphite; neon gas (~50% of global supply pre-2022).",
    },
    {
        "iso2": "MN", "name": "Mongolia", "iso3": "MNG", "region": "Asia",
        "comtrade_code": 496,
        "common_names": ["Mongolia"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Copper and gold (Oyu Tolgoi, Rio Tinto); coal; emerging rare earth deposits.",
    },
    {
        "iso2": "LK", "name": "Sri Lanka", "iso3": "LKA", "region": "Asia",
        "comtrade_code": 144,
        "common_names": ["Sri Lanka", "Sri Lanka, Democratic Socialist Republic of"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Natural vein graphite (highest purity globally); minor but high-value.",
    },
    {
        "iso2": "UZ", "name": "Uzbekistan", "iso3": "UZB", "region": "Asia",
        "comtrade_code": 860,
        "common_names": ["Uzbekistan"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Uranium; rare earth elements; copper (Almalyk).",
    },
    # ── Additional producers: copper / lithium ───────────────────────────────
    {
        "iso2": "EC", "name": "Ecuador", "iso3": "ECU", "region": "South America",
        "comtrade_code": 218,
        "common_names": ["Ecuador"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Emerging copper producer (Mirador mine); gold.",
    },
    {
        "iso2": "RS", "name": "Serbia", "iso3": "SRB", "region": "Europe",
        "comtrade_code": 688,
        "common_names": ["Serbia"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Jadar lithium-boron deposit (Rio Tinto); one of Europe's largest lithium resources.",
    },
    {
        "iso2": "AT", "name": "Austria", "iso3": "AUT", "region": "Europe",
        "comtrade_code": 40,
        "common_names": ["Austria", "Österreich"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Tungsten (Wolfram Bergbau, ~10% of EU supply); lithium (European Lithium Wolfsberg).",
    },
    # ── Additional producers: tantalum / coltan / REE ────────────────────────
    {
        "iso2": "RW", "name": "Rwanda", "iso3": "RWA", "region": "Africa",
        "comtrade_code": 646,
        "common_names": ["Rwanda"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Tantalum/coltan (~15% of global tantalum); tin; tungsten (3TG minerals).",
    },
    {
        "iso2": "BI", "name": "Burundi", "iso3": "BDI", "region": "Africa",
        "comtrade_code": 108,
        "common_names": ["Burundi"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Coltan and tantalum; nickel (Musongati deposit — large but undeveloped).",
    },
    {
        "iso2": "UG", "name": "Uganda", "iso3": "UGA", "region": "Africa",
        "comtrade_code": 800,
        "common_names": ["Uganda"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Cobalt; rare earths (Makuutu deposit); coltan.",
    },
    {
        "iso2": "AO", "name": "Angola", "iso3": "AGO", "region": "Africa",
        "comtrade_code": 24,
        "common_names": ["Angola"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Cobalt and manganese; phosphate (emerging); oil-revenue-funded battery-mineral exploration.",
    },
    # ── Trade-policy actors and refining hubs ────────────────────────────────
    {
        "iso2": "SA", "name": "Saudi Arabia", "iso3": "SAU", "region": "Asia",
        "comtrade_code": 682,
        "common_names": ["Saudi Arabia", "Saudi Arabia, Kingdom of", "Kingdom of Saudi Arabia"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Vision 2030 battery investment (SABIC, ARAMCO ventures); significant GTA trade-policy actor.",
    },
    {
        "iso2": "AE", "name": "United Arab Emirates", "iso3": "ARE", "region": "Asia",
        "comtrade_code": 784,
        "common_names": ["United Arab Emirates", "UAE", "U.A.E."],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Metals trading and re-export hub; EV investment (Masdar, TAQA); GTA-active.",
    },
    {
        "iso2": "CH", "name": "Switzerland", "iso3": "CHE", "region": "Europe",
        "comtrade_code": 756,
        "common_names": ["Switzerland", "Suisse", "Schweiz"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Glencore HQ (cobalt/nickel/copper trading); commodity price-setting hub.",
    },
    {
        "iso2": "EG", "name": "Egypt", "iso3": "EGY", "region": "Africa",
        "comtrade_code": 818,
        "common_names": ["Egypt", "Egypt, Arab Republic of"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Phosphate rock (significant producer); regional trade hub.",
    },
    {
        "iso2": "PK", "name": "Pakistan", "iso3": "PAK", "region": "Asia",
        "comtrade_code": 586,
        "common_names": ["Pakistan"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "GTA-active trade-policy actor; minor chromite.",
    },
    {
        "iso2": "BD", "name": "Bangladesh", "iso3": "BGD", "region": "Asia",
        "comtrade_code": 50,
        "common_names": ["Bangladesh"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "GTA-active; growing EV two-wheeler market.",
    },
    # ── EU manufacturing nations not previously covered ───────────────────────
    {
        "iso2": "SK", "name": "Slovakia", "iso3": "SVK", "region": "Europe",
        "comtrade_code": 703,
        "common_names": ["Slovakia", "Slovak Republic"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Volkswagen battery and EV production (Bratislava); Inobat gigafactory.",
    },
    {
        "iso2": "RO", "name": "Romania", "iso3": "ROU", "region": "Europe",
        "comtrade_code": 642,
        "common_names": ["Romania"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "EU coverage; Ford EV assembly; emerging battery supply chain.",
    },
    # ── High-sanctions-risk / Iran-Korea-North Korea ─────────────────────────
    {
        "iso2": "IR", "name": "Iran", "iso3": "IRN", "region": "Asia",
        "comtrade_code": 364,
        "common_names": ["Iran", "Iran, Islamic Republic of", "Islamic Republic of Iran"],
        "detection_patterns": [{"pattern": "iran", "context": "primary"}],
        "is_sanctions_risk": True,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Subject to comprehensive US/EU/UN sanctions; high OpenSanctions entity count.",
    },
    {
        "iso2": "KP", "name": "North Korea", "iso3": "PRK", "region": "Asia",
        "comtrade_code": 408,
        "common_names": ["North Korea", "Korea, Democratic People's Republic of", "DPRK"],
        "detection_patterns": [
            {"pattern": "north korea", "context": "primary"},
            {"pattern": "dprk",        "context": "primary"},
        ],
        "is_sanctions_risk": True,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Subject to comprehensive US/EU/UN sanctions; high OpenSanctions entity count.",
    },
    # ── Bloc identifiers (not ISO 3166 countries) ────────────────────────────
    {
        "iso2": "EU", "name": "European Union", "iso3": None, "region": "Europe",
        "comtrade_code": None,
        "common_names": ["European Union", "EU"],
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": True,
        "notes": "Bloc identifier used in regulation_geography_scope and risk_event_geography.",
    },

    # ── Added 2026-05-09: countries appearing in MCS 2026 World Production
    # rows that were missing from this seed.  Five affect launch-10 chapters
    # (PHOSPHATE ROCK: IL/SY/TN; TUNGSTEN: RW already present; GRAPHITE: LK
    # already present; REE: GL — reserves only, no current production).
    # The remainder appear in non-launch chapters (potash, soda ash, talc,
    # etc.) but are added here so future MCS editions don't silently drop
    # their data.  Minimal entries — flags default False; partner can
    # promote individuals if/when they become priority producers.

    # Phosphate Rock producers (Middle East / North Africa)
    {
        "iso2": "IL", "name": "Israel", "iso3": "ISR", "region": "Asia",
        "comtrade_code": 376,
        "common_names": ["Israel"],
        "detection_patterns": [
            {"pattern": "israel",  "context": "primary"},
            {"pattern": "israeli", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Phosphate Rock and bromine producer (Dead Sea Works / ICL).",
    },
    {
        "iso2": "TN", "name": "Tunisia", "iso3": "TUN", "region": "Africa",
        "comtrade_code": 788,
        "common_names": ["Tunisia"],
        "detection_patterns": [
            {"pattern": "tunisia",  "context": "primary"},
            {"pattern": "tunisian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Phosphate Rock — top-10 global producer (Compagnie des Phosphates de Gafsa).",
    },
    {
        "iso2": "SY", "name": "Syria", "iso3": "SYR", "region": "Asia",
        "comtrade_code": 760,
        "common_names": ["Syria", "Syrian Arab Republic"],
        "detection_patterns": [
            {"pattern": "syria",  "context": "primary"},
            {"pattern": "syrian", "context": "mentioned"},
        ],
        "is_sanctions_risk": True,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Phosphate Rock producer (limited operations under sanctions).",
    },

    # Greenland — REE reserves (no current production); Botswana — natural soda ash
    {
        "iso2": "GL", "name": "Greenland", "iso3": "GRL", "region": "Europe",
        "comtrade_code": 304,
        "common_names": ["Greenland"],
        "detection_patterns": [
            {"pattern": "greenland", "context": "primary"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Significant REE reserves (Kvanefjeld, Kringlerne) — production not yet active.",
    },
    {
        "iso2": "BW", "name": "Botswana", "iso3": "BWA", "region": "Africa",
        "comtrade_code": 72,
        "common_names": ["Botswana"],
        "detection_patterns": [
            {"pattern": "botswana", "context": "primary"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Natural soda ash producer (Botswana Ash); also copper/nickel via Khoemacau.",
    },

    # Belarus — sanctioned potash producer
    {
        "iso2": "BY", "name": "Belarus", "iso3": "BLR", "region": "Europe",
        "comtrade_code": 112,
        "common_names": ["Belarus", "Belorussia"],
        "detection_patterns": [
            {"pattern": "belarus",     "context": "primary"},
            {"pattern": "belarusian",  "context": "mentioned"},
            {"pattern": "belorussian", "context": "mentioned"},
        ],
        "is_sanctions_risk": True,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Potash (Belaruskali) — sanctions disruption since 2021.",
    },

    # Smaller producers — minimal entries, flags default false
    {
        "iso2": "BT", "name": "Bhutan", "iso3": "BTN", "region": "Asia",
        "comtrade_code": 64,
        "common_names": ["Bhutan"],
        "detection_patterns": [{"pattern": "bhutan", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "CM", "name": "Cameroon", "iso3": "CMR", "region": "Africa",
        "comtrade_code": 120,
        "common_names": ["Cameroon"],
        "detection_patterns": [
            {"pattern": "cameroon",   "context": "primary"},
            {"pattern": "cameroonian","context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "CY", "name": "Cyprus", "iso3": "CYP", "region": "Europe",
        "comtrade_code": 196,
        "common_names": ["Cyprus"],
        "detection_patterns": [
            {"pattern": "cyprus",   "context": "primary"},
            {"pattern": "cypriot",  "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "DK", "name": "Denmark", "iso3": "DNK", "region": "Europe",
        "comtrade_code": 208,
        "common_names": ["Denmark"],
        "detection_patterns": [
            {"pattern": "denmark",  "context": "primary"},
            {"pattern": "danish",   "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "EE", "name": "Estonia", "iso3": "EST", "region": "Europe",
        "comtrade_code": 233,
        "common_names": ["Estonia"],
        "detection_patterns": [
            {"pattern": "estonia",  "context": "primary"},
            {"pattern": "estonian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "GE", "name": "Georgia", "iso3": "GEO", "region": "Asia",
        "comtrade_code": 268,
        "common_names": ["Georgia"],
        # Note: NO detection_patterns — "georgia" collides with the US state
        # of Georgia, which appears in MRDS / facility free-text data.  Keep
        # detection off until partner adds context-aware disambiguation.
        "detection_patterns": None,
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Caucasus country — manganese producer (Chiatura).",
    },
    {
        "iso2": "GT", "name": "Guatemala", "iso3": "GTM", "region": "North America",
        "comtrade_code": 320,
        "common_names": ["Guatemala"],
        "detection_patterns": [
            {"pattern": "guatemala",  "context": "primary"},
            {"pattern": "guatemalan", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "KW", "name": "Kuwait", "iso3": "KWT", "region": "Asia",
        "comtrade_code": 414,
        "common_names": ["Kuwait"],
        "detection_patterns": [
            {"pattern": "kuwait",  "context": "primary"},
            {"pattern": "kuwaiti", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "LV", "name": "Latvia", "iso3": "LVA", "region": "Europe",
        "comtrade_code": 428,
        "common_names": ["Latvia"],
        "detection_patterns": [
            {"pattern": "latvia",  "context": "primary"},
            {"pattern": "latvian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "LS", "name": "Lesotho", "iso3": "LSO", "region": "Africa",
        "comtrade_code": 426,
        "common_names": ["Lesotho"],
        "detection_patterns": [{"pattern": "lesotho", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "LT", "name": "Lithuania", "iso3": "LTU", "region": "Europe",
        "comtrade_code": 440,
        "common_names": ["Lithuania"],
        "detection_patterns": [
            {"pattern": "lithuania",  "context": "primary"},
            {"pattern": "lithuanian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "OM", "name": "Oman", "iso3": "OMN", "region": "Asia",
        "comtrade_code": 512,
        "common_names": ["Oman"],
        "detection_patterns": [
            {"pattern": "oman",  "context": "primary"},
            {"pattern": "omani", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "QA", "name": "Qatar", "iso3": "QAT", "region": "Asia",
        "comtrade_code": 634,
        "common_names": ["Qatar"],
        "detection_patterns": [
            {"pattern": "qatar",  "context": "primary"},
            {"pattern": "qatari", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "TT", "name": "Trinidad and Tobago", "iso3": "TTO", "region": "North America",
        "comtrade_code": 780,
        "common_names": ["Trinidad and Tobago", "Trinidad"],
        "detection_patterns": [{"pattern": "trinidad", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },

    # ── Added 2026-05-09 (second wave): countries that were in the parser's
    # legacy ``_COUNTRY_ISO2`` dict but missing from this seed.  Architectural
    # cleanup — single source of truth for country name → ISO-2 resolution
    # is now this seed's ``common_names`` field.  Several affect launch-10
    # data: PHOSPHATE ROCK (Jordan/Algeria/Senegal/Mali); SILICON (Iceland);
    # ALUMINUM via BAUXITE chapter (Greece/Jamaica/Sierra Leone/Mauritania/
    # Guyana producers).  Lithium is a separate launch material; Mali is a
    # listed lithium producer.

    # Phosphate Rock producers
    {
        "iso2": "JO", "name": "Jordan", "iso3": "JOR", "region": "Asia",
        "comtrade_code": 400,
        "common_names": ["Jordan"],
        "detection_patterns": [
            {"pattern": "jordan",   "context": "primary"},
            {"pattern": "jordanian","context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Phosphate Rock and potash producer (Jordan Phosphate Mines / APC).",
    },
    {
        "iso2": "DZ", "name": "Algeria", "iso3": "DZA", "region": "Africa",
        "comtrade_code": 12,
        "common_names": ["Algeria"],
        "detection_patterns": [
            {"pattern": "algeria",  "context": "primary"},
            {"pattern": "algerian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Phosphate Rock producer (Ferphos).",
    },
    {
        "iso2": "SN", "name": "Senegal", "iso3": "SEN", "region": "Africa",
        "comtrade_code": 686,
        "common_names": ["Senegal"],
        "detection_patterns": [
            {"pattern": "senegal",   "context": "primary"},
            {"pattern": "senegalese","context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Phosphate Rock and zircon producer.",
    },

    # Bauxite producers (BAUXITE AND ALUMINA chapter feeds Aluminum ore stage)
    {
        "iso2": "GR", "name": "Greece", "iso3": "GRC", "region": "Europe",
        "comtrade_code": 300,
        "common_names": ["Greece"],
        "detection_patterns": [
            {"pattern": "greece", "context": "primary"},
            {"pattern": "greek",  "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Bauxite producer (Mytilineos / Aluminium of Greece).",
    },
    {
        "iso2": "JM", "name": "Jamaica", "iso3": "JAM", "region": "North America",
        "comtrade_code": 388,
        "common_names": ["Jamaica"],
        "detection_patterns": [
            {"pattern": "jamaica",  "context": "primary"},
            {"pattern": "jamaican", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Bauxite producer.",
    },
    {
        "iso2": "SL", "name": "Sierra Leone", "iso3": "SLE", "region": "Africa",
        "comtrade_code": 694,
        "common_names": ["Sierra Leone"],
        "detection_patterns": [{"pattern": "sierra leone", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Bauxite, rutile (titanium ore), iron ore.",
    },
    {
        "iso2": "GY", "name": "Guyana", "iso3": "GUY", "region": "South America",
        "comtrade_code": 328,
        "common_names": ["Guyana"],
        "detection_patterns": [{"pattern": "guyana", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Bauxite producer.",
    },
    {
        "iso2": "MR", "name": "Mauritania", "iso3": "MRT", "region": "Africa",
        "comtrade_code": 478,
        "common_names": ["Mauritania"],
        "detection_patterns": [
            {"pattern": "mauritania",  "context": "primary"},
            {"pattern": "mauritanian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Iron ore producer (SNIM); copper.",
    },

    # Silicon metal / ferrosilicon producers
    {
        "iso2": "IS", "name": "Iceland", "iso3": "ISL", "region": "Europe",
        "comtrade_code": 352,
        "common_names": ["Iceland"],
        "detection_patterns": [
            {"pattern": "iceland",   "context": "primary"},
            {"pattern": "icelandic", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Silicon metal smelter (PCC BakkiSilicon); aluminum smelting.",
    },

    # Lithium producers (Mali appears in MCS 2026 lithium production)
    {
        "iso2": "ML", "name": "Mali", "iso3": "MLI", "region": "Africa",
        "comtrade_code": 466,
        "common_names": ["Mali"],
        "detection_patterns": [
            {"pattern": "mali",   "context": "primary"},
            {"pattern": "malian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Lithium (Goulamina), gold producer.",
    },

    # Other countries appearing in MCS production rows
    {
        "iso2": "AF", "name": "Afghanistan", "iso3": "AFG", "region": "Asia",
        "comtrade_code": 4,
        "common_names": ["Afghanistan"],
        "detection_patterns": [
            {"pattern": "afghanistan", "context": "primary"},
            {"pattern": "afghan",      "context": "mentioned"},
        ],
        "is_sanctions_risk": True,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Significant lithium / REE / copper reserves; production limited.",
    },
    {
        "iso2": "AL", "name": "Albania", "iso3": "ALB", "region": "Europe",
        "comtrade_code": 8,
        "common_names": ["Albania"],
        "detection_patterns": [
            {"pattern": "albania",  "context": "primary"},
            {"pattern": "albanian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Chromite producer.",
    },
    {
        "iso2": "AM", "name": "Armenia", "iso3": "ARM", "region": "Asia",
        "comtrade_code": 51,
        "common_names": ["Armenia"],
        "detection_patterns": [
            {"pattern": "armenia",  "context": "primary"},
            {"pattern": "armenian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Copper, molybdenum producer.",
    },
    {
        "iso2": "AZ", "name": "Azerbaijan", "iso3": "AZE", "region": "Asia",
        "comtrade_code": 31,
        "common_names": ["Azerbaijan"],
        "detection_patterns": [
            {"pattern": "azerbaijan", "context": "primary"},
            {"pattern": "azeri",      "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "BH", "name": "Bahrain", "iso3": "BHR", "region": "Asia",
        "comtrade_code": 48,
        "common_names": ["Bahrain"],
        "detection_patterns": [
            {"pattern": "bahrain",  "context": "primary"},
            {"pattern": "bahraini", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": True, "is_major_consumer": False,
        "notes": "Aluminum smelter (Aluminium Bahrain / ALBA).",
    },
    {
        "iso2": "BA", "name": "Bosnia and Herzegovina", "iso3": "BIH", "region": "Europe",
        "comtrade_code": 70,
        "common_names": ["Bosnia and Herzegovina", "Bosnia"],
        "detection_patterns": [
            {"pattern": "bosnia",     "context": "primary"},
            {"pattern": "bosnian",    "context": "mentioned"},
            {"pattern": "herzegovina","context": "primary"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "BG", "name": "Bulgaria", "iso3": "BGR", "region": "Europe",
        "comtrade_code": 100,
        "common_names": ["Bulgaria"],
        "detection_patterns": [
            {"pattern": "bulgaria",  "context": "primary"},
            {"pattern": "bulgarian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Copper smelter (Aurubis Bulgaria); lead/zinc.",
    },
    {
        "iso2": "KH", "name": "Cambodia", "iso3": "KHM", "region": "Asia",
        "comtrade_code": 116,
        "common_names": ["Cambodia"],
        "detection_patterns": [
            {"pattern": "cambodia",  "context": "primary"},
            {"pattern": "cambodian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "CO", "name": "Colombia", "iso3": "COL", "region": "South America",
        "comtrade_code": 170,
        "common_names": ["Colombia"],
        "detection_patterns": [
            {"pattern": "colombia",  "context": "primary"},
            {"pattern": "colombian", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Coal, ferronickel, gold.",
    },
    {
        "iso2": "ER", "name": "Eritrea", "iso3": "ERI", "region": "Africa",
        "comtrade_code": 232,
        "common_names": ["Eritrea"],
        "detection_patterns": [{"pattern": "eritrea", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Copper, zinc, gold producer.",
    },
    {
        "iso2": "IE", "name": "Ireland", "iso3": "IRL", "region": "Europe",
        "comtrade_code": 372,
        "common_names": ["Ireland"],
        "detection_patterns": [
            {"pattern": "ireland", "context": "primary"},
            {"pattern": "irish",   "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Zinc mining (Tara Mines).",
    },
    {
        "iso2": "KE", "name": "Kenya", "iso3": "KEN", "region": "Africa",
        "comtrade_code": 404,
        "common_names": ["Kenya"],
        "detection_patterns": [
            {"pattern": "kenya",  "context": "primary"},
            {"pattern": "kenyan", "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Soda ash, fluorspar producer.",
    },
    {
        "iso2": "XK", "name": "Kosovo", "iso3": "XKX", "region": "Europe",
        "comtrade_code": None,  # Kosovo lacks an M49 numeric code
        "common_names": ["Kosovo"],
        "detection_patterns": [{"pattern": "kosovo", "context": "primary"}],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Lead, zinc, lignite.  No UN M49 code; ISO 3166-1 alpha-2 'XK' is provisional.",
    },
    {
        "iso2": "KG", "name": "Kyrgyzstan", "iso3": "KGZ", "region": "Asia",
        "comtrade_code": 417,
        "common_names": ["Kyrgyzstan"],
        "detection_patterns": [
            {"pattern": "kyrgyzstan", "context": "primary"},
            {"pattern": "kyrgyz",     "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Gold (Kumtor); antimony.",
    },
    {
        "iso2": "NE", "name": "Niger", "iso3": "NER", "region": "Africa",
        "comtrade_code": 562,
        "common_names": ["Niger"],
        "detection_patterns": [
            {"pattern": "niger",     "context": "primary"},
            # Skip "nigerien" / "nigerian" — high collision with Nigeria
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Uranium producer (Orano).",
    },
    {
        "iso2": "SD", "name": "Sudan", "iso3": "SDN", "region": "Africa",
        "comtrade_code": 729,
        "common_names": ["Sudan"],
        "detection_patterns": [
            {"pattern": "sudan",    "context": "primary"},
            {"pattern": "sudanese", "context": "mentioned"},
        ],
        "is_sanctions_risk": True,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Gold producer; chromium.",
    },
    {
        "iso2": "TJ", "name": "Tajikistan", "iso3": "TJK", "region": "Asia",
        "comtrade_code": 762,
        "common_names": ["Tajikistan"],
        "detection_patterns": [
            {"pattern": "tajikistan", "context": "primary"},
            {"pattern": "tajik",      "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Aluminum smelter (TALCO); antimony.",
    },
    {
        "iso2": "TG", "name": "Togo", "iso3": "TGO", "region": "Africa",
        "comtrade_code": 768,
        "common_names": ["Togo"],
        "detection_patterns": [
            {"pattern": "togo",    "context": "primary"},
            {"pattern": "togolese","context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Phosphate Rock producer.",
    },
    {
        "iso2": "TM", "name": "Turkmenistan", "iso3": "TKM", "region": "Asia",
        "comtrade_code": 795,
        "common_names": ["Turkmenistan"],
        "detection_patterns": [
            {"pattern": "turkmenistan", "context": "primary"},
            {"pattern": "turkmen",      "context": "mentioned"},
        ],
        "is_sanctions_risk": False,
        "is_major_producer": False, "is_major_consumer": False,
    },
    {
        "iso2": "VE", "name": "Venezuela", "iso3": "VEN", "region": "South America",
        "comtrade_code": 862,
        "common_names": ["Venezuela"],
        "detection_patterns": [
            {"pattern": "venezuela",  "context": "primary"},
            {"pattern": "venezuelan", "context": "mentioned"},
        ],
        "is_sanctions_risk": True,
        "is_major_producer": False, "is_major_consumer": False,
        "notes": "Iron ore, bauxite (limited operations under sanctions).",
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

    Returns ``{"inserted": int, "updated": int, "total": int}``.
    """
    from sqlalchemy import select

    # Snapshot the iso2 set BEFORE the loop so we can distinguish inserts
    # from updates without relying on ``result.rowcount`` (which psycopg3
    # returns as 0 or -1 for ``INSERT ... ON CONFLICT DO UPDATE`` — bug
    # surfaced 2026-05-09 when the function reported 0/0 despite all 50
    # rows successfully landing in the table).
    existing_iso2s = set(session.scalars(select(Country.iso2)).all())

    inserted = updated = 0
    for c in _COUNTRIES:
        stmt = (
            pg_insert(Country)
            .values(**c)
            .on_conflict_do_update(
                index_elements=["iso2"],
                set_={
                    "name":                c["name"],
                    "iso3":                c.get("iso3"),
                    "region":              c.get("region"),
                    "comtrade_code":       c.get("comtrade_code"),
                    "common_names":        c.get("common_names"),
                    "detection_patterns":  c.get("detection_patterns"),
                    "is_sanctions_risk":   c.get("is_sanctions_risk", False),
                    "is_major_producer":   c["is_major_producer"],
                    "is_major_consumer":   c["is_major_consumer"],
                    "notes":               c.get("notes"),
                },
            )
        )
        session.execute(stmt)
        if c["iso2"] in existing_iso2s:
            updated += 1
        else:
            inserted += 1

    session.flush()
    log.info(
        "seed_countries.done",
        inserted=inserted,
        updated=updated,
        total=len(_COUNTRIES),
    )
    return {
        "inserted": inserted,
        "updated": updated,
        "total": len(_COUNTRIES),
    }
