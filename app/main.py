"""FastAPI application entrypoint.

Phase 1 adds Clerk-authenticated routes for the admin dashboard. See
``app/api/deps.py`` for the auth flow and dev-mode bypass.
Phase 2 adds reference-data browsers for materials, facilities, and chemistries,
plus generalized per-entity analyst-note (flag-issue) endpoints.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import inngest.fast_api
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes.chemistries import router as chemistries_router
from app.api.routes.companies import router as companies_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.facilities import router as facilities_router
from app.api.routes.health import router as health_router
from app.api.routes.ingestion import router as ingestion_router
from app.api.routes.intelligence import router as intelligence_router
from app.api.routes.market_scores import router as market_scores_router
from app.api.routes.materials import router as materials_router
from app.api.routes.regulations import router as regulations_router
from app.api.routes.risk_events import router as risk_events_router
from app.api.routes.sources import router as sources_router
from app.api.routes.trade_flows import router as trade_flows_router
from app.core.config import get_settings
from app.core.inngest import inngest_client
from app.core.logging import configure_logging
from app.tasks import ALL_SCHEDULED_FUNCTIONS


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging(get_settings().log_level)
    yield


app = FastAPI(
    title="Battery Data Intelligence Engine",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "Intelligence API backing the admin dashboard. Protected routes verify "
        "Clerk session JWTs via JWKS; see app/api/deps.py."
    ),
)


_settings = get_settings()
_origins = [o.strip() for o in _settings.frontend_url.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def _http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.__class__.__name__, "detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "ValidationError", "detail": exc.errors()},
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": exc.__class__.__name__, "detail": str(exc)},
    )


# Phase 1
app.include_router(health_router, prefix="/api/v1")
app.include_router(sources_router, prefix="/api/v1")
app.include_router(ingestion_router, prefix="/api/v1")
app.include_router(companies_router, prefix="/api/v1")
app.include_router(dashboard_router, prefix="/api/v1")
app.include_router(trade_flows_router, prefix="/api/v1")

# Phase 2 — reference data browsers + generalized flag-issue notes
app.include_router(regulations_router, prefix="/api/v1")
app.include_router(risk_events_router, prefix="/api/v1")
app.include_router(materials_router, prefix="/api/v1")
app.include_router(facilities_router, prefix="/api/v1")
app.include_router(chemistries_router, prefix="/api/v1")

# Foundation Phase 2 — market intelligence scores (material × geography)
app.include_router(market_scores_router, prefix="/api/v1")

# Foundation Phase 4 — Intelligence Hub (public content site backend)
app.include_router(intelligence_router, prefix="/api/v1")

# Foundation Phase 3 — Inngest scheduled scoring jobs.
# Serves the standard ``/api/inngest`` discovery + invocation endpoint that
# the Inngest Dev Server (and Inngest Cloud) expects. ``SCHEDULED_FUNCTIONS``
# is populated by the import side-effects in ``app.tasks``.
inngest.fast_api.serve(app, inngest_client, ALL_SCHEDULED_FUNCTIONS)
