"""Partner-curated facility seed loader (G4c, 2026-05-08).

Reads a partner-curated Excel template and upserts rows into:
  - ``companies``                (one per (canonical_name, headquarters_country))
  - ``facilities``               (one per (name, country))
  - ``company_facilities``       (junction; supports JV co-ownership)
  - ``facility_material_links``  (capacity / stage / hs_mapping_id)

Why a separate loader (not ``seed_facilities.py``)
--------------------------------------------------
``seed_facilities.py`` carries a curated Python list of cell factories,
pack plants, and recyclers — facilities the platform team has researched
itself.  This loader is fed by the partner's ongoing research workflow:
mining + refining facilities for the launch-list materials, edited in
Excel, ingested on demand.  Keeping the two paths separate avoids the
partner overwriting platform-curated rows on every refresh and keeps
the loader free to assume the partner-specific column shape.

Idempotency contract
--------------------
Re-running the loader on the same XLSX is a no-op:

  * Companies dedup on ``(canonical_name, headquarters_country)`` — JV
    rows for the same parent company in different countries are NOT
    merged (rare edge case; partner can flag if it bites).
  * Facilities dedup on ``(name, country)``.  Two rows referencing the
    same facility (the JV pattern) collapse to one ``Facility`` row plus
    two ``CompanyFacility`` junctions.
  * ``CompanyFacility`` dedup on ``(company_id, facility_id)`` — ownership
    fields update on re-run.
  * ``FacilityMaterialLink`` dedup on ``(facility_id, material_id)`` —
    capacity / stage / hs_mapping_id update on re-run.

Validation
----------
A row is rejected (and flagged in the report) when any of the following
fail; the rest of the rows still load:

  * ``material_canonical`` doesn't resolve to a tracked Material
  * ``country`` / ``company_country`` is not a valid ISO-2
  * ``facility_type`` / ``status`` / ``supply_chain_stage`` / ``ownership_type``
    is outside the allowed enum
  * ``hs_code`` is set but doesn't resolve to an ``HsCodeMaterialMapping``
    for the given material
  * ``ownership_pct`` is outside [0, 1]
  * ``capacity_tpy`` is negative
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import structlog
from openpyxl import load_workbook
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.facility import CompanyFacility, Facility, FacilityMaterialLink
from app.models.supply import HsCodeMaterialMapping, Material

log = structlog.get_logger(__name__)


# ── Allowed enum values ───────────────────────────────────────────────
# Mirrors the drop-down lists in the partner template.  Any additions
# here must also be added to facility_seed_template.xlsx so the partner
# sees them in her Excel drop-down.

_FACILITY_TYPES = {
    "mine", "refinery", "smelter", "concentrator",
    "processing", "recycling", "cell_factory", "pack_plant",
    "r_and_d", "hq", "other",
}
_STATUSES = {
    "operating", "planned", "under_construction",
    "mothballed", "closed", "care_maintenance",
}
_STAGES = {
    "ore", "concentrate", "intermediate", "refined",
    "battery_grade", "fabricated", "scrap",
}
_OWNERSHIP_TYPES = {
    "operator", "jv_partner", "lessee", "minority_stake", "other",
}
_CAPACITY_UNITS = {"t/yr", "kt/yr", "mt/yr", "lb/yr", "GWh/yr", "other"}

_ISO2_RE = re.compile(r"^[A-Z]{2}$")

# Column ordering in the partner template (must match the XLSX header row).
EXPECTED_COLUMNS = [
    "company_name", "company_country", "facility_name", "facility_type",
    "country", "region", "city", "lat", "lon", "status",
    "material_canonical", "capacity_tpy", "capacity_unit",
    "is_primary_product", "supply_chain_stage", "hs_code",
    "ownership_type", "ownership_pct", "source_url", "notes",
]

# Header row + hint row + 2 example rows + 1 note row = 5 rows of preamble.
# Real data starts at row 6.  The loader tolerates either: it scans for
# the header and starts from the row after the hint row.

DEFAULT_DATA_SOURCE = "partner_facility_seed"


# ─────────────────────────────────────────────────────────────────────
# Result shapes
# ─────────────────────────────────────────────────────────────────────

@dataclass
class RowError:
    row_num: int
    field: Optional[str]
    message: str


@dataclass
class LoadReport:
    rows_seen: int = 0
    rows_loaded: int = 0
    rows_skipped: int = 0   # blank rows or example rows
    rows_failed: int = 0    # validation errors
    companies_inserted: int = 0
    companies_updated: int = 0
    facilities_inserted: int = 0
    facilities_updated: int = 0
    company_facilities_inserted: int = 0
    company_facilities_updated: int = 0
    material_links_inserted: int = 0
    material_links_updated: int = 0
    hs_lookup_misses: int = 0
    errors: list[RowError] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_seen": self.rows_seen,
            "rows_loaded": self.rows_loaded,
            "rows_skipped": self.rows_skipped,
            "rows_failed": self.rows_failed,
            "companies_inserted": self.companies_inserted,
            "companies_updated": self.companies_updated,
            "facilities_inserted": self.facilities_inserted,
            "facilities_updated": self.facilities_updated,
            "company_facilities_inserted": self.company_facilities_inserted,
            "company_facilities_updated": self.company_facilities_updated,
            "material_links_inserted": self.material_links_inserted,
            "material_links_updated": self.material_links_updated,
            "hs_lookup_misses": self.hs_lookup_misses,
            "errors": [
                {"row": e.row_num, "field": e.field, "message": e.message}
                for e in self.errors
            ],
        }


# ─────────────────────────────────────────────────────────────────────
# XLSX → row dicts
# ─────────────────────────────────────────────────────────────────────

def _read_rows(
    xlsx_path: str | Path,
    sheet: str = "Facility Seed",
) -> tuple[list[dict[str, Any]], list[RowError]]:
    """Read non-blank data rows from the partner template.

    Returns ``(rows, header_errors)``.  ``rows`` is a list of dicts whose
    keys are the canonical column names from ``EXPECTED_COLUMNS``; missing
    cells map to ``None``.  Each dict carries an extra ``__row_num__`` key
    holding the 1-indexed Excel row number for error reporting.
    """
    wb = load_workbook(filename=str(xlsx_path), read_only=True, data_only=True)
    if sheet not in wb.sheetnames:
        return [], [RowError(0, None, f"Sheet {sheet!r} not found in workbook")]

    ws = wb[sheet]
    iterator = ws.iter_rows(values_only=False)

    # Find the header row by scanning for ``company_name`` in column A.
    header: dict[int, str] = {}
    header_row_num: Optional[int] = None
    errors: list[RowError] = []

    for row in iterator:
        if not row:
            continue
        first = row[0].value if row[0] is not None else None
        if isinstance(first, str) and first.strip().lower() == "company_name":
            header_row_num = row[0].row
            for cell in row:
                if cell.value is not None:
                    header[cell.column] = str(cell.value).strip()
            break

    if header_row_num is None:
        errors.append(RowError(
            0, None,
            "Could not locate header row (looking for 'company_name' in column A)",
        ))
        return [], errors

    missing = [c for c in EXPECTED_COLUMNS if c not in header.values()]
    if missing:
        errors.append(RowError(
            header_row_num, None,
            f"Missing required columns: {', '.join(missing)}",
        ))
        return [], errors

    # Map column index → canonical name for fast lookup.
    col_map = {idx: name for idx, name in header.items()}

    out: list[dict[str, Any]] = []
    for row in iterator:
        if not row:
            continue
        # Excel row index — derived from the first cell that exposes one.
        # In read-only mode, blank trailing cells materialise as EmptyCell
        # which doesn't have a ``.row`` / ``.column`` attribute, so we
        # walk the row defensively.
        excel_row: Optional[int] = None
        for cell in row:
            r = getattr(cell, "row", None)
            if r is not None:
                excel_row = r
                break
        if excel_row is None or excel_row <= header_row_num:
            continue
        # Skip the hint row immediately after the header (italic guidance).
        if excel_row == header_row_num + 1:
            continue
        record: dict[str, Any] = {"__row_num__": excel_row}
        any_value = False
        for cell in row:
            col = getattr(cell, "column", None)
            if col is None:
                continue
            name = col_map.get(col)
            if name is None:
                continue
            v = cell.value
            if isinstance(v, str):
                v = v.strip()
                if v == "":
                    v = None
            record[name] = v
            if v is not None:
                any_value = True
        if any_value:
            out.append(record)

    return out, errors


# ─────────────────────────────────────────────────────────────────────
# Row validation + normalisation
# ─────────────────────────────────────────────────────────────────────

def _coerce_bool(v: Any) -> Optional[bool]:
    """Accept TRUE/FALSE/1/0/yes/no strings, bools, ints."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in {"true", "t", "yes", "y", "1"}:
        return True
    if s in {"false", "f", "no", "n", "0"}:
        return False
    return None


def _coerce_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm_iso2(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().upper()
    return s if _ISO2_RE.match(s) else None


def _norm_capacity(value: Optional[float], unit: Optional[str]) -> Optional[float]:
    """Normalise to t/yr.  Returns None if either input is missing.

    kt/yr   → ×1_000
    mt/yr   → ×1_000_000
    GWh/yr  → kept as-is but tagged in the unit column (operational pillar
              currently only consumes mass-based capacity, so GWh rows are
              persisted but won't feed structural_dependency yet).
    """
    if value is None or unit is None:
        return value
    unit = unit.strip().lower()
    if unit in {"t/yr", "tonne/yr", "tonnes/yr", "ton/yr"}:
        return value
    if unit == "kt/yr":
        return value * 1_000
    if unit == "mt/yr":
        return value * 1_000_000
    if unit == "lb/yr":
        return value / 2204.62
    # GWh/yr or other — pass through, caller decides what to do.
    return value


# ─────────────────────────────────────────────────────────────────────
# Resolver helpers
# ─────────────────────────────────────────────────────────────────────

def _resolve_material(
    session: Session, name: Optional[str],
) -> Optional[Material]:
    """Look up by canonical_name (case-insensitive)."""
    if not name:
        return None
    return session.scalar(
        select(Material).where(Material.canonical_name.ilike(name))
    )


def _resolve_hs_mapping_id(
    session: Session, material_id: int, hs_code: Optional[str],
) -> Optional[int]:
    """Resolve (material_id, hs_code) to an HsCodeMaterialMapping.id.

    Accepts either dotted (``2836.91``) or undotted (``283691``) prefixes.
    Tries exact match first, then 4-digit prefix match.  Returns ``None``
    if no mapping exists — caller flags the miss.
    """
    if not hs_code:
        return None
    raw = str(hs_code).strip()
    candidates = {raw, raw.replace(".", ""), raw.replace(" ", "")}
    # Pad to 4 digits; also try the 4-digit prefix.
    for c in list(candidates):
        if len(c) >= 4 and c.isdigit():
            candidates.add(c[:4])
    for c in candidates:
        row = session.scalar(
            select(HsCodeMaterialMapping).where(
                HsCodeMaterialMapping.material_id == material_id,
                HsCodeMaterialMapping.hs_code_prefix == c,
            )
        )
        if row is not None:
            return row.id
    return None


def _find_or_create_company(
    session: Session,
    canonical_name: str,
    country: Optional[str],
) -> tuple[Company, bool]:
    """Returns (company, created).  Looks up by canonical_name; updates
    headquarters_country if it was previously NULL."""
    existing = session.scalar(
        select(Company).where(Company.canonical_name == canonical_name)
    )
    if existing is not None:
        if country and not existing.headquarters_country:
            existing.headquarters_country = country
        return existing, False
    company = Company(
        canonical_name=canonical_name,
        headquarters_country=country,
        data_source=DEFAULT_DATA_SOURCE,
        verified=True,
    )
    session.add(company)
    session.flush()
    return company, True


def _find_or_create_facility(
    session: Session,
    name: str,
    country: str,
    seed: dict[str, Any],
) -> tuple[Facility, bool, bool]:
    """Dedup on (name, country).

    Returns ``(facility, created, updated)``.  ``seed`` carries mutable
    fields the partner re-edits over time (status, region, city, lat/lon).
    The ``created`` flag is required because ``session.flush()`` clears
    ``session.new`` immediately, so the caller can't distinguish inserts
    from updates by inspecting the session.
    """
    existing = session.scalar(
        select(Facility).where(
            and_(Facility.name == name, Facility.country == country)
        )
    )
    if existing is None:
        facility = Facility(
            name=name,
            country=country,
            facility_type=seed["facility_type"],
            region=seed.get("region"),
            city=seed.get("city"),
            status=seed.get("status", "operating"),
            latitude=seed.get("latitude"),
            longitude=seed.get("longitude"),
            data_source=DEFAULT_DATA_SOURCE,
            verified=True,
        )
        session.add(facility)
        session.flush()
        return facility, True, False
    # Update mutable fields when partner edits the spreadsheet.
    updated = False
    for col in ("region", "city", "status", "latitude", "longitude"):
        new = seed.get(col)
        if new is not None and getattr(existing, col) != new:
            setattr(existing, col, new)
            updated = True
    if seed.get("facility_type") and existing.facility_type != seed["facility_type"]:
        existing.facility_type = seed["facility_type"]
        updated = True
    return existing, False, updated


def _upsert_company_facility(
    session: Session,
    company_id: Any,
    facility_id: Any,
    ownership_type: str,
    ownership_pct: Optional[float],
) -> tuple[CompanyFacility, bool, bool]:
    """Returns (link, created, updated)."""
    existing = session.scalar(
        select(CompanyFacility).where(
            and_(
                CompanyFacility.company_id == company_id,
                CompanyFacility.facility_id == facility_id,
            )
        )
    )
    if existing is None:
        link = CompanyFacility(
            company_id=company_id,
            facility_id=facility_id,
            ownership_type=ownership_type,
            ownership_pct=ownership_pct,
            verified=True,
        )
        session.add(link)
        return link, True, False
    updated = False
    if existing.ownership_type != ownership_type:
        existing.ownership_type = ownership_type
        updated = True
    if existing.ownership_pct != ownership_pct:
        existing.ownership_pct = ownership_pct
        updated = True
    return existing, False, updated


def _upsert_material_link(
    session: Session,
    facility_id: Any,
    material_id: int,
    capacity_tpy: Optional[float],
    capacity_unit: str,
    is_primary: bool,
    stage: Optional[str],
    hs_mapping_id: Optional[int],
) -> tuple[FacilityMaterialLink, bool, bool]:
    existing = session.scalar(
        select(FacilityMaterialLink).where(
            and_(
                FacilityMaterialLink.facility_id == facility_id,
                FacilityMaterialLink.material_id == material_id,
            )
        )
    )
    if existing is None:
        link = FacilityMaterialLink(
            facility_id=facility_id,
            material_id=material_id,
            annual_capacity_tpy=capacity_tpy,
            capacity_unit=capacity_unit,
            is_primary_product=is_primary,
            supply_chain_stage=stage,
            hs_mapping_id=hs_mapping_id,
        )
        session.add(link)
        return link, True, False
    updated = False
    for attr, val in (
        ("annual_capacity_tpy", capacity_tpy),
        ("capacity_unit", capacity_unit),
        ("is_primary_product", is_primary),
        ("supply_chain_stage", stage),
        ("hs_mapping_id", hs_mapping_id),
    ):
        if val is not None and getattr(existing, attr) != val:
            setattr(existing, attr, val)
            updated = True
    return existing, False, updated


# ─────────────────────────────────────────────────────────────────────
# Public entrypoint
# ─────────────────────────────────────────────────────────────────────

# The two example rows in the template (Albemarle + Mineral Resources at
# Kemerton) are skipped by default so the partner can leave them in place
# as documentation.  Pass ``include_examples=True`` for fixture builds.
_EXAMPLE_ROW_KEYS: set[tuple[str, str]] = {
    ("Albemarle Corporation", "Kemerton Lithium Hydroxide Plant"),
    ("Mineral Resources Ltd", "Kemerton Lithium Hydroxide Plant"),
}


def load_partner_facility_seed(
    session: Session,
    xlsx_path: str | Path,
    *,
    sheet: str = "Facility Seed",
    dry_run: bool = False,
    include_examples: bool = False,
) -> LoadReport:
    """Load a partner-curated facility seed XLSX into the DB.

    Args:
        session:           Active SQLAlchemy session.
        xlsx_path:         Path to the partner XLSX file.
        sheet:             Sheet name within the workbook (default
                           ``"Facility Seed"``).
        dry_run:           Validate + report without committing.  All
                           writes are rolled back at the end of the call.
        include_examples:  Load the two Albemarle/Kemerton example rows
                           too — defaults to False so partners can keep
                           them visible without polluting prod data.

    Returns a ``LoadReport`` with per-row error detail.  Always commits
    (or rolls back) before returning so the session is consistent.
    """
    report = LoadReport()

    rows, header_errors = _read_rows(xlsx_path, sheet=sheet)
    report.errors.extend(header_errors)
    if header_errors:
        log.warning("seed_facilities_partner.header_errors", count=len(header_errors))
        return report

    # Cache material lookups by lowercase canonical name.
    material_cache: dict[str, Optional[Material]] = {}

    for row in rows:
        report.rows_seen += 1
        rownum = row["__row_num__"]

        company_name = row.get("company_name")
        facility_name = row.get("facility_name")

        # ── Skip blank rows ───────────────────────────────────────────
        if not company_name or not facility_name:
            report.rows_skipped += 1
            continue

        # ── Skip example rows unless explicitly included ─────────────
        if not include_examples and (company_name, facility_name) in _EXAMPLE_ROW_KEYS:
            report.rows_skipped += 1
            continue

        # ── Validate enums + ISO codes ───────────────────────────────
        row_errors: list[RowError] = []

        company_country = _norm_iso2(row.get("company_country"))
        if row.get("company_country") and company_country is None:
            row_errors.append(RowError(
                rownum, "company_country",
                f"'{row['company_country']}' is not a valid ISO-2 country",
            ))

        country = _norm_iso2(row.get("country"))
        if country is None:
            row_errors.append(RowError(
                rownum, "country",
                f"'{row.get('country')}' is not a valid ISO-2 country (required)",
            ))

        facility_type = (row.get("facility_type") or "").strip().lower()
        if facility_type not in _FACILITY_TYPES:
            row_errors.append(RowError(
                rownum, "facility_type",
                f"'{facility_type}' not in allowed set: {sorted(_FACILITY_TYPES)}",
            ))

        status = (row.get("status") or "operating").strip().lower()
        if status not in _STATUSES:
            row_errors.append(RowError(
                rownum, "status",
                f"'{status}' not in allowed set: {sorted(_STATUSES)}",
            ))

        stage = row.get("supply_chain_stage")
        stage = stage.strip().lower() if isinstance(stage, str) else stage
        if stage and stage not in _STAGES:
            row_errors.append(RowError(
                rownum, "supply_chain_stage",
                f"'{stage}' not in allowed set: {sorted(_STAGES)}",
            ))

        ownership_type = (row.get("ownership_type") or "operator").strip().lower()
        if ownership_type not in _OWNERSHIP_TYPES:
            row_errors.append(RowError(
                rownum, "ownership_type",
                f"'{ownership_type}' not in allowed set: {sorted(_OWNERSHIP_TYPES)}",
            ))

        ownership_pct = _coerce_float(row.get("ownership_pct"))
        if ownership_pct is not None and not (0.0 <= ownership_pct <= 1.0):
            row_errors.append(RowError(
                rownum, "ownership_pct",
                f"{ownership_pct} outside [0, 1]",
            ))

        capacity_tpy_raw = _coerce_float(row.get("capacity_tpy"))
        if capacity_tpy_raw is not None and capacity_tpy_raw < 0:
            row_errors.append(RowError(
                rownum, "capacity_tpy",
                f"{capacity_tpy_raw} cannot be negative",
            ))

        capacity_unit = (row.get("capacity_unit") or "t/yr").strip()
        if capacity_unit not in _CAPACITY_UNITS:
            row_errors.append(RowError(
                rownum, "capacity_unit",
                f"'{capacity_unit}' not in allowed set: {sorted(_CAPACITY_UNITS)}",
            ))

        material_canonical = (row.get("material_canonical") or "").strip()
        material: Optional[Material] = None
        if material_canonical:
            cache_key = material_canonical.lower()
            if cache_key not in material_cache:
                material_cache[cache_key] = _resolve_material(session, material_canonical)
            material = material_cache[cache_key]
        if material is None:
            row_errors.append(RowError(
                rownum, "material_canonical",
                f"'{material_canonical}' not found in materials table — "
                f"check the Critical Minerals tab for canonical names",
            ))

        if row_errors:
            report.errors.extend(row_errors)
            report.rows_failed += 1
            continue

        # All required validations passed.  Coerce the optional fields.
        is_primary = _coerce_bool(row.get("is_primary_product"))
        if is_primary is None:
            is_primary = True

        capacity_tpy = _norm_capacity(capacity_tpy_raw, capacity_unit)

        hs_mapping_id = _resolve_hs_mapping_id(
            session, material.id, row.get("hs_code"),
        )
        if row.get("hs_code") and hs_mapping_id is None:
            report.hs_lookup_misses += 1
            log.info(
                "seed_facilities_partner.hs_lookup_miss",
                row=rownum,
                material=material.canonical_name,
                hs_code=row["hs_code"],
            )

        # ── Upsert chain ──────────────────────────────────────────────
        company, c_created = _find_or_create_company(
            session, company_name.strip(), company_country,
        )
        if c_created:
            report.companies_inserted += 1

        facility_seed = {
            "facility_type": facility_type,
            "region": (row.get("region") or "").strip() or None,
            "city": (row.get("city") or "").strip() or None,
            "status": status,
            "latitude": _coerce_float(row.get("lat")),
            "longitude": _coerce_float(row.get("lon")),
        }
        facility, f_created, f_updated = _find_or_create_facility(
            session, facility_name.strip(), country, facility_seed,
        )
        if f_created:
            report.facilities_inserted += 1
        elif f_updated:
            report.facilities_updated += 1

        cf, cf_created, cf_updated = _upsert_company_facility(
            session, company.id, facility.id, ownership_type, ownership_pct,
        )
        if cf_created:
            report.company_facilities_inserted += 1
        elif cf_updated:
            report.company_facilities_updated += 1

        ml, ml_created, ml_updated = _upsert_material_link(
            session,
            facility_id=facility.id,
            material_id=material.id,
            capacity_tpy=capacity_tpy,
            capacity_unit=capacity_unit,
            is_primary=is_primary,
            stage=stage,
            hs_mapping_id=hs_mapping_id,
        )
        if ml_created:
            report.material_links_inserted += 1
        elif ml_updated:
            report.material_links_updated += 1

        report.rows_loaded += 1

    if dry_run:
        session.rollback()
        log.info(
            "seed_facilities_partner.dry_run_complete",
            **{k: v for k, v in report.to_dict().items() if k != "errors"},
        )
    else:
        session.commit()
        log.info(
            "seed_facilities_partner.loaded",
            **{k: v for k, v in report.to_dict().items() if k != "errors"},
        )

    return report


__all__ = [
    "DEFAULT_DATA_SOURCE",
    "EXPECTED_COLUMNS",
    "LoadReport",
    "RowError",
    "load_partner_facility_seed",
]
