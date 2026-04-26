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

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
    SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON
if not hasattr(SQLiteTypeCompiler, "visit_ARRAY"):
    SQLiteTypeCompiler.visit_ARRAY = SQLiteTypeCompiler.visit_JSON
