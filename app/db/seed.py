"""Reference data and default `sources` rows (run via `python -m app.cli seed`)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Source
from app.models.enums import ImplementationPhase, SourceType


def seed_if_empty(session: Session) -> dict[str, int]:
    """Idempotent seed: only inserts when tables are empty."""
    stats: dict[str, int] = {}

    # Materials are seeded via: uv run bdi-ingest ingest-usgs <path/to/MCS2025_World_Data.csv>
    # Companies are seeded via: bdi-ingest seed-companies
    # Countries are seeded via: bdi-ingest seed-countries
    # Do not seed materials, companies, or countries here.

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
                        "time": "latest",
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
