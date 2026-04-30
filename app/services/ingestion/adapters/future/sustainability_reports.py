"""
OEM sustainability / integrated annual report PDFs (Phase 2 placeholder).

Planned role: discover OEM report URLs, download PDFs, call `pdf_report_parser`,
and link extracted battery-chain claims to suppliers and materials.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.models.enums import ImplementationPhase, SourceType
from app.services.ingestion.base import FetchBundle, SourceAdapter


class SustainabilityReportsAdapter(SourceAdapter):
    """Phase 2: TODO implement crawl + PDF download + parse pipeline."""

    @property
    def source_type(self) -> str:
        return SourceType.SUSTAINABILITY_REPORT.value

    def fetch(
        self,
        client: httpx.Client,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[FetchBundle]:
        _ = client, params
        raise NotImplementedError(
            f"{ImplementationPhase.PHASE_2.value}: sustainability report ingestion not implemented yet"
        )
