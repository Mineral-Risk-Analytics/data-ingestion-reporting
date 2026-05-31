"""Inngest client used for scheduled background jobs.

Why a dedicated module
----------------------
- Keeps the import graph clean: scheduled functions in ``app/tasks/`` import
  the client from here; ``app/main.py`` imports both the client and the
  function list to wire the FastAPI ``/api/inngest`` serve endpoint.
- Single place to change the ``app_id`` / production-mode behaviour.

Dev vs production
-----------------
``is_production`` is derived from ``Settings.app_env``: anything other than
``"production"`` boots the SDK in dev mode (talks to a local Inngest Dev
Server, no signing key required). Production mode requires the standard
``INNGEST_SIGNING_KEY`` and ``INNGEST_EVENT_KEY`` environment variables.

Setting ``INNGEST_DEV=1`` still forces dev mode regardless of ``app_env``,
matching the SDK's documented escape hatch.

Local dev workflow:

.. code-block:: bash

    # Terminal 1: FastAPI app
    uvicorn app.main:app --reload   # app_env=development → dev mode

    # Terminal 2: Inngest Dev Server (auto-discovers /api/inngest)
    npx --ignore-scripts=false inngest-cli@latest dev \\
        -u http://127.0.0.1:8000/api/inngest --no-discovery
"""

from __future__ import annotations

import logging
import os

import inngest

from app.core.config import get_settings

_settings = get_settings()
_is_production = (
    bool(os.getenv("INNGEST_SIGNING_KEY")) and not os.getenv("INNGEST_DEV")
)

inngest_client: inngest.Inngest = inngest.Inngest(
    app_id="battery-data-intelligence-engine",
    logger=logging.getLogger("uvicorn"),
    is_production=_is_production,
)

__all__ = ["inngest_client"]
