"""
IEA / NGO PDF and HTML reports (Phase 2 placeholder).

Planned role: catalog releases, fetch reports, chunk for RAG, tag scenarios affecting
North American battery supply positioning.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.models.enums import ImplementationPhase, SourceType
from app.services.ingestion.base import FetchBundle, SourceAdapter


class NgoReportsAdapter(SourceAdapter):
    """Phase 2: TODO feed registry + download + `pdf_report_parser` integration."""

    @property
    def source_type(self) -> str:
        return SourceType.NGO_REPORT.value

    def fetch(
        self,
        client: httpx.Client,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[FetchBundle]:
        _ = client, params
        raise NotImplementedError(
            f"{ImplementationPhase.PHASE_2.value}: NGO / IEA report ingestion not implemented yet"
        )
