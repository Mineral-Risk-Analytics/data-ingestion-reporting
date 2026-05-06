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
# ---------------------------------------------------------------------------
# Per-commodity routing keywords (parser-level, Category C)
# ---------------------------------------------------------------------------
# Tells the parser which row to filter to within a multi-type commodity
# (e.g. for SILICON, the file has both ferrosilicon and silicon-metal rows;
# we want the world mine production rollup keyed by `mine_type_keyword`).
#
# Static metadata (canonical_name, category, hs_codes, IRA/CRMA flags) is
# no longer carried here — it now lives in ``seed_materials.py``.  The
# CSV-name → canonical mapping is in ``material_source_aliases``
# (source_system='mcs_2025_csv').  This dict is parser-internal only.
_COMMODITY_TYPE_KEYWORD: dict[str, str] = {
    'Lithium '                                        : 'mine production',
    'Cobalt'                                          : 'mine production',
    'Nickel'                                          : 'mine production',
    'Graphite'                                        : 'mine production',
    'Manganese'                                       : 'mine production',
    'Copper '                                         : 'mine production',
    'Aluminum'                                        : 'smelter production',
    'Rare earths'                                     : 'mine production',
    'Vanadium'                                        : 'mine production',
    'Silicon'                                         : 'silicon metal',
    'Phosphate rock '                                 : 'mine production',
    'Gallium '                                        : 'primary production',
    'Gemanium'                                        : 'primary and secondary refinery production',
    'Chromium'                                        : 'mine production',
    'Molybdenum '                                     : 'mine production',
    'Niobium'                                         : 'mine production',
    'Tantalum'                                        : 'mine production',
    'Tellurium'                                       : 'refinery production, tellurium content',
    'Titanium Mineral Concentrates'                   : 'mine production',
    'Zirconium and Hafnium'                           : 'mine production',
    'Iron Ore  '                                      : 'mine production',
    'Magnesium Compounds'                             : 'mine production',
    'Platinum-Group metals'                           : 'mine production',
    'Tungsten '                                       : 'mine production',
    'Indium'                                          : 'refinery production',
    'Tin'                                             : 'mine production',
    'Silver'                                          : 'mine production',
    'Fluorspar'                                       : 'mine production',
    'Boron '                                          : 'boron all types',
    'Selenium'                                        : 'refinery production, selenium content',
    'Bismuth'                                         : 'refinery production',
    'Antimony'                                        : 'mine production',
    'Zinc'                                            : 'mine production',
    'Rhenium'                                         : 'mine production',
}


# ---------------------------------------------------------------------------
# CSV-derived per-HS-node production shares
# ---------------------------------------------------------------------------
# For commodities where USGS splits production into multiple sub-types
# (e.g. Silicon: Ferosilicon vs silicon metal; Copper: mine vs refinery),
# this mapping tells the parser which sub-type rows to aggregate into a
# stage-specific production-share stream and which HS-mapping prefix to
# attribute them to.  Independent of `_COMMODITY_CONFIG.mine_type_keyword`
# (which governs the legacy material-level criticality calc).
#
# Why it matters: the current material-level `material_production_shares`
# rows aggregate ALL sub-types into a single per-country share.  For
# scoring purposes, that conflates ferrosilicon producers (Bhutan, India,
# Kazakhstan, Malaysia, Poland — none of whom produce silicon metal) with
# silicon-metal producers (Australia, Germany — neither of whom produce
# ferrosilicon).  The HHI for combined "Silicon" is fine, but the
# stage-aware Level-0 scorer wants distinct geographic distributions per
# HS node — that's what this config enables.
#
# Only includes battery-relevant sub-types.  Non-battery sub-types (e.g.
# titanium dioxide pigments) are intentionally excluded — including them
# would pollute the supply-chain risk signal for battery scoring.
#
# Format:
#     {csv_commodity_key: [(type_substring_lowercase, hs_prefix), ...]}
#
# `type_substring` is matched case-insensitively as a substring of the
# CSV's TYPE column.  Note that USGS uses the misspelling "Ferosilicon"
# (one r) in MCS 2025 — match the CSV verbatim, not the standard spelling.
# ---------------------------------------------------------------------------

# IMPORTANT: keys MUST exactly match the CSV commodity strings used as
# keys in `_COMMODITY_CONFIG` above.  USGS MCS 2025 has trailing
# whitespace on several commodities (e.g. 'Copper ' with a trailing
# space) — match the CSV verbatim or the lookup misses.

_HS_NODE_SHARES_CONFIG: dict[str, list[tuple[str, str]]] = {
    # Silicon — ferrosilicon (battery_grade per partner) vs silicon metal
    # (refined).  Different geographic profiles in MCS 2025 — see comment
    # block above.
    "Silicon": [
        ("ferosilicon", "720221"),  # USGS spelling matches "Plant production, Ferosilicon, silicon content"
        ("silicon metal", "280461"),
    ],
    # Copper — mine production (ore) vs refinery production (refined cathode).
    # Both are battery-relevant; ore covers concentrate exports (DR Congo,
    # Peru, Chile) while refinery covers cathode-stage producers (China is
    # heavy on refining despite less mine output).
    # NOTE: CSV key is 'Copper ' WITH trailing space.
    "Copper ": [
        ("mine production", "2603"),
        ("refinery production", "7403"),
    ],
    # Titanium — initially planned to split sponge metal vs pigment capacity
    # (CSV commodity 'Titanium & titanium dioxide').  But the parser's
    # _COMMODITY_CONFIG uses the OTHER titanium commodity ('Titanium Mineral
    # Concentrates'), which only has ore-stage data (ilmenite + rutile, both
    # mapped to HS 2614).  Splitting ilmenite vs rutile by HS node isn't
    # meaningful — both are ore.  Sponge-metal split would require reading
    # an additional CSV commodity key, which is more parser work than is
    # justified for a single new sub-type.  Skipped for now; revisit if
    # battery-grade titanium scoring needs the distinction.
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
            if commodity not in _COMMODITY_TYPE_KEYWORD:
                continue
            raw.setdefault(commodity, []).append(row)

    results = []

    for csv_commodity, mine_keyword in _COMMODITY_TYPE_KEYWORD.items():
        rows = raw.get(csv_commodity, [])
        if not rows:
            continue

        keyword = mine_keyword.lower()

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

        # Per-HS-node production shares — for commodities where USGS reports
        # multiple distinct sub-types (ferrosilicon vs silicon metal, copper
        # mine vs refinery, etc.) we want stage-specific country distributions
        # in `hs_code_production_shares` so the Level-0 scorer can compute
        # per-stage HHI.  See `_HS_NODE_SHARES_CONFIG` for what's mapped.
        # Empty list when the commodity has no entry in the config.
        hs_production_shares: list[dict] = []
        sub_type_specs = _HS_NODE_SHARES_CONFIG.get(csv_commodity, [])
        for type_substring, hs_prefix in sub_type_specs:
            # Filter ALL rows for this commodity (not just `production_rows`,
            # which was already narrowed by the legacy `mine_type_keyword`).
            sub_rows = [r for r in rows if type_substring in r["TYPE"].lower()]
            if not sub_rows:
                continue

            sub_world_total_row = next(
                (r for r in sub_rows
                 if r["COUNTRY"].strip().lower().startswith("world total")),
                None,
            )
            sub_country_rows = [
                r for r in sub_rows
                if r["COUNTRY"].strip().lower() not in _EXCLUDE_COUNTRIES
            ]

            sub_country_prod: dict[str, float] = {}
            sub_volumes_raw: dict[str, float] = {}
            for r in sub_country_rows:
                vol = (
                    _parse_number(r["PROD_2023"])
                    or _parse_number(r["PROD_EST_ 2024"])
                )
                if vol is None:
                    continue
                country_name = r["COUNTRY"].strip()
                iso2 = _COUNTRY_ISO2.get(country_name)
                if iso2 is None:
                    continue
                sub_country_prod[iso2] = sub_country_prod.get(iso2, 0.0) + vol
                sub_volumes_raw[iso2] = vol

            sub_world_prod = (
                _parse_number(sub_world_total_row["PROD_2023"])
                if sub_world_total_row else None
            ) or (
                _parse_number(sub_world_total_row["PROD_EST_ 2024"])
                if sub_world_total_row else None
            )
            # Fallback: derive world total by summing country values when the
            # world-total row is missing or unparseable.
            if not sub_world_prod and sub_country_prod:
                sub_world_prod = sum(sub_country_prod.values())

            sub_unit = (
                sub_world_total_row["UNIT_MEAS"].strip()
                if sub_world_total_row else ""
            )

            if sub_country_prod and sub_world_prod and sub_world_prod > 0:
                for iso2, vol in sub_country_prod.items():
                    hs_production_shares.append({
                        "hs_code_prefix": hs_prefix,
                        "country_code": iso2,
                        "production_volume": vol,
                        "production_share": round(vol / sub_world_prod, 6),
                        "unit_of_measure": sub_unit or None,
                        "type_substring": type_substring,  # provenance for logs
                    })

        material = {
            "source_system":           "mcs_2025_csv",
            "source_name":             csv_commodity,
            "criticality_score":       criticality,
            "hhi_score":               criticality,
            "reserve_hhi_score":       reserve_hhi,
            "reserve_life_index":      reserve_life_index,
            "production_yoy_pct":      production_yoy_pct,
            "capacity_utilization":    capacity_utilization,
            "production_shares":       production_shares,
            "hs_production_shares":    hs_production_shares,
            "ranked_countries":        ranked_countries,
            "world_total":             world_prod,
            "world_unit":              unit or None,
            "notes":                   " ".join(notes_parts),
        }
        results.append(material)

    return results
