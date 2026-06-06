"""
U.S. Census Bureau international trade time-series — PARKED RESEARCH SCAFFOLD.

================================================================================
STATUS: NOT IN PRODUCTION USE — PARKED FOR PHASE 2 BUYER-REGION SCOPING
================================================================================

This adapter is a research scaffold for a future feature, NOT a live ingest
path.  No scheduled job runs it; the matching CLI debug entry has been
removed.  Calling :py:meth:`fetch` directly raises ``NotImplementedError``
to prevent accidental invocation against stale defaults.

Why it exists
-------------
The platform's near-term (Phase 1) regional scoping is served by filtering
the existing per-source-country scores in ``material_geography_risk_scores``
— no bilateral trade ingest required.

Phase 2 adds **buyer-region rollup scoring**: "What's cobalt risk from a
US-buyer perspective, weighted by US import shares from each source
country?"  That feature needs **bilateral trade flows** (US imports from
country X, by HS code, by period).

The current UN Comtrade adapter ingests at ``partnerCode=0`` (world
aggregate), not bilateral.  Extending Comtrade to bilateral would multiply
API calls by ~25 (one per partner country) and immediately blow the
Basic-tier 500-calls/day quota.  US Census bilateral data is therefore
the right Phase 2 path for the US buyer-region scope; it returns one row
per (partner country, HS code) natively and has a much higher quota.

What needs to happen before this can be unparked
------------------------------------------------
See task tracker for the full Phase 2 prerequisites.  Short version:

  1. Parameterise the ``time=`` argument for multi-month iteration
     (today defaults to a single hardcoded month).
  2. Add HS-prefix filtering (``&I_COMMODITY=`` / ``&E_COMMODITY=``) so
     the call returns only launch-list commodities rather than every
     traded good.
  3. Build an Inngest scheduled monthly job (mirror the Comtrade
     ``ingest-comtrade-daily`` pattern).
  4. Decide 10-digit HTS handling: Census returns 10-digit HTS codes,
     ``MaterialResolver.resolve_by_hs_code`` is built for Comtrade's
     6-digit HS.  Either truncate at ingest (loses chemistry
     granularity) or extend ``hs_code_material_mappings`` with 10-digit
     rows.
  5. Storage decision: new ``material_buyer_region_risk_scores`` table
     keyed on ``(material_id, buyer_region, as_of_date)``, OR extend
     ``material_geography_risk_scores`` with a ``scope_region`` column.
  6. Build the US-scoped rollup pipeline in ``trade_signal_builder.py``
     (or a sibling module).
  7. Decide EU-scope path — probably requires upgrading to Comtrade
     Premium tier (~$249/yr, 5,000 calls/day) so EU bilateral becomes
     feasible.

Reference
---------
Dataset docs: ``timeseries/intltrade/imports/hs`` —
https://www.census.gov/data/developers/data-sets/international-trade.html
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings
from app.models.enums import SourceType
from app.services.ingestion.base import FetchBundle, SourceAdapter


# Module-level marker so other code (or operator queries) can detect that
# Census ingest is intentionally disabled rather than missing-by-accident.
CENSUS_TRADE_PARKED: bool = True

_PARKED_MESSAGE = (
    "CensusTradeAdapter is parked: not in production use. "
    "It exists as a Phase 2 scaffold for buyer-region scoping (US "
    "imports rollup).  See the module docstring for the unparking "
    "checklist.  If you intend to fetch from the Census Bureau API, "
    "implement the Phase 2 prerequisites first."
)


class CensusTradeAdapter(SourceAdapter):
    """U.S. Census trade adapter — parked research scaffold.

    Do not invoke ``fetch()`` against this class; it raises
    ``NotImplementedError`` on purpose.  See the module docstring for the
    full reasoning and the Phase 2 unpark checklist.
    """

    @property
    def source_type(self) -> str:
        return SourceType.CENSUS_TRADE.value

    def default_params(self) -> dict[str, Any]:
        # Default params are preserved as documentation of the historical
        # query shape; they are NOT a live-use suggestion.  The hardcoded
        # ``time="2024-11"`` is the bit-rotted Phase 1 default and would
        # produce stale data if this adapter were ever called.
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
        # Fail loudly rather than silently fetching stale-month data.
        # When Phase 2 unparks this adapter, restore the live fetch
        # implementation (see git history for the previous version) and
        # ensure each Phase 2 prerequisite is satisfied first.
        raise NotImplementedError(_PARKED_MESSAGE)

    def _build_parked_endpoint(self, params: dict[str, Any] | None = None) -> str:
        """Construct the Census endpoint URL that ``fetch`` *would* call.

        Retained for testability + Phase 2 reference.  Not used at runtime
        while the adapter is parked.
        """
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
        return f"{base}/{dataset_path}?{urlencode(q)}"
