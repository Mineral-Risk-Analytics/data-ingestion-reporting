"""
Resolve company name variants using `company_aliases` and exact `canonical_name`.

Kept as supplier_resolver.py for import-path compatibility; the underlying
model is now Company/CompanyAlias (not Supplier/SupplierAlias).

Phase 1: case-insensitive match; Phase 2+ may add fuzzy matching / legal-entity cleanup.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company, CompanyAlias


class SupplierResolver:
    """Resolves a company name string to a Company ORM row.

    Named SupplierResolver for backward compatibility with callers that
    import this class. The resolved objects are Company instances.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def resolve_name(self, name: str | None) -> Company | None:
        if not name or not name.strip():
            return None
        key = name.strip()
        lower = key.lower()
        alias = self._db.execute(
            select(CompanyAlias).where(CompanyAlias.alias.ilike(key))
        ).scalar_one_or_none()
        if alias:
            return alias.company
        company = self._db.execute(
            select(Company).where(Company.canonical_name.ilike(key))
        ).scalar_one_or_none()
        if company:
            return company
        # token containment heuristic (very light)
        for c in self._db.scalars(select(Company)).all():
            if lower in c.canonical_name.lower() or c.canonical_name.lower() in lower:
                return c
        return None
