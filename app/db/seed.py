"""Reference data and default `sources` rows (run via `python -m app.cli seed`)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Country, Material, Source
from app.models.company import Company, CompanyAlias
from app.models.enums import ImplementationPhase, SourceType, SupplyChainStage


def seed_if_empty(session: Session) -> dict[str, int]:
    """Idempotent seed: only inserts when tables are empty."""
    stats: dict[str, int] = {}

    if session.scalar(select(Country).limit(1)) is None:
        session.add_all(
            [
                Country(iso2="US", iso3="USA", name="United States", region="north_america"),
                Country(iso2="CA", iso3="CAN", name="Canada", region="north_america"),
                Country(iso2="CN", iso3="CHN", name="China", region="asia_pacific"),
                Country(iso2="KR", iso3="KOR", name="South Korea", region="asia_pacific"),
            ]
        )
        stats["countries"] = 4

    if session.scalar(select(Material).limit(1)) is None:
        session.add_all(
            [
                Material(
                    canonical_name="Lithium chemicals",
                    category="critical_mineral",
                    symbol_or_code="Li",
                    notes="Upstream lithium salts / hydroxide",
                ),
                Material(
                    canonical_name="Lithium-ion battery cells",
                    category="component",
                    symbol_or_code="LIB",
                    notes="HS-like grouping for cells/packs",
                ),
                Material(
                    canonical_name="Rare earth compounds",
                    category="critical_mineral",
                    notes="Separators / magnets exposure",
                ),
            ]
        )
        stats["materials"] = 3

    if session.scalar(select(Company).limit(1)) is None:
        tesla = Company(
            canonical_name="Tesla Inc.",
            supply_chain_stage=SupplyChainStage.OEM.value,
            headquarters_country="US",
            public_ticker="TSLA",
            is_public=True,
            notes="Seed OEM for SEC demo CIK",
        )
        alb = Company(
            canonical_name="Albemarle Corporation",
            supply_chain_stage=SupplyChainStage.MINER.value,
            headquarters_country="US",
            public_ticker="ALB",
            is_public=True,
            notes="Lithium producer (alias demo)",
        )
        session.add_all([tesla, alb])
        session.flush()
        session.add_all(
            [
                CompanyAlias(company_id=tesla.id, alias="Tesla Motors Inc", alias_type="aka"),
                CompanyAlias(company_id=alb.id, alias="Albemarle Corp.", alias_type="legal"),
            ]
        )
        stats["companies"] = 2
        stats["company_aliases"] = 2

    if session.scalar(select(Source).limit(1)) is None:
        session.add_all(
            [
                Source(
                    name="Federal Register — battery search",
                    source_type=SourceType.FEDERAL_REGISTER.value,
                    phase=ImplementationPhase.PHASE_1.value,
                    is_active=True,
                    config_json={"term": "battery OR lithium", "per_page": 10},
                ),
                Source(
                    name="U.S. Census — HS imports",
                    source_type=SourceType.CENSUS_TRADE.value,
                    phase=ImplementationPhase.PHASE_1.value,
                    is_active=True,
                    config_json={
                        "dataset_path": "imports/hs",
                        "time": "2024-11",
                        "get": "CTY_CODE,CTY_NAME,I_COMMODITY,I_COMMODITY_LDESC,GEN_VAL_MO",
                    },
                ),
                Source(
                    name="SEC EDGAR — Tesla submissions",
                    source_type=SourceType.SEC_EDGAR.value,
                    phase=ImplementationPhase.PHASE_1.value,
                    is_active=True,
                    config_json={"ciks": ["0001318605"], "max_filings": 8},
                ),
                Source(
                    name="News — stub provider",
                    source_type=SourceType.NEWS.value,
                    phase=ImplementationPhase.PHASE_1.value,
                    is_active=True,
                    config_json={"topic": "EV battery"},
                ),
                Source(
                    name="OEM sustainability reports",
                    source_type=SourceType.SUSTAINABILITY_REPORT.value,
                    phase=ImplementationPhase.PHASE_2.value,
                    is_active=False,
                    config_json={},
                ),
                Source(
                    name="US / EPA / NHTSA policy pages",
                    source_type=SourceType.POLICY_PAGE.value,
                    phase=ImplementationPhase.PHASE_2.value,
                    is_active=False,
                    config_json={},
                ),
                Source(
                    name="IEA / NGO reports",
                    source_type=SourceType.NGO_REPORT.value,
                    phase=ImplementationPhase.PHASE_2.value,
                    is_active=False,
                    config_json={},
                ),
                Source(
                    name="NREL / AFDC charging",
                    source_type=SourceType.NREL_CHARGING.value,
                    phase=ImplementationPhase.PHASE_3.value,
                    is_active=False,
                    config_json={},
                ),
                Source(
                    name="USITC tariff enrichment",
                    source_type=SourceType.USITC.value,
                    phase=ImplementationPhase.PHASE_3.value,
                    is_active=False,
                    config_json={},
                ),
                Source(
                    name="Canada battery policy",
                    source_type=SourceType.CANADA_POLICY.value,
                    phase=ImplementationPhase.PHASE_3.value,
                    is_active=False,
                    config_json={},
                ),
            ]
        )
        stats["sources"] = 10

    session.commit()
    return stats
