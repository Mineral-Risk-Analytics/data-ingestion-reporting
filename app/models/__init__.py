"""ORM models — import side effects register all metadata with Base."""

from app.models.battery_chemistry import BatteryChemistry, BatteryChemistryMaterial, ChemistryRiskScore
from app.models.criticality_signal import MaterialCriticalitySignal
from app.models.company import (
    Company,
    CompanyAlias,
    CompanyMaterialExposure,
    CompanyScore,
    CompanySupplyRelationship,
)
from app.models.documents import DocumentChunk, SourceDocument
from app.models.facility import Facility
from app.models.ingestion import IngestionRun, RawApiPayload
from app.models.platform import Tenant, UsageEvent, User
from app.models.regulatory import (
    CompanyRegulationExposure,
    Regulation,
    RegulationGeographyScope,
    RegulationMaterialScope,
    RiskEvent,
    RiskEventCompany,
    RiskEventGeography,
    RiskEventMaterial,
    RiskEventRegulation,
)
from app.models.reporting import (
    AnalystNote,
    ReportInsight,
    ReportRun,
    ReportTemplate,
    ReportTemplateFocusEntity,
)
from app.models.scoring import GeographyScore, MaterialScore
from app.models.source import Source
from app.models.supply_chain_context import SupplyChainContext
from app.models.supply import (
    CommodityPrice,
    HsCodeMaterialMapping,
    Material,
    TradeFlow,
)

__all__ = [
    # Battery chemistry
    "BatteryChemistry",
    "BatteryChemistryMaterial",
    "ChemistryRiskScore",
    "MaterialCriticalitySignal",
    # Company layer
    "Company",
    "CompanyAlias",
    "CompanyMaterialExposure",
    "CompanyScore",
    "CompanySupplyRelationship",
    # Documents
    "DocumentChunk",
    "SourceDocument",
    # Facility
    "Facility",
    # Ingestion
    "IngestionRun",
    "RawApiPayload",
    # Platform
    "Tenant",
    "User",
    "UsageEvent",
    # Regulatory + events
    "CompanyRegulationExposure",
    "Regulation",
    "RegulationGeographyScope",
    "RegulationMaterialScope",
    "RiskEvent",
    "RiskEventCompany",
    "RiskEventGeography",
    "RiskEventMaterial",
    "RiskEventRegulation",
    # Reporting
    "AnalystNote",
    "ReportInsight",
    "ReportRun",
    "ReportTemplate",
    "ReportTemplateFocusEntity",
    # Scoring
    "GeographyScore",
    "MaterialScore",
    # Source
    "Source",
    # Domain config
    "SupplyChainContext",
    # Supply
    "CommodityPrice",
    "HsCodeMaterialMapping",
    "Material",
    "TradeFlow",
]
