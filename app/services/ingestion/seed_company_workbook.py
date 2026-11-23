"""Partner company-seed workbook loader — ENRICHMENT layer (G4d).

Reads the ``Company Seed`` tab of ``company_seed_expanded_v{N}.xlsx`` and
enriches existing ``companies`` rows with the partner-curated identity and
compliance metadata gathered during filing walkthroughs.

Division of responsibility (decided 2026-07-10)
-----------------------------------------------
``seed_companies.py``   IDENTITY layer — owns canonical_name (short form),
                        aliases, parent links, and the existence of the core
                        battery-chain graph (incl. downstream OEMs/cell
                        makers the workbook does not cover). Runs first.
This module             ENRICHMENT layer — resolves each workbook row via
                        canonical OR alias (never renames anything) and
                        updates the fields the workbook is authoritative
                        for: cik, lei, sic, duns, tickers/exchanges,
                        incorporated_country, state-owned / sanctions /
                        UFLPA flags, activity stage FK, notes, verified.

The ``publish`` flag (workbook column, added 2026-07-10)
--------------------------------------------------------
- publish=FALSE (or blank) → row is SKIPPED entirely.
- publish=TRUE + resolves   → enrich.
- publish=TRUE + unresolved → CREATE. Canonical name comes from the
  optional ``short_name`` column when present, else the legal
  ``company_name``; the legal form is registered as an alias either way.
  This is the only creation path — the loader never creates
  publish=FALSE companies "for completeness".

How the gate applies to the CME / SR tabs (decided 2026-07-11):

- ``Company Material Exposure`` rows: gated on the row's company being
  publish=TRUE in the Company Seed tab (exposure data is unreviewed
  walkthrough output — same review boundary as enrichment).
- ``Supply Relationships`` rows: publish gates CREATION only. An SR row
  loads when BOTH endpoints already resolve in the DB (from
  seed_companies.py identity graph, or created earlier in this run
  because publish=TRUE). A relationship between two existing companies
  never creates anything unreviewed, so publish=FALSE does not block it.
  Endpoints that resolve nowhere → row skipped with an issue; flip the
  company to publish=TRUE (or add it to seed_companies.py) and re-run.

Company Material Exposure — hybrid exposure_score (decided 2026-07-11)
----------------------------------------------------------------------
The workbook CME tab carries filing FACTS (production, revenue share,
battery_grade_relevance) but not the 0–1 ``exposure_score`` judgment the
material-concentration pillar consumes. Scores come from:

1. ``curated_seed`` — the (company, material) pair exists in the
   seed_material_exposures.py dict: every curated per-stage entry is
   seeded with its curated score / source_geography / confidence /
   rationale, and the workbook facts attach to the entry whose stage
   matches the row's stage (see stage rules below).
2. ``derived_revenue_share`` — no curated entry, revenue_share_pct
   present: score = clamp(0.25 + 0.75 × revenue_share_pct, 0.25, 0.95),
   data_confidence 0.4.
3. ``default_unscored`` — no curated entry, no revenue share: score 0.5
   (the engine's fallback made explicit), data_confidence 0.3.

Derived/default rows are flagged via ``score_derivation`` and
``verified=False`` — they are the partner-review queue.

Stage default: the optional ``stage`` override column on the CME tab
wins; else the company's ``primary_activity_stage_fk`` (DB); else the
workbook row's ``primary_activity_stage``; activity codes map to the CME
vocabulary (mining|refining|cell|pack|oem) via _ACTIVITY_TO_CME_STAGE.

Field-ownership rules (drift prevention):
- NEVER writes ``canonical_name`` or ``supply_chain_stage`` of existing
  rows, never re-parents a company whose ``parent_company_id`` is set.
- Enrichment fields are only written when the workbook cell is non-empty
  (never nulls-out DB values).
- Alias writes are ADDITIVE only (legal name, ``former_names``,
  ``aliases`` columns; guarded by not-exists).
- ``verified`` is set True when the row carries a ``last_verified_date``
  (the walkthrough stamp). NOTE: the date itself is not persisted — the
  companies table has no last_verified column; add via migration if the
  staleness tracker needs it.
- ``hq_state_region`` / ``hq_city`` are deliberately NOT mapped:
  ``Company.headquarters_region`` means world-region ("Europe") in the
  identity seed, not state/province — semantic mismatch.

Run order:
    bdi-ingest seed-companies              # identity layer first
    bdi-ingest seed-company-workbook <xlsx>  # this module
    bdi-ingest seed-facilities-partner <xlsx>
    python scripts/load_manual_risk_events.py <xlsx>
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import (
    Company,
    CompanyAlias,
    CompanyMaterialExposure,
    CompanySupplyRelationship,
)
from app.models.supply import Material

log = structlog.get_logger(__name__)

_ISO2_RE = re.compile(r"^[A-Z]{2}$")

# CME supply_chain_stage vocabulary (matches seed_material_exposures.py and
# the CompanyMaterialExposure docstring — NOT the supply_chain_stages
# activity axis, and NOT the facilities product-form axis).
_CME_STAGES = {"mining", "refining", "cell", "pack", "oem"}

# Activity-axis stage_code (supply_chain_stages / workbook
# primary_activity_stage) → CME stage. Codes with no single CME home
# (integrated, trading, financial, fabrication) are absent on purpose —
# rows for those companies need the explicit ``stage`` override column.
_ACTIVITY_TO_CME_STAGE = {
    "mining": "mining",
    "beneficiation": "mining",
    "refining": "refining",
    "precursor_production": "refining",
    "cathode_active_material": "refining",
    "anode_active_material": "refining",
    "electrolyte_production": "refining",
    "recycling": "refining",
    "cell_making": "cell",
    "separator_production": "cell",
    "module_assembly": "pack",
    "pack_assembly": "pack",
    "vehicle_assembly": "oem",
}

_SR_RELATIONSHIP_TYPES = {"direct", "indirect", "estimated", "framework"}
_SR_AGREEMENT_TYPES = {
    "offtake", "supply_agreement", "joint_development", "equity_offtake",
    "framework", "spot", "jv", "unknown",
}
# data_confidence assigned to NEW relationships by evidential type.
# Existing rows keep their curated confidence (only filled when NULL).
_SR_CONFIDENCE_BY_TYPE = {
    "direct": 0.9, "framework": 0.6, "indirect": 0.5, "estimated": 0.4,
}

# Workbook header → handler. Order irrelevant; lookup is by header name so
# extra workbook columns are ignored and column order is free to change.
_SIMPLE_STR_FIELDS = {
    "legal_name": "legal_name",
    "lei": "lei",
    "duns": "duns_number",
    "uflpa_status": "uflpa_status",
}
_BOOL_FIELDS = {
    "is_state_owned_or_influenced": "is_state_owned_or_influenced",
    "operates_as_trader": "operates_as_trader",
    "has_facilities": "has_facilities",
    "is_sanctioned": "is_sanctioned",
    "has_uflpa_designation": "has_uflpa_designation",
}


@dataclass
class RowIssue:
    row_num: int
    company: Optional[str]
    message: str


@dataclass
class WorkbookReport:
    rows_seen: int = 0
    rows_unpublished: int = 0
    rows_enriched: int = 0
    rows_created: int = 0
    rows_unchanged: int = 0
    aliases_added: int = 0
    parents_linked: int = 0
    fields_written: int = 0
    # ── Company Material Exposure tab ─────────────────────────────
    cme_rows_seen: int = 0
    cme_rows_unpublished: int = 0
    cme_rows_skipped: int = 0
    cme_exposures_curated: int = 0
    cme_exposures_derived: int = 0
    cme_exposures_default: int = 0
    cme_exposures_updated: int = 0
    # ── Supply Relationships tab ──────────────────────────────────
    sr_rows_seen: int = 0
    sr_rows_created: int = 0
    sr_rows_updated: int = 0
    sr_rows_skipped: int = 0
    issues: list[RowIssue] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "issues"}
        d["issues"] = [f"row {i.row_num} [{i.company}]: {i.message}" for i in self.issues]
        return d


def _coerce_bool(v: Any) -> Optional[bool]:
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "y"):
        return True
    if s in ("false", "0", "no", "n"):
        return False
    return None


def _clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "None", "NULL", "null"):
        return None
    return s


def _split_semicolon_or_comma(v: Any) -> list[str]:
    """For simple token lists (exchanges, jurisdictions) — NOT names."""
    s = _clean(v)
    if not s:
        return []
    sep = ";" if ";" in s else ","
    return [t.strip() for t in s.split(sep) if t.strip()]


def _resolve_company(session: Session, name: str) -> Optional[Company]:
    """Canonical first, then alias — same contract as the facility loader."""
    company = session.scalar(
        select(Company).where(Company.canonical_name == name)
    )
    if company is None:
        company = session.scalar(
            select(Company)
            .join(CompanyAlias, CompanyAlias.company_id == Company.id)
            .where(CompanyAlias.alias == name)
        )
    return company


def _add_alias(session: Session, company: Company, alias: str,
               alias_type: str, report: WorkbookReport) -> None:
    alias = alias.strip()
    if not alias or alias == company.canonical_name:
        return
    exists = session.scalar(
        select(CompanyAlias).where(CompanyAlias.alias == alias)
    )
    if exists is None:
        session.add(CompanyAlias(
            company_id=company.id, alias=alias, alias_type=alias_type,
        ))
        report.aliases_added += 1


def _coerce_float(v: Any) -> Optional[float]:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _coerce_year(v: Any) -> Optional[int]:
    f = _coerce_float(v)
    if f is None:
        return None
    y = int(f)
    return y if 1900 <= y <= 2100 else None


def _coerce_date(v: Any):
    """Workbook cells arrive as datetime (openpyxl) or ISO string."""
    from datetime import date, datetime
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _resolve_material(session: Session, name: Optional[str]) -> Optional[Material]:
    name = _clean(name)
    if not name:
        return None
    return session.scalar(
        select(Material).where(Material.canonical_name == name)
    )


def _curated_exposure_lookup() -> dict[tuple[str, str], list[dict]]:
    """(canonical company, canonical material) → curated seed entries.

    Keys use the engine's SHORT canonical names ('Vale', 'Glencore') — the
    same names workbook rows resolve to via aliases — so lookup happens
    AFTER company resolution, never on the workbook's legal-name strings.
    """
    from app.services.ingestion.seed_material_exposures import _EXPOSURES
    lut: dict[tuple[str, str], list[dict]] = {}
    for e in _EXPOSURES:
        lut.setdefault((e["company"], e["material"]), []).append(e)
    return lut


def _map_activity_stage(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    return _ACTIVITY_TO_CME_STAGE.get(code.strip().lower())


def _slugify(name: str) -> str:
    """URL-safe slug from a canonical name (matches migration 054 backfill)."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:160]


def _find_header_row(ws, first_header: str = "company_name") -> Optional[int]:
    for r in range(1, min(ws.max_row, 10) + 1):
        v = ws.cell(row=r, column=1).value
        if isinstance(v, str) and v.strip().lower() == first_header:
            return r
    return None


def _looks_like_descriptor(name: str) -> bool:
    """Row 2 of the partner template holds field descriptions, not data."""
    return len(name) > 120 or "CANONICAL" in name.upper()


def _iter_data_rows(ws, header_row: int, col: dict[str, int]):
    """Yield (rownum, {header: value}) for data rows, skipping descriptors."""
    for rownum, cells in enumerate(
        ws.iter_rows(min_row=header_row + 1, values_only=True),
        start=header_row + 1,
    ):
        row = {h: cells[i] if i < len(cells) else None for h, i in col.items()}
        yield rownum, row


def _load_cme_tab(
    session: Session,
    wb,
    sheet: str,
    publish_by_name: dict[str, bool],
    stage_by_name: dict[str, Optional[str]],
    report: WorkbookReport,
) -> None:
    """Load the ``Company Material Exposure`` tab. See module docstring."""
    if sheet not in wb.sheetnames:
        log.info("seed_company_workbook.cme_sheet_absent", sheet=sheet)
        return
    ws = wb[sheet]
    header_row = _find_header_row(ws)
    if header_row is None:
        report.issues.append(RowIssue(0, None, f"'{sheet}': no header row"))
        return
    headers = [
        (c.value.strip() if isinstance(c.value, str) else c.value)
        for c in ws[header_row]
    ]
    col = {h: i for i, h in enumerate(headers) if h}
    curated = _curated_exposure_lookup()

    for rownum, row in _iter_data_rows(ws, header_row, col):
        name = _clean(row.get("company_name"))
        if not name:
            continue
        if _looks_like_descriptor(name):
            continue
        report.cme_rows_seen += 1

        # ── publish gate (CME rows share the company review boundary) ─
        if name not in publish_by_name:
            report.cme_rows_skipped += 1
            report.issues.append(RowIssue(
                rownum, name, "CME row's company not in Company Seed tab — "
                "publish gate cannot be evaluated; row skipped"))
            continue
        if not publish_by_name[name]:
            report.cme_rows_unpublished += 1
            continue

        company = _resolve_company(session, name)
        if company is None:
            report.cme_rows_skipped += 1
            report.issues.append(RowIssue(
                rownum, name, "CME company unresolved in DB — row skipped"))
            continue

        mat_name = _clean(row.get("material_canonical"))
        material = _resolve_material(session, mat_name)
        if material is None:
            report.cme_rows_skipped += 1
            report.issues.append(RowIssue(
                rownum, name,
                f"material '{mat_name}' not in materials table — row skipped"))
            continue

        # ── stage: override column → DB activity FK → workbook stage ──
        stage = (_clean(row.get("stage")) or "").lower() or None
        if stage and stage not in _CME_STAGES:
            report.issues.append(RowIssue(
                rownum, name,
                f"stage override '{stage}' not in {sorted(_CME_STAGES)} — "
                "falling back to company primary stage"))
            stage = None
        if stage is None:
            stage = _map_activity_stage(company.primary_activity_stage_fk)
        if stage is None:
            stage = _map_activity_stage(stage_by_name.get(name))
        if stage is None:
            report.cme_rows_skipped += 1
            report.issues.append(RowIssue(
                rownum, name,
                "no CME stage derivable (override blank, activity stage "
                "unmapped) — set the 'stage' column for this row"))
            continue

        facts = {
            "production_tonnage": _coerce_float(row.get("production_tonnage")),
            "production_unit": _clean(row.get("production_unit")),
            "production_year": _coerce_year(row.get("production_year")),
            "revenue_share_pct": _coerce_float(row.get("revenue_share_pct")),
            "revenue_year": _coerce_year(row.get("revenue_year")),
            "battery_grade_relevance": _coerce_float(
                row.get("battery_grade_relevance")),
            "source_url": _clean(row.get("source_url")),
        }
        verified_date = _coerce_date(row.get("last_verified_date"))
        evidence_parts = [
            p for p in (
                _clean(row.get("customer_end_use_notes")),
                _clean(row.get("notes")),
                _clean(row.get("source")),
            ) if p
        ]
        evidence = "\n".join(evidence_parts) or None

        def _upsert(stage_: str, score: float, derivation: str,
                    confidence: Optional[float], geography: Optional[str],
                    rationale: Optional[str], attach_facts: bool,
                    verified: bool) -> None:
            existing = session.scalar(
                select(CompanyMaterialExposure).where(
                    CompanyMaterialExposure.company_id == company.id,
                    CompanyMaterialExposure.material_id == material.id,
                    CompanyMaterialExposure.supply_chain_stage == stage_,
                )
            )
            if existing is None:
                existing = CompanyMaterialExposure(
                    company_id=company.id,
                    material_id=material.id,
                    supply_chain_stage=stage_,
                    exposure_score=score,
                    score_derivation=derivation,
                    data_confidence=confidence,
                    source_geography=geography,
                    rationale=rationale,
                    verified=verified,
                )
                session.add(existing)
                if derivation == "curated_seed":
                    report.cme_exposures_curated += 1
                elif derivation == "derived_revenue_share":
                    report.cme_exposures_derived += 1
                else:
                    report.cme_exposures_default += 1
            else:
                # Never downgrade a curated score to a derived one.
                if not (existing.score_derivation == "curated_seed"
                        and derivation != "curated_seed"):
                    existing.exposure_score = score
                    existing.score_derivation = derivation
                    if confidence is not None:
                        existing.data_confidence = confidence
                    if geography:
                        existing.source_geography = geography
                if rationale:
                    existing.rationale = rationale
                if verified:
                    existing.verified = True
                report.cme_exposures_updated += 1
            if attach_facts:
                for attr, val in facts.items():
                    if val is not None:
                        setattr(existing, attr, val)
                if verified_date:
                    existing.as_of_date = verified_date

        entries = curated.get(
            (company.canonical_name, material.canonical_name))
        if entries:
            fact_stage = stage if any(
                e["stage"] == stage for e in entries
            ) else entries[0]["stage"]
            if fact_stage != stage:
                report.issues.append(RowIssue(
                    rownum, name,
                    f"workbook stage '{stage}' has no curated entry for "
                    f"{material.canonical_name}; facts attached to curated "
                    f"'{fact_stage}' row instead (review)"))
            for e in entries:
                _upsert(
                    stage_=e["stage"],
                    score=e["exposure_score"],
                    derivation="curated_seed",
                    confidence=e.get("data_confidence"),
                    geography=e.get("source_geography"),
                    rationale="\n".join(p for p in (
                        e.get("rationale"),
                        f"[workbook] {evidence}" if evidence else None,
                    ) if p) or None,
                    attach_facts=(e["stage"] == fact_stage),
                    verified=bool(verified_date) and e["stage"] == fact_stage,
                )
        else:
            rev = facts["revenue_share_pct"]
            if rev is not None:
                score = min(0.95, max(0.25, 0.25 + 0.75 * rev))
                derivation, confidence = "derived_revenue_share", 0.4
            else:
                score, derivation, confidence = 0.5, "default_unscored", 0.3
            _upsert(
                stage_=stage,
                score=round(score, 3),
                derivation=derivation,
                confidence=confidence,
                geography=None,
                rationale="\n".join(p for p in (
                    f"DERIVED ({derivation}) — pending partner review.",
                    evidence,
                ) if p),
                attach_facts=True,
                verified=False,  # derived rows are the review queue
            )


def _load_sr_tab(
    session: Session,
    wb,
    sheet: str,
    report: WorkbookReport,
) -> None:
    """Load the ``Supply Relationships`` tab. Publish gates CREATION only:
    rows load when both endpoints already resolve in the DB; the loader
    never creates a company from this tab."""
    if sheet not in wb.sheetnames:
        log.info("seed_company_workbook.sr_sheet_absent", sheet=sheet)
        return
    ws = wb[sheet]
    header_row = _find_header_row(ws, first_header="buyer_company_name")
    if header_row is None:
        report.issues.append(RowIssue(0, None, f"'{sheet}': no header row"))
        return
    headers = [
        (c.value.strip() if isinstance(c.value, str) else c.value)
        for c in ws[header_row]
    ]
    col = {h: i for i, h in enumerate(headers) if h}

    for rownum, row in _iter_data_rows(ws, header_row, col):
        buyer_name = _clean(row.get("buyer_company_name"))
        if not buyer_name:
            continue
        if _looks_like_descriptor(buyer_name):
            continue
        report.sr_rows_seen += 1

        supplier_name = _clean(row.get("supplier_company_name"))
        buyer = _resolve_company(session, buyer_name)
        supplier = (
            _resolve_company(session, supplier_name) if supplier_name else None
        )
        missing = [
            n for n, c in ((buyer_name, buyer), (supplier_name, supplier))
            if c is None
        ]
        if missing:
            report.sr_rows_skipped += 1
            report.issues.append(RowIssue(
                rownum, buyer_name,
                f"SR endpoint(s) unresolved: {missing} — row skipped "
                "(flip publish=TRUE or add to seed_companies.py, re-run)"))
            continue
        if buyer.id == supplier.id:
            report.sr_rows_skipped += 1
            report.issues.append(RowIssue(
                rownum, buyer_name, "buyer == supplier — row skipped"))
            continue

        mat_name = _clean(row.get("material_canonical"))
        material = _resolve_material(session, mat_name)
        if material is None:
            report.sr_rows_skipped += 1
            report.issues.append(RowIssue(
                rownum, buyer_name,
                f"material '{mat_name}' not in materials table — row skipped"))
            continue

        rel_type = (_clean(row.get("relationship_type")) or "").lower()
        if rel_type not in _SR_RELATIONSHIP_TYPES:
            report.issues.append(RowIssue(
                rownum, buyer_name,
                f"relationship_type '{rel_type}' invalid — defaulted to "
                "'estimated'"))
            rel_type = "estimated"
        agr_type = (_clean(row.get("agreement_type")) or "").lower() or None
        if agr_type and agr_type not in _SR_AGREEMENT_TYPES:
            report.issues.append(RowIssue(
                rownum, buyer_name,
                f"agreement_type '{agr_type}' invalid — stored as 'unknown'"))
            agr_type = "unknown"

        vol = _coerce_float(row.get("volume_share_pct"))
        if vol is not None and not (0.0 <= vol <= 1.0):
            report.issues.append(RowIssue(
                rownum, buyer_name,
                f"volume_share_pct {vol} outside 0-1 — dropped"))
            vol = None

        detail = {
            "agreement_type": agr_type,
            "contract_term_years": _coerce_float(
                row.get("contract_term_years")),
            "announced_date": _coerce_date(row.get("announced_date")),
            "valid_from": _coerce_date(row.get("effective_from")),
            "valid_to": _coerce_date(row.get("effective_to")),
            "source_url": _clean(row.get("source_url")),
            "notes": _clean(row.get("notes")),
        }

        existing = session.scalar(
            select(CompanySupplyRelationship).where(
                CompanySupplyRelationship.buyer_id == buyer.id,
                CompanySupplyRelationship.supplier_id == supplier.id,
                CompanySupplyRelationship.material_id == material.id,
            )
        )
        if existing is None:
            session.add(CompanySupplyRelationship(
                buyer_id=buyer.id,
                supplier_id=supplier.id,
                material_id=material.id,
                relationship_type=rel_type,
                data_confidence=_SR_CONFIDENCE_BY_TYPE.get(rel_type),
                volume_share_pct=vol,
                **detail,
            ))
            report.sr_rows_created += 1
        else:
            # Workbook is the fresher source for agreement detail; curated
            # confidence (seed script) is kept unless NULL.
            existing.relationship_type = rel_type
            if vol is not None:
                existing.volume_share_pct = vol
            if existing.data_confidence is None:
                existing.data_confidence = _SR_CONFIDENCE_BY_TYPE.get(rel_type)
            for attr, val in detail.items():
                if val is not None:
                    setattr(existing, attr, val)
            report.sr_rows_updated += 1


def seed_company_workbook(
    session: Session,
    xlsx_path: str,
    *,
    sheet: str = "Company Seed",
    cme_sheet: str = "Company Material Exposure",
    sr_sheet: str = "Supply Relationships",
    load_exposures: bool = True,
    load_relationships: bool = True,
    dry_run: bool = False,
) -> WorkbookReport:
    report = WorkbookReport()
    wb = load_workbook(xlsx_path, data_only=True, read_only=True)
    if sheet not in wb.sheetnames:
        raise ValueError(f"sheet '{sheet}' not found in {xlsx_path}")
    ws = wb[sheet]

    header_row = _find_header_row(ws)
    if header_row is None:
        raise ValueError("could not locate header row ('company_name' in column A)")
    headers = [
        (c.value.strip() if isinstance(c.value, str) else c.value)
        for c in ws[header_row]
    ]
    col = {h: i for i, h in enumerate(headers) if h}

    from app.models.supply_chain_context import SupplyChainStage as StageRow
    valid_stage_codes = set(
        session.scalars(select(StageRow.stage_code)).all()
    )

    if "publish" not in col:
        raise ValueError(
            "workbook has no 'publish' column — refusing to bulk-load; "
            "add the flag (company_seed_expanded_v32+) first"
        )

    # Collected for the CME / SR passes: workbook company_name → publish
    # flag and → primary_activity_stage (fallback when the DB FK is NULL).
    publish_by_name: dict[str, bool] = {}
    stage_by_name: dict[str, Optional[str]] = {}

    for rownum, cells in enumerate(
        ws.iter_rows(min_row=header_row + 1, values_only=True),
        start=header_row + 1,
    ):
        row = {h: cells[i] if i < len(cells) else None for h, i in col.items()}
        name = _clean(row.get("company_name"))
        if not name:
            continue
        if _looks_like_descriptor(name):
            continue
        report.rows_seen += 1

        publish = _coerce_bool(row.get("publish"))
        publish_by_name[name] = bool(publish)
        stage_by_name[name] = _clean(row.get("primary_activity_stage"))
        if not publish:
            report.rows_unpublished += 1
            continue

        company = _resolve_company(session, name)
        created = False
        if company is None:
            canonical = _clean(row.get("short_name")) or name
            company = Company(
                canonical_name=canonical,
                slug=_slugify(canonical),
                is_published=True,  # only publish=TRUE rows reach creation
                legal_name=_clean(row.get("legal_name")) or name,
                headquarters_country=(
                    _clean(row.get("hq_country"))
                    if _clean(row.get("hq_country"))
                    and _ISO2_RE.match(_clean(row.get("hq_country")))
                    else None
                ),
                data_source="partner_company_seed",
                verified=bool(_clean(row.get("last_verified_date"))),
            )
            session.add(company)
            session.flush()
            created = True
            report.rows_created += 1
            if canonical != name:
                _add_alias(session, company, name, "legal_name", report)

        wrote = 0

        # ── public-visibility gate (migration 054) ───────────────────
        # We are inside the publish=TRUE gate here, so the row is
        # partner-reviewed: mark it publicly visible.  One-directional —
        # the loader never sets is_published back to False (unpublishing
        # is a deliberate admin action, not a workbook side effect).
        if not company.is_published:
            company.is_published = True
            wrote += 1
        if not company.slug:
            company.slug = _slugify(company.canonical_name)
            wrote += 1

        # ── simple string fields ────────────────────────────────────
        for wcol, attr in _SIMPLE_STR_FIELDS.items():
            v = _clean(row.get(wcol))
            if v and getattr(company, attr) != v:
                setattr(company, attr, v)
                wrote += 1

        # cik: normalise float artifacts ("831259.0") and cap at 10 chars
        cik = _clean(row.get("cik"))
        if cik:
            cik = cik.split(".")[0].strip()
            if cik.isdigit() and company.cik != cik:
                company.cik = cik
                wrote += 1

        sic = _clean(row.get("sic_code"))
        if sic:
            sic = sic.split(".")[0].strip()[:4]
            if company.sic != sic:
                company.sic = sic
                wrote += 1

        ticker = _clean(row.get("primary_ticker"))
        if ticker and company.public_ticker != ticker:
            company.public_ticker = ticker
            wrote += 1
        ownership_type = (_clean(row.get("ownership_type")) or "").lower()
        if (ticker or ownership_type == "public") and not company.is_public:
            company.is_public = True
            wrote += 1

        exchanges = _split_semicolon_or_comma(row.get("exchanges"))
        if exchanges and company.exchanges != exchanges:
            company.exchanges = exchanges
            wrote += 1

        inc = _clean(row.get("incorporated_country"))
        if inc:
            if _ISO2_RE.match(inc):
                if company.incorporated_country != inc:
                    company.incorporated_country = inc
                    wrote += 1
            else:
                report.issues.append(RowIssue(
                    rownum, name, f"incorporated_country '{inc}' not ISO2 — skipped"))
        hq = _clean(row.get("hq_country"))
        if hq and _ISO2_RE.match(hq) and company.headquarters_country != hq:
            company.headquarters_country = hq
            wrote += 1

        # ── booleans (only set True/False when the cell is explicit) ─
        for wcol, attr in _BOOL_FIELDS.items():
            b = _coerce_bool(row.get(wcol))
            if b is not None and getattr(company, attr) != b:
                setattr(company, attr, b)
                wrote += 1

        juris = _split_semicolon_or_comma(row.get("sanctioning_jurisdictions"))
        if juris and company.sanctioning_jurisdictions != juris:
            company.sanctioning_jurisdictions = juris
            wrote += 1

        stage = _clean(row.get("primary_activity_stage"))
        if stage and company.primary_activity_stage_fk != stage:
            # primary_activity_stage_fk is a REAL FK to
            # supply_chain_stages.stage_code — writing an unknown value
            # (workbook has e.g. 'financial', 'fabrication') violates it.
            if stage in valid_stage_codes:
                company.primary_activity_stage_fk = stage
                wrote += 1
            else:
                report.issues.append(RowIssue(
                    rownum, name,
                    f"primary_activity_stage '{stage}' not a stage_code in "
                    f"supply_chain_stages — skipped (fix workbook or add stage)",
                ))

        # ── verified stamp ───────────────────────────────────────────
        if _clean(row.get("last_verified_date")) and not company.verified:
            company.verified = True
            wrote += 1

        # ── notes: workbook wins; prior note preserved as suffix ─────
        notes = _clean(row.get("notes"))
        if notes and company.notes != notes:
            prior = (company.notes or "").strip()
            if prior and prior not in notes:
                notes = f"{notes}\n[prior note: {prior}]"
            company.notes = notes
            wrote += 1

        # ── public_intro (057): partner-authored public profile copy ──
        # Optional column; loaded verbatim when present, workbook wins on
        # re-run (single authored source — no prior-copy suffixing).  This
        # is the ONLY path that writes public-facing free text; ``notes``
        # stays internal and is never exposed by the public API.
        public_intro = _clean(row.get("public_intro"))
        if public_intro and company.public_intro != public_intro:
            company.public_intro = public_intro
            wrote += 1

        # ── additive aliases ─────────────────────────────────────────
        _add_alias(session, company, name, "legal_name", report)
        for a in _split_semicolon_or_comma(row.get("aliases")):
            _add_alias(session, company, a, "aka", report)
        for a in _split_semicolon_or_comma(row.get("former_names")):
            _add_alias(session, company, a, "former_name", report)

        # ── parent link: only when currently NULL ────────────────────
        pcn = _clean(row.get("parent_company_name"))
        if pcn and company.parent_company_id is None:
            parent = _resolve_company(session, pcn)
            if parent is not None and parent.id != company.id:
                company.parent_company_id = parent.id
                report.parents_linked += 1
                wrote += 1
            elif parent is None:
                report.issues.append(RowIssue(
                    rownum, name, f"parent '{pcn}' unresolved — link skipped"))

        report.fields_written += wrote
        if created:
            pass  # already counted
        elif wrote:
            report.rows_enriched += 1
        else:
            report.rows_unchanged += 1

    # ── CME / SR tabs (companies flushed so this-run creations resolve) ─
    session.flush()
    if load_exposures:
        _load_cme_tab(session, wb, cme_sheet,
                      publish_by_name, stage_by_name, report)
    if load_relationships:
        _load_sr_tab(session, wb, sr_sheet, report)

    if dry_run:
        session.rollback()
        log.info("seed_company_workbook.dry_run", **report.as_dict())
    else:
        session.commit()
        log.info("seed_company_workbook.done", **report.as_dict())
    return report


__all__ = ["seed_company_workbook", "WorkbookReport"]
