"""
Resolve supplier name variants using `supplier_aliases` and exact `canonical_name`.

Phase 1: case-insensitive match; Phase 2+ may add fuzzy matching / legal-entity cleanup.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.supply import Supplier, SupplierAlias


class SupplierResolver:
    def __init__(self, db: Session) -> None:
        self._db = db

    def resolve_name(self, name: str | None) -> Supplier | None:
        if not name or not name.strip():
            return None
        key = name.strip()
        lower = key.lower()
        alias = self._db.execute(
            select(SupplierAlias).where(SupplierAlias.alias.ilike(key))
        ).scalar_one_or_none()
        if alias:
            return alias.supplier
        supplier = self._db.execute(
            select(Supplier).where(Supplier.canonical_name.ilike(key))
        ).scalar_one_or_none()
        if supplier:
            return supplier
        # token containment heuristic (very light)
        for s in self._db.scalars(select(Supplier)).all():
            if lower in s.canonical_name.lower() or s.canonical_name.lower() in lower:
                return s
        return None
