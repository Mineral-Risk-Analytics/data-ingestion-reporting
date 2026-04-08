"""Source adapters: Phase 1 live + Phase 2/3 placeholders."""

from app.models.enums import SourceType
from app.services.ingestion.adapters.canada_policy import CanadaPolicyAdapter
from app.services.ingestion.adapters.census_trade import CensusTradeAdapter
from app.services.ingestion.adapters.federal_register import FederalRegisterAdapter
from app.services.ingestion.adapters.news import NewsAdapter
from app.services.ingestion.adapters.ngo_reports import NgoReportsAdapter
from app.services.ingestion.adapters.nrel_charging import NrelChargingAdapter
from app.services.ingestion.adapters.policy_pages import PolicyPagesAdapter
from app.services.ingestion.adapters.sec_edgar import SecEdgarAdapter
from app.services.ingestion.adapters.sustainability_reports import SustainabilityReportsAdapter
from app.services.ingestion.adapters.usitc import UsitcAdapter

ADAPTER_BY_TYPE: dict[str, type] = {
    SourceType.FEDERAL_REGISTER.value: FederalRegisterAdapter,
    SourceType.CENSUS_TRADE.value: CensusTradeAdapter,
    SourceType.SEC_EDGAR.value: SecEdgarAdapter,
    SourceType.NEWS.value: NewsAdapter,
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
