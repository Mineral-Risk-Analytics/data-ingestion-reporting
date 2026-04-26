"""Ingestion-pipeline feature flags.

Single source of truth for booleans that gate behaviour the ingestion layer
used to perform unconditionally. Gated behaviour is preserved (table, ORM,
helper functions) so flipping the flag back to ``True`` is a one-line change.

Why these live here (not in ``Settings``)
-----------------------------------------
Settings are read from the environment and are appropriate for things that
differ across deploys (DB URL, API keys). Feature flags here express
*architectural* decisions for the current scoring layer iteration. They
change with the codebase, not the deploy.

Current flags
-------------
``LINK_EVENTS_TO_COMPANIES`` (default ``False``):
    Foundation phase 3 disables risk event → company linking at ingestion
    time. The new architecture derives event/company relevance from a
    customer's configured exposure profile (Phase 5 — company overlay).
    The ``risk_event_companies`` junction table, its ORM model, and
    ``persist_company_links`` are intentionally retained for that future
    use; the writers in ingestion services are short-circuited.

    When this flag is ``False``, the gated call sites emit a single
    ``structlog`` WARNING with the count of suppressed links so the
    behaviour is auditable in production logs.

    Tests that exercise the upsert / dedupe semantics of the writer
    helpers should monkeypatch this flag back to ``True`` (see
    ``tests/test_entity_resolution.py``).

Re-importing for monkeypatchability
-----------------------------------
Call sites read the flag through the module attribute (``feature_flags
.LINK_EVENTS_TO_COMPANIES``) rather than a closure-bound local. This keeps
``monkeypatch.setattr("app.services.ingestion.feature_flags
.LINK_EVENTS_TO_COMPANIES", True)`` effective in tests without needing
to re-import the call site.
"""

from __future__ import annotations

LINK_EVENTS_TO_COMPANIES: bool = False
