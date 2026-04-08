"""
White House / EPA / NHTSA policy HTML pages (Phase 2 placeholder).

Planned role: polite crawling with etag/last-modified, snapshot HTML to raw storage,
extract rule-makings and guidance linked to `regulations` and `risk_events`.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.models.enums import ImplementationPhase, SourceType
from app.services.ingestion.base import FetchBundle, SourceAdapter


class PolicyPagesAdapter(SourceAdapter):
    """Phase 2: TODO robots-aware crawl + HTML normalization."""

    @property
    def source_type(self) -> str:
        return SourceType.POLICY_PAGE.value

    def fetch(
        self,
        client: httpx.Client,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[FetchBundle]:
        _ = client, params
        raise NotImplementedError(
            f"{ImplementationPhase.PHASE_2.value}: policy page ingestion not implemented yet"
        )
