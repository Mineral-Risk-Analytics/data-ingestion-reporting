"""Source adapters.

Live (Phase 1): FederalRegisterAdapter, NewsAdapter, SecEdgarAdapter.
Parked scaffolds: CensusTradeAdapter — Phase 2 buyer-region scoping scaffold
    (raises ``NotImplementedError`` on ``fetch``; see the module docstring
    for the unpark checklist).
Placeholders (Phase 2/3): imported from ``adapters.future`` — all raise
    ``NotImplementedError``.
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
    SourceType.SEC_EDGAR.value: SecEdgarAdapter,
    SourceType.NEWS.value: NewsAdapter,
    # Parked scaffold — registered so SourceType.CENSUS_TRADE still routes,
    # but the adapter's fetch() raises NotImplementedError to prevent
    # accidental invocation against the bit-rotted Phase 1 defaults.
    # Unpark when buyer-region scoping (Phase 2) is built — see the
    # module docstring on census_trade.py for prerequisites.
    SourceType.CENSUS_TRADE.value: CensusTradeAdapter,
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
