"""Scraper for ev-database.org -> ``CompanyVehicleModel`` + ``VehicleModelChemistry``.

Pipeline (one full run, ~12 min at 1 req/s default rate-limit):

1. ``discover_variants()`` fetches a single cheatsheet page
   (``/cheatsheet/range-electric-car``) and harvests every
   ``<a href="/car/{id}/{slug}">{display name}</a>`` in the markdown table.
   This is the cheapest discovery path: one HTTP request returns ~700 variants,
   no JS-rendered listing page required.

2. ``resolve_brand()`` maps each display name to an existing ``Company`` via a
   built-in brand-prefix map (e.g. ``"Audi" -> "Volkswagen Group"``) and falls
   back to ``Company.canonical_name`` / ``CompanyAlias.alias`` lookups. Variants
   whose brand has no match are logged and skipped — no new ``Company`` rows
   are ever created here.

3. ``parse_variant_detail()`` pulls the raw detail HTML and extracts:
     - model_year_start / model_year_end (``MY24-26`` regex first, then plain
       ``(YYYY-YYYY)`` in title, then "Discontinued (... - ...)", then
       "Available to order since {Month} {Year}")
     - useable battery kWh
     - battery chemistry (Battery panel "Cathode Material" cell — that is the
       label ev-database actually uses; ``"No Data"``/``"n/a"``/``"unknown"``
       are treated as missing. Falls back to a "Battery Chemistry" label if
       ever exposed, and finally to scanning the title for ``LFP|NMC|NCA`` —
       which covers variants like "Tesla Model Y RWD (CATL LFP)").

4. ``chemistry_to_share_rows()`` resolves the raw chemistry text to one or
   more ``BatteryChemistry`` rows. Dual chemistries (``"LFP & NMC"``) split
   evenly. Unknown / missing -> empty list (variant gets a model row but no
   chemistry row, with a warning).

5. ``upsert_variant()`` is idempotent on
   ``(company_id, model_name, model_year_start)``. On re-run, the chemistry
   rows for the model are wiped and re-inserted because ``share_pct`` must
   sum to 1.0 within the active window.

Production volume is intentionally always ``NULL`` — ev-database does not
expose it. Downstream scoring weights variants uniformly when volume is
absent (see ``derive_material_inputs`` in the scoring pipeline).
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Optional

import httpx
import structlog
from bs4 import BeautifulSoup
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.battery_chemistry import BatteryChemistry
from app.models.company import Company, CompanyAlias
from app.models.vehicle import CompanyVehicleModel, VehicleModelChemistry

log = structlog.get_logger(__name__)

BASE_URL = "https://ev-database.org"
DISCOVERY_PATH = "/cheatsheet/range-electric-car"
DEFAULT_USER_AGENT = (
    "battery-data-intelligence-engine/0.1 (research; contact via repo)"
)

# Brands that appear on ev-database but live under a parent Company in our DB.
# Order is irrelevant (we always pick the longest matching prefix). Keys MUST
# match the leading token(s) of the ev-database display name verbatim.
BRAND_TO_CANONICAL: dict[str, str] = {
    # OEMs that already have an exact canonical_name match are not strictly
    # required here, but listing them documents the supported coverage.
    "Tesla": "Tesla",
    "BYD": "BYD",
    "Volkswagen": "Volkswagen Group",
    "Audi": "Volkswagen Group",
    "Porsche": "Volkswagen Group",
    "CUPRA": "Volkswagen Group",
    "SEAT": "Volkswagen Group",
    "Skoda": "Volkswagen Group",
    "Škoda": "Volkswagen Group",
    "BMW": "BMW Group",
    "MINI": "BMW Group",
    "Mercedes-Benz": "Mercedes-Benz Group",
    "Mercedes": "Mercedes-Benz Group",
    "Smart": "Mercedes-Benz Group",
    "Hyundai": "Hyundai Motor Company",
    "Genesis": "Hyundai Motor Company",
    "Kia": "Kia Corporation",
    "Ford": "Ford Motor Company",
    "Cadillac": "General Motors",
    "Chevrolet": "General Motors",
    "GMC": "General Motors",
    "Toyota": "Toyota Motor Corporation",
    "Lexus": "Toyota Motor Corporation",
    "Honda": "Honda Motor Company",
    "Nissan": "Nissan Motor Company",
    "Subaru": "Subaru Corporation",
    "Stellantis": "Stellantis",
    "Fiat": "Stellantis",
    "Abarth": "Stellantis",
    "Alfa Romeo": "Stellantis",
    "Citroën": "Stellantis",
    "Citroen": "Stellantis",
    "DS Automobiles": "Stellantis",
    "Jeep": "Stellantis",
    "Lancia": "Stellantis",
    "Maserati": "Stellantis",
    "Opel": "Stellantis",
    "Peugeot": "Stellantis",
    "Vauxhall": "Stellantis",
    "Volvo": "Volvo Car Group",
    "Polestar": "Polestar Automotive",
    "Lucid": "Lucid Group",
    "Rivian": "Rivian Automotive",
    "Geely": "Geely Auto Group",
    "Zeekr": "Zeekr",
    "Lynk & Co": "Geely Auto Group",
    "Lotus": "Geely Auto Group",
    "Smart #": "Geely Auto Group",  # Smart (post-2022) is a Geely-Mercedes JV
}

# Multi-word brand keys MUST be checked before single-word. Sort once at import.
_BRAND_KEYS_BY_LEN: list[str] = sorted(BRAND_TO_CANONICAL, key=len, reverse=True)

# ev-database chemistry text -> BatteryChemistry.slug. Splits ("&", "/", "+",
# "and") yield multiple equally-shared rows.
#
# Exact-match lookup table; for nickel-ratio subtypes (NMC811, NMC622, ...)
# see _resolve_chem_token below.
CHEMISTRY_SLUG_MAP: dict[str, str] = {
    "lfp": "lfp",
    "nmc": "nmc",
    "nca": "nca",
    "nmca": "nmc",  # NMC + Aluminum doping; treat as nmc family.
    "lmfp": "lfmp",  # LMFP is the same chemistry; our DB uses 'lfmp'
    "lfmp": "lfmp",
    "lto": "lto",
    "sodium-ion": "sodium_ion",
    "na-ion": "sodium_ion",
}

# Matches NMC-family nickel-ratio subtypes (NMC811, NMC622, NMC532, NMC111,
# NMC9.5.5, "NMC 811", etc.). Anything matching this collapses to slug "nmc"
# because the subtype-level chemistry isn't carried in our schema.
_NMC_SUBTYPE_RE = re.compile(r"^nmc[\s\-]?\d+(?:[.\-]?\d+)*$", re.IGNORECASE)
# LFP subtypes ("LFP-Mn", "LFMP", "LMFP" handled separately above).
_LFP_SUBTYPE_RE = re.compile(r"^lfp[\s\-]?\d+(?:[.\-]?\d+)*$", re.IGNORECASE)


def _resolve_chem_token(token: str) -> Optional[str]:
    """Map a single chemistry token to a seeded BatteryChemistry slug.

    Order: exact lookup → NMC subtype → LFP subtype → None.
    """
    key = token.strip().lower()
    if key in CHEMISTRY_SLUG_MAP:
        return CHEMISTRY_SLUG_MAP[key]
    if _NMC_SUBTYPE_RE.match(key):
        return "nmc"
    if _LFP_SUBTYPE_RE.match(key):
        return "lfp"
    return None

_CHEM_SPLIT_RE = re.compile(r"\s*(?:&|/|\+|\band\b)\s*", re.IGNORECASE)
_MY_RANGE_RE = re.compile(r"\(MY(\d{2})(?:-(\d{2}))?\)")
_YEAR_RANGE_RE = re.compile(r"\((\d{4})(?:-(\d{4}))?\)")
_AVAILABLE_SINCE_RE = re.compile(
    r"Available to order since\s+[A-Za-z]+\s+(\d{4})"
)
_DISCONTINUED_RANGE_RE = re.compile(
    r"Discontinued\s*\(\s*[A-Za-z]+\s+(\d{4})\s*-\s*[A-Za-z]+\s+(\d{4})\s*\)"
)
_CAR_LINK_RE = re.compile(r'/car/(\d+)/([A-Za-z0-9\-]+)')
_USEABLE_KWH_RE = re.compile(r"([\d.,]+)\s*kWh\s*\*?\s*Useable Battery", re.IGNORECASE)
# Strings ev-database puts in the Cathode Material cell when chemistry is unknown.
_CHEMISTRY_NULL_TOKENS: frozenset[str] = frozenset(
    {"no data", "n/a", "na", "unknown", "tbd", "-", "—"}
)


@dataclass(frozen=True)
class Variant:
    """A single ev-database variant discovered from the cheatsheet page."""

    display_name: str
    car_id: int
    slug: str

    @property
    def url(self) -> str:
        return f"{BASE_URL}/car/{self.car_id}/{self.slug}"


@dataclass
class ParsedVariant:
    """Result of parsing a single ev-database detail page."""

    model_year_start: Optional[int] = None
    model_year_end: Optional[int] = None
    useable_battery_kwh: Optional[float] = None
    chemistry_raw: Optional[str] = None
    is_discontinued: bool = False


@dataclass
class ChemShareRow:
    chemistry_slug: str
    share_pct: float
    valid_from: date


@dataclass
class RunStats:
    discovered: int = 0
    brand_skipped: int = 0
    already_stored_skipped: int = 0
    fetch_errors: int = 0
    parse_errors: int = 0
    chem_skipped: int = 0
    models_upserted: int = 0
    models_unchanged: int = 0
    chem_rows_upserted: int = 0
    rate_limit_hits: int = 0
    rate_limit_aborted: bool = False
    remaining_after_abort: int = 0
    skipped_brands: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "discovered": self.discovered,
            "brand_skipped": self.brand_skipped,
            "already_stored_skipped": self.already_stored_skipped,
            "fetch_errors": self.fetch_errors,
            "parse_errors": self.parse_errors,
            "chem_skipped": self.chem_skipped,
            "models_upserted": self.models_upserted,
            "models_unchanged": self.models_unchanged,
            "chem_rows_upserted": self.chem_rows_upserted,
            "rate_limit_hits": self.rate_limit_hits,
            "rate_limit_aborted": self.rate_limit_aborted,
            "remaining_after_abort": self.remaining_after_abort,
            "top_skipped_brands": dict(
                sorted(self.skipped_brands.items(), key=lambda x: -x[1])[:10]
            ),
        }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_variants(client: httpx.Client) -> list[Variant]:
    """Fetch the cheatsheet page and harvest every ``/car/{id}/{slug}`` link.

    The cheatsheet is a single static HTML table (~700 rows) that lists every
    active variant. We deduplicate by ``car_id`` because some pages list a
    variant in multiple sections.
    """
    response = client.get(BASE_URL + DISCOVERY_PATH)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")

    seen: dict[int, Variant] = {}
    for anchor in soup.select("a[href^='/car/']"):
        href = anchor.get("href", "")
        match = _CAR_LINK_RE.match(href)
        if not match:
            continue
        car_id = int(match.group(1))
        slug = match.group(2)
        display_name = anchor.get_text(strip=True)
        if not display_name or car_id in seen:
            continue
        seen[car_id] = Variant(
            display_name=display_name, car_id=car_id, slug=slug
        )

    variants = sorted(seen.values(), key=lambda v: v.display_name.lower())
    log.info("ev_database.discovered", count=len(variants))
    return variants


# ---------------------------------------------------------------------------
# Brand resolution
# ---------------------------------------------------------------------------


def _brand_prefix(display_name: str) -> Optional[str]:
    """Longest brand prefix from BRAND_TO_CANONICAL that matches at the start."""
    for key in _BRAND_KEYS_BY_LEN:
        if display_name == key or display_name.startswith(key + " "):
            return key
    return None


def resolve_brand(session: Session, display_name: str) -> Optional[Company]:
    """Map an ev-database display name to an existing ``Company`` row.

    Resolution order:
      1. Built-in ``BRAND_TO_CANONICAL`` map -> exact ``canonical_name`` lookup.
      2. Fallback: greedy ``CompanyAlias.alias`` match on the brand prefix.

    Returns ``None`` (caller should log + skip) when neither path yields a
    company.
    """
    brand = _brand_prefix(display_name)
    if brand is not None:
        canonical = BRAND_TO_CANONICAL[brand]
        company = session.scalar(
            select(Company).where(Company.canonical_name == canonical)
        )
        if company is not None:
            return company
        # Mapping says we should match but the seeded company is missing —
        # treat as a skip rather than crash.
        log.warning(
            "ev_database.brand_mapped_but_company_missing",
            display_name=display_name,
            brand=brand,
            expected_canonical=canonical,
        )
        return None

    # Fallback: try CompanyAlias.alias (case-insensitive, brand-prefix only).
    first_token = display_name.split(" ", 1)[0]
    if not first_token:
        return None
    alias = session.scalar(
        select(CompanyAlias).where(
            CompanyAlias.alias.ilike(first_token)
        )
    )
    if alias is None:
        return None
    company = session.scalar(
        select(Company).where(Company.id == alias.company_id)
    )
    return company


# ---------------------------------------------------------------------------
# Detail-page parsing
# ---------------------------------------------------------------------------


def _parse_year_window(soup: BeautifulSoup) -> tuple[Optional[int], Optional[int]]:
    """Extract (model_year_start, model_year_end) from the detail page."""
    title_text = ""
    title_tag = soup.find("title")
    if title_tag is not None:
        title_text = title_tag.get_text(" ", strip=True)
    h1_tag = soup.find("h1")
    if h1_tag is not None:
        title_text = title_text + " " + h1_tag.get_text(" ", strip=True)

    page_text = soup.get_text(" ", strip=True)

    my_match = _MY_RANGE_RE.search(title_text) or _MY_RANGE_RE.search(page_text)
    if my_match:
        start_yy = int(my_match.group(1))
        end_yy = int(my_match.group(2)) if my_match.group(2) else None
        # ev-database "MY24" means 2024.
        start_year = 2000 + start_yy
        end_year = 2000 + end_yy if end_yy is not None else None
        return start_year, end_year

    # Fallback A: plain "(YYYY-YYYY)" or "(YYYY)" in title — this is the
    # majority case; ev-database titles look like
    # "BMW iX xDrive60 (2025-2026) price and specifications - EV Database".
    yr_match = _YEAR_RANGE_RE.search(title_text)
    if yr_match:
        start_year = int(yr_match.group(1))
        end_year = int(yr_match.group(2)) if yr_match.group(2) else None
        return start_year, end_year

    # Fallback B: discontinued range "Discontinued (Month YYYY - Month YYYY)"
    disc_match = _DISCONTINUED_RANGE_RE.search(page_text)
    if disc_match:
        return int(disc_match.group(1)), int(disc_match.group(2))

    # Fallback C: "Available to order since Month YYYY"
    avail_match = _AVAILABLE_SINCE_RE.search(page_text)
    if avail_match:
        return int(avail_match.group(1)), None

    return None, None


def _parse_useable_kwh(soup: BeautifulSoup) -> Optional[float]:
    text = soup.get_text(" ", strip=True)
    match = _USEABLE_KWH_RE.search(text)
    if not match:
        return None
    raw = match.group(1).replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _clean_chem_value(value: Optional[str]) -> Optional[str]:
    """Normalize a Cathode Material / Battery Chemistry cell value.

    Returns None for null-equivalent strings ("No Data", "n/a", "—", etc.) so
    downstream code records the model with no chemistry rather than upserting
    a junk slug.
    """
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.lower() in _CHEMISTRY_NULL_TOKENS:
        return None
    if len(cleaned) >= 64:
        return None
    return cleaned


def _value_from_label(soup: BeautifulSoup, label_pattern: re.Pattern[str]) -> Optional[str]:
    """Find a panel cell whose label matches ``label_pattern`` and return the
    next-sibling cell's text. Handles both ``<th>/<td>`` and ``<dt>/<dd>``.
    """
    for tag in soup.find_all(string=label_pattern):
        parent = tag.parent
        if parent is None:
            continue
        sibling = parent.find_next_sibling()
        if sibling is not None:
            value = sibling.get_text(" ", strip=True)
            cleaned = _clean_chem_value(value)
            if cleaned is not None:
                return cleaned
            # If the cell is "No Data" we still consume this label and bail —
            # don't accidentally fall through to the title regex.
            if value and value.strip().lower() in _CHEMISTRY_NULL_TOKENS:
                return None
        # Inline "{label}: {value}" pattern within a single tag.
        own_text = parent.get_text(" ", strip=True)
        inline = re.search(
            r"(?:Cathode\s*Material|Battery\s*Chemistry)\s*[:\-]\s*([A-Za-z0-9 &/+\-]+)",
            own_text,
            re.IGNORECASE,
        )
        if inline:
            return _clean_chem_value(inline.group(1))
    return None


def _parse_chemistry_raw(soup: BeautifulSoup, fallback_text: str) -> Optional[str]:
    """Locate the chemistry value on a detail page.

    Strategies, tried in order:
      1. The Battery panel's ``Cathode Material`` cell — this is the label
         ev-database actually uses today (e.g. "NMC", "LFP"). "No Data" and
         similar null tokens are treated as missing rather than as a value.
      2. A legacy/forward-compatible ``Battery Chemistry`` label, in case the
         site exposes one in addition to (or instead of) Cathode Material.
      3. Inline ``LFP|NMC|NCA|LMFP`` token in the page title — covers variants
         like "Tesla Model Y RWD (CATL LFP)" where the chemistry hint is in
         the variant name itself.
    """
    cathode = _value_from_label(soup, re.compile(r"Cathode\s*Material", re.I))
    if cathode is not None:
        return cathode

    legacy = _value_from_label(soup, re.compile(r"Battery\s*Chemistry", re.I))
    if legacy is not None:
        return legacy

    title_match = re.search(
        r"\b(LMFP|LFMP|LFP\s*&\s*NMC|LFP|NMC|NCA|LTO|Sodium-ion|Na-ion)\b",
        fallback_text,
        re.IGNORECASE,
    )
    if title_match:
        return title_match.group(1)
    return None


def parse_variant_detail(html: str) -> ParsedVariant:
    """Parse a single ev-database detail page into a ``ParsedVariant``."""
    soup = BeautifulSoup(html, "lxml")
    title_text = soup.title.get_text() if soup.title else ""
    h1_tag = soup.find("h1")
    if h1_tag is not None:
        title_text = title_text + " " + h1_tag.get_text(" ", strip=True)

    start, end = _parse_year_window(soup)
    return ParsedVariant(
        model_year_start=start,
        model_year_end=end,
        useable_battery_kwh=_parse_useable_kwh(soup),
        chemistry_raw=_parse_chemistry_raw(soup, title_text),
        is_discontinued="Discontinued" in soup.get_text(),
    )


# ---------------------------------------------------------------------------
# Chemistry resolution
# ---------------------------------------------------------------------------


def chemistry_to_share_rows(
    raw: Optional[str], valid_from: date
) -> list[tuple[str, float, date]]:
    """Resolve ev-database chemistry text into ``(slug, share_pct, valid_from)`` tuples.

    Examples
    --------
    "NMC"          -> [("nmc", 1.0, valid_from)]
    "LFP"          -> [("lfp", 1.0, valid_from)]
    "LFP & NMC"    -> [("lfp", 0.5, ...), ("nmc", 0.5, ...)]
    "NMC / LFP"    -> [("nmc", 0.5, ...), ("lfp", 0.5, ...)]
    None / unknown -> []
    """
    if not raw:
        return []
    parts = [p.strip() for p in _CHEM_SPLIT_RE.split(raw) if p and p.strip()]
    resolved: list[str] = []
    for part in parts:
        slug = _resolve_chem_token(part)
        if slug is None:
            log.warning("ev_database.unknown_chemistry_token", token=part, raw=raw)
            continue
        if slug not in resolved:  # dedup; keep order
            resolved.append(slug)
    if not resolved:
        return []
    share = round(1.0 / len(resolved), 6)
    return [(slug, share, valid_from) for slug in resolved]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _existing_chem_signature(
    session: Session, vehicle_model_id: int
) -> set[tuple[int, float, date]]:
    """Read straight from the table; avoids relying on the back-populates
    collection being auto-populated when rows are inserted via the FK column.
    """
    rows = session.execute(
        select(
            VehicleModelChemistry.battery_chemistry_id,
            VehicleModelChemistry.share_pct,
            VehicleModelChemistry.valid_from,
        ).where(VehicleModelChemistry.vehicle_model_id == vehicle_model_id)
    ).all()
    return {(chem_id, round(share, 6), valid_from) for chem_id, share, valid_from in rows}


def upsert_variant(
    session: Session,
    *,
    company_id: uuid.UUID,
    variant: Variant,
    parsed: ParsedVariant,
    chem_rows: list[tuple[str, float, date]],
    chemistry_id_by_slug: dict[str, int],
) -> tuple[CompanyVehicleModel, bool, int]:
    """Idempotent upsert keyed on ``(company_id, model_name, model_year_start)``.

    Returns ``(model, model_changed, chem_rows_written)`` where ``model_changed``
    is True if a new row was inserted OR an existing row's mutable fields were
    updated.
    """
    metadata_json = {
        "source_url": variant.url,
        "ev_database_id": variant.car_id,
        "useable_battery_kwh": parsed.useable_battery_kwh,
        "chemistry_raw": parsed.chemistry_raw,
        "is_discontinued": parsed.is_discontinued,
    }

    existing = session.scalar(
        select(CompanyVehicleModel).where(
            CompanyVehicleModel.company_id == company_id,
            CompanyVehicleModel.model_name == variant.display_name,
            CompanyVehicleModel.model_year_start == parsed.model_year_start,
        )
    )

    if existing is None:
        model = CompanyVehicleModel(
            company_id=company_id,
            model_name=variant.display_name,
            model_year_start=parsed.model_year_start,
            model_year_end=parsed.model_year_end,
            production_volume_units=None,
            production_volume_year=None,
            is_active=not parsed.is_discontinued,
            data_source="ev-database.org",
            metadata_json=metadata_json,
        )
        session.add(model)
        session.flush()
        model_changed = True
    else:
        model = existing
        model_changed = False
        # Update mutable fields if anything actually changed.
        for attr, new_val in (
            ("model_year_end", parsed.model_year_end),
            ("is_active", not parsed.is_discontinued),
            ("data_source", "ev-database.org"),
            ("metadata_json", metadata_json),
        ):
            if getattr(model, attr) != new_val:
                setattr(model, attr, new_val)
                model_changed = True

    # Resolve chemistry rows to FK ids; drop any whose slug isn't seeded.
    desired: list[tuple[int, float, date]] = []
    for slug, share, valid_from in chem_rows:
        chem_id = chemistry_id_by_slug.get(slug)
        if chem_id is None:
            log.warning(
                "ev_database.chemistry_slug_not_seeded",
                slug=slug,
                model=variant.display_name,
            )
            continue
        desired.append((chem_id, round(share, 6), valid_from))

    desired_sig = {row for row in desired}
    current_sig = _existing_chem_signature(session, model.id)

    if desired_sig == current_sig:
        return model, model_changed, 0

    # Replace policy: shares within a model must sum to 1.0 over the active
    # window, so any change forces a full wipe-and-reinsert for that model.
    session.execute(
        delete(VehicleModelChemistry).where(
            VehicleModelChemistry.vehicle_model_id == model.id
        )
    )
    session.flush()
    for chem_id, share, valid_from in desired:
        session.add(
            VehicleModelChemistry(
                vehicle_model_id=model.id,
                battery_chemistry_id=chem_id,
                share_pct=share,
                valid_from=valid_from,
                valid_to=None,
            )
        )
    session.flush()
    return model, True, len(desired)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _load_chemistry_id_by_slug(session: Session) -> dict[str, int]:
    rows = session.execute(select(BatteryChemistry.slug, BatteryChemistry.id)).all()
    return {slug: chem_id for slug, chem_id in rows}


def _already_stored_car_ids(session: Session) -> set[int]:
    """Return the set of ``ev_database_id`` values already persisted from a
    previous run (read out of ``CompanyVehicleModel.metadata_json``).

    Driven by ``data_source == 'ev-database.org'`` so we don't accidentally
    skip rows seeded by other sources that happen to share an integer id.
    Robust against rows whose metadata_json is ``None`` or lacks the key.
    """
    rows = session.execute(
        select(CompanyVehicleModel.metadata_json).where(
            CompanyVehicleModel.data_source == "ev-database.org"
        )
    ).all()
    out: set[int] = set()
    for (md,) in rows:
        if not isinstance(md, dict):
            continue
        car_id = md.get("ev_database_id")
        if isinstance(car_id, int):
            out.add(car_id)
    return out


class RateLimitedError(httpx.HTTPError):
    """Raised after exhausting all 429 retries for a single URL."""


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Best-effort parse of an HTTP ``Retry-After`` header (seconds form only)."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (ValueError, TypeError):
        return None


def _get_with_backoff(
    client: httpx.Client,
    url: str,
    *,
    max_retries: int,
    backoff_base: float,
    backoff_cap: float,
    on_rate_limit: Optional[Callable[[], None]] = None,
) -> httpx.Response:
    """GET ``url`` with exponential backoff on HTTP 429.

    Honors a ``Retry-After`` header when present; otherwise sleeps
    ``min(backoff_cap, backoff_base * 2**attempt)`` seconds. Raises
    ``RateLimitedError`` once retries are exhausted.
    """
    attempt = 0
    while True:
        response = client.get(url)
        if response.status_code != 429:
            response.raise_for_status()
            return response

        if on_rate_limit is not None:
            on_rate_limit()

        if attempt >= max_retries:
            raise RateLimitedError(
                f"429 Too Many Requests for {url} after {max_retries} retries"
            )

        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is None:
            retry_after = min(backoff_cap, backoff_base * (2 ** attempt))
        log.warning(
            "ev_database.rate_limited_backoff",
            url=url,
            attempt=attempt + 1,
            sleep_seconds=retry_after,
        )
        time.sleep(retry_after)
        attempt += 1


def _filter_brands(
    variants: Iterable[Variant], brands: Optional[Iterable[str]]
) -> list[Variant]:
    if not brands:
        return list(variants)
    wanted = {b.strip().lower() for b in brands if b and b.strip()}
    if not wanted:
        return list(variants)
    out: list[Variant] = []
    for v in variants:
        prefix = _brand_prefix(v.display_name) or v.display_name.split(" ", 1)[0]
        if prefix.lower() in wanted:
            out.append(v)
    return out


def run(
    session: Session,
    *,
    limit: Optional[int] = None,
    brands: Optional[list[str]] = None,
    rate_limit_delay: float = 3.0,
    dry_run: bool = False,
    user_agent: str = DEFAULT_USER_AGENT,
    client: Optional[httpx.Client] = None,
    max_retries: int = 4,
    backoff_base: float = 30.0,
    backoff_cap: float = 300.0,
    abort_after_consecutive_429: int = 3,
    skip_existing: bool = True,
) -> dict:
    """End-to-end scrape + upsert.

    Pass an existing ``httpx.Client`` to inject a transport for tests; in
    normal use a default client is constructed with the project User-Agent and
    a 30s timeout.

    Rate limiting / 429 handling
    ----------------------------
    - ev-database aggressively rate-limits *detail* pages (the cheatsheet is
      fine). Default ``rate_limit_delay=3.0s`` (~20 req/min) is conservative;
      bump to 5-10s if you're still seeing 429s.
    - ``rate_limit_delay`` is slept *before every detail fetch* (not after),
      so error paths still apply backpressure.
    - On HTTP 429 the request is retried up to ``max_retries`` times, honoring
      ``Retry-After`` when present and otherwise using exponential backoff:
      ``min(backoff_cap, backoff_base * 2**attempt)`` seconds.
    - After ``abort_after_consecutive_429`` URLs in a row exhaust retries the
      run aborts cleanly with ``rate_limit_aborted=True`` and reports
      ``remaining_after_abort`` so you can see how many variants are left.
      Set to 0 to disable the guard.

    Resumability
    ------------
    With ``skip_existing=True`` (default), variants whose ``ev_database_id``
    is already persisted from a previous run are pruned BEFORE any HTTP fetch
    happens. This makes the scrape resumable across multiple sessions: once
    you hit the rate limit, just rerun later and only the missing variants
    are fetched. Pass ``skip_existing=False`` to refresh every variant.
    """
    own_client = client is None
    if client is None:
        client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=30.0,
            follow_redirects=True,
        )

    stats = RunStats()
    chemistry_id_by_slug = _load_chemistry_id_by_slug(session)
    consecutive_rate_limit_failures = 0

    try:
        variants = discover_variants(client)
        stats.discovered = len(variants)

        variants = _filter_brands(variants, brands)

        if skip_existing:
            stored = _already_stored_car_ids(session)
            if stored:
                before = len(variants)
                variants = [v for v in variants if v.car_id not in stored]
                stats.already_stored_skipped = before - len(variants)
                log.info(
                    "ev_database.skip_existing",
                    already_stored=stats.already_stored_skipped,
                    remaining=len(variants),
                )

        if limit is not None:
            variants = variants[:limit]

        first_fetch = True
        for processed_idx, variant in enumerate(variants):
            company = resolve_brand(session, variant.display_name)
            if company is None:
                stats.brand_skipped += 1
                prefix = variant.display_name.split(" ", 1)[0]
                stats.skipped_brands[prefix] = stats.skipped_brands.get(prefix, 0) + 1
                log.info(
                    "ev_database.brand_skipped",
                    display_name=variant.display_name,
                )
                continue

            # Rate-limit gate: sleep BEFORE every detail fetch (skipped for the
            # very first one). This guarantees a minimum delay even when the
            # previous iteration short-circuited via fetch_error / 429 / parse
            # error, which the old end-of-loop sleep failed to do.
            if not first_fetch and rate_limit_delay > 0:
                time.sleep(rate_limit_delay)
            first_fetch = False

            try:
                response = _get_with_backoff(
                    client,
                    variant.url,
                    max_retries=max_retries,
                    backoff_base=backoff_base,
                    backoff_cap=backoff_cap,
                    on_rate_limit=lambda: setattr(stats, "rate_limit_hits", stats.rate_limit_hits + 1),
                )
            except RateLimitedError as exc:
                stats.fetch_errors += 1
                consecutive_rate_limit_failures += 1
                log.warning(
                    "ev_database.fetch_error",
                    url=variant.url,
                    error=str(exc),
                    consecutive_429_failures=consecutive_rate_limit_failures,
                )
                if (
                    abort_after_consecutive_429 > 0
                    and consecutive_rate_limit_failures >= abort_after_consecutive_429
                ):
                    stats.rate_limit_aborted = True
                    # Variants we never even attempted (current one inclusive
                    # because it failed). Useful for telling the user "X left".
                    stats.remaining_after_abort = len(variants) - processed_idx - 1
                    log.error(
                        "ev_database.rate_limit_abort",
                        consecutive_failures=consecutive_rate_limit_failures,
                        threshold=abort_after_consecutive_429,
                        remaining_after_abort=stats.remaining_after_abort,
                        message=(
                            "Aborting run after consecutive 429 retry "
                            "exhaustions. Wait an hour or two and rerun — "
                            "skip_existing=True will pick up where this left off."
                        ),
                    )
                    break
                continue
            except httpx.HTTPError as exc:
                stats.fetch_errors += 1
                consecutive_rate_limit_failures = 0
                log.warning(
                    "ev_database.fetch_error",
                    url=variant.url,
                    error=str(exc),
                )
                continue

            consecutive_rate_limit_failures = 0

            try:
                parsed = parse_variant_detail(response.text)
            except Exception as exc:  # pragma: no cover — defensive only
                stats.parse_errors += 1
                log.warning(
                    "ev_database.parse_error",
                    url=variant.url,
                    error=str(exc),
                )
                continue

            valid_from_year = parsed.model_year_start or date.today().year
            valid_from = date(valid_from_year, 1, 1)
            chem_rows = chemistry_to_share_rows(parsed.chemistry_raw, valid_from)
            if not chem_rows:
                stats.chem_skipped += 1

            if dry_run:
                log.info(
                    "ev_database.dry_run_variant",
                    company=company.canonical_name,
                    display_name=variant.display_name,
                    model_year_start=parsed.model_year_start,
                    model_year_end=parsed.model_year_end,
                    useable_kwh=parsed.useable_battery_kwh,
                    chemistry_raw=parsed.chemistry_raw,
                    chem_rows=[(s, p) for s, p, _ in chem_rows],
                )
            else:
                _, changed, chem_written = upsert_variant(
                    session,
                    company_id=company.id,
                    variant=variant,
                    parsed=parsed,
                    chem_rows=chem_rows,
                    chemistry_id_by_slug=chemistry_id_by_slug,
                )
                if changed:
                    stats.models_upserted += 1
                else:
                    stats.models_unchanged += 1
                stats.chem_rows_upserted += chem_written

        if not dry_run:
            session.commit()
    finally:
        if own_client:
            client.close()

    log.info("ev_database.run_complete", **stats.as_dict())
    return stats.as_dict()
