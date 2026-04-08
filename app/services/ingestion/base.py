"""
Abstract ingestion adapter (Phase 1 live implementations; Phase 2/3 stubs extend this).

Adapters are responsible for **fetching** structured or semi-structured data from external
systems. Persistence, parsing, and normalization are handled by `IngestionPipeline`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class FetchBundle:
    """One HTTP-equivalent batch: raw bytes for storage plus normalized item dicts."""

    endpoint: str
    raw_body: bytes
    items: list[dict[str, Any]] = field(default_factory=list)
    request_params: dict[str, Any] | None = None


class SourceAdapter(ABC):
    """Base class for all source adapters."""

    @property
    @abstractmethod
    def source_type(self) -> str:
        """Matches `Source.source_type` / `SourceType` enum value."""

    @abstractmethod
    def fetch(
        self,
        client: httpx.Client,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[FetchBundle]:
        """
        Return one or more fetch batches. Each batch stores `raw_body` as audit trail
        and expands into `items` for parsers (one dict per logical document / row group).
        """

    def default_params(self) -> dict[str, Any]:
        """Override to provide CLI/API defaults when `params` is omitted."""
        return {}
