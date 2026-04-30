"""
Canada federal / provincial battery-industrial policy sources (Phase 3 placeholder).

Planned role: mirror `policy_pages` patterns for bilingual pages and map to NA geography.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.models.enums import ImplementationPhase, SourceType
from app.services.ingestion.base import FetchBundle, SourceAdapter


class CanadaPolicyAdapter(SourceAdapter):
    """Phase 3: TODO English/French crawl + linkage to USMCA context."""

    @property
    def source_type(self) -> str:
        return SourceType.CANADA_POLICY.value

    def fetch(
        self,
        client: httpx.Client,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[FetchBundle]:
        _ = client, params
        raise NotImplementedError(
            f"{ImplementationPhase.PHASE_3.value}: Canada policy ingestion not implemented yet"
        )
