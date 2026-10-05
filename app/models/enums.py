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
    """Physical facility classification, ordered roughly upstream → downstream.

    Extended 2026-07-07 to unify three previously divergent taxonomies:
    this enum (7 values), seed_facilities_partner._FACILITY_TYPES (11 values),
    and the MRDS ingester's granular types (mine / concentrator / smelter /
    refinery / leach / processing).  The partner loader now derives its
    allowed set from this enum — extend HERE, not there.
    """

    MINE = "mine"                                # extraction: open pit / underground / brine / well
    CONCENTRATOR = "concentrator"                # mill / flotation -> concentrate
    LEACH = "leach"                              # heap / tank leach, hydromet (MRDS conservative)
    PROCESSING = "processing"                    # ambiguous mid-stream plant (prefer a granular type)
    SMELTER = "smelter"                          # concentrate -> matte / blister / crude metal
    REFINERY = "refinery"                        # -> refined metal / battery-grade chemical
    FABRICATION = "fabrication"                  # metal / alloy / magnet / component manufacture (e.g. NdFeB)
    CELL_FACTORY = "cell_factory"
    PACK_PLANT = "pack_plant"
    RECYCLING = "recycling"
    PORT = "port"                                # logistics node (export terminal, rail port)
    EXPLORATION_PROJECT = "exploration_project"  # pre-development; pair with status=planned
    INTEGRATED = "integrated"                    # multi-stage site (mine+smelter+refinery); prefer
    #                                              splitting into per-stage rows where data allows
    R_AND_D = "r_and_d"
    HQ = "hq"
    OTHER = "other"


class FacilityStatus(str, Enum):
    OPERATING = "operating"
    PLANNED = "planned"
    UNDER_CONSTRUCTION = "under_construction"
    MOTHBALLED = "mothballed"
    CLOSED = "closed"
    # Added 2026-07-07: accepted by seed_facilities_partner since inception
    # but missing here; distinct from mothballed (active preservation,
    # restart-ready — e.g. Albemarle Kemerton trains, BHP WA Nickel).
    CARE_MAINTENANCE = "care_maintenance"
    SUSPENDED = "suspended"  # involuntary/indefinite halt (strike, flooding, court order)
    DIVESTED = "divested"    # company-facility link ended by sale; facility may
    #                          continue operating under the new owner


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
