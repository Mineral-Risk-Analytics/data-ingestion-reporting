"""Curated buyer-supplier relationships across the EV battery supply chain.

Three tiers are covered:
1) OEM -> cell maker
2) Cell maker -> refiner / CAM supplier
3) Refiner / CAM supplier -> miner

relationship_type values:
- "direct": publicly confirmed via press release, JV filing, or earnings disclosure
- "estimated": well-known in industry but not formally announced

volume_share_pct is the approximate fraction of the buyer's demand for that
material/cell supplied by this specific supplier. NULL means unknown.

data_confidence reflects certainty in relationship existence, not volume accuracy.

Run order:
    bdi-ingest seed-companies
    bdi-ingest seed-supply-relationships
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company, CompanySupplyRelationship
from app.models.supply import Material

log = structlog.get_logger(__name__)


def _d(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


_RELATIONSHIPS: list[dict] = [
    # ── TIER 1: OEM -> CELL MAKER ─────────────────────────────────────────
    {"buyer_canonical_name": "Tesla", "supplier_canonical_name": "Panasonic Energy", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.95, "volume_share_pct": 0.30, "valid_from": "2014-01-01", "valid_to": None},
    {"buyer_canonical_name": "Tesla", "supplier_canonical_name": "CATL", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.90, "volume_share_pct": 0.40, "valid_from": "2020-01-01", "valid_to": None},
    {"buyer_canonical_name": "Tesla", "supplier_canonical_name": "LG Energy Solution", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.85, "volume_share_pct": 0.30, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "BMW Group", "supplier_canonical_name": "Samsung SDI", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.95, "volume_share_pct": 0.45, "valid_from": "2015-01-01", "valid_to": None},
    {"buyer_canonical_name": "BMW Group", "supplier_canonical_name": "CATL", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.90, "volume_share_pct": 0.40, "valid_from": "2018-01-01", "valid_to": None},
    {"buyer_canonical_name": "BMW Group", "supplier_canonical_name": "Northvolt", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.70, "volume_share_pct": 0.15, "valid_from": "2020-01-01", "valid_to": "2024-11-01"},
    {"buyer_canonical_name": "Volkswagen Group", "supplier_canonical_name": "CATL", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.90, "volume_share_pct": 0.40, "valid_from": "2019-01-01", "valid_to": None},
    {"buyer_canonical_name": "Volkswagen Group", "supplier_canonical_name": "LG Energy Solution", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.85, "volume_share_pct": 0.30, "valid_from": "2019-01-01", "valid_to": None},
    {"buyer_canonical_name": "Volkswagen Group", "supplier_canonical_name": "Samsung SDI", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.85, "volume_share_pct": 0.20, "valid_from": "2019-01-01", "valid_to": None},
    {"buyer_canonical_name": "Volkswagen Group", "supplier_canonical_name": "SK On", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.10, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "General Motors", "supplier_canonical_name": "LG Energy Solution", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.95, "volume_share_pct": 0.90, "valid_from": "2020-01-01", "valid_to": None},
    {"buyer_canonical_name": "General Motors", "supplier_canonical_name": "CATL", "material_canonical_name": None, "relationship_type": "estimated", "data_confidence": 0.60, "volume_share_pct": None, "valid_from": "2023-01-01", "valid_to": None},
    {"buyer_canonical_name": "Ford Motor Company", "supplier_canonical_name": "SK On", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.95, "volume_share_pct": 0.75, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "Ford Motor Company", "supplier_canonical_name": "CATL", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.70, "volume_share_pct": 0.25, "valid_from": "2023-01-01", "valid_to": None},
    {"buyer_canonical_name": "Hyundai Motor Company", "supplier_canonical_name": "SK On", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.90, "volume_share_pct": 0.45, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "Hyundai Motor Company", "supplier_canonical_name": "LG Energy Solution", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.85, "volume_share_pct": 0.35, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "Hyundai Motor Company", "supplier_canonical_name": "Samsung SDI", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.20, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "Kia Corporation", "supplier_canonical_name": "SK On", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.85, "volume_share_pct": 0.50, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "Kia Corporation", "supplier_canonical_name": "LG Energy Solution", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.30, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "Kia Corporation", "supplier_canonical_name": "Samsung SDI", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.20, "valid_from": "2022-01-01", "valid_to": None},
    {"buyer_canonical_name": "Stellantis", "supplier_canonical_name": "Samsung SDI", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.95, "volume_share_pct": 0.60, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "Stellantis", "supplier_canonical_name": "CATL", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.25, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "Stellantis", "supplier_canonical_name": "LG Energy Solution", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.15, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "Mercedes-Benz Group", "supplier_canonical_name": "CATL", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.90, "volume_share_pct": 0.50, "valid_from": "2019-01-01", "valid_to": None},
    {"buyer_canonical_name": "Mercedes-Benz Group", "supplier_canonical_name": "Samsung SDI", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.25, "valid_from": "2019-01-01", "valid_to": None},
    {"buyer_canonical_name": "Rivian Automotive", "supplier_canonical_name": "Samsung SDI", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.90, "volume_share_pct": 0.95, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "Toyota Motor Corporation", "supplier_canonical_name": "Prime Planet and Energy Solutions", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.95, "volume_share_pct": 0.70, "valid_from": "2020-01-01", "valid_to": None},
    {"buyer_canonical_name": "Toyota Motor Corporation", "supplier_canonical_name": "CATL", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.20, "valid_from": "2022-01-01", "valid_to": None},
    {"buyer_canonical_name": "Toyota Motor Corporation", "supplier_canonical_name": "Panasonic Energy", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.10, "valid_from": "2014-01-01", "valid_to": None},
    {"buyer_canonical_name": "Honda Motor Company", "supplier_canonical_name": "LG Energy Solution", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.85, "volume_share_pct": 0.70, "valid_from": "2022-01-01", "valid_to": None},
    {"buyer_canonical_name": "Honda Motor Company", "supplier_canonical_name": "CATL", "material_canonical_name": None, "relationship_type": "estimated", "data_confidence": 0.65, "volume_share_pct": None, "valid_from": "2023-01-01", "valid_to": None},
    {"buyer_canonical_name": "Nissan Motor Company", "supplier_canonical_name": "Envision AESC", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.90, "volume_share_pct": 0.85, "valid_from": "2010-01-01", "valid_to": None},
    {"buyer_canonical_name": "Nissan Motor Company", "supplier_canonical_name": "CATL", "material_canonical_name": None, "relationship_type": "estimated", "data_confidence": 0.65, "volume_share_pct": None, "valid_from": "2023-01-01", "valid_to": None},
    {"buyer_canonical_name": "Volvo Car Group", "supplier_canonical_name": "CATL", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.90, "volume_share_pct": 0.80, "valid_from": "2019-01-01", "valid_to": None},
    {"buyer_canonical_name": "Volvo Car Group", "supplier_canonical_name": "LG Energy Solution", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.20, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "Polestar Automotive", "supplier_canonical_name": "CATL", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.85, "volume_share_pct": 0.90, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "Lucid Group", "supplier_canonical_name": "Samsung SDI", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.85, "volume_share_pct": 0.95, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "Subaru Corporation", "supplier_canonical_name": "Panasonic Energy", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.75, "valid_from": "2022-01-01", "valid_to": None},
    {"buyer_canonical_name": "Subaru Corporation", "supplier_canonical_name": "Samsung SDI", "material_canonical_name": None, "relationship_type": "estimated", "data_confidence": 0.65, "volume_share_pct": None, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "BYD", "supplier_canonical_name": "FinDreams Battery", "material_canonical_name": None, "relationship_type": "direct", "data_confidence": 0.95, "volume_share_pct": 0.95, "valid_from": "2019-01-01", "valid_to": None},

    # ── TIER 2: CELL MAKER -> MATERIAL SUPPLIER ───────────────────────────
    {"buyer_canonical_name": "CATL", "supplier_canonical_name": "SQM", "material_canonical_name": "Lithium", "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.20, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "CATL", "supplier_canonical_name": "Ganfeng Lithium", "material_canonical_name": "Lithium", "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.25, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "CATL", "supplier_canonical_name": "Albemarle Corporation", "material_canonical_name": "Lithium", "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.15, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "LG Energy Solution", "supplier_canonical_name": "Ganfeng Lithium", "material_canonical_name": "Lithium", "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.30, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "LG Energy Solution", "supplier_canonical_name": "SQM", "material_canonical_name": "Lithium", "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.25, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "Samsung SDI", "supplier_canonical_name": "Ganfeng Lithium", "material_canonical_name": "Lithium", "relationship_type": "estimated", "data_confidence": 0.70, "volume_share_pct": None, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "Samsung SDI", "supplier_canonical_name": "SQM", "material_canonical_name": "Lithium", "relationship_type": "estimated", "data_confidence": 0.70, "volume_share_pct": None, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "SK On", "supplier_canonical_name": "SQM", "material_canonical_name": "Lithium", "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.25, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "SK On", "supplier_canonical_name": "Ganfeng Lithium", "material_canonical_name": "Lithium", "relationship_type": "estimated", "data_confidence": 0.65, "volume_share_pct": None, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "Panasonic Energy", "supplier_canonical_name": "Albemarle Corporation", "material_canonical_name": "Lithium", "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.40, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "FinDreams Battery", "supplier_canonical_name": "SQM", "material_canonical_name": "Lithium", "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.25, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "FinDreams Battery", "supplier_canonical_name": "Ganfeng Lithium", "material_canonical_name": "Lithium", "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.20, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "CATL", "supplier_canonical_name": "Huayou Cobalt", "material_canonical_name": "Cobalt", "relationship_type": "direct", "data_confidence": 0.85, "volume_share_pct": 0.30, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "CATL", "supplier_canonical_name": "Glencore", "material_canonical_name": "Cobalt", "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.20, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "LG Energy Solution", "supplier_canonical_name": "Umicore", "material_canonical_name": "Cobalt", "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.25, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "LG Energy Solution", "supplier_canonical_name": "Huayou Cobalt", "material_canonical_name": "Cobalt", "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.20, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "Samsung SDI", "supplier_canonical_name": "Umicore", "material_canonical_name": "Cobalt", "relationship_type": "estimated", "data_confidence": 0.70, "volume_share_pct": None, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "Panasonic Energy", "supplier_canonical_name": "Sumitomo Metal Mining", "material_canonical_name": "Cobalt", "relationship_type": "direct", "data_confidence": 0.85, "volume_share_pct": 0.60, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "Panasonic Energy", "supplier_canonical_name": "Sumitomo Metal Mining", "material_canonical_name": "Nickel", "relationship_type": "direct", "data_confidence": 0.90, "volume_share_pct": 0.70, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "LG Energy Solution", "supplier_canonical_name": "Umicore", "material_canonical_name": "Nickel", "relationship_type": "direct", "data_confidence": 0.85, "volume_share_pct": 0.30, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "LG Energy Solution", "supplier_canonical_name": "Ecopro BM", "material_canonical_name": "Nickel", "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.35, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "LG Energy Solution", "supplier_canonical_name": "POSCO Future M", "material_canonical_name": "Nickel", "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.25, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "Samsung SDI", "supplier_canonical_name": "Ecopro BM", "material_canonical_name": "Nickel", "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.40, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "Samsung SDI", "supplier_canonical_name": "POSCO Future M", "material_canonical_name": "Nickel", "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.35, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "SK On", "supplier_canonical_name": "POSCO Future M", "material_canonical_name": "Nickel", "relationship_type": "direct", "data_confidence": 0.85, "volume_share_pct": 0.45, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "SK On", "supplier_canonical_name": "Ecopro BM", "material_canonical_name": "Nickel", "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.35, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "CATL", "supplier_canonical_name": "BTR New Energy", "material_canonical_name": "Natural Graphite", "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.35, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "CATL", "supplier_canonical_name": "ShanShan Corporation", "material_canonical_name": "Natural Graphite", "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.20, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "CATL", "supplier_canonical_name": "POSCO Future M", "material_canonical_name": "Natural Graphite", "relationship_type": "direct", "data_confidence": 0.70, "volume_share_pct": 0.15, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "LG Energy Solution", "supplier_canonical_name": "BTR New Energy", "material_canonical_name": "Natural Graphite", "relationship_type": "estimated", "data_confidence": 0.65, "volume_share_pct": None, "valid_from": None, "valid_to": None},

    # ── TIER 3: REFINER / CAM SUPPLIER -> MINER ───────────────────────────
    {"buyer_canonical_name": "POSCO Future M", "supplier_canonical_name": "Pilbara Minerals", "material_canonical_name": "Lithium", "relationship_type": "direct", "data_confidence": 0.90, "volume_share_pct": 0.30, "valid_from": "2021-01-01", "valid_to": None},
    {"buyer_canonical_name": "Ganfeng Lithium", "supplier_canonical_name": "Pilbara Minerals", "material_canonical_name": "Lithium", "relationship_type": "direct", "data_confidence": 0.85, "volume_share_pct": 0.25, "valid_from": "2020-01-01", "valid_to": None},
    {"buyer_canonical_name": "Tianqi Lithium", "supplier_canonical_name": "Albemarle Corporation", "material_canonical_name": "Lithium", "relationship_type": "direct", "data_confidence": 0.90, "volume_share_pct": 0.50, "valid_from": "2014-01-01", "valid_to": None},
    {"buyer_canonical_name": "Ganfeng Lithium", "supplier_canonical_name": "SQM", "material_canonical_name": "Lithium", "relationship_type": "estimated", "data_confidence": 0.65, "volume_share_pct": None, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "Umicore", "supplier_canonical_name": "Glencore", "material_canonical_name": "Cobalt", "relationship_type": "direct", "data_confidence": 0.85, "volume_share_pct": 0.40, "valid_from": "2018-01-01", "valid_to": None},
    {"buyer_canonical_name": "Umicore", "supplier_canonical_name": "Huayou Cobalt", "material_canonical_name": "Cobalt", "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.20, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "Ecopro BM", "supplier_canonical_name": "Huayou Cobalt", "material_canonical_name": "Cobalt", "relationship_type": "direct", "data_confidence": 0.75, "volume_share_pct": 0.25, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "POSCO Future M", "supplier_canonical_name": "Huayou Cobalt", "material_canonical_name": "Cobalt", "relationship_type": "estimated", "data_confidence": 0.65, "volume_share_pct": None, "valid_from": None, "valid_to": None},
    {"buyer_canonical_name": "Sumitomo Metal Mining", "supplier_canonical_name": "Vale Base Metals", "material_canonical_name": "Nickel", "relationship_type": "direct", "data_confidence": 0.80, "volume_share_pct": 0.35, "valid_from": "2010-01-01", "valid_to": None},
    {"buyer_canonical_name": "Umicore", "supplier_canonical_name": "Norilsk Nickel", "material_canonical_name": "Nickel", "relationship_type": "estimated", "data_confidence": 0.65, "volume_share_pct": None, "valid_from": None, "valid_to": None},
]


def seed_supply_relationships(session: Session) -> dict[str, int]:
    """Insert curated buyer-supplier relationships. Idempotent."""
    inserted = 0
    skipped = 0
    companies_not_found = 0
    materials_not_found = 0

    company_map: dict[str, uuid.UUID] = {
        c.canonical_name: c.id
        for c in session.scalars(select(Company))
    }
    material_map: dict[str, int] = {
        m.canonical_name: m.id
        for m in session.scalars(select(Material))
    }

    for entry in _RELATIONSHIPS:
        buyer_name = entry["buyer_canonical_name"]
        supplier_name = entry["supplier_canonical_name"]
        material_name = entry["material_canonical_name"]

        buyer_id = company_map.get(buyer_name)
        supplier_id = company_map.get(supplier_name)
        if buyer_id is None or supplier_id is None:
            log.warning(
                "seed_supply_rel.company_not_found",
                buyer_canonical_name=buyer_name,
                supplier_canonical_name=supplier_name,
            )
            companies_not_found += 1
            continue

        material_id: Optional[int] = None
        if material_name is not None:
            material_id = material_map.get(material_name)
            if material_id is None:
                log.warning(
                    "seed_supply_rel.material_not_found",
                    buyer_canonical_name=buyer_name,
                    supplier_canonical_name=supplier_name,
                    material_canonical_name=material_name,
                )
                materials_not_found += 1
                continue

        valid_from = _d(entry["valid_from"])
        valid_to = _d(entry["valid_to"])

        if material_id is None:
            existing_id = session.scalar(
                select(CompanySupplyRelationship.id).where(
                    CompanySupplyRelationship.buyer_id == buyer_id,
                    CompanySupplyRelationship.supplier_id == supplier_id,
                    CompanySupplyRelationship.material_id.is_(None),
                )
            )
        else:
            existing_id = session.scalar(
                select(CompanySupplyRelationship.id).where(
                    CompanySupplyRelationship.buyer_id == buyer_id,
                    CompanySupplyRelationship.supplier_id == supplier_id,
                    CompanySupplyRelationship.material_id == material_id,
                )
            )

        if existing_id is not None:
            log.debug(
                "seed_supply_rel.skip_existing",
                buyer_canonical_name=buyer_name,
                supplier_canonical_name=supplier_name,
                material_canonical_name=material_name,
            )
            skipped += 1
            continue

        session.add(
            CompanySupplyRelationship(
                buyer_id=buyer_id,
                supplier_id=supplier_id,
                material_id=material_id,
                relationship_type=entry["relationship_type"],
                data_confidence=entry["data_confidence"],
                volume_share_pct=entry["volume_share_pct"],
                valid_from=valid_from,
                valid_to=valid_to,
            )
        )
        inserted += 1
        log.info(
            "seed_supply_rel.inserted",
            buyer_canonical_name=buyer_name,
            supplier_canonical_name=supplier_name,
            material_canonical_name=material_name,
        )

    session.commit()
    log.info(
        "seed_supply_rel.done",
        inserted=inserted,
        skipped=skipped,
        companies_not_found=companies_not_found,
        materials_not_found=materials_not_found,
    )
    return {
        "inserted": inserted,
        "skipped": skipped,
        "companies_not_found": companies_not_found,
        "materials_not_found": materials_not_found,
    }
