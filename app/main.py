"""FastAPI application entrypoint (internal API, Phase 1)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.api.routes.health import router as health_router
from app.api.routes.ingestion import router as ingestion_router
from app.api.routes.regulations import router as regulations_router
from app.api.routes.risk_events import router as risk_events_router
from app.api.routes.sources import router as sources_router
from app.api.routes.suppliers import router as suppliers_router
from app.api.routes.trade_flows import router as trade_flows_router
from app.core.config import get_settings
from app.core.logging import configure_logging


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging(get_settings().log_level)
    yield


app = FastAPI(
    title="Battery Data Intelligence Engine",
    version="0.1.0",
    lifespan=lifespan,
    description="Internal ingestion and reporting API (no auth in Phase 1).",
)

app.include_router(health_router, prefix="/api/v1")
app.include_router(sources_router, prefix="/api/v1")
app.include_router(ingestion_router, prefix="/api/v1")
app.include_router(risk_events_router, prefix="/api/v1")
app.include_router(regulations_router, prefix="/api/v1")
app.include_router(suppliers_router, prefix="/api/v1")
app.include_router(trade_flows_router, prefix="/api/v1")
