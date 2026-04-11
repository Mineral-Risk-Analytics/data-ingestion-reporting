"""
News ingestion via provider interface (Phase 1: stub articles).

Future: NewsAPI, GDELT, or licensed feeds — keep `NEWS_PROVIDER` / `NEWS_API_KEY` in settings.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from app.models.enums import SourceType
from app.services.ingestion.base import FetchBundle, SourceAdapter


class NewsProviderProtocol(Protocol):
    """Pluggable news provider (stub or HTTP-backed)."""

    def fetch_articles(self, *, params: dict[str, Any]) -> list[dict[str, Any]]:
        ...


class StubNewsProvider:
    """Deterministic in-memory articles for demos and tests."""

    def fetch_articles(self, *, params: dict[str, Any]) -> list[dict[str, Any]]:
        topic = params.get("topic", "battery supply chain")
        return [
            {
                "id": "stub-article-1",
                "title": f"Policy watchers flag new {topic} constraints",
                "source": "stub-wire",
                "url": "https://example.com/news/stub-article-1",
                "published_at": "2024-06-01T12:00:00+00:00",
                "body": "Analysts noted tightening environmental review timelines for "
                "gigafactory expansions and downstream lithium refining projects.",
                "entities": [{"type": "material", "name": "lithium"}],
                "event_classification": ["regulatory_compliance", "geopolitical_trade"],
            }
        ]


class NewsAdapter(SourceAdapter):
    """Aggregates articles from the configured `NewsProviderProtocol` implementation."""

    def __init__(self, provider: NewsProviderProtocol | None = None) -> None:
        self._provider = provider or StubNewsProvider()

    @property
    def source_type(self) -> str:
        return SourceType.NEWS.value

    def default_params(self) -> dict[str, Any]:
        return {"topic": "EV battery"}

    def fetch(
        self,
        client: httpx.Client,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[FetchBundle]:
        _ = client  # HTTP not used for stub provider
        p = {**self.default_params(), **(params or {})}
        articles = self._provider.fetch_articles(params=p)
        raw = json.dumps({"articles": articles}, default=str).encode("utf-8")
        items = [a for a in articles if isinstance(a, dict)]
        return [
            FetchBundle(
                endpoint="stub://news-provider",
                raw_body=raw,
                items=items,
                request_params=p,
            )
        ]
