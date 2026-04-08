"""
Cluster near-duplicate signals (same story, multiple sources).

Phase 1: normalize titles and bucket by Jaccard-ish token overlap (cheap).
Future: embeddings + HDBSCAN / online clustering service.
"""

from __future__ import annotations

from typing import Protocol


class DuplicateClusterer(Protocol):
    def cluster_key(self, title: str, url: str | None) -> str:
        ...


class StubDuplicateClusterer:
    def cluster_key(self, title: str, url: str | None) -> str:
        if url:
            return f"url:{url}"
        tokens = "".join(c.lower() for c in title if c.isalnum() or c.isspace()).split()
        sig = "-".join(sorted(set(tokens))[:12])
        return f"title:{sig}"
