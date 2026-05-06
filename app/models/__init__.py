"""ORM models — import side effects register all metadata with Base."""

from app.models.battery_chemistry import BatteryChemistry, BatteryChemistryMaterial, ChemistryRiskScore
from app.models.country import Country
from app.models.criticality_signal import MaterialCriticalitySignal
from app.models.company import (
    Company,
    CompanyAlias,
    CompanyMaterialExposure,
    CompanyScore,
    CompanySupplyRelationship,
)
from app.models.documents import DocumentChunk, SourceDocument
from app.models.intelligence import InsightPost
from app.models.facility import CompanyFacility, Facility, FacilityMaterialLink
from app.models.ingestion import IngestionRun, RawApiPayload
from app.models.platform import Tenant, UsageEvent, User
from app.models.regulatory import (
    CompanyRegulationExposure,
    Regulation,
    RegulationGeographyScope,
    RegulationMaterialScope,
    RegulationSourceAlias,
    RiskEvent,
    RiskEventCompany,
    RiskEventFacility,
    RiskEventGeography,
    RiskEventHsMapping,
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
from app.models.review import SeedReviewFinding, SeedReviewRun
from app.models.scoring import (
    HsCodeGeographyRiskScore,
    MaterialGeographyRiskScore,
    MaterialGlobalRiskScore,
)
from app.models.source import Source
from app.models.supply_chain_context import SupplyChainContext
from app.models.supply import (
    CommodityPrice,
    HsCodeMaterialMapping,
    HsCodeProductionShare,
    Material,
    MaterialProductionShare,
    TradeFlow,
)
from app.models.vehicle import CompanyVehicleModel, VehicleModelChemistry

__all__ = [
    # Battery chemistry
    "BatteryChemistry",
    "BatteryChemistryMaterial",
    "ChemistryRiskScore",
    # Country reference
    "Country",
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
    # Intelligence hub
    "InsightPost",
    # Facility
    "CompanyFacility",
    "Facility",
    "FacilityMaterialLink",
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
    "RegulationSourceAlias",
    "RiskEvent",
    "RiskEventCompany",
    "RiskEventFacility",
    "RiskEventGeography",
    "RiskEventHsMapping",
    "RiskEventMaterial",
    "RiskEventRegulation",
    # Reporting
    "AnalystNote",
    "ReportInsight",
    "ReportRun",
    "ReportTemplate",
    "ReportTemplateFocusEntity",
    # Seed review
    "SeedReviewRun",
    "SeedReviewFinding",
    # Scoring
    "HsCodeGeographyRiskScore",
    "MaterialGeographyRiskScore",
    "MaterialGlobalRiskScore",
    # Source
    "Source",
    # Domain config
    "SupplyChainContext",
    # Supply
    "CommodityPrice",
    "HsCodeMaterialMapping",
    "HsCodeProductionShare",
    "Material",
    "MaterialProductionShare",
    "TradeFlow",
    # Vehicle (chemistry mix)
    "CompanyVehicleModel",
    "VehicleModelChemistry",
]
