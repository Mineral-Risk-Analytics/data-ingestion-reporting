"""
SEC EDGAR `submissions` API (Phase 1).

Requires a descriptive `User-Agent` per https://www.sec.gov/os/accessing-edgar-data
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.config import get_settings
from app.models.enums import SourceType
from app.services.ingestion.base import FetchBundle, SourceAdapter


class SecEdgarAdapter(SourceAdapter):
    @property
    def source_type(self) -> str:
        return SourceType.SEC_EDGAR.value

    def default_params(self) -> dict[str, Any]:
        # Tesla Inc. CIK — override via source `config_json` or ingest params
        return {"ciks": ["0001318605"]}

    def fetch(
        self,
        client: httpx.Client,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[FetchBundle]:
        settings = get_settings()
        p = {**self.default_params(), **(params or {})}
        ciks = p.get("ciks") or []
        if isinstance(ciks, str):
            ciks = [ciks]
        bundles: list[FetchBundle] = []
        for raw_cik in ciks:
            cik = str(raw_cik).strip().zfill(10)
            endpoint = f"{settings.sec_edgar_base_url.rstrip('/')}/submissions/CIK{cik}.json"
            resp = client.get(endpoint, timeout=60.0)
            resp.raise_for_status()
            data = resp.json()
            items = [data] if isinstance(data, dict) else []
            bundles.append(
                FetchBundle(
                    endpoint=endpoint,
                    raw_body=json.dumps(data, default=str).encode("utf-8"),
                    items=items,
                    request_params={"cik": cik},
                )
            )
        return bundles
