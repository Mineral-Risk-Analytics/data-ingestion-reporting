"""
Summarize ingested documents for analyst triage.

Phase 1: `StubDocumentSummarizer` uses extractive heuristics (first sentences).
Future: inject `OpenAiSummarizer` or similar behind the same protocol.
"""

from __future__ import annotations

from typing import Any, Protocol


class DocumentSummarizer(Protocol):
    def summarize(self, text: str, *, metadata: dict[str, Any] | None = None) -> str:
        ...


class StubDocumentSummarizer:
    """Cheap local default — avoids network and API keys."""

    def summarize(self, text: str, *, metadata: dict[str, Any] | None = None) -> str:
        _ = metadata
        if not text or not text.strip():
            return ""
        sentences = [s.strip() for s in text.replace("\n", " ").split(".") if s.strip()]
        return ". ".join(sentences[:3]) + ("." if sentences else "")
