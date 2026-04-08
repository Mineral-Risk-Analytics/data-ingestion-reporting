"""
Federal Register API adapter (Phase 1).

Public API: https://www.federalregister.gov/developers/documentation/api/v1
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings
from app.models.enums import SourceType
from app.services.ingestion.base import FetchBundle, SourceAdapter


class FederalRegisterAdapter(SourceAdapter):
    @property
    def source_type(self) -> str:
        return SourceType.FEDERAL_REGISTER.value

    def default_params(self) -> dict[str, Any]:
        return {"per_page": 15, "term": "battery OR lithium OR \"critical mineral\""}

    def fetch(
        self,
        client: httpx.Client,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[FetchBundle]:
        settings = get_settings()
        p = {**self.default_params(), **(params or {})}
        base = settings.federal_register_base_url.rstrip("/")
        query = {
            "per_page": min(int(p.get("per_page", 20)), 1000),
            "page": int(p.get("page", 1)),
            "order": p.get("order", "newest"),
            "conditions[term]": p.get("term", "battery"),
        }
        endpoint = f"{base}/documents.json?{urlencode(query)}"
        resp = client.get(endpoint, timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or []
        if not isinstance(results, list):
            results = []
        items = [r for r in results if isinstance(r, dict)]
        return [
            FetchBundle(
                endpoint=endpoint,
                raw_body=json.dumps(data, default=str).encode("utf-8"),
                items=items,
                request_params=query,
            )
        ]
