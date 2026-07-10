"""Load manually-extracted risk events from xlsx into the DB.

Workflow:
  1.  You walk through a filing (10-K, 20-F, press release, etc.)
  2.  For each risk event you find, add a row to
      ``Automotive Data Solutions/manual_risk_events.xlsx``
  3.  Run this script with ``--dry-run`` first to preview.
  4.  Re-run without ``--dry-run`` to actually insert.  The script writes
      ``load_status`` and ``loaded_event_id`` back to the spreadsheet so
      you can see what landed and what didn't.

FK RESOLUTION — hard vs soft (Option C):

  HARD requirements — row is rejected if unresolved:
    - materials  (must exist in materials.canonical_name — scoring reads
      risk_event_materials as primary attribution path)
    - primary_country  (ISO2, no FK lookup needed)

  SOFT references — row loads with a warning, junction skipped for the
  unresolved entity, other entities still linked:
    - companies    (junction gated by feature_flags.LINK_EVENTS_TO_COMPANIES
                    even when resolved — matches auto-ingester pattern)
    - facilities   (no auto ingester writes this junction today either)
    - regulations  (optional; typically only meaningful when event IS a reg)

  Status codes reflect this split:
    - loaded         — event + all junctions written cleanly
    - loaded_partial — event + material/geography written; some soft refs
                       were unresolved (see load_message for details)
    - skipped        — content_hash already exists (idempotency)
    - error          — hard requirement missing/unresolved; row rejected

Tables written per xlsx row:
  - ``sources`` (one row for "manual_walkthrough", created on first run)
  - ``source_documents`` (one row per unique source_url)
  - ``risk_events`` (the main row)
  - ``risk_event_materials`` (one row per linked material) — REQUIRED
  - ``risk_event_geographies`` (one primary + N secondary rows) — REQUIRED
  - ``risk_event_companies`` (one row per linked company)
      → gated by feature_flags.LINK_EVENTS_TO_COMPANIES (default False)
  - ``risk_event_facilities`` (one row per linked facility, if resolved)
  - ``risk_event_regulations`` (one row per linked regulation, if resolved)

Idempotency: events are deduped by content_hash = SHA-256(title + summary +
event_date).  Re-running the loader on an unchanged spreadsheet is a no-op.

Usage:
    python scripts/load_manual_risk_events.py \\
        --seed-path "Automotive Data Solutions/manual_risk_events.xlsx" \\
        --dry-run

    python scripts/load_manual_risk_events.py \\
        --seed-path "Automotive Data Solutions/manual_risk_events.xlsx"
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import openpyxl
from openpyxl.styles import PatternFill
from sqlalchemy import select
from sqlalchemy.orm import Session

# DB models — adjust import paths to match your project layout
from app.db.session import get_session_factory
from app.models.company import Company
from app.models.documents import SourceDocument
from app.models.facility import CompanyFacility, Facility
from app.models.regulatory import (
    Regulation,
    RiskEvent,
    RiskEventCompany,
    RiskEventFacility,
    RiskEventGeography,
    RiskEventMaterial,
    RiskEventRegulation,
)
from app.models.source import Source
from app.models.supply import Material
from app.services.ingestion import feature_flags


# Canonical source row for all manually-entered events.
_MANUAL_SOURCE_NAME = "manual_walkthrough"
_MANUAL_SOURCE_TYPE = "manual"
_MANUAL_SOURCE_PHASE = "curated"  # 7 chars — Source.phase = String(8) constraint


# ---------------------------------------------------------------------------
# Spreadsheet schema
# ---------------------------------------------------------------------------

# HARD REQUIREMENTS — a row fails to load if any of these are missing or unresolvable.
# Materials and primary_country are load-bearing: they populate the junctions the
# scoring engine actually reads from (risk_event_materials, risk_event_geographies).
# Everything else on this list is either a semantic requirement (title, summary,
# severity) or provenance (source_url, source_form_type).
REQUIRED_FIELDS = {
    "event_type",
    "event_date",
    "title",
    "summary",
    "severity_score",
    "confidence_score",
    "risk_categories",
    "primary_country",
    "materials",     # hard-required: scoring reads risk_event_materials
    "source_url",
    "source_form_type",
}

# SOFT FIELDS — unresolvable references log a warning but do NOT fail the row.
# Rationale: company_junction is gated by LINK_EVENTS_TO_COMPANIES (Phase 5),
# facility_junction is not populated by any existing ingester, and regulation
# junction is optional (only meaningful when the event IS a regulation).
# Rows still load with material + geography linkage intact; unresolved refs
# are logged in load_message so you can fix them and re-load with --reload.
SOFT_REFERENCE_FIELDS = {"companies", "facilities", "regulations"}

CANONICAL_EVENT_TYPES = {
    "TARIFF",
    "EXPORT_RESTRICTION",
    "IMPORT_DISRUPTION",
    "TRADE_CONCENTRATION",
    "REGULATORY_COMPLIANCE",
    "TRADE_POLICY",
    "INCIDENT",
    "LITIGATION",
    "SETTLEMENT",
    "OPERATIONAL_DISRUPTION",
}

CANONICAL_RISK_CATEGORIES = {
    "material_concentration",
    "geopolitical",
    "operational",
    "regulatory_compliance",
    "financial_pressure",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_csv(value: Any) -> list[str]:
    """Split a comma-separated cell value into trimmed strings."""
    if value is None or str(value).strip() == "":
        return []
    return [s.strip() for s in str(value).split(",") if s.strip()]


def _coerce_date(value: Any) -> Optional[datetime]:
    """Coerce an Excel date or string into a tz-aware-equivalent datetime.

    Excel cells come back as datetime when correctly formatted; strings need
    parsing.  We store at midnight UTC for date-only inputs.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(value.strip(), fmt)
            except ValueError:
                continue
    return None


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compute_content_hash(title: str, summary: str, event_date: datetime) -> str:
    """SHA-256 hash matching the ingester convention (see risk_events.content_hash)."""
    parts = [
        (title or "").strip(),
        (summary or "").strip(),
        event_date.isoformat() if event_date else "",
    ]
    payload = "||".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _get_or_create_manual_source(session: Session) -> int:
    """Return id of the canonical 'manual_walkthrough' Source row."""
    existing = session.scalar(
        select(Source).where(Source.name == _MANUAL_SOURCE_NAME)
    )
    if existing is not None:
        return existing.id
    source = Source(
        name=_MANUAL_SOURCE_NAME,
        source_type=_MANUAL_SOURCE_TYPE,
        phase=_MANUAL_SOURCE_PHASE,
        is_active=True,
        config_json={
            "description": "Risk events captured via manual filing walkthroughs",
            "loader": "scripts/load_manual_risk_events.py",
        },
    )
    session.add(source)
    session.flush()
    return source.id


def _get_or_create_source_document(
    session: Session,
    source_id: int,
    source_url: str,
    title: str,
    form_type: str,
    accession: Optional[str],
    filing_date: Optional[datetime],
) -> int:
    """Get or create source_documents row keyed on (source_id, external_id).

    external_id = SEC accession when present, else SHA-256(url) so re-running
    with the same source_url reuses the same row.
    """
    external_id = accession.strip() if accession else hashlib.sha256(source_url.encode()).hexdigest()[:32]
    existing = session.scalar(
        select(SourceDocument).where(
            SourceDocument.source_id == source_id,
            SourceDocument.external_id == external_id,
        )
    )
    if existing is not None:
        return existing.id
    doc = SourceDocument(
        source_id=source_id,
        external_id=external_id,
        title=title,
        url=source_url,
        published_at=filing_date,
        document_type=form_type or "manual_entry",
        metadata_json={"manual_entry": True, "accession_number": accession},
    )
    session.add(doc)
    session.flush()
    return doc.id


# ---------------------------------------------------------------------------
# FK resolution
# ---------------------------------------------------------------------------


def _resolve_companies(
    session: Session, names: list[str]
) -> tuple[list[Company], list[str]]:
    """Return (resolved_companies, unresolved_names)."""
    resolved: list[Company] = []
    unresolved: list[str] = []
    for name in names:
        company = session.scalar(
            select(Company).where(Company.canonical_name == name)
        )
        if company is None:
            unresolved.append(name)
        else:
            resolved.append(company)
    return resolved, unresolved


def _resolve_materials(
    session: Session, names: list[str]
) -> tuple[list[Material], list[str]]:
    resolved: list[Material] = []
    unresolved: list[str] = []
    for name in names:
        material = session.scalar(
            select(Material).where(Material.canonical_name == name)
        )
        if material is None:
            unresolved.append(name)
        else:
            resolved.append(material)
    return resolved, unresolved


def _resolve_facilities(
    session: Session,
    names: list[str],
    company_ids: list,
) -> tuple[list[Facility], list[str]]:
    """Resolve facilities, scoped to the linked companies to avoid name collisions.

    Notes on the schema:
      - ``Facility`` uses ``name`` (not ``canonical_name``).
      - Facilities do NOT carry ``company_id`` directly. The company↔facility
        relationship lives in the ``company_facilities`` junction table
        (``CompanyFacility``). So we JOIN through that table when a
        company scope is provided.
    """
    resolved: list[Facility] = []
    unresolved: list[str] = []
    for name in names:
        if company_ids:
            stmt = (
                select(Facility)
                .join(CompanyFacility, CompanyFacility.facility_id == Facility.id)
                .where(
                    Facility.name == name,
                    CompanyFacility.company_id.in_(company_ids),
                )
            )
        else:
            stmt = select(Facility).where(Facility.name == name)
        facility = session.scalar(stmt)
        if facility is None:
            unresolved.append(name)
        else:
            resolved.append(facility)
    return resolved, unresolved


def _resolve_regulations(
    session: Session, keys: list[str]
) -> tuple[list[Regulation], list[str]]:
    resolved: list[Regulation] = []
    unresolved: list[str] = []
    for key in keys:
        reg = session.scalar(
            select(Regulation).where(Regulation.regulation_key == key)
        )
        if reg is None:
            unresolved.append(key)
        else:
            resolved.append(reg)
    return resolved, unresolved


# ---------------------------------------------------------------------------
# Row processing
# ---------------------------------------------------------------------------


def _validate_row(row: dict) -> list[str]:
    """Return list of validation errors for a row (empty list = OK)."""
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if row.get(field) is None or str(row.get(field, "")).strip() == "":
            errors.append(f"missing_required:{field}")

    event_type = (row.get("event_type") or "").strip().upper()
    if event_type and event_type not in CANONICAL_EVENT_TYPES:
        errors.append(f"invalid_event_type:{event_type}")

    cats = _split_csv(row.get("risk_categories"))
    for c in cats:
        if c not in CANONICAL_RISK_CATEGORIES:
            errors.append(f"invalid_risk_category:{c}")

    sev = _coerce_float(row.get("severity_score"))
    if sev is not None and not (0.0 <= sev <= 1.0):
        errors.append(f"severity_out_of_range:{sev}")

    conf = _coerce_float(row.get("confidence_score"))
    if conf is not None and not (0.0 <= conf <= 1.0):
        errors.append(f"confidence_out_of_range:{conf}")

    primary = (row.get("primary_country") or "").strip().upper()
    if primary and len(primary) != 2:
        errors.append(f"primary_country_not_iso2:{primary}")

    for sec in _split_csv(row.get("secondary_countries")):
        if len(sec) != 2:
            errors.append(f"secondary_country_not_iso2:{sec}")

    if _coerce_date(row.get("event_date")) is None and "event_date" not in [e.split(":", 1)[1] for e in errors if e.startswith("missing")]:
        errors.append(f"unparseable_event_date:{row.get('event_date')!r}")

    return errors


def _process_row(
    session: Session,
    row: dict,
    dry_run: bool,
) -> dict:
    """Process one xlsx row. Returns dict with status, message, event_id.

    Option C behavior (see SOFT_REFERENCE_FIELDS docstring):
      - Materials must resolve (hard requirement — scoring reads risk_event_materials)
      - Primary geography is auto-written from primary_country (no FK)
      - Companies/facilities/regulations: unresolved refs log warnings but do
        NOT fail the row. Junctions are only written for entities that resolve.
      - Company-junction writes additionally honor
        feature_flags.LINK_EVENTS_TO_COMPANIES (matches existing ingester pattern).
    """
    result = {"status": "", "message": "", "event_id": ""}

    # Validate
    errors = _validate_row(row)
    if errors:
        result["status"] = "error"
        result["message"] = "; ".join(errors[:5])
        return result

    # Resolve FKs
    companies = _split_csv(row.get("companies"))
    materials = _split_csv(row.get("materials"))
    facilities = _split_csv(row.get("facilities"))
    regulations = _split_csv(row.get("regulations"))

    resolved_companies, missing_co = _resolve_companies(session, companies)
    resolved_materials, missing_mat = _resolve_materials(session, materials)
    resolved_regs, missing_reg = _resolve_regulations(session, regulations)
    company_ids = [c.id for c in resolved_companies]
    resolved_facs, missing_fac = _resolve_facilities(session, facilities, company_ids)

    # HARD FAIL: material FK must resolve.  Everything downstream in scoring
    # reads risk_event_materials — a row without a resolvable material would
    # be invisible to the material scoring path.
    if missing_mat:
        result["status"] = "error"
        result["message"] = f"material_not_found:{'|'.join(missing_mat)}"
        return result

    # SOFT WARNINGS: unresolved companies/facilities/regulations get logged
    # into load_message but do NOT fail the row.  These junctions are either
    # gated by a feature flag (companies), not yet populated by any auto
    # ingester (facilities), or optional-by-nature (regulations).
    warnings: list[str] = []
    if missing_co:
        warnings.append(f"unresolved_companies={len(missing_co)}[{'|'.join(missing_co)}]")
    if missing_fac:
        warnings.append(f"unresolved_facilities={len(missing_fac)}[{'|'.join(missing_fac)}]")
    if missing_reg:
        warnings.append(f"unresolved_regulations={len(missing_reg)}[{'|'.join(missing_reg)}]")

    # Compute content hash
    event_date = _coerce_date(row.get("event_date"))
    content_hash = _compute_content_hash(
        row.get("title") or "",
        row.get("summary") or "",
        event_date,
    )

    # Idempotency check
    existing = session.scalar(
        select(RiskEvent).where(RiskEvent.content_hash == content_hash)
    )
    if existing is not None:
        result["status"] = "skipped"
        result["message"] = f"content_hash_exists:{existing.id}"
        result["event_id"] = str(existing.id)
        return result

    if dry_run:
        # Preview what juction rows would be written under Option C behavior.
        company_write_msg = (
            f"companies_link={len(resolved_companies)}"
            if feature_flags.LINK_EVENTS_TO_COMPANIES
            else f"companies_link=SKIPPED[flag_off, {len(resolved_companies)} resolved]"
        )
        parts = [
            f"would_insert: materials={len(resolved_materials)}",
            company_write_msg,
            f"facilities={len(resolved_facs)}",
            f"regulations={len(resolved_regs)}",
        ]
        if warnings:
            parts.append("warnings=[" + "; ".join(warnings) + "]")
        result["status"] = "dry_ok"
        result["message"] = "; ".join(parts)
        return result

    # Resolve source + source_document
    source_id = _get_or_create_manual_source(session)
    source_doc_id = _get_or_create_source_document(
        session=session,
        source_id=source_id,
        source_url=row.get("source_url") or "",
        title=row.get("title") or "",
        form_type=row.get("source_form_type") or "",
        accession=row.get("source_accession"),
        filing_date=_coerce_date(row.get("source_filing_date")),
    )

    # Build geography_json
    primary_country = (row.get("primary_country") or "").strip().upper()
    secondary_countries = [
        s.upper() for s in _split_csv(row.get("secondary_countries"))
    ]
    geography_json = {
        "primary": primary_country,
        "secondary": secondary_countries,
    }

    # Build metadata
    metadata_json = {
        "source_section": row.get("source_section"),
        "source_form_type": row.get("source_form_type"),
        "source_accession": row.get("source_accession"),
        "event_subtype": row.get("event_subtype"),
        "notes": row.get("notes"),
        "loader": "load_manual_risk_events.py",
        "loaded_at": datetime.utcnow().isoformat(),
    }
    metadata_json = {k: v for k, v in metadata_json.items() if v is not None and v != ""}

    # Insert risk_event
    event = RiskEvent(
        source_document_id=source_doc_id,
        event_type=(row.get("event_type") or "").strip().upper(),
        event_subtype=(row.get("event_subtype") or "").strip() or None,
        event_date=event_date,
        title=(row.get("title") or "").strip(),
        summary=(row.get("summary") or "").strip(),
        severity_score=_coerce_float(row.get("severity_score")),
        confidence_score=_coerce_float(row.get("confidence_score")),
        risk_categories_json=_split_csv(row.get("risk_categories")),
        geography_json=geography_json,
        content_hash=content_hash,
        metadata_json=metadata_json,
        verified=True,  # manual entry = verified by definition
    )
    session.add(event)
    session.flush()  # get event.id

    # Insert junctions
    # Companies: gated by LINK_EVENTS_TO_COMPANIES to match existing ingesters.
    # When flag is False, manually-resolved companies are still logged in the
    # result but no rows are written to risk_event_companies.  Phase 5 will
    # flip the flag; at that point re-run with --reload to backfill the links.
    company_link_written = 0
    company_link_skipped = 0
    if feature_flags.LINK_EVENTS_TO_COMPANIES:
        for c in resolved_companies:
            session.add(
                RiskEventCompany(
                    risk_event_id=event.id,
                    company_id=c.id,
                    relevance_score=1.0,
                    match_reason="named_company",
                    review_status="confirmed",
                    review_note="Loaded from manual_risk_events.xlsx",
                )
            )
            company_link_written += 1
    else:
        company_link_skipped = len(resolved_companies)
    for m in resolved_materials:
        session.add(
            RiskEventMaterial(
                risk_event_id=event.id,
                material_id=m.id,
                relevance_score=1.0,
                match_reason="named_material",
            )
        )
    for f in resolved_facs:
        session.add(
            RiskEventFacility(
                risk_event_id=event.id,
                facility_id=f.id,
                relevance_score=1.0,
                match_reason="named_facility",
            )
        )
    for r in resolved_regs:
        session.add(
            RiskEventRegulation(
                risk_event_id=event.id,
                regulation_id=r.id,
                relevance_score=1.0,
                match_reason="named_regulation",
            )
        )

    # Geographies: primary + secondaries
    if primary_country:
        session.add(
            RiskEventGeography(
                risk_event_id=event.id,
                country_code=primary_country,
                geography_context="primary",
                relevance_score=1.0,
            )
        )
    for cc in secondary_countries:
        if cc != primary_country:  # avoid dup on unique constraint
            session.add(
                RiskEventGeography(
                    risk_event_id=event.id,
                    country_code=cc,
                    geography_context="secondary",
                    relevance_score=0.85,
                )
            )

    session.commit()

    # Report exactly what junctions were written vs. skipped
    company_summary = (
        f"co_link={company_link_written}"
        if company_link_written
        else f"co_link=SKIPPED[flag_off, {company_link_skipped} resolved]"
        if company_link_skipped
        else "co_link=0"
    )
    parts = [
        f"event_id={event.id}",
        f"mat={len(resolved_materials)}",
        company_summary,
        f"fac={len(resolved_facs)}",
        f"reg={len(resolved_regs)}",
        f"geo={1 + len(secondary_countries)}",
    ]
    if warnings:
        parts.append("warnings=[" + "; ".join(warnings) + "]")
        result["status"] = "loaded_partial"  # loaded, but some soft refs unresolved
    else:
        result["status"] = "loaded"
    result["message"] = "; ".join(parts)
    result["event_id"] = str(event.id)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def load(
    seed_path: Path,
    dry_run: bool = False,
    only_new: bool = True,
    verbose: bool = False,
) -> dict:
    """Read seed_path, process each row, write status back to spreadsheet."""
    wb = openpyxl.load_workbook(seed_path)
    if "Risk Events" not in wb.sheetnames:
        raise SystemExit(f"Sheet 'Risk Events' not found in {seed_path}")
    ws = wb["Risk Events"]

    # Row 1 = column names, Row 2 = descriptions, Row 3+ = data
    headers = [c.value for c in ws[1]]
    header_idx = {h: i for i, h in enumerate(headers) if h}

    # Resolve status columns
    status_col = (header_idx.get("load_status", -1) + 1) if "load_status" in header_idx else None
    message_col = (header_idx.get("load_message", -1) + 1) if "load_message" in header_idx else None
    event_id_col = (header_idx.get("loaded_event_id", -1) + 1) if "loaded_event_id" in header_idx else None

    counts = {
        "loaded": 0,
        "loaded_partial": 0,  # loaded + some soft refs unresolved
        "skipped": 0,
        "error": 0,
        "dry_ok": 0,
        "blank": 0,
    }

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        for row_idx, raw_row in enumerate(
            ws.iter_rows(min_row=3, values_only=True), start=3
        ):
            row = {h: raw_row[i] for i, h in enumerate(headers) if h}

            if not (row.get("event_type") or row.get("title")):
                counts["blank"] += 1
                continue

            existing_status = (row.get("load_status") or "").strip().lower()
            if only_new and existing_status in ("loaded", "loaded_partial", "skipped"):
                counts["skipped"] += 1
                continue

            try:
                result = _process_row(session, row, dry_run=dry_run)
            except Exception as e:
                session.rollback()
                result = {"status": "error", "message": f"exception:{type(e).__name__}:{str(e)[:200]}", "event_id": ""}

            counts[result["status"]] = counts.get(result["status"], 0) + 1

            if verbose:
                slug = row.get("event_slug") or f"row{row_idx}"
                print(f"  [{result['status']:>8}] {slug}: {result['message']}")

            # Write status back to spreadsheet
            if not dry_run:  # only update spreadsheet for real runs
                if status_col:
                    ws.cell(row=row_idx, column=status_col, value=result["status"])
                if message_col:
                    ws.cell(row=row_idx, column=message_col, value=result["message"])
                if event_id_col:
                    ws.cell(row=row_idx, column=event_id_col, value=result["event_id"])

                # Color the status cell for quick visual scan
                if status_col:
                    color = {
                        "loaded": "C6EFCE",           # green — fully linked
                        "loaded_partial": "D9E1F2",   # blue — loaded but soft refs unresolved
                        "error": "FFC7CE",            # red — row rejected
                        "skipped": "FFEB9C",          # yellow — no-op (already done)
                    }.get(result["status"], "FFFFFF")
                    ws.cell(row=row_idx, column=status_col).fill = PatternFill(
                        "solid", fgColor=color
                    )

    if not dry_run:
        wb.save(seed_path)

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-path", type=Path, required=True,
                        help="Path to manual_risk_events.xlsx")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate everything; do not write to DB or spreadsheet")
    parser.add_argument("--reload", action="store_true",
                        help="Re-process rows even if load_status=loaded "
                             "(idempotency still applies via content_hash)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Per-row status output")
    args = parser.parse_args()

    mode = "DRY-RUN" if args.dry_run else "LOAD"
    print(f"[{mode}] Loading from {args.seed_path}", file=sys.stderr)
    print(
        f"[{mode}] LINK_EVENTS_TO_COMPANIES = "
        f"{feature_flags.LINK_EVENTS_TO_COMPANIES} "
        f"(company junctions {'ENABLED' if feature_flags.LINK_EVENTS_TO_COMPANIES else 'SUPPRESSED'})",
        file=sys.stderr,
    )

    counts = load(
        seed_path=args.seed_path,
        dry_run=args.dry_run,
        only_new=not args.reload,
        verbose=args.verbose,
    )

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Summary ({mode}):", file=sys.stderr)
    for k in ("loaded", "loaded_partial", "dry_ok", "skipped", "error", "blank"):
        if k in counts:
            print(f"  {k:>15}: {counts[k]}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    if not args.dry_run:
        print(f"\nSpreadsheet updated: {args.seed_path}", file=sys.stderr)

    return 1 if counts.get("error", 0) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
