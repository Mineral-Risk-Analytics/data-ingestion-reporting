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

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
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
