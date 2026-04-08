"""
Map HS codes / keywords to `materials` rows (DB-backed with prefix fallbacks).

Phase 1: prefix rules + optional SQLAlchemy lookup.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.supply import Material


# HS chapter / heading hints → canonical material name keys (must match seed or DB)
_HS_PREFIX_RULES: list[tuple[str, str]] = [
    ("8507", "Lithium-ion battery cells"),
    ("850760", "Lithium-ion battery cells"),
    ("2805", "Lithium chemicals"),
    ("284390", "Rare earth compounds"),
    ("810820", "Unwrought lithium"),
]


class MaterialResolver:
    def __init__(self, db: Session) -> None:
        self._db = db

    def resolve_by_hs_code(self, hs_code: str | None) -> int | None:
        if not hs_code:
            return None
        code = str(hs_code).strip()
        for prefix, name in _HS_PREFIX_RULES:
            if code.startswith(prefix):
                row = self._db.execute(
                    select(Material).where(Material.canonical_name == name)
                ).scalar_one_or_none()
                if row:
                    return row.id
        return None

    def resolve_by_canonical_name(self, name: str) -> Material | None:
        return self._db.execute(
            select(Material).where(Material.canonical_name == name)
        ).scalar_one_or_none()
