"""Ingestion adapters, parsers, normalizers, and orchestration (Phase 1)."""

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.ingestion.pipeline import IngestionPipeline


def __getattr__(name: str):
    """Lazy attribute lookup.

    Keeps ``IngestionPipeline`` import-cheap (originally added to break a
    circular import via ``app.services.ingestion.pipeline``) while still
    allowing standard submodule access — ``from app.services.ingestion
    import feature_flags`` and friends.
    """
    if name == "IngestionPipeline":
        from app.services.ingestion.pipeline import IngestionPipeline

        return IngestionPipeline
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ImportError as exc:
        raise AttributeError(name) from exc


__all__ = ["IngestionPipeline"]
