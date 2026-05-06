"""Per-regulation review configuration for the regulations reviewer.

Maps each seeded ``regulation_key`` to three signal sources:

- ``fr_query_names``: values of ``source_documents.metadata_json->>'query_name'``
  that correlate with this regulation. These are set by
  :mod:`app.services.ingestion.ingest_federal_register`. Matching these is the
  strongest signal because the ingestion pipeline already classified the
  document into the same topic bucket as the regulation.
- ``keywords``: case-insensitive substrings scanned against the document title
  and ``raw_text``. Useful for pulling in documents that didn't match a
  ``query_name`` but clearly discuss the regulation's subject matter.
- ``agency_hints``: issuing agency names compared against
  ``metadata_json->>'agencies'``. Establishes that the regulator responsible
  for this rule has spoken.

Regulations without an entry here are skipped with a log warning; add a new
entry when a new seeded regulation is introduced.

Kept in sync with:

- :mod:`app.services.ingestion.seed_regulations` (``regulation_key`` values)
- :mod:`app.services.ingestion.ingest_federal_register` (``QueryConfig.name``)
"""

from __future__ import annotations

from typing import TypedDict


class RegulationReviewConfig(TypedDict, total=False):
    fr_query_names: list[str]
    keywords: list[str]
    agency_hints: list[str]


REGULATION_REVIEW_CONFIG: dict[str, RegulationReviewConfig] = {
    "UFLPA": {
        "fr_query_names": ["uflpa", "export_control"],
        "keywords": [
            "UFLPA",
            "Uyghur",
            "forced labor",
            "Xinjiang",
            "withhold release order",
            "entity list",
        ],
        "agency_hints": [
            "Homeland Security",
            "Customs and Border Protection",
            "CBP",
            "Forced Labor Enforcement Task Force",
        ],
    },
    "IRA_DOMESTIC": {
        "fr_query_names": ["critical_minerals", "lithium_battery", "uflpa"],
        "keywords": [
            "FEOC",
            "foreign entity of concern",
            "clean vehicle credit",
            "domestic content",
            "45X",
            "30D",
            "advanced manufacturing production credit",
            "battery component",
            "critical mineral",
            "Inflation Reduction Act",
        ],
        "agency_hints": [
            "Treasury",
            "Internal Revenue Service",
            "IRS",
            "Energy",
            "Department of Energy",
        ],
    },
    "EU_BATTERY_REG_2023": {
        "fr_query_names": ["critical_minerals", "lithium_battery"],
        "keywords": [
            "EU Battery Regulation",
            "battery passport",
            "carbon footprint declaration",
            "due diligence",
            "extended producer responsibility",
            "Regulation (EU) 2023/1542",
        ],
        "agency_hints": [
            "European Commission",
            "European Union",
            "EU",
        ],
    },
    "CRMA_2024": {
        "fr_query_names": ["critical_minerals"],
        "keywords": [
            "Critical Raw Materials",
            "CRMA",
            "strategic projects",
            "Critical Raw Materials Act",
        ],
        "agency_hints": [
            "European Commission",
            "European Union",
        ],
    },
    "EU_CBAM": {
        "fr_query_names": ["tariff", "critical_minerals"],
        "keywords": [
            "CBAM",
            "Carbon Border Adjustment Mechanism",
            "embedded emissions",
            "definitive period",
        ],
        "agency_hints": [
            "European Commission",
            "European Union",
        ],
    },
    "SEC_CLIMATE_2024": {
        "fr_query_names": [],
        "keywords": [
            "climate-related disclosures",
            "Scope 1",
            "Scope 2",
            "Scope 3",
            "material climate risk",
        ],
        "agency_hints": [
            "Securities and Exchange Commission",
            "SEC",
        ],
    },
}


def get_config(regulation_key: str) -> RegulationReviewConfig | None:
    """Return the review config for a regulation_key, or None if not configured."""
    return REGULATION_REVIEW_CONFIG.get(regulation_key)
