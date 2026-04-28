"""Add geography_compliance_weights to regulations + seed values.

Adds a JSONB column to ``regulations`` that stores per-geography compliance
risk weights (0.0–1.0) for each regulation.  The scoring engine uses this to
replace the universal 0.50 default with geography-specific values so that,
for example, UFLPA scores CN at 1.0 (targeted) while AU scores 0.10
(traceable origin, minimal risk).

Keys in the JSON object:
    ISO2 country code   — specific override for that geography
    "DEFAULT"           — fallback when no specific key matches

A NULL column value means "no curation yet" — the scorer falls back to 0.50
for all geographies, preserving backward compatibility.

Regulations seeded and their base pts in COMPLIANCE_OBLIGATIONS
(regulatory_risk.py — updated in the same PR as this migration):

    UFLPA               — 25 base pts
    EU_BATTERY_REG_2023 — 20 base pts
    CRMA_2024           — 15 base pts
    IRA_DOMESTIC        — 15 base pts
    EU_CSDDD            — 10 base pts
    EU_REACH_COBALT     —  8 base pts
    EU_CBAM             —  5 base pts
    EU_CONFLICT_MINERALS—  3 base pts
    SEC_CLIMATE_2024    —  0 base pts (stayed by court; no weights seeded)

Revision ID: 017_reg_geo_weights
Revises:     016_chem_five_pillars
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "017_reg_geo_weights"
down_revision: Union[str, None] = "016_chem_five_pillars"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "regulations",
        sa.Column(
            "geography_compliance_weights",
            JSONB,
            nullable=True,
            comment=(
                "Per-geography compliance risk weights (0.0–1.0). "
                "Keys: ISO2 country codes or 'DEFAULT'. "
                "1.0 = highest risk (targeted/non-compliant); 0.0 = exempt. "
                "NULL = use 0.50 universal default (no curation)."
            ),
        ),
    )

    # ── Seed weights for regulations in COMPLIANCE_OBLIGATIONS ────────────────

    # UFLPA — 25 base pts
    # Creates a rebuttable presumption that goods processed in CN Xinjiang
    # involve forced labour.  Natural graphite, cobalt, and lithium are the
    # primary battery-relevant commodities.  CN = 1.0 because the presumption
    # is extremely difficult to rebut for graphite (dominant Xinjiang processing
    # share).  Non-CN producing geographies face a due-diligence burden but not
    # the presumption, hence DEFAULT = 0.25.  US domestic production is exempt.
    op.execute("""
        UPDATE regulations
        SET geography_compliance_weights = '{
            "CN": 1.0,
            "US": 0.05,
            "AU": 0.10,
            "CL": 0.10,
            "CA": 0.10,
            "MG": 0.10,
            "MZ": 0.10,
            "ZM": 0.15,
            "CD": 0.20,
            "DEFAULT": 0.25
        }'::jsonb
        WHERE regulation_key = 'UFLPA'
    """)

    # IRA_DOMESTIC — 15 base pts
    # FEOC framework: CN, RU, KP, IR are designated Foreign Entities of Concern;
    # materials from these geographies disqualify battery components and critical
    # minerals from IRA incentives.  US = 0.0 (domestic sourcing earns the
    # credit by design).  FTA partners (AU, CA, CL, JP, KR) = 0.10 because
    # they qualify under critical minerals agreements.  EU = 0.30 (close partner
    # but no formal critical minerals FTA equivalency yet).
    op.execute("""
        UPDATE regulations
        SET geography_compliance_weights = '{
            "CN": 1.0,
            "RU": 1.0,
            "KP": 1.0,
            "IR": 1.0,
            "US": 0.00,
            "AU": 0.10,
            "CA": 0.10,
            "CL": 0.10,
            "JP": 0.10,
            "KR": 0.10,
            "EU": 0.30,
            "DEFAULT": 0.50
        }'::jsonb
        WHERE regulation_key = 'IRA_DOMESTIC'
    """)

    # EU_BATTERY_REG_2023 — 20 base pts
    # Supply chain due diligence (cobalt, graphite, lithium, nickel) effective
    # Aug 2025; battery passport from Feb 2026.  CN = 0.90: supply chain opacity
    # + human rights scrutiny (graphite processing, cobalt refining) makes
    # compliance documentation hardest to produce.  DRC (CD) = 0.85: artisanal
    # cobalt mining human rights exposure is the single biggest compliance risk
    # under Article 3.  EU domestic = 0.05 (already within the regulation's
    # home jurisdiction, documented supply chains).
    op.execute("""
        UPDATE regulations
        SET geography_compliance_weights = '{
            "CN": 0.90,
            "RU": 0.90,
            "CD": 0.85,
            "IN": 0.55,
            "ZM": 0.45,
            "ZA": 0.30,
            "CL": 0.20,
            "AU": 0.15,
            "CA": 0.15,
            "EU": 0.05,
            "DEFAULT": 0.45
        }'::jsonb
        WHERE regulation_key = 'EU_BATTERY_REG_2023'
    """)

    # CRMA_2024 — 15 base pts
    # Sets a 65% single-country cap on strategic raw material supply.  CN
    # dominates natural graphite (>70%), is a major refiner for cobalt/nickel,
    # and is a significant lithium processor — triggering the CRMA's
    # concentration concern across multiple battery materials.  RU = 0.75:
    # strategic concern plus sanctions complexity.  EU = 0.05: the regulation
    # is designed to build EU supply resilience, so EU domestic sourcing is the
    # explicitly desired outcome.
    op.execute("""
        UPDATE regulations
        SET geography_compliance_weights = '{
            "CN": 0.90,
            "RU": 0.75,
            "ZA": 0.25,
            "CL": 0.20,
            "AU": 0.15,
            "CA": 0.15,
            "EU": 0.05,
            "DEFAULT": 0.40
        }'::jsonb
        WHERE regulation_key = 'CRMA_2024'
    """)

    # EU_CSDDD — 10 base pts
    # Corporate Sustainability Due Diligence Directive: requires large companies
    # to conduct human rights and environmental due diligence across their full
    # value chains.  Effective July 2027 for the largest companies.  Broadly
    # overlaps EU_BATTERY_REG_2023 for battery materials but is not material-
    # specific — it captures the entire upstream supply chain.  CD = 0.90 and
    # MM = 0.90 are the primary battery-adjacent concerns (DRC artisanal cobalt,
    # Myanmar forced labour under military junta).  Weights kept slightly lower
    # than the Battery Reg on non-conflict geographies because the Battery Reg
    # already captures the same documentation burden for battery materials.
    op.execute("""
        UPDATE regulations
        SET geography_compliance_weights = '{
            "CD": 0.90,
            "MM": 0.90,
            "CN": 0.85,
            "RU": 0.80,
            "IN": 0.55,
            "ZM": 0.50,
            "ZA": 0.35,
            "CL": 0.20,
            "AU": 0.15,
            "CA": 0.15,
            "EU": 0.05,
            "DEFAULT": 0.45
        }'::jsonb
        WHERE regulation_key = 'EU_CSDDD'
    """)

    # EU_REACH_COBALT — 8 base pts
    # REACH Regulation: cobalt compounds are listed as Substances of Very High
    # Concern (SVHC).  Creates authorization requirements and substitution
    # pressure for EU manufacturers using cobalt.  Relevant only when material
    # is cobalt — RegulationMaterialScope should link this to cobalt only.
    # CD = 0.90: primary cobalt source with artisanal mining SVHC exposure.
    # CN = 0.75: dominant cobalt refiner — REACH documentation complex for
    # CN-processed cobalt entering EU supply chains.
    # EU = 0.15 (not 0.05): unlike geographic origin regulations, REACH
    # compliance burden applies to EU processors using the SVHC, not just
    # importers from outside.
    op.execute("""
        UPDATE regulations
        SET geography_compliance_weights = '{
            "CD": 0.90,
            "CN": 0.75,
            "ZM": 0.65,
            "PH": 0.50,
            "ZA": 0.40,
            "AU": 0.20,
            "CA": 0.15,
            "EU": 0.15,
            "DEFAULT": 0.40
        }'::jsonb
        WHERE regulation_key = 'EU_REACH_COBALT'
    """)

    # EU_CBAM — 5 base pts
    # Carbon Border Adjustment Mechanism: full certificate requirement from
    # January 2026 for iron/steel, aluminium, copper, cement, fertilisers,
    # electricity, hydrogen.  Battery-direct relevance is limited to copper
    # (busbars, wiring) and aluminium (packaging) — cathode materials are not
    # in scope.  RU = 0.85: high carbon intensity combined with sanctions makes
    # CBAM certificate production operationally near-impossible.  EU = 0.00:
    # CBAM does not apply to domestic EU production by design.
    op.execute("""
        UPDATE regulations
        SET geography_compliance_weights = '{
            "RU": 0.85,
            "CN": 0.80,
            "IN": 0.65,
            "ZA": 0.55,
            "UA": 0.35,
            "AU": 0.20,
            "CA": 0.15,
            "US": 0.15,
            "EU": 0.00,
            "DEFAULT": 0.40
        }'::jsonb
        WHERE regulation_key = 'EU_CBAM'
    """)

    # EU_CONFLICT_MINERALS — 3 base pts
    # EU 2017/821: responsible sourcing of tin, tantalum, tungsten, and gold
    # (3TG) from conflict-affected and high-risk areas.  Battery cathode
    # materials (Li, Co, Ni, graphite) are NOT in scope — battery relevance is
    # limited to 3TG used in BMS electronics (tin solder, tantalum capacitors).
    # Low base pts reflect this indirect exposure.  Only appears in scoring when
    # RegulationMaterialScope links it to a covered material, or via geography
    # scope for conflict-affected geographies.  CD = 1.0 (DRC is the primary
    # target of this regulation).
    op.execute("""
        UPDATE regulations
        SET geography_compliance_weights = '{
            "CD": 1.00,
            "CF": 0.95,
            "SS": 0.85,
            "SD": 0.85,
            "ZW": 0.75,
            "RW": 0.55,
            "UG": 0.55,
            "CN": 0.25,
            "EU": 0.05,
            "DEFAULT": 0.15
        }'::jsonb
        WHERE regulation_key = 'EU_CONFLICT_MINERALS'
    """)

    # SEC_CLIMATE_2024 — 0 base pts (no weights seeded)
    # Stayed by federal court as of early 2025.  A stayed disclosure rule that
    # does not prohibit sourcing from any geography does not warrant geography
    # risk weights.  Revisit if the legal challenge resolves in the SEC's favour.


def downgrade() -> None:
    op.drop_column("regulations", "geography_compliance_weights")
