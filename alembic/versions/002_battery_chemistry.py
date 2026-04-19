"""Battery chemistry risk layer: new tables and materials column additions.

Adds:
  - materials.patent_occurrence_trend  (denormalized cache from material_criticality_signals)
  - materials.data_availability        (commercial | limited | no_benchmark)
  - battery_chemistries                (NMC, LFP, NCA, LFMP, sodium_ion, solid_state)
  - battery_chemistry_materials        (versioned junction with valid_from/valid_to)
  - material_criticality_signals       (timeseries from usgs_mcs / eu_crma / iea_report / patstat / manual)
  - chemistry_risk_scores              (pre-computed per-chemistry risk scores with confidence)

Junction data (battery_chemistry_materials rows) is seeded separately by the
``seed-materials`` CLI command, which runs after materials have been loaded
by ``ingest-usgs``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002_battery_chemistry"
down_revision: Union[str, None] = "001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # -------------------------------------------------------------------------
    # 1. Add new columns to materials
    # -------------------------------------------------------------------------
    op.add_column(
        "materials",
        sa.Column(
            "patent_occurrence_trend",
            sa.String(16),
            nullable=True,
            comment="rising | declining | stable. DENORMALIZED CACHE — authoritative "
                    "source is material_criticality_signals. Refreshed by "
                    "_sync_patent_trend() after any signal write.",
        ),
    )
    op.add_column(
        "materials",
        sa.Column(
            "data_availability",
            sa.String(32),
            nullable=True,
            comment="commercial | limited | no_benchmark. Used by chemistry_risk.py "
                    "to compute score_confidence.",
        ),
    )

    # -------------------------------------------------------------------------
    # 2. battery_chemistries
    # -------------------------------------------------------------------------
    op.create_table(
        "battery_chemistries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            comment="commercial | emerging | research",
        ),
        sa.Column(
            "current_market_share_pct",
            sa.Float(),
            nullable=True,
            comment="Approximate global market share 0.0–1.0. See market_share_as_of_date.",
        ),
        sa.Column(
            "market_share_as_of_date",
            sa.Date(),
            nullable=True,
            comment="Date the current_market_share_pct estimate reflects. "
                    "Required when current_market_share_pct is set — without it "
                    "the figure is uninterpretable as market share shifts rapidly.",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("slug", name="uq_battery_chemistry_slug"),
    )
    op.create_index("ix_battery_chemistry_slug", "battery_chemistries", ["slug"], unique=True)

    # -------------------------------------------------------------------------
    # 3. battery_chemistry_materials (versioned junction)
    # -------------------------------------------------------------------------
    op.create_table(
        "battery_chemistry_materials",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "battery_chemistry_id",
            sa.Integer(),
            sa.ForeignKey("battery_chemistries.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "material_id",
            sa.Integer(),
            sa.ForeignKey("materials.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "role",
            sa.String(64),
            nullable=False,
            comment="cathode_active | anode | electrolyte | current_collector | other",
        ),
        sa.Column(
            "intensity",
            sa.Float(),
            nullable=False,
            comment="0–1: relative material intensity in this chemistry. "
                    "Higher = more critical to cell performance.",
        ),
        sa.Column(
            "is_substitutable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "valid_from",
            sa.Date(),
            nullable=False,
            comment="Date this intensity value became applicable. "
                    "Required for score auditability as chemistry compositions evolve "
                    "(e.g. NMC nickel content migrating from 0.6→0.8).",
        ),
        sa.Column(
            "valid_to",
            sa.Date(),
            nullable=True,
            comment="NULL means currently active. "
                    "Point-in-time query: valid_from <= as_of <= COALESCE(valid_to, 'infinity').",
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "battery_chemistry_id", "material_id", "role", "valid_from",
            name="uq_chem_material_role_date",
        ),
    )

    # -------------------------------------------------------------------------
    # 4. material_criticality_signals (authoritative timeseries)
    # -------------------------------------------------------------------------
    op.create_table(
        "material_criticality_signals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "material_id",
            sa.Integer(),
            sa.ForeignKey("materials.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            comment="usgs_mcs | eu_crma | iea_report | patstat | manual",
        ),
        sa.Column(
            "reference_year",
            sa.Integer(),
            nullable=False,
            comment="Calendar year this signal reflects (e.g. 2025 for MCS 2025).",
        ),
        sa.Column(
            "criticality_score",
            sa.Float(),
            nullable=True,
            comment="Normalised criticality 0.0–1.0. For usgs_mcs this equals the "
                    "normalised HHI computed from Σ(country_share²).",
        ),
        sa.Column(
            "trend_direction",
            sa.String(16),
            nullable=True,
            comment="rising | declining | stable. Written to materials.patent_occurrence_trend "
                    "by _sync_patent_trend() to keep the denorm cache fresh.",
        ),
        sa.Column(
            "hhi_score",
            sa.Float(),
            nullable=True,
            comment="Raw HHI value 0.0–1.0 (Σ share_i²). Stored for methodological "
                    "transparency. Traditional HHI uses 0–10000 (shares as percentages); "
                    "this codebase uses 0–1 throughout.",
        ),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "material_id", "source", "reference_year",
            name="uq_criticality_signal",
        ),
    )
    op.create_index(
        "ix_criticality_signal_material_source",
        "material_criticality_signals",
        ["material_id", "source"],
    )

    # -------------------------------------------------------------------------
    # 5. chemistry_risk_scores (pre-computed, append-only like company_scores)
    # -------------------------------------------------------------------------
    op.create_table(
        "chemistry_risk_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "battery_chemistry_id",
            sa.Integer(),
            sa.ForeignKey("battery_chemistries.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "as_of_date",
            sa.Date(),
            nullable=False,
            comment="Point-in-time date used to select active battery_chemistry_materials rows.",
        ),
        sa.Column(
            "methodology_version",
            sa.String(16),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column(
            "material_concentration_score",
            sa.Float(),
            nullable=True,
            comment="Intensity-weighted material concentration sub-score 0–100.",
        ),
        sa.Column(
            "geopolitical_score",
            sa.Float(),
            nullable=True,
            comment="Intensity-weighted geopolitical concentration sub-score 0–100.",
        ),
        sa.Column(
            "composite_risk_score",
            sa.Float(),
            nullable=True,
            comment="Blended risk score 0–100.",
        ),
        sa.Column(
            "score_confidence",
            sa.Float(),
            nullable=True,
            comment="0–1: penalised when materials have data_availability=no_benchmark. "
                    "A score backed only by no_benchmark materials floors at 0.3.",
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=True,
            comment="Auditability: signal_sources, geo_coverage, materials_missing_hs, "
                    "patent_modifiers_applied, trade_flows_vintage, no_benchmark_materials.",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    # -------------------------------------------------------------------------
    # 6. Seed battery_chemistries rows
    #
    # NMC variants (111/622/811) are intentionally collapsed into the single
    # 'nmc' slug for v1. Intensity values in battery_chemistry_materials use
    # NMC 622 as a midpoint. NMC 811 (≈80% Ni) is the current industry direction
    # and will be split as a separate slug once temporal junction data warrants it.
    #
    # Deferred chemistries: magnesium_ion, lithium_sulfur, vanadium_redox_flow.
    # Market share estimates are approximate 2024 figures (source: BNEF, Rho Motion).
    # -------------------------------------------------------------------------
    op.execute(
        sa.text(
            """
            INSERT INTO battery_chemistries
                (slug, name, description, status,
                 current_market_share_pct, market_share_as_of_date, is_active)
            VALUES
                ('nmc',
                 'NMC (Lithium Nickel Manganese Cobalt Oxide)',
                 'Dominant high-energy-density chemistry for passenger EVs. '
                 'NMC 111/622/811 variants collapsed here; intensity values reflect '
                 'NMC 622. NMC 811 (0.80 Ni) is the current direction and will be split '
                 'as separate slug when temporal junction data warrants it.',
                 'commercial', 0.38, '2024-12-31', true),

                ('lfp',
                 'LFP (Lithium Iron Phosphate)',
                 'Rapidly growing chemistry for standard-range EVs and stationary storage. '
                 'No cobalt or nickel. LFP went from ~6% to 40%+ of Chinese EV market '
                 'in three years. Projected largest chemistry by market share by 2035.',
                 'commercial', 0.40, '2024-12-31', true),

                ('nca',
                 'NCA (Lithium Nickel Cobalt Aluminum Oxide)',
                 'High-energy chemistry used by Tesla/Panasonic. High nickel content '
                 '(~80%). Requires tight thermal management.',
                 'commercial', 0.08, '2024-12-31', true),

                ('lfmp',
                 'LFMP (Lithium Iron Manganese Phosphate)',
                 'Extension of LFP adding manganese for higher voltage and energy density. '
                 'Emerging as bridge chemistry between LFP and NMC.',
                 'commercial', 0.04, '2024-12-31', true),

                ('sodium_ion',
                 'Sodium-Ion (NIB)',
                 'Already in commercial production (~10 GWh in 2025). Does not use lithium. '
                 'Materials are more geographically distributed, reducing concentration risk. '
                 'Primary near-term disruption vector for lithium demand. China holds '
                 'majority specialization in Na-ion patents (2015–2021 RTA data).',
                 'commercial', 0.03, '2024-12-31', true),

                ('solid_state',
                 'Solid-State Lithium (SSL)',
                 'Replaces liquid electrolyte with solid (sulfide, oxide, or polymer). '
                 'Enables lithium metal anode. Higher energy density potential. '
                 'Introduces new material dependencies: germanium/silicon (sulfide '
                 'electrolytes), tantalum (oxide electrolytes). Still pre-commercial '
                 'at scale.',
                 'emerging', 0.01, '2024-12-31', true)
            """
        )
    )


def downgrade() -> None:
    op.drop_table("chemistry_risk_scores")
    op.drop_table("material_criticality_signals")
    op.drop_table("battery_chemistry_materials")
    op.drop_table("battery_chemistries")
    op.drop_column("materials", "data_availability")
    op.drop_column("materials", "patent_occurrence_trend")
