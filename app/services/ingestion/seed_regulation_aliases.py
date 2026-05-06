"""Seed the ``regulation_aliases`` reference table.

Single source of truth for "what does external source X call canonical
regulation Y?".  Mirrors ``seed_material_source_aliases.py`` for materials.

Replaces the in-ingester regulation manifests that previously lived in
``eurlex.py`` (BATTERY_REGULATIONS list) and the auto-create branch of
``pipeline.py`` (which created arbitrary Regulation rows from any caller-
supplied regulation_key).

After this seed runs, ingesters MUST resolve external IDs through
``RegulationAliasResolver`` before attaching events:

    eurlex.py            → CELEX  (e.g. "32023R1542")  → EU_BATTERY_REG_2023
    federal_register.py  → FR doc number              → US regulation_key
    iea_policy_tracker   → IEA policy ID              → regulation_key (future)
    GTA / OpenSanctions  → case ID                    → regulation_key (future)

Format
------
Each row is one of:

    ("source_system", "source_key", "regulation_key")
        → alias resolves to a tracked regulation.

    ("source_system", "source_key", None, "skip_reason")
        → explicit "we considered this and chose not to ingest" decision.
          Useful when an ingester surfaces an external ID for a regulation
          that isn't battery-supply-chain relevant — preserves the audit
          trail of considered-and-rejected.

When adding a new regulation
----------------------------
1. Add the regulation to ``seed_regulations.py:_REGULATIONS``.
2. Add an alias row here for each external ID system that publishes it
   (CELEX for EU, Federal Register doc + Public Law citation for US, etc.).
3. Re-run ``seed-regulations`` then ``seed-regulation-aliases``.

Run ordering on a fresh DB
--------------------------
    alembic upgrade head
    seed-companies                   ← (companies must exist for exposures)
    seed-materials                   ← (materials must exist for scopes)
    seed-regulations                 ← writes ``regulations`` rows
    seed-regulation-aliases          ← this file (FK-references regulation rows)
"""

from __future__ import annotations

from typing import Optional, Union

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.regulatory import Regulation, RegulationSourceAlias

log = structlog.get_logger(__name__)


# Two forms (see module docstring):
#   3-tuple: alias resolves to canonical regulation_key
#   4-tuple with None canonical: explicit skip with reason
_AliasRow = Union[
    tuple[str, str, str],
    tuple[str, str, None, str],
]

# ---------------------------------------------------------------------------
# Source: EUR-Lex CELEX numbers (eurlex_celex)
#
# CELEX is the EU's stable document identifier — see
# https://eur-lex.europa.eu/content/help/faq/intro.html for format details.
# Verbatim CELEX (case insensitive, normalised on lookup).
# ---------------------------------------------------------------------------

_EURLEX_CELEX: list[_AliasRow] = [
    ("eurlex_celex", "32023R1542", "EU_BATTERY_REG_2023"),
    ("eurlex_celex", "32024R1252", "CRMA_2024"),
    ("eurlex_celex", "32023R0956", "EU_CBAM"),
    ("eurlex_celex", "32024L1760", "EU_CSDDD"),
    ("eurlex_celex", "32017R0821", "EU_CONFLICT_MINERALS"),
    ("eurlex_celex", "32006R1907", "EU_REACH_COBALT"),

    # Explicit skips — partner-curated audit trail.  Add CELEX numbers
    # here when an EUR-Lex pull surfaces an EU regulation that surfaces
    # in our query (e.g. a co-published agricultural directive bundled
    # with a battery-related publication) but is out of scope.
    # No skip rows yet — populate as ingest discovers borderline cases.
]

# ---------------------------------------------------------------------------
# Source: Federal Register document numbers (federal_register)
#
# US Federal Register publishes per-document IDs (e.g. "2024-12345") that
# are stable post-publication.  Multiple FR documents can map to the same
# canonical regulation when one is the "rule" and another is a
# clarifying notice.
# ---------------------------------------------------------------------------

_FEDERAL_REGISTER: list[_AliasRow] = [
    # Pre-seeded regulations defined in seed_regulations.py.  Add FR doc
    # numbers as ingest pulls them — leaving the list short for now keeps
    # the seed honest about what we have actually source-verified.
    # Pattern when adding:
    #   ("federal_register", "2024-12345", "UFLPA")
]

# ---------------------------------------------------------------------------
# Source: US Code / Public Law citations (us_code)
#
# Stable across time once the law is enacted.  Use the Public Law number
# (e.g. "PL_117-78" for UFLPA) or a US Code section reference.
# ---------------------------------------------------------------------------

_US_CODE: list[_AliasRow] = [
    ("us_code", "PL_117-78",   "UFLPA"),         # Public Law 117-78
    ("us_code", "PL_117-169",  "IRA_DOMESTIC"),  # Public Law 117-169 (Inflation Reduction Act)
]


# Aggregate of all alias rows.  Order of definition above doesn't matter
# at write time but matters for readability when editing this file.
_ALIASES: list[_AliasRow] = [
    *_EURLEX_CELEX,
    *_FEDERAL_REGISTER,
    *_US_CODE,
]


# ---------------------------------------------------------------------------
# Seed function — UPSERT on (source_system, lower(btrim(source_key)))
# ---------------------------------------------------------------------------

def seed_regulation_aliases(
    session: Session, *, force_update: bool = False
) -> dict[str, int]:
    """Upsert all regulation alias rows.  Idempotent.

    Behavior:
      - INSERT when (source_system, normalised source_key) is new.
      - SKIP when the row exists and force_update=False.
      - UPDATE the regulation_id / is_skipped / skip_reason / notes when
        force_update=True.

    ``force_update`` is the right flag to pass after partner edits to the
    alias lists in this module.

    Returns counts {"inserted", "updated", "skipped_existing",
    "skipped_unknown_regulation"}.
    """
    # Build regulation_key → regulation_id lookup once.
    reg_rows = session.scalars(select(Regulation)).all()
    reg_id_by_key: dict[str, int] = {r.regulation_key: r.id for r in reg_rows}

    # Pre-cache existing aliases keyed by (source_system, normalised_key).
    existing_rows = session.scalars(select(RegulationSourceAlias)).all()
    existing_by_key: dict[tuple[str, str], RegulationSourceAlias] = {
        (r.source_system, r.source_key.strip().lower()): r
        for r in existing_rows
    }

    inserted = updated = skipped_existing = skipped_unknown = 0

    for row in _ALIASES:
        skip_reason: Optional[str] = None
        notes: Optional[str] = None

        if len(row) == 3:
            source_system, source_key, regulation_key = row
        elif len(row) == 4:
            # Skip with reason: regulation_key is None.
            source_system, source_key, regulation_key, skip_reason = row
        else:
            log.warning("seed_regulation_aliases.malformed_row", row=row)
            continue

        is_skipped = regulation_key is None
        regulation_id: Optional[int] = None
        if not is_skipped:
            regulation_id = reg_id_by_key.get(regulation_key)
            if regulation_id is None:
                log.warning(
                    "seed_regulation_aliases.unknown_regulation",
                    source_system=source_system,
                    source_key=source_key,
                    regulation_key=regulation_key,
                )
                skipped_unknown += 1
                continue

        lookup_key = (source_system, source_key.strip().lower())
        existing = existing_by_key.get(lookup_key)

        if existing is None:
            session.add(
                RegulationSourceAlias(
                    source_system=source_system,
                    source_key=source_key,
                    regulation_id=regulation_id,
                    is_skipped=is_skipped,
                    skip_reason=skip_reason,
                    notes=notes,
                )
            )
            inserted += 1
        elif force_update:
            existing.regulation_id = regulation_id
            existing.is_skipped = is_skipped
            existing.skip_reason = skip_reason
            existing.notes = notes
            updated += 1
        else:
            skipped_existing += 1

    return {
        "inserted": inserted,
        "updated": updated,
        "skipped_existing": skipped_existing,
        "skipped_unknown_regulation": skipped_unknown,
    }


# Backwards-compat alias for callers that import a generic ``run_seed`` name
# (matches the convention used in ``seed_material_source_aliases``).
def run_seed(session: Session, *, force_update: bool = False) -> dict[str, int]:
    return seed_regulation_aliases(session, force_update=force_update)


__all__ = [
    "seed_regulation_aliases",
    "run_seed",
]
