"""Source adapters.

Live (Phase 1): CensusTradeAdapter, FederalRegisterAdapter, NewsAdapter, SecEdgarAdapter.
Placeholders (Phase 2/3): imported from ``adapters.future`` — all raise NotImplementedError.
"""

from app.models.enums import SourceType
from app.services.ingestion.adapters.census_trade import CensusTradeAdapter
from app.services.ingestion.adapters.federal_register import FederalRegisterAdapter
from app.services.ingestion.adapters.news import NewsAdapter
from app.services.ingestion.adapters.sec_edgar import SecEdgarAdapter
from app.services.ingestion.adapters.future import (
    CanadaPolicyAdapter,
    NgoReportsAdapter,
    NrelChargingAdapter,
    PolicyPagesAdapter,
    SustainabilityReportsAdapter,
    UsitcAdapter,
)

ADAPTER_BY_TYPE: dict[str, type] = {
    # Phase 1 — live
    SourceType.FEDERAL_REGISTER.value: FederalRegisterAdapter,
    SourceType.CENSUS_TRADE.value: CensusTradeAdapter,
    SourceType.SEC_EDGAR.value: SecEdgarAdapter,
    SourceType.NEWS.value: NewsAdapter,
    # Phase 2/3 — placeholders (raise NotImplementedError on fetch)
    SourceType.SUSTAINABILITY_REPORT.value: SustainabilityReportsAdapter,
    SourceType.POLICY_PAGE.value: PolicyPagesAdapter,
    SourceType.NGO_REPORT.value: NgoReportsAdapter,
    SourceType.NREL_CHARGING.value: NrelChargingAdapter,
    SourceType.USITC.value: UsitcAdapter,
    SourceType.CANADA_POLICY.value: CanadaPolicyAdapter,
}

__all__ = [
    "ADAPTER_BY_TYPE",
    "CanadaPolicyAdapter",
    "CensusTradeAdapter",
    "FederalRegisterAdapter",
    "NewsAdapter",
    "NgoReportsAdapter",
    "NrelChargingAdapter",
    "PolicyPagesAdapter",
    "SecEdgarAdapter",
    "SustainabilityReportsAdapter",
    "UsitcAdapter",
]
