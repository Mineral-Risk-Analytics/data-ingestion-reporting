"""Pytest configuration.

Global SQLite ↔ PostgreSQL DDL compatibility shim
-------------------------------------------------
Several models use PostgreSQL-only column types — ``JSONB`` (most metadata
columns) and ``ARRAY(String)`` (``insight_posts.materials/geographies``).
These cannot be rendered by SQLite's DDL compiler out of the box, which
breaks every in-memory test fixture that does ``Base.metadata.create_all``.

We patch the SQLite type compiler at *collection* time to render those
types as ``JSON``. This affects DDL only; no test exercises PG-specific
operators (e.g. ``@>`` for JSONB, array indexing) against SQLite.

Centralising the patch here means individual test modules no longer need
to ship their own copy. Tests that defined ``_patch_sqlite_jsonb`` locally
keep working because the SQLAlchemy compiler module is global state.
"""

from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.dialects.sqlite.base import SQLiteDialect, SQLiteTypeCompiler
from sqlalchemy.dialects.sqlite.json import JSON as SQLITE_JSON

if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
    SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON
if not hasattr(SQLiteTypeCompiler, "visit_ARRAY"):
    SQLiteTypeCompiler.visit_ARRAY = SQLiteTypeCompiler.visit_JSON

# 2026-07-22: DDL rendering alone is NOT enough — binding a Python list to
# an ARRAY column raised sqlite3.ProgrammingError on the first test that
# actually WROTE an array (the tags PATCH regression test; every earlier
# test only wrote NULLs, so this stayed latent and the docstring above
# claiming arrays "survive round-trips" was aspirational). Map PG ARRAY /
# JSONB to SQLite's JSON impl so values genuinely serialize/deserialize.


class _PgTypeAsSqliteJson(SQLITE_JSON):
    """Swallows the PG type's constructor args (item_type, astext_type…)."""

    def __init__(self, *args, **kwargs):  # noqa: ARG002 — deliberate
        super().__init__()


SQLiteDialect.colspecs = {
    **SQLiteDialect.colspecs,
    PG_ARRAY: _PgTypeAsSqliteJson,
    PG_JSONB: _PgTypeAsSqliteJson,
}
