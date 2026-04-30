"""Parser for the USGS Mineral Commodity Summaries World Data CSV.

Downloads or reads MCS2025_World_Data.csv and produces Material rows
derived entirely from the published USGS data — no synthetic values.

Derived fields
--------------
primary_producing_countries
    ISO2 codes of countries with PROD_2023 mine production data,
    ranked descending by production volume. Excludes aggregate rows
    ("World total", "Other Countries").

criticality_score  (0.0 – 1.0)
    Normalised Herfindahl-Hirschman Index (HHI) computed from each
    country's share of world mine production (PROD_2023).
    HHI = Σ(share_i²)  where share_i = country_prod / world_total.
    Raw HHI range is 0–1; this value IS the normalised score.
    Higher score = more geographically concentrated = higher supply risk.
    This is a standard methodology used in competition economics and
    supply chain risk literature. Documented here so the methodology
    is auditable.

Fields NOT derived from this CSV (configured per-commodity below)
-----------------------------------------------------------------
hs_codes, category, symbol_or_code, price_unit,
is_ira_critical_mineral, is_eu_crma_critical

These are sourced from:
  - HS codes: WCO Harmonized System 2022 edition
  - IRA critical minerals: U.S. Federal Register Vol. 88 No. 214 (2023)
  - EU CRMA strategic list: EU Regulation 2024/1252 Annex II

Source
------
USGS Mineral Commodity Summaries 2025
https://pubs.usgs.gov/publication/mcs2025
Published January 2025. Re-run annually when new MCS is released.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Country name → ISO2 mapping for countries appearing in the MCS dataset.
# Extend as needed when new countries appear in future MCS editions.
# ---------------------------------------------------------------------------
_COUNTRY_ISO2: dict[str, str] = {
    "Afghanistan": "AF",
    "Albania": "AL",
    "Algeria": "DZ",
    "Angola": "AO",
    "Argentina": "AR",
    "Armenia": "AM",
    "Australia": "AU",
    "Austria": "AT",
    "Azerbaijan": "AZ",
    "Bahrain": "BH",
    "Bolivia": "BO",
    "Bosnia and Herzegovina": "BA",
    "Brazil": "BR",
    "Bulgaria": "BG",
    "Burma": "MM",
    "Cambodia": "KH",
    "Canada": "CA",
    "Chile": "CL",
    "China": "CN",
    "Colombia": "CO",
    "Congo (Kinshasa)": "CD",
    "Cuba": "CU",
    "Czech Republic": "CZ",
    "Czechia": "CZ",
    "Ecuador": "EC",
    "Egypt": "EG",
    "Eritrea": "ER",
    "Ethiopia": "ET",
    "Finland": "FI",
    "France": "FR",
    "Gabon": "GA",
    "Germany": "DE",
    "Ghana": "GH",
    "Greece": "GR",
    "Guinea": "GN",
    "Guyana": "GY",
    "Iceland": "IS",
    "India": "IN",
    "Indonesia": "ID",
    "Iran": "IR",
    "Ireland": "IE",
    "Italy": "IT",
    "Jamaica": "JM",
    "Japan": "JP",
    "Jordan": "JO",
    "Kazakhstan": "KZ",
    "Kenya": "KE",
    "Korea, North": "KP",
    "Korea, Republic of": "KR",
    "Kosovo": "XK",
    "Kyrgyzstan": "KG",
    "Laos": "LA",
    "Madagascar": "MG",
    "Malaysia": "MY",
    "Mali": "ML",
    "Mauritania": "MR",
    "Mexico": "MX",
    "Mongolia": "MN",
    "Morocco": "MA",
    "Mozambique": "MZ",
    "Namibia": "NA",
    "New Caledonia": "NC",
    "Niger": "NE",
    "Nigeria": "NG",
    "Norway": "NO",
    "Pakistan": "PK",
    "Papua New Guinea": "PG",
    "Peru": "PE",
    "Philippines": "PH",
    "Poland": "PL",
    "Portugal": "PT",
    "Romania": "RO",
    "Russia": "RU",
    "Saudi Arabia": "SA",
    "Senegal": "SN",
    "Serbia": "RS",
    "Sierra Leone": "SL",
    "Slovakia": "SK",
    "South Africa": "ZA",
    "Spain": "ES",
    "Sudan": "SD",
    "Sweden": "SE",
    "Tajikistan": "TJ",
    "Tanzania": "TZ",
    "Thailand": "TH",
    "Togo": "TG",
    "Turkey": "TR",
    "Türkiye": "TR",
    "Turkmenistan": "TM",
    "Uganda": "UG",
    "Ukraine": "UA",
    "United Arab Emirates": "AE",
    "United Kingdom": "GB",
    "United States": "US",
    "Uzbekistan": "UZ",
    "Venezuela": "VE",
    "Vietnam": "VN",
    "Zambia": "ZM",
    "Zimbabwe": "ZW",
}

# Aggregate/non-country rows to exclude from country rankings.
_EXCLUDE_COUNTRIES = {
    "world total (rounded)",
    "world total",
    "other countries",
    "united states and canada",
}

# ---------------------------------------------------------------------------
# Per-commodity static config: fields not derivable from the CSV.
# hs_codes: WCO HS 2022 chapter.heading format.
# ---------------------------------------------------------------------------
_COMMODITY_CONFIG: dict[str, dict] = {
    "Lithium ": {  # trailing space in CSV
        "canonical_name": "Lithium",
        "category": "cathode_active",
        "symbol_or_code": "Li",
        "hs_codes": ["2825.20", "2836.91"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": "stable",
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },
    "Cobalt": {
        "canonical_name": "Cobalt",
        "category": "cathode_active",
        "symbol_or_code": "Co",
        "hs_codes": ["2836.20", "2605.00"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": "declining",  # LFP shift reducing cobalt use
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },
    "Nickel": {
        "canonical_name": "Nickel",
        "category": "cathode_active",
        "symbol_or_code": "Ni",
        "hs_codes": ["2604.00", "7502.10"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": "declining",  # LFP shift; NMC 811 partially offsets
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },
    "Graphite": {
        "canonical_name": "Natural Graphite",
        "category": "anode",
        "symbol_or_code": "C",
        "hs_codes": ["3801.10", "3801.20"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": "stable",
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },
    "Manganese": {
        "canonical_name": "Manganese",
        "category": "cathode_active",
        "symbol_or_code": "Mn",
        "hs_codes": ["2602.00", "2820.10"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": "stable",  # peaked per paper
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },
    "Copper ": {  # trailing space in CSV
        "canonical_name": "Copper",
        "category": "structural",
        "symbol_or_code": "Cu",
        "hs_codes": ["7401.00", "7408.11"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": False,
        "patent_occurrence_trend": "stable",
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },
    "Aluminum": {
        "canonical_name": "Aluminum",
        "category": "structural",
        "symbol_or_code": "Al",
        "hs_codes": ["7601.10", "7601.20"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": False,
        "patent_occurrence_trend": "stable",
        "data_availability": "commercial",
        "mine_type_keyword": "smelter production",  # MCS reports smelter, not mine
    },
    "Rare earths": {
        "canonical_name": "Rare Earth Elements",
        "category": "component",
        "symbol_or_code": "REE",
        "hs_codes": ["2846.10", "2846.90"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,  # aggregate row; individual REEs tracked separately
        "data_availability": "limited",
        "mine_type_keyword": "mine production",
    },
    "Vanadium": {
        "canonical_name": "Vanadium",
        "category": "cathode_active",
        "symbol_or_code": "V",
        "hs_codes": ["2615.20"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": False,
        "patent_occurrence_trend": None,
        "data_availability": "limited",
        "mine_type_keyword": "mine production",
    },
    "Silicon": {
        "canonical_name": "Silicon (Anode Grade)",
        "category": "anode",
        "symbol_or_code": "Si",
        "hs_codes": ["2804.61", "2804.69"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": False,
        "patent_occurrence_trend": "rising",
        "data_availability": "limited",
        "mine_type_keyword": "silicon metal",  # prefer metal over ferrosilicon
    },
    "Phosphate rock ": {  # trailing space in CSV
        "canonical_name": "Phosphate (Battery Grade)",
        "category": "cathode_active",
        "symbol_or_code": "P",
        "hs_codes": ["2835.26", "2835.29"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": False,
        "patent_occurrence_trend": "rising",
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },

    # -----------------------------------------------------------------------
    # Expansion set — minerals added from MCS2025_World_Data.csv beyond the
    # original 11. All are present in the CSV with production data that yields
    # HHI-derived criticality scores. Static config below; production/HHI
    # values are derived by the parser at ingest time.
    #
    # patent_occurrence_trend: rising/declining/stable sourced from the
    #   EPO PATSTAT analysis in "Critical Minerals for EV Batteries" paper
    #   (Natalia et al., 2024). None = no PATSTAT coverage yet.
    # data_availability: commercial = active LME/spot price benchmarks;
    #   limited = sporadic or opaque pricing; no_benchmark = no public price.
    # EU CRM Act 2024 (EU Regulation 2024/1252 Annex II strategic list):
    #   Gallium, Germanium, Titanium, Niobium, Tantalum, Tellurium,
    #   Chromium, Molybdenum, Zirconium, Magnesium, Boron, Bismuth, Selenium,
    #   Rhenium, Tungsten, Indium, Fluorspar are all listed.
    # IRA critical minerals list (U.S. Federal Register Vol. 88 No. 214, 2023):
    #   Gallium, Germanium, Titanium, Niobium, Tantalum, Tellurium,
    #   Chromium, Molybdenum, Zirconium, Tin, Zinc, Bismuth, Selenium,
    #   Rhenium, Tungsten, Indium, Antimony, Silver listed.
    # -----------------------------------------------------------------------

    "Gallium ": {  # trailing space in CSV
        "canonical_name": "Gallium",
        "category": "component",
        "symbol_or_code": "Ga",
        "hs_codes": ["2805.19"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": "rising",
        "data_availability": "no_benchmark",  # ~94% CN-sourced; no established exchange
        "mine_type_keyword": "primary production",  # CSV TYPE = "Primary production"
    },
    "Gemanium": {  # NOTE: CSV contains a typo ("Gemanium" not "Germanium")
        "canonical_name": "Germanium",
        "category": "component",
        "symbol_or_code": "Ge",
        "hs_codes": ["2804.90"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": "rising",
        "data_availability": "no_benchmark",  # no exchange price; spot market opaque
        "mine_type_keyword": "primary and secondary refinery production",  # CSV TYPE exact match
    },
    "Chromium": {
        "canonical_name": "Chromium",
        "category": "cathode_active",
        "symbol_or_code": "Cr",
        "hs_codes": ["2610.00", "7202.41"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": "rising",
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },
    "Molybdenum ": {  # trailing space in CSV
        "canonical_name": "Molybdenum",
        "category": "component",
        "symbol_or_code": "Mo",
        "hs_codes": ["2613.10", "2613.90"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": "rising",
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },
    "Niobium": {
        "canonical_name": "Niobium",
        "category": "component",
        "symbol_or_code": "Nb",
        "hs_codes": ["2615.90", "8112.92"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": "rising",
        "data_availability": "limited",
        "mine_type_keyword": "mine production",
    },
    "Tantalum": {
        "canonical_name": "Tantalum",
        "category": "component",
        "symbol_or_code": "Ta",
        "hs_codes": ["2615.90", "8103.20"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": "rising",
        "data_availability": "limited",
        "mine_type_keyword": "mine production",
    },
    "Tellurium": {
        "canonical_name": "Tellurium",
        "category": "component",
        "symbol_or_code": "Te",
        "hs_codes": ["2804.19"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": "rising",
        "data_availability": "no_benchmark",
        "mine_type_keyword": "refinery production, tellurium content",  # CSV TYPE exact match
    },
    "Titanium Mineral Concentrates": {
        # Prefer "Titanium Mineral Concentrates" over "Titanium & titanium dioxide"
        # for supply concentration signal (mine-level, not processing-level).
        "canonical_name": "Titanium",
        "category": "component",
        "symbol_or_code": "Ti",
        "hs_codes": ["2614.00", "8108.20"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": "rising",
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },
    "Zirconium and Hafnium": {
        # Hafnium is co-produced with zirconium; tracked under the Zirconium row.
        "canonical_name": "Zirconium",
        "category": "component",
        "symbol_or_code": "Zr",
        "hs_codes": ["2615.10", "8109.20"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": "rising",
        "data_availability": "limited",
        "mine_type_keyword": "mine production",
    },
    "Iron Ore  ": {  # two trailing spaces in CSV
        "canonical_name": "Iron Ore (LFP Grade)",
        "category": "cathode_active",
        "symbol_or_code": "Fe",
        "hs_codes": ["2601.11", "2601.12"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": False,
        "patent_occurrence_trend": "rising",  # LFP cathode shift drives iron demand
        "data_availability": "commercial",    # actively traded on exchanges
        "mine_type_keyword": "mine production",
    },
    "Magnesium Compounds": {
        # "Magnesium Compounds" covers mined production (brucite, magnesite).
        # More useful than "Magnesium metal" (smelter) for supply concentration signal.
        "canonical_name": "Magnesium",
        "category": "structural",
        "symbol_or_code": "Mg",
        "hs_codes": ["2519.10", "2519.90"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },
    "Platinum-Group metals": {
        # Aggregate only — Pt and Pd are not separable from mine production data
        # in this CSV. Use this row for overall PGM supply concentration.
        "canonical_name": "Platinum-Group Metals",
        "category": "component",
        "symbol_or_code": "PGM",
        "hs_codes": ["7110.11", "7110.21", "7110.31"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },
    "Tungsten ": {  # trailing space in CSV
        "canonical_name": "Tungsten",
        "category": "component",
        "symbol_or_code": "W",
        "hs_codes": ["2611.00", "8101.10"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,
        "data_availability": "limited",
        "mine_type_keyword": "mine production",
    },
    "Indium": {
        "canonical_name": "Indium",
        "category": "component",
        "symbol_or_code": "In",
        "hs_codes": ["8112.13", "8112.92"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,
        "data_availability": "limited",
        "mine_type_keyword": "refinery production",  # CSV TYPE = "Refinery production"
    },
    "Tin": {
        "canonical_name": "Tin",
        "category": "structural",
        "symbol_or_code": "Sn",
        "hs_codes": ["2609.00", "8001.10"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": False,
        "patent_occurrence_trend": "declining",
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },
    "Silver": {
        "canonical_name": "Silver",
        "category": "structural",
        "symbol_or_code": "Ag",
        "hs_codes": ["2616.10", "7106.10"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": False,
        "patent_occurrence_trend": "stable",
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },
    "Fluorspar": {
        "canonical_name": "Fluorspar",
        "category": "electrolyte",  # used in LFP electrolyte and fluoride solid electrolytes
        "symbol_or_code": "CaF2",
        "hs_codes": ["2529.21", "2529.22"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,
        "data_availability": "limited",
        "mine_type_keyword": "mine production",
    },
    "Boron ": {  # trailing space in CSV
        "canonical_name": "Boron",
        "category": "electrolyte",
        "symbol_or_code": "B",
        "hs_codes": ["2528.00"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,
        "data_availability": "commercial",
        "mine_type_keyword": "boron all types",  # CSV TYPE = "Boron all types"
    },
    "Selenium": {
        "canonical_name": "Selenium",
        "category": "component",
        "symbol_or_code": "Se",
        "hs_codes": ["2804.19"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,
        "data_availability": "limited",
        "mine_type_keyword": "refinery production, selenium content",  # CSV TYPE exact match
    },
    "Bismuth": {
        "canonical_name": "Bismuth",
        "category": "component",
        "symbol_or_code": "Bi",
        "hs_codes": ["2616.90", "8106.00"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,
        "data_availability": "limited",
        "mine_type_keyword": "refinery production",  # CSV TYPE = "Refinery production"
    },
    "Antimony": {
        "canonical_name": "Antimony",
        "category": "component",
        "symbol_or_code": "Sb",
        "hs_codes": ["2617.10", "8110.10"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": True,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,
        "data_availability": "limited",
        "mine_type_keyword": "mine production",
    },
    "Zinc": {
        "canonical_name": "Zinc",
        "category": "current_collector",  # current collector (anode side in some chemistries)
        "symbol_or_code": "Zn",
        "hs_codes": ["2608.00", "7901.11"],
        "price_unit": "per_mt",
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": False,
        "patent_occurrence_trend": None,
        "data_availability": "commercial",
        "mine_type_keyword": "mine production",
    },
    "Rhenium": {
        "canonical_name": "Rhenium",
        "category": "component",
        "symbol_or_code": "Re",
        "hs_codes": ["2804.19", "8112.92"],
        "price_unit": "per_kg",
        "is_ira_critical_mineral": False,
        "is_eu_crma_critical": True,
        "patent_occurrence_trend": None,
        "data_availability": "no_benchmark",
        "mine_type_keyword": "mine production",
    },
}


def _parse_number(value: str) -> Optional[float]:
    """Parse a production/reserve value, return None if missing or non-numeric."""
    v = value.strip().lstrip(">").replace(",", "")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _hhi(country_productions: dict[str, float]) -> float:
    """
    Compute normalised HHI from {country: production_volume} dict.
    Returns 0.0 if total is zero.  Range: 0.0 (perfectly distributed) –
    1.0 (single country produces everything).
    """
    total = sum(country_productions.values())
    if total == 0:
        return 0.0
    return sum((v / total) ** 2 for v in country_productions.values())


def parse_usgs_csv(filepath: str | Path) -> list[dict]:
    """
    Parse MCS World Data CSV and return a list of Material field dicts,
    one per configured commodity.

    Only commodities listed in _COMMODITY_CONFIG are returned.
    """
    filepath = Path(filepath)

    # Load all relevant rows into memory, keyed by (commodity, type).
    raw: dict[str, list[dict]] = {}  # commodity_key -> list of row dicts

    with filepath.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            commodity = row["COMMODITY"]
            if commodity not in _COMMODITY_CONFIG:
                continue
            raw.setdefault(commodity, []).append(row)

    results = []

    for csv_commodity, config in _COMMODITY_CONFIG.items():
        rows = raw.get(csv_commodity, [])
        if not rows:
            continue

        keyword = config["mine_type_keyword"].lower()

        # Filter to the relevant production type rows.
        production_rows = [
            r for r in rows
            if keyword in r["TYPE"].lower()
        ]

        if not production_rows:
            continue

        # Separate world total from country rows.
        world_total_row = next(
            (r for r in production_rows
             if r["COUNTRY"].strip().lower().startswith("world total")),
            None,
        )
        country_rows = [
            r for r in production_rows
            if r["COUNTRY"].strip().lower() not in _EXCLUDE_COUNTRIES
        ]

        # Build {iso2: production} from PROD_2023, fall back to PROD_EST_2024.
        country_prod: dict[str, float] = {}
        country_reserves: dict[str, float] = {}
        for r in country_rows:
            prod = _parse_number(r["PROD_2023"]) or _parse_number(r["PROD_EST_ 2024"])
            country_name = r["COUNTRY"].strip()
            iso2 = _COUNTRY_ISO2.get(country_name)
            if iso2 is None:
                continue
            if prod is not None:
                # Accumulate in case the same country appears on multiple rows.
                country_prod[iso2] = country_prod.get(iso2, 0.0) + prod
            reserves = _parse_number(r["RESERVES_2024"])
            if reserves is not None:
                country_reserves[iso2] = country_reserves.get(iso2, 0.0) + reserves

        # Rank countries by production descending.
        ranked_countries = [
            iso2 for iso2, _ in sorted(
                country_prod.items(), key=lambda x: x[1], reverse=True
            )
        ]

        # Criticality score: normalised HHI on mine production.
        criticality = round(_hhi(country_prod), 4) if country_prod else None

        # Reserve HHI: same methodology applied to reserve distribution.
        reserve_hhi = round(_hhi(country_reserves), 4) if country_reserves else None

        # World totals for notes, share computation, and derived metrics.
        world_prod_2023 = _parse_number(world_total_row["PROD_2023"]) if world_total_row else None
        world_prod_est_2024 = _parse_number(world_total_row["PROD_EST_ 2024"]) if world_total_row else None
        world_prod = world_prod_2023 or world_prod_est_2024
        world_reserves = (
            _parse_number(world_total_row["RESERVES_2024"])
        ) if world_total_row else None

        # World capacity from CAP_2023 (prefer) or CAP_EST_2024.
        world_capacity = None
        if world_total_row:
            world_capacity = (
                _parse_number(world_total_row.get("CAP_2023", ""))
                or _parse_number(world_total_row.get("CAP_EST_ 2024", ""))
            )

        # Reserve life index: years of supply at current production rate.
        # Plausibility guard: MCS reserves and production occasionally use
        # different scales for the same row (e.g. production in metric tons,
        # reserves in thousands of metric tons), producing RLI values well
        # below 1.0 that are clearly erroneous. We null these out rather than
        # attempt per-commodity unit inference; the scoring engine will fall back
        # to a neutral 0.5 scarcity signal for affected materials.
        _RLI_MIN_PLAUSIBLE = 2.0  # years — anything below this is a data artefact
        reserve_life_index: Optional[float] = None
        if world_reserves is not None and world_prod and world_prod > 0:
            rli_candidate = round(world_reserves / world_prod, 1)
            if rli_candidate >= _RLI_MIN_PLAUSIBLE:
                reserve_life_index = rli_candidate

        # Production YoY %: (est 2024 - actual 2023) / actual 2023.
        production_yoy_pct: Optional[float] = None
        if world_prod_2023 and world_prod_2023 > 0 and world_prod_est_2024 is not None:
            production_yoy_pct = round(
                (world_prod_est_2024 - world_prod_2023) / world_prod_2023, 4
            )

        # Capacity utilization: production / capacity.
        capacity_utilization: Optional[float] = None
        if world_capacity and world_capacity > 0 and world_prod is not None:
            capacity_utilization = round(world_prod / world_capacity, 4)

        unit = world_total_row["UNIT_MEAS"].strip() if world_total_row else ""

        # Build production share rows — fraction of world total per country.
        # Stored under _production_shares so the CLI can persist them separately
        # from the Material row (same pattern as _hhi_score).
        production_shares: list[dict] = []
        if country_prod and world_prod and world_prod > 0:
            for iso2, vol in country_prod.items():
                production_shares.append({
                    "country_code": iso2,
                    "production_volume": vol,
                    "production_share": round(vol / world_prod, 6),
                    "unit_of_measure": unit or None,
                })
        prod_type = world_total_row["TYPE"].strip() if world_total_row else ""

        notes_parts = [
            f"Source: USGS Mineral Commodity Summaries 2025 "
            f"(https://pubs.usgs.gov/publication/mcs2025).",
            f"Production type: {prod_type}.",
        ]
        if world_prod_2023 is not None:
            notes_parts.append(f"World mine production 2023: {world_prod_2023:,.0f} {unit}.")
        if world_prod_est_2024 is not None:
            notes_parts.append(f"World mine production est. 2024: {world_prod_est_2024:,.0f} {unit}.")
        if production_yoy_pct is not None:
            notes_parts.append(
                f"Production YoY change (2023→est.2024): {production_yoy_pct:+.1%}."
            )
        if world_reserves is not None:
            notes_parts.append(f"World reserves 2024: {world_reserves:,.0f} {unit}.")
        if reserve_life_index is not None:
            notes_parts.append(f"Reserve life index: {reserve_life_index:.0f} years.")
        if world_capacity is not None:
            notes_parts.append(f"World capacity 2023/est.2024: {world_capacity:,.0f} {unit}.")
        if capacity_utilization is not None:
            notes_parts.append(f"Capacity utilization: {capacity_utilization:.1%}.")
        notes_parts.append(
            f"criticality_score methodology: normalised HHI computed from "
            f"country shares of world mine production (PROD_2023). "
            f"Range 0 (perfectly distributed) – 1 (single-country monopoly)."
        )
        if reserve_hhi is not None:
            notes_parts.append(
                f"reserve_hhi_score: normalised HHI on country reserve shares "
                f"(RESERVES_2024). Same methodology as criticality_score."
            )
        if ranked_countries:
            notes_parts.append(
                f"Top producing countries (ranked): {', '.join(ranked_countries[:5])}."
            )

        material = {
            "canonical_name": config["canonical_name"],
            "category": config["category"],
            "symbol_or_code": config["symbol_or_code"],
            "hs_codes": config["hs_codes"],
            "criticality_score": criticality,
            "primary_producing_countries": ranked_countries,
            "price_unit": config["price_unit"],
            "is_ira_critical_mineral": config["is_ira_critical_mineral"],
            "is_eu_crma_critical": config["is_eu_crma_critical"],
            # New fields from migration 002 — optional in config, default None.
            "patent_occurrence_trend": config.get("patent_occurrence_trend"),
            "data_availability": config.get("data_availability"),
            # hhi_score is same as criticality_score (both are normalised HHI 0–1)
            # but named separately for clarity when writing material_criticality_signals.
            "_hhi_score": criticality,
            # Supply metric signals — stripped before ORM Material creation,
            # written to material_criticality_signals by ingest-usgs.
            "_reserve_hhi_score": reserve_hhi,
            "_reserve_life_index": reserve_life_index,
            "_production_yoy_pct": production_yoy_pct,
            "_capacity_utilization": capacity_utilization,
            # Production shares stripped before creating Material ORM objects;
            # persisted separately by ingest-usgs into material_production_shares.
            "_production_shares": production_shares,
            "notes": " ".join(notes_parts),
        }
        results.append(material)

    return results
