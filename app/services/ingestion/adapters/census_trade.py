"""
U.S. Census Bureau international trade time-series (Phase 1).

Dataset example: `timeseries/intltrade/imports/hs` — see
https://www.census.gov/data/developers/data-sets/international-trade.html
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings
from app.models.enums import SourceType
from app.services.ingestion.base import FetchBundle, SourceAdapter


class CensusTradeAdapter(SourceAdapter):
    @property
    def source_type(self) -> str:
        return SourceType.CENSUS_TRADE.value

    def default_params(self) -> dict[str, Any]:
        return {
            "dataset_path": "imports/hs",
            "time": "2024-11",
            "get": "CTY_CODE,CTY_NAME,I_COMMODITY,I_COMMODITY_LDESC,GEN_VAL_MO",
        }

    def fetch(
        self,
        client: httpx.Client,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[FetchBundle]:
        settings = get_settings()
        p = {**self.default_params(), **(params or {})}
        dataset_path = str(p.get("dataset_path", "imports/hs")).strip("/")
        base = settings.census_trade_base_url.rstrip("/")
        q = {
            "get": p.get("get"),
            "time": p.get("time"),
        }
        if settings.census_api_key:
            q["key"] = settings.census_api_key
        endpoint = f"{base}/{dataset_path}?{urlencode(q)}"
        resp = client.get(endpoint, timeout=120.0)
        resp.raise_for_status()
        try:
            table = resp.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError("Census API returned non-JSON (check API key / quota)") from exc
        items: list[dict[str, Any]] = [
            {
                "census_table": table,
                "time": p.get("time"),
                "dataset_path": dataset_path,
            }
        ]
        return [
            FetchBundle(
                endpoint=endpoint,
                raw_body=resp.content,
                items=items,
                request_params=q,
            )
        ]
