"""Country reference table.

Single source of truth for ISO 3166-1 alpha-2 country codes and their
metadata used across the ingestion and scoring pipeline.

Why this exists
---------------
Country codes appear as bare ISO2 strings in at least eight tables
(trade_flows, material_geography_risk_scores, risk_event_geography,
regulation_geography_scope, company_material_exposures, company_facilities,
etc.) with no FK enforcement.  This table provides:

  1. A canonical place to store Comtrade numeric reporter codes — previously
     hardcoded in ``REPORTER_COUNTRIES`` and ``CONSUMER_COUNTRIES`` dicts in
     ``comtrade.py``.
  2. ``common_names`` — a JSONB array of name variants ("China",
     "People's Republic of China", "CN", …) used by ``_resolve_country()``
     in ``gta.py`` (and any future ingester that receives full country names)
     to normalise to ISO2 without maintaining per-source hardcoded maps.
  3. ``is_major_producer`` / ``is_major_consumer`` flags that replace the
     hardcoded reporter/consumer country sets in ``comtrade.py``.

Adding a new country or updating Comtrade codes no longer requires a code
change — update the seed data and re-run ``bdi-ingest seed-countries``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Country(Base):
    """ISO 3166-1 alpha-2 country / territory reference row.

    ``iso2`` is the primary key — all FK-less country_code columns in other
    tables are intended to reference this.

    ``common_names`` is a JSONB array of alternative names and abbreviations
    used by external data sources (GTA, Comtrade, IEA, etc.) to refer to this
    country.  Used by ``_resolve_country_from_db()`` in the GTA ingester to
    resolve full names to ISO2 without per-source hardcoded maps.

    ``comtrade_code`` is the UN Comtrade M49 numeric reporter code used when
    querying the Comtrade API.  ``None`` for territories not tracked by
    Comtrade (e.g. "EU" bloc identifier).

    ``is_major_producer`` flags countries that are tracked as export reporters
    in Comtrade ingestion (supply-side concentration signals).
    ``is_major_consumer`` flags countries tracked as import reporters
    (demand-side disruption signals).  Both can be true.
    """

    __tablename__ = "countries"

    iso2: Mapped[str] = mapped_column(
        String(2), primary_key=True,
        comment="ISO 3166-1 alpha-2 code, upper-case (e.g. 'CN', 'US'). "
                "Bloc identifiers like 'EU' are also allowed.",
    )
    name: Mapped[str] = mapped_column(
        String(128), nullable=False,
        comment="Official or canonical display name (e.g. 'China', 'United States').",
    )
    iso3: Mapped[Optional[str]] = mapped_column(
        String(3), nullable=True,
        comment="ISO 3166-1 alpha-3 code (e.g. 'CHN', 'USA'). NULL for bloc identifiers.",
    )
    region: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="Broad geographic region (e.g. 'Asia', 'Africa', 'Europe').",
    )
    comtrade_code: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True,
        comment="UN Comtrade M49 numeric reporter code. NULL for bloc identifiers "
                "or countries not tracked by Comtrade.",
    )
    common_names: Mapped[Optional[Any]] = mapped_column(
        JSONB, nullable=True,
        comment="Array of name variants used by external data sources to refer to "
                "this country (e.g. [\"China\", \"People's Republic of China\", \"PRC\"]). "
                "Used for name→ISO2 resolution in ingesters that receive full country names.",
    )
    detection_patterns: Mapped[Optional[Any]] = mapped_column(
        JSONB, nullable=True,
        comment="Array of {pattern, context} objects for free-text country detection. "
                "context is 'primary' (direct reference, relevance 0.9) or "
                "'mentioned' (adjectival/contextual, relevance 0.6). "
                "Used by GeographyCache in normalizers/geography_resolver.py.",
    )
    is_sanctions_risk: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="True when this country has a high concentration of sanctioned entities "
                "in OpenSanctions. Replaces the hardcoded _DEFAULT_HIGH_CONCENTRATION_GEOS "
                "list in opensanctions.py.",
    )
    is_major_producer: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="True if this country is a primary producer of battery materials — "
                "tracked as an export reporter in Comtrade ingestion runs.",
    )
    is_major_consumer: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="True if this country is a major battery/EV consumer market — "
                "tracked as an import reporter in Comtrade ingestion runs.",
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CountryGovernanceSignal(Base):
    """Step 3 (migration 050) — World Bank WGI governance scores per country.

    One row per (country_code, reference_year, source).  Powers the JRC-aligned
    governance overlay on the Geopolitical pillar's ``country_concentration``
    sub-input.  See ``alembic/versions/050_country_governance_signals.py`` for
    the full rationale and the WGI percentile-rank scale.

    Read pattern: callers want the latest available row for a given
    country with ``reference_year <= as_of_date.year``.  Annual ingestion
    accumulates history rather than replacing it.

    ``composite_pct`` is the denormalised mean of the six dimension
    percentile ranks (NULL-safe over the non-NULL subset).  This is what
    ``apply_wgi_governance_overlay`` reads — stored rather than recomputed
    so we don't pay a six-column read per overlay call.
    """

    __tablename__ = "country_governance_signals"
    __table_args__ = (
        UniqueConstraint(
            "country_code", "reference_year", "source",
            name="uq_country_governance_signal",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    reference_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="worldbank_wgi")

    voice_accountability_pct: Mapped[Optional[float]] = mapped_column(Float)
    political_stability_pct: Mapped[Optional[float]] = mapped_column(Float)
    government_effectiveness_pct: Mapped[Optional[float]] = mapped_column(Float)
    regulatory_quality_pct: Mapped[Optional[float]] = mapped_column(Float)
    rule_of_law_pct: Mapped[Optional[float]] = mapped_column(Float)
    control_of_corruption_pct: Mapped[Optional[float]] = mapped_column(Float)
    composite_pct: Mapped[Optional[float]] = mapped_column(Float)
    n_dimensions_present: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
