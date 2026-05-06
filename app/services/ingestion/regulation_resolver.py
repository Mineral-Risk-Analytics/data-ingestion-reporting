"""Resolve external regulation identifiers to canonical regulation rows
via ``regulation_aliases``.

Mirrors ``app.services.ingestion.material_resolver.MaterialAliasResolver``
for the regulations schema.

Usage from an ingester
----------------------
    resolver = RegulationAliasResolver(session)
    result = resolver.resolve("eurlex_celex", "32023R1542")
    if result.status == "ok":
        regulation = result.regulation
        # update mutable fields, attach RiskEvent, etc.
    elif result.status == "skipped":
        continue
    elif result.status == "unknown":
        # surface a warning — partner needs to add an alias row
        # (or a new regulation + alias) before this external ID
        # can be tracked.
        log.warning("unknown CELEX", celex=...)

Performance
-----------
The resolver loads all aliases for a given ``source_system`` on first
call and caches them in-process.  ~5–20 rows per source system is the
expected size — trivial.  Subsequent lookups are O(1) dict hits.

Cache invalidation: callers that hold a resolver across long-running
processes should call ``resolver.refresh()`` after seeding/updating
``regulation_aliases``.

Normalisation
-------------
Lookups are normalised the same way as the unique expression index in
migration 039: ``lower(btrim(source_key))``.  CELEX numbers written as
``" 32023R1542 "`` resolve identically to ``"32023r1542"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.regulatory import Regulation, RegulationSourceAlias

ResolveStatus = Literal["ok", "skipped", "unknown"]


@dataclass
class ResolveResult:
    """Result of a single regulation alias lookup.

    Fields:
      regulation:  Resolved Regulation row, or None when status != "ok".
      status:      "ok" / "skipped" / "unknown".
    """

    regulation: Optional[Regulation]
    status: ResolveStatus


class RegulationAliasResolver:
    """In-process cache + lookup helper for ``regulation_aliases``."""

    def __init__(self, session: Session) -> None:
        self._session = session
        # cache[source_system][normalised_key] = (Regulation | None, is_skipped)
        self._cache: dict[
            str, dict[str, tuple[Optional[Regulation], bool]]
        ] = {}

    def _ensure_loaded(self, source_system: str) -> None:
        if source_system in self._cache:
            return
        rows = self._session.scalars(
            select(RegulationSourceAlias).where(
                RegulationSourceAlias.source_system == source_system
            )
        ).all()
        bucket: dict[str, tuple[Optional[Regulation], bool]] = {}
        for row in rows:
            key = row.source_key.strip().lower()
            bucket[key] = (row.regulation, row.is_skipped)
        self._cache[source_system] = bucket

    def resolve(
        self, source_system: str, source_key: str
    ) -> ResolveResult:
        """Resolve ``source_key`` under ``source_system``.

        Returns a ``ResolveResult`` with status:
          "ok"        — alias resolves to a tracked regulation.
          "skipped"   — alias exists but is_skipped=True (partner-curated
                        decision not to ingest).
          "unknown"   — no alias row matches (caller surfaces warning;
                        partner needs to add a row before events from
                        this external ID can be attributed).
        """
        if not source_key:
            return ResolveResult(regulation=None, status="unknown")
        self._ensure_loaded(source_system)
        key = source_key.strip().lower()
        entry = self._cache[source_system].get(key)
        if entry is None:
            return ResolveResult(regulation=None, status="unknown")
        regulation, is_skipped = entry
        if is_skipped:
            return ResolveResult(regulation=None, status="skipped")
        return ResolveResult(regulation=regulation, status="ok")

    def resolve_or_raise(
        self, source_system: str, source_key: str
    ) -> Regulation:
        """Convenience: resolve and raise on skipped/unknown.

        Useful in code paths where the caller has already filtered out
        skipped rows and an unknown alias is a real error.
        """
        result = self.resolve(source_system, source_key)
        if result.status == "ok":
            assert result.regulation is not None
            return result.regulation
        if result.status == "skipped":
            raise SkippedAliasError(
                f"Alias ({source_system!r}, {source_key!r}) is marked is_skipped=True"
            )
        raise UnknownAliasError(
            f"No alias for ({source_system!r}, {source_key!r}) — add a row "
            f"to seed_regulation_aliases.py and re-seed."
        )

    def refresh(self) -> None:
        """Drop the in-process cache.  Call after seeding new aliases."""
        self._cache.clear()


class SkippedAliasError(Exception):
    """Raised by resolve_or_raise when the alias is is_skipped=True."""


class UnknownAliasError(Exception):
    """Raised by resolve_or_raise when no alias row matches."""


# Functional convenience for one-off callers that don't want to manage a
# resolver instance.  Builds and discards a one-row cache — fine for
# small CLIs, wasteful in tight loops.
def resolve_regulation(
    session: Session, source_system: str, source_key: str
) -> ResolveResult:
    return RegulationAliasResolver(session).resolve(source_system, source_key)


__all__ = [
    "RegulationAliasResolver",
    "resolve_regulation",
    "ResolveResult",
    "ResolveStatus",
    "SkippedAliasError",
    "UnknownAliasError",
]
