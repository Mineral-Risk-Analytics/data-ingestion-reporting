"""String-backed enumerations stored in the database (SQLAlchemy / API)."""

from enum import Enum


class SourceType(str, Enum):
    FEDERAL_REGISTER = "federal_register"
    CENSUS_TRADE = "census_trade"
    SEC_EDGAR = "sec_edgar"
    NEWS = "news"
    SUSTAINABILITY_REPORT = "sustainability_report"
    POLICY_PAGE = "policy_page"
    NGO_REPORT = "ngo_report"
    NREL_CHARGING = "nrel_charging"
    USITC = "usitc"
    CANADA_POLICY = "canada_policy"


class ImplementationPhase(str, Enum):
    PHASE_1 = "1"
    PHASE_2 = "2"
    PHASE_3 = "3"


class IngestionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class DocumentType(str, Enum):
    API_JSON = "api_json"
    HTML = "html"
    PDF = "pdf"
    FILING = "filing"
    ARTICLE = "article"
    TABULAR_EXPORT = "tabular_export"
    UNKNOWN = "unknown"


class SupplierType(str, Enum):
    OEM = "oem"
    TIER1 = "tier1"
    MINER = "miner"
    REFINER = "refiner"
    CELL_MAKER = "cell_maker"
    OTHER = "other"


class ReportType(str, Enum):
    OEM = "oem"
    SUPPLIER = "supplier"
    INVESTOR = "investor"


class ReportAudience(str, Enum):
    INTERNAL = "internal"
    OEM = "oem"
    INVESTOR = "investor"
    PUBLIC = "public"


class ReportRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
