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
    COMTRADE = "comtrade"
    LME = "lme"
    USGS = "usgs"


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


class SupplyChainStage(str, Enum):
    """Position in the battery supply chain — replaces SupplierType."""
    MINER = "miner"
    REFINER = "refiner"
    PRECURSOR = "precursor"        # precursor cathode active material
    CELL_MAKER = "cell_maker"
    PACK_MAKER = "pack_maker"
    OEM = "oem"
    RECYCLER = "recycler"
    TRADER = "trader"
    OTHER = "other"


class FacilityType(str, Enum):
    MINE = "mine"
    REFINERY = "refinery"
    CELL_FACTORY = "cell_factory"
    PACK_PLANT = "pack_plant"
    RECYCLING = "recycling"
    R_AND_D = "r_and_d"
    HQ = "hq"


class FacilityStatus(str, Enum):
    OPERATING = "operating"
    PLANNED = "planned"
    UNDER_CONSTRUCTION = "under_construction"
    MOTHBALLED = "mothballed"
    CLOSED = "closed"


class ComplianceStatus(str, Enum):
    COMPLIANT = "compliant"
    NON_COMPLIANT = "non_compliant"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class ReportType(str, Enum):
    MARKET_INTELLIGENCE = "market_intelligence"
    SUPPLIER_RISK = "supplier_risk"
    REGULATORY_IMPACT = "regulatory_impact"
    GEOGRAPHY_RISK = "geography_risk"


class ReportFocusType(str, Enum):
    MATERIAL = "material"
    COMPANY = "company"
    REGULATION = "regulation"
    GEOGRAPHY = "geography"
    BATTERY_CHEMISTRY = "battery_chemistry"


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


class TenantPlan(str, Enum):
    STARTER = "starter"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class UserRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    ANALYST = "analyst"
    MEMBER = "member"
