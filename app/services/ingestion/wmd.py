"""World Mining Data (world-mining-data.info) xlsx ingester.

Files (annual edition, from the site's Data Section):
  6.4  Production of individual Countries by Minerals  -> wmd_production (PRIMARY)
  6.3c Political stability                             -> wmd_group_production (stability)
  6.3d Country Groups and Economic Blocks              -> wmd_group_production (bloc)
  6.5  Share of World Production                       -> parse-validation only (shares
       and HHI are recomputed from 6.4 and compared; never stored)

Design rules (ADS/wmd_usgs_comparison_and_ingest_plan.md):
  * every country name must resolve via COUNTRY_TO_ISO2 — unmapped = row error,
    never a guess (same discipline as the HS-code validation rule);
  * per-sheet, per-year sums must match the sheet's Total row within 0.1%;
  * shares/HHI are computed on read over the FULL universe — never stored;
  * WMD validates USGS; it does not feed scoring (single-source-per-stage).

Structure of every sheet (validated against the 2026 edition, 65 sheets):
  row 1 = title, row 2 = header (Country | unit | 2020..2024 | data source),
  rows 3.. = data, final row = Total.  Values arrive as strings.
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.supply import Material
from app.models.wmd import WmdCommodity, WmdGroupProduction, WmdProduction

log = structlog.get_logger(__name__)

YEARS = (2020, 2021, 2022, 2023, 2024)

# ── Country name → ISO2 (complete for the 2026 edition; unmapped = error) ──
COUNTRY_TO_ISO2: dict[str, str] = {
    "Afghanistan": "AF", "Albania": "AL", "Algeria": "DZ", "Angola": "AO",
    "Argentina": "AR", "Armenia": "AM", "Australia": "AU", "Austria": "AT",
    "Azerbaijan": "AZ", "Bahamas": "BS", "Bahrain": "BH", "Bangladesh": "BD",
    "Barbados": "BB", "Belarus": "BY", "Belgium": "BE", "Benin": "BJ",
    "Bhutan": "BT", "Bolivia": "BO", "Bosnia-Herzegovina": "BA",
    "Botswana": "BW", "Brazil": "BR", "Brunei": "BN", "Bulgaria": "BG",
    "Burkina Faso": "BF", "Burundi": "BI", "Cambodia": "KH", "Cameroon": "CM",
    "Canada": "CA", "Cape Verde": "CV", "Central African Republic": "CF",
    "Chad": "TD", "Chile": "CL", "China": "CN", "Christmas Island": "CX",
    "Colombia": "CO", "Congo, D.R.": "CD", "Congo, Rep.": "CG",
    "Costa Rica": "CR", "Cote d'Ivoire": "CI", "Croatia": "HR", "Cuba": "CU",
    "Cyprus": "CY", "Czechia": "CZ", "Denmark": "DK",
    "Dominican Republic": "DO", "Ecuador": "EC", "Egypt": "EG",
    "El Salvador": "SV", "Equatorial Guinea": "GQ", "Eritrea": "ER",
    "Estonia": "EE", "Eswatini": "SZ", "Ethiopia": "ET", "Fiji": "FJ",
    "Finland": "FI", "France": "FR", "French Guiana": "GF", "Gabon": "GA",
    "Georgia": "GE", "Germany": "DE", "Ghana": "GH", "Greece": "GR",
    "Guatemala": "GT", "Guinea": "GN", "Guyana": "GY", "Honduras": "HN",
    "Hungary": "HU", "Iceland": "IS", "India": "IN", "Indonesia": "ID",
    "Iran": "IR", "Iraq": "IQ", "Ireland": "IE", "Israel": "IL",
    "Italy": "IT", "Jamaica": "JM", "Japan": "JP", "Jordan": "JO",
    "Kazakhstan": "KZ", "Kenya": "KE", "Korea, North": "KP",
    "Korea, South": "KR", "Kosovo": "XK", "Kuwait": "KW", "Kyrgyzstan": "KG",
    "Laos": "LA", "Latvia": "LV", "Lebanon": "LB", "Lesotho": "LS",
    "Liberia": "LR", "Libya": "LY", "Lithuania": "LT", "Madagascar": "MG",
    "Malawi": "MW", "Malaysia": "MY", "Mali": "ML", "Malta": "MT",
    "Mauritania": "MR", "Mauritius": "MU", "Mexico": "MX", "Moldova": "MD",
    "Mongolia": "MN", "Montenegro": "ME", "Morocco": "MA",
    "Mozambique": "MZ", "Myanmar": "MM", "Namibia": "NA", "Nauru": "NR",
    "Nepal": "NP", "Netherlands": "NL", "New Caledonia": "NC",
    "New Zealand": "NZ", "Nicaragua": "NI", "Niger": "NE", "Nigeria": "NG",
    "North Macedonia": "MK", "Norway": "NO", "Oman": "OM", "Pakistan": "PK",
    "Panama": "PA", "Papua New Guinea": "PG", "Paraguay": "PY", "Peru": "PE",
    "Philippines": "PH", "Poland": "PL", "Portugal": "PT", "Qatar": "QA",
    "Romania": "RO", "Russia": "RU", "Rwanda": "RW", "Saudi Arabia": "SA",
    "Senegal": "SN", "Serbia": "RS", "Sierra Leone": "SL", "Slovakia": "SK",
    "Slovenia": "SI", "Solomon Islands": "SB", "South Africa": "ZA",
    "South Sudan": "SS", "Spain": "ES", "Sri Lanka": "LK", "Sudan": "SD",
    "Suriname": "SR", "Sweden": "SE", "Switzerland": "CH", "Syria": "SY",
    "Taiwan": "TW", "Tajikistan": "TJ", "Tanzania": "TZ", "Thailand": "TH",
    "Togo": "TG", "Trinidad and Tobago": "TT", "Tunisia": "TN",
    "Turkmenistan": "TM", "Türkiye": "TR", "Uganda": "UG", "Ukraine": "UA",
    "United Arab Emirates": "AE", "United Kingdom": "GB",
    "United States": "US", "Uruguay": "UY", "Uzbekistan": "UZ",
    "Venezuela": "VE", "Vietnam": "VN", "Yemen": "YE", "Zambia": "ZM",
    "Zimbabwe": "ZW",
}

# ── Sheet name → (group, content_basis, material canonical_name or None) ──
# material mapping resolved against materials.canonical_name at load time;
# a listed name that doesn't resolve is a hard error (no silent NULLs for
# names we CLAIM map).  None = deliberately unmapped (stored, no material).
_IF = "iron_ferroalloy"; _NF = "non_ferrous"; _PM = "precious"
_IM = "industrial"; _FU = "fuel"
SHEET_META: dict[str, tuple[str, Optional[str], Optional[str]]] = {
    "Iron (Fe)":            (_IF, "Fe",       "Iron Ore"),   # NB our Iron Ore basis = t ore; WMD = Fe content
    "Chromium (Cr2O3)":     (_IF, "Cr2O3",    "Chromium"),
    "Cobalt":               (_IF, "Co",       "Cobalt"),
    "Manganese":            (_IF, "Mn",       "Manganese"),
    "Molybdenum":           (_IF, "Mo",       "Molybdenum"),
    "Nickel":               (_IF, "Ni",       "Nickel"),
    "Niobium (Nb2O5)":      (_IF, "Nb2O5",    "Niobium"),
    "Tantalum (Ta2O5)":     (_IF, "Ta2O5",    "Tantalum"),
    "Titanium (TiO2)":      (_IF, "TiO2",     "Titanium"),   # NB our USGS Titanium table = sponge metal (different stage)
    "Tungsten (W)":         (_IF, "W",        "Tungsten"),
    "Vanadium (V)":         (_IF, "V",        "Vanadium"),
    "Aluminium":            (_NF, "smelter_metal", "Aluminum"),  # refined-stage basis in BOTH sources
    "Antimony":             (_NF, "Sb",       "Antimony"),
    "Arsenic":              (_NF, "As2O3",    None),
    "Bauxite":              (_NF, "crude_ore", None),
    "Beryllium (conc.)":    (_NF, "concentrate", None),
    "Bismuth":              (_NF, "Bi",       "Bismuth"),
    "Cadmium":              (_NF, "Cd",       None),
    "Copper":               (_NF, "Cu",       "Copper"),     # MINE stage — unlike our contaminated USGS table
    "Gallium":              (_NF, "Ga",       "Gallium"),
    "Germanium":            (_NF, "Ge",       "Germanium"),
    "Indium":               (_NF, "In",       "Indium"),
    "Lead":                 (_NF, "Pb",       None),
    "Lithium (Li2O)":       (_NF, "Li2O",     "Lithium"),    # x2.153 for LCE — conversion NEVER auto-applied
    "Mercury":              (_NF, "Hg",       None),
    "Rare Earths (REO)":    (_NF, "REO",      "Rare Earth Elements"),
    "Rhenium":              (_NF, "Re",       "Rhenium"),    # unit kg
    "Selenium":             (_NF, "Se",       "Selenium"),
    "Tellurium":            (_NF, "Te",       "Tellurium"),
    "Tin":                  (_NF, "Sn",       "Tin"),
    "Zinc":                 (_NF, "Zn",       "Zinc"),
    "Gold":                 (_PM, "Au",       "Gold"),       # unit kg
    "Palladium":            (_PM, "Pd",       "Platinum-Group Metals"),  # 3 sheets -> 1 material
    "Platinum":             (_PM, "Pt",       "Platinum-Group Metals"),
    "Rhodium":              (_PM, "Rh",       "Platinum-Group Metals"),
    "Silver":               (_PM, "Ag",       "Silver"),
    "Asbestos":             (_IM, None,       None),
    "Baryte":               (_IM, None,       None),
    "Bentonite":            (_IM, None,       None),
    "Boron Minerals":       (_IM, "B_minerals", "Boron"),
    "Diamonds (Gem)":       (_IM, "carat",    None),
    "Diamonds (Ind)":       (_IM, "carat",    None),
    "Diatomite":            (_IM, None,       None),
    "Feldspar":             (_IM, None,       None),
    "Fluorspar":            (_IM, "CaF2_ore", "Fluorspar"),
    "Graphite":             (_IM, "natural_graphite", "Natural Graphite"),
    "Gypsum and Anhydrite": (_IM, None,       None),
    "Kaolin (China-Clay)":  (_IM, None,       None),
    "Magnesite":            (_IM, "MgCO3_ore", None),  # NOT mapped to Magnesium (metal) — different product
    "Perlite":              (_IM, None,       None),
    "Phosphate Rock (P2O5)": (_IM, "P2O5",    "Phosphate"),
    "Potash (K2O)":         (_IM, "K2O",      None),
    "Salt (rock, brines, marine)": (_IM, None, None),  # NOT mapped to Sodium — decide separately
    "Sulfur (elementar & industrial)": (_IM, None, None),
    "Talc, Steatite & Pyrophyllite": (_IM, None, None),
    "Vermiculite":          (_IM, None,       None),
    "Zircon":               (_IM, "zircon_conc", "Zirconium"),
    "Steam Coal ":          (_FU, None,       None),   # trailing space is in the file
    "Coking Coal":          (_FU, None,       None),
    "Lignite":              (_FU, None,       None),
    "Natural Gas":          (_FU, None,       None),
    "Petroleum":            (_FU, None,       None),
    "Oil Sands (part of Petroleum)": (_FU, None, None),
    "Oil Shales":           (_FU, None,       None),
    "Uranium (U3O8)":       (_FU, "U3O8",     None),
}

_STABILITY_KEYS = {"Stable", "Fair", "Unstable", "Extreme Unstable"}
_TOTAL_TOLERANCE = 0.001  # 0.1%


@dataclass
class WmdIngestReport:
    edition: int = 0
    sheets_seen: int = 0
    sheets_loaded: int = 0
    commodities_inserted: int = 0
    production_upserts: int = 0
    group_upserts: int = 0
    rows_failed: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _to_float(v) -> Optional[float]:
    if v is None or str(v).strip() in ("", "-"):
        return None
    return float(str(v).replace(" ", "").replace(",", ""))


def _parse_country_sheet(ws, sheet: str, report: WmdIngestReport):
    """Parse one 6.4 sheet -> (unit, {iso2: {year: (value, src)}}). Total-verified."""
    rows = list(ws.iter_rows(values_only=True))
    hdr = rows[1]
    if str(hdr[0]).strip() != "Country" or str(hdr[2]) != "2020" or str(hdr[6]) != "2024":
        report.errors.append(f"{sheet}: unrecognized header {hdr!r} — file layout changed?")
        return None, None
    unit: Optional[str] = None
    data: dict[str, dict[int, tuple[float, Optional[str]]]] = {}
    total: Optional[dict[int, Optional[float]]] = None
    for r in rows[2:]:
        if r[0] is None:
            continue
        name = str(r[0]).strip()
        unit = unit or (str(r[1]).strip() if r[1] else None)
        vals: dict[int, Optional[float]] = {}
        for j, yr in enumerate(YEARS):
            try:
                vals[yr] = _to_float(r[2 + j])
            except ValueError:
                report.errors.append(f"{sheet}/{name}: bad value {r[2+j]!r}")
                report.rows_failed += 1
                vals[yr] = None
        if name == "Total":
            total = vals
            continue
        iso2 = COUNTRY_TO_ISO2.get(name)
        if iso2 is None:
            report.errors.append(f"{sheet}: unmapped country name {name!r} — add to COUNTRY_TO_ISO2")
            report.rows_failed += 1
            continue
        src = str(r[7]).strip() if len(r) > 7 and r[7] else None
        data[iso2] = {yr: (v, src) for yr, v in vals.items() if v is not None}
    if total is None:
        report.errors.append(f"{sheet}: no Total row")
        return None, None
    for yr in YEARS:
        t = total.get(yr) or 0.0
        s = sum(v[0] for cv in data.values() for y, v in cv.items() if y == yr)
        if t and abs(s - t) / t > _TOTAL_TOLERANCE:
            report.errors.append(f"{sheet} {yr}: parsed sum {s:.1f} != Total {t:.1f}")
            return None, None
    return unit, data


def _parse_group_sheet(ws, sheet: str, group_type: str, report: WmdIngestReport):
    """Parse one 6.3c/6.3d sheet -> {group_key: {year: value}}."""
    rows = list(ws.iter_rows(values_only=True))
    hdr = rows[1]
    if str(hdr[2]) != "2020" or str(hdr[6]) != "2024":
        report.errors.append(f"{sheet} ({group_type}): unrecognized header {hdr!r}")
        return None
    out: dict[str, dict[int, float]] = {}
    for r in rows[2:]:
        if r[0] is None:
            continue
        key = str(r[0]).strip()
        if key == "Total":
            continue
        if group_type == "stability" and key not in _STABILITY_KEYS:
            report.errors.append(f"{sheet}: unexpected stability class {key!r}")
            continue
        vals = {}
        for j, yr in enumerate(YEARS):
            try:
                v = _to_float(r[2 + j])
            except ValueError:
                report.errors.append(f"{sheet}/{key} ({group_type}): bad value {r[2+j]!r}")
                v = None
            if v is not None:
                vals[yr] = v
        out[key] = vals
    return out


def ingest_wmd(
    session: Session,
    data_dir: str | Path,
    *,
    edition: int,
    dry_run: bool = False,
) -> WmdIngestReport:
    """Load the WMD xlsx files from ``data_dir``.

    Expects filenames containing '6.4.', '6.3c' and '6.3d' (6.5 optional,
    used only by the crosscheck CLI).  Idempotent: upserts by natural key,
    so re-running an edition (or loading a newer one) revises in place.
    """
    import openpyxl

    report = WmdIngestReport(edition=edition, dry_run=dry_run)
    data_dir = Path(data_dir)

    def _find(fragment: str) -> Optional[Path]:
        hits = sorted(p for p in data_dir.glob("*.xlsx") if fragment in p.name)
        return hits[0] if hits else None

    f64 = _find("6.4")
    if f64 is None:
        report.errors.append(f"no 6.4 file found in {data_dir}")
        return report
    f63c, f63d = _find("6.3c"), _find("6.3d")

    # ── Resolve material mapping up front (hard error on claimed-but-missing)
    mat_by_name = {
        m.canonical_name: m.id for m in session.scalars(select(Material)).all()
    }
    claimed = {n for (_, _, n) in SHEET_META.values() if n}
    missing = claimed - set(mat_by_name)
    if missing:
        report.errors.append(f"materials table missing canonical names: {sorted(missing)}")
        return report

    # ── Commodities registry
    existing = {c.wmd_name: c for c in session.scalars(select(WmdCommodity)).all()}

    wb = openpyxl.load_workbook(f64, read_only=True)
    unknown_sheets = [s for s in wb.sheetnames if s not in SHEET_META]
    if unknown_sheets:
        report.errors.append(
            f"unknown sheets (edition layout changed — extend SHEET_META): {unknown_sheets}"
        )
        wb.close()
        return report

    parsed: dict[str, tuple] = {}
    for sheet in wb.sheetnames:
        report.sheets_seen += 1
        unit, data = _parse_country_sheet(wb[sheet], sheet, report)
        if data is None:
            continue
        parsed[sheet] = (unit, data)
    wb.close()

    if report.errors and any("!= Total" in e or "unmapped" in e for e in report.errors):
        # Validation-gate failures: refuse to write anything (all-or-nothing).
        log.warning("wmd.ingest.validation_failed", errors=len(report.errors))
        session.rollback()
        return report

    for sheet, (unit, data) in parsed.items():
        group, basis, mat_name = SHEET_META[sheet]
        comm = existing.get(sheet)
        if comm is None:
            comm = WmdCommodity(
                wmd_name=sheet, commodity_group=group, unit=unit,
                content_basis=basis,
                material_id=mat_by_name[mat_name] if mat_name else None,
            )
            session.add(comm)
            session.flush()
            existing[sheet] = comm
            report.commodities_inserted += 1
        rows_by_key = {
            (r.country_code, r.year): r
            for r in session.scalars(
                select(WmdProduction).where(WmdProduction.commodity_id == comm.id)
            ).all()
        }
        for iso2, per_year in data.items():
            for yr, (vol, src) in per_year.items():
                row = rows_by_key.get((iso2, yr))
                if row is None:
                    session.add(WmdProduction(
                        commodity_id=comm.id, country_code=iso2, year=yr,
                        volume=vol, source_code=src, edition=edition,
                    ))
                else:
                    row.volume, row.source_code, row.edition = vol, src, edition
                report.production_upserts += 1
        report.sheets_loaded += 1

    # ── Group files
    for path, gtype in ((f63c, "stability"), (f63d, "bloc")):
        if path is None:
            report.errors.append(f"no {gtype} file (6.3c/6.3d) found — skipped")
            continue
        wbg = openpyxl.load_workbook(path, read_only=True)
        for sheet in wbg.sheetnames:
            if sheet not in SHEET_META or sheet not in existing:
                continue
            out = _parse_group_sheet(wbg[sheet], sheet, gtype, report)
            if out is None:
                continue
            comm = existing[sheet]
            grows = {
                (g.group_key, g.year): g
                for g in session.scalars(
                    select(WmdGroupProduction).where(
                        WmdGroupProduction.commodity_id == comm.id,
                        WmdGroupProduction.group_type == gtype,
                    )
                ).all()
            }
            for key, per_year in out.items():
                for yr, vol in per_year.items():
                    g = grows.get((key, yr))
                    if g is None:
                        session.add(WmdGroupProduction(
                            commodity_id=comm.id, group_type=gtype,
                            group_key=key, year=yr, volume=vol, edition=edition,
                        ))
                    else:
                        g.volume, g.edition = vol, edition
                    report.group_upserts += 1
        wbg.close()

    if dry_run:
        session.rollback()
        log.info("wmd.ingest.dry_run", **{k: v for k, v in report.to_dict().items() if k != "errors"})
    else:
        session.commit()
        log.info("wmd.ingest.loaded", **{k: v for k, v in report.to_dict().items() if k != "errors"})
    return report


def wmd_shares(session: Session, commodity_id: int, year: int) -> dict[str, float]:
    """Shares over the FULL universe for one commodity-year (computed, never stored)."""
    rows = session.scalars(
        select(WmdProduction).where(
            WmdProduction.commodity_id == commodity_id,
            WmdProduction.year == year,
        )
    ).all()
    total = sum(r.volume for r in rows)
    if total <= 0:
        return {}
    return {r.country_code: r.volume / total for r in rows if r.volume > 0}
