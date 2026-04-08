"""
NREL / AFDC charging and alternative-fuel station data (Phase 3 placeholder).

Planned role: ingest station locations and power levels as `infrastructure_ecosystem`
context for demand / grid stress signals.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.models.enums import ImplementationPhase, SourceType
from app.services.ingestion.base import FetchBundle, SourceAdapter


class NrelChargingAdapter(SourceAdapter):
    """Phase 3: TODO AFDC API client + geospatial normalization."""

    @property
    def source_type(self) -> str:
        return SourceType.NREL_CHARGING.value

    def fetch(
        self,
        client: httpx.Client,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[FetchBundle]:
        _ = client, params
        raise NotImplementedError(
            f"{ImplementationPhase.PHASE_3.value}: NREL/AFDC charging ingest not implemented yet"
        )
