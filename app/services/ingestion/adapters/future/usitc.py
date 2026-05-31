"""
USITC tariff / trade remedy enrichment (Phase 3 placeholder).

Planned role: attach investigation IDs and duty rates to HS / partner flows in `trade_flows.metadata_json`.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.models.enums import ImplementationPhase, SourceType
from app.services.ingestion.base import FetchBundle, SourceAdapter


class UsitcAdapter(SourceAdapter):
    """Phase 3: TODO USITC DataWeb or HTS integration."""

    @property
    def source_type(self) -> str:
        return SourceType.USITC.value

    def fetch(
        self,
        client: httpx.Client,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[FetchBundle]:
        _ = client, params
        raise NotImplementedError(
            f"{ImplementationPhase.PHASE_3.value}: USITC enrichment not implemented yet"
        )
