"""Ingestion adapters, parsers, normalizers, and orchestration (Phase 1)."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.ingestion.pipeline import IngestionPipeline


def __getattr__(name: str):
    if name == "IngestionPipeline":
        from app.services.ingestion.pipeline import IngestionPipeline

        return IngestionPipeline
    raise AttributeError(name)


__all__ = ["IngestionPipeline"]
