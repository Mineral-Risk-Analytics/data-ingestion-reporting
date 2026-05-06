"""Resolve external commodity names to canonical materials via material_source_aliases.

Replaces four scattered Python dicts (``_CHAPTER_TO_MATERIAL``,
``_PRICE_NAME_TO_MATERIAL``, ``_MCS_COMMODITY_MAP``, ``_COMMODITY_CONFIG``)
with a single DB-backed lookup.  Each parser passes its source-system
slug and the raw commodity name from the source artifact; the resolver
returns either a ``Material`` row (alias resolves), a "skipped" sentinel
(deliberate non-ingest), or "unknown" (unmapped — usually a partner-
review trigger).

Usage from a parser
-------------------
    resolver = MaterialAliasResolver(session)
    result = resolver.resolve("mcs_2026_csv", "ALUMINUM")
    if result.status == "ok":
        # result.material.canonical_name == "Aluminum"
        # result.writes_material_signals == True (primary chapter)
        ...
    elif result.status == "skipped":
        continue
    elif result.status == "unknown":
        # Surface a warning — partner needs to add an alias row.
        ...

Performance
-----------
The resolver loads all aliases for a given ``source_system`` on first
call and caches them in-process.  ~50–60 rows per source — trivial.
Subsequent lookups are O(1) dict hits.

Cache invalidation: callers that hold a resolver across long-running
processes should call ``resolver.refresh()`` after seeding/updating
``material_source_aliases``.

Normalisation
-------------
Lookups are normalised the same way as the unique expression index in
migration 036: ``lower(btrim(source_name))``.  This tolerates USGS's
trailing-whitespace quirks (``'Boron '``, ``'Iron Ore  '``) and casing
inconsistencies without requiring duplicate alias rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.supply import Material, MaterialSourceAlias

ResolveStatus = Literal["ok", "skipped", "unknown"]


@dataclass
class ResolveResult:
    """Result of a single alias lookup.

    Fields:
      material:                  Resolved Material row, or None when
                                 status != "ok".
      status:                    "ok" / "skipped" / "unknown".
      writes_material_signals:   When status == "ok", True for primary
                                 chapters and False for secondary chapters
                                 that share a canonical with a sibling
                                 (BAUXITE AND ALUMINA → Aluminum).  CLI
                                 uses this to skip material-level signal
                                 upserts that would otherwise collide.
                                 Always False when status != "ok".
    """

    material: Optional[Material]
    status: ResolveStatus
    writes_material_signals: bool = False


class MaterialAliasResolver:
    """In-process cache + lookup helper for material_source_aliases."""

    def __init__(self, session: Session) -> None:
        self._session = session
        # cache[source_system][normalised_name] = (Material | None, is_skipped, writes_material_signals)
        self._cache: dict[
            str, dict[str, tuple[Optional[Material], bool, bool]]
        ] = {}

    def _ensure_loaded(self, source_system: str) -> None:
        if source_system in self._cache:
            return
        rows = self._session.scalars(
            select(MaterialSourceAlias).where(
                MaterialSourceAlias.source_system == source_system
            )
        ).all()
        bucket: dict[str, tuple[Optional[Material], bool, bool]] = {}
        for row in rows:
            key = row.source_name.strip().lower()
            bucket[key] = (
                row.canonical_material,
                row.is_skipped,
                row.writes_material_signals,
            )
        self._cache[source_system] = bucket

    def resolve(
        self, source_system: str, source_name: str
    ) -> ResolveResult:
        """Resolve ``source_name`` under ``source_system``.

        Returns a ``ResolveResult`` with status:
          "ok"        — alias resolves to a tracked material; check
                        ``writes_material_signals`` to decide whether
                        to write material-level signals.
          "skipped"   — alias exists but is_skipped=True
          "unknown"   — no alias row matches (caller surfaces warning)
        """
        if not source_name:
            return ResolveResult(material=None, status="unknown")
        self._ensure_loaded(source_system)
        key = source_name.strip().lower()
        entry = self._cache[source_system].get(key)
        if entry is None:
            return ResolveResult(material=None, status="unknown")
        material, is_skipped, writes_material_signals = entry
        if is_skipped:
            return ResolveResult(material=None, status="skipped")
        return ResolveResult(
            material=material,
            status="ok",
            writes_material_signals=writes_material_signals,
        )

    def resolve_or_raise(
        self, source_system: str, source_name: str
    ) -> Material:
        """Convenience: resolve and raise on skipped/unknown.

        Useful in code paths where the caller has already filtered out
        skipped rows and an unknown alias is a real error.
        """
        result = self.resolve(source_system, source_name)
        if result.status == "ok":
            assert result.material is not None
            return result.material
        if result.status == "skipped":
            raise SkippedAliasError(
                f"Alias ({source_system!r}, {source_name!r}) is marked is_skipped=True"
            )
        raise UnknownAliasError(
            f"No alias for ({source_system!r}, {source_name!r}) — add a row "
            f"to seed_material_source_aliases.py and re-seed."
        )

    def refresh(self) -> None:
        """Drop the in-process cache.  Call after seeding new aliases."""
        self._cache.clear()


class SkippedAliasError(Exception):
    """Raised by resolve_or_raise when the alias is is_skipped=True."""


class UnknownAliasError(Exception):
    """Raised by resolve_or_raise when no alias row matches."""


# Free-text → material keyword matching lives in
# ``app.services.ingestion.normalizers.material_resolver.MaterialCache``
# (May 2026 unification — single source of truth, with inverse-frequency
# relevance weighting baked in).  This module owns alias resolution
# (source_name → canonical material) only; keyword matching lives there
# so the three production callers (ingest_federal_register,
# iea_policy_tracker, pipeline) don't have to switch APIs.


# Functional convenience for one-off callers that don't want to manage
# a resolver instance.  Builds and discards a one-row cache — fine for
# small CLIs, wasteful in tight loops.
def resolve_material(
    session: Session, source_system: str, source_name: str
) -> ResolveResult:
    return MaterialAliasResolver(session).resolve(source_system, source_name)


__all__ = [
    "MaterialAliasResolver",
    "resolve_material",
    "ResolveResult",
    "ResolveStatus",
    "SkippedAliasError",
    "UnknownAliasError",
]
