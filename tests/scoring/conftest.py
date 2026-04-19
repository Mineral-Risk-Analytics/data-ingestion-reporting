"""Shared fixtures for scoring tests.

Builds an in-memory SQLite database with all ORM tables registered. JSONB is
rendered as JSON for DDL compatibility (no JSONB-specific query operators are
used by the scoring layer).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# Ensure all ORM models are registered before create_all.
import app.models  # noqa: F401
from app.db.base import Base


def _patch_sqlite_jsonb() -> None:
    """Render JSONB as JSON in SQLite DDL (no JSONB query operators used here)."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler  # type: ignore[import]

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]


@pytest.fixture()
def sqlite_session() -> Session:
    """In-memory SQLite session with all ORM tables created."""
    _patch_sqlite_jsonb()
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    Session_ = sessionmaker(bind=engine)
    session = Session_()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
