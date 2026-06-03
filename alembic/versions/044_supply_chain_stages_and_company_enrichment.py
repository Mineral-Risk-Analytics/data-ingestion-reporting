"""Add ``supply_chain_stages`` reference table + Company enrichment columns.

Two-axis stage taxonomy
-----------------------
The codebase carries TWO orthogonal "stage" axes that historically shared
the column name ``supply_chain_stage``, causing confusion:

* **Axis A — product form** (the material's physical/chemical state):
  ``ore | concentrate | intermediate | refined | battery_grade |
  fabricated | scrap``.  Lives on ``hs_code_material_mappings.supply_chain_stage``
  and ``facilities.supply_chain_stage``.  Used by ``hs_node_scorer.py``
  to match facility output capacity to the right HS-node concentration
  calculation.

* **Axis B — activity** (what the company / facility does):
  ``mining | refining | cell_making | …``.  Lives on
  ``companies.supply_chain_stage`` (legacy free string) and
  ``company_material_exposures.supply_chain_stage`` (also free string).
  Used by bottleneck-stage weighting in company scoring rollup.

This migration normalises **Axis B** into the new ``supply_chain_stages``
reference table.  Axis A is left alone (still a free string on its
two columns) — it complements but does not overlap Axis B.  The
``typical_output_forms`` JSONB column on the new table captures the
many-to-many relationship between the two axes as an advisory mapping
(e.g., "refining" activity typically produces ``intermediate / refined
/ battery_grade`` product forms).

To avoid the historical naming confusion, the new FK column on
``companies`` is named ``primary_activity_stage_fk`` (not
``primary_supply_chain_stage_fk``), and the new join table is
``company_activity_stages`` (not ``company_operating_stages``).  The
legacy free-string column ``companies.supply_chain_stage`` is unchanged
during Phase 1 coexistence and gets dropped in a later cleanup.

Background
----------
Three concurrent gaps motivated this migration:

1. **Stage taxonomy drift (Axis B).**  Three free-string columns were
   carrying overlapping but non-matching activity vocabularies:

     * ``companies.supply_chain_stage`` — "miner | refiner | cell_maker | …"
     * ``facilities.facility_type``     — "mine | refinery | smelter | …"
     * ``company_material_exposures.supply_chain_stage`` — "mining | refining | …"

   None reference each other; values drifted over time; scoring code
   maps them ad-hoc.  Per partner-curation review (2026-05-23) we want
   the activity axis to be a *normalised reference* with descriptions
   surfaceable in the UI, bottleneck weights inline, and a stable code
   that both companies and exposures FK into.

2. **Company-side identifier gap.**  Companies has no ``cik`` column;
   SEC CIK lives only in ``source_documents.metadata_json["cik"]`` and a
   hardcoded ``CIK_MAP`` in ``app/tasks/ingestion_jobs.py``.  Same for
   SIC code and exchange listings — Workstream A (SEC ingester refactor)
   needs these as first-class fields, and partner curation needs a
   stable home for them.

3. **Corporate structure can't be modelled.**  JV vehicles like TLEA
   (51% Tianqi / 49% IGO) cannot be expressed today — ``parent_company_id``
   is a single FK, so it captures *one* parent for a wholly-owned
   subsidiary but not the multi-parent JV case.  Risk roll-up to all
   parents is a partner requirement for jurisdictional scoring.

Scope (Phase 1)
---------------
Additive only.  We do not touch:

  * The existing ``companies.supply_chain_stage`` free-string column.
    A new nullable ``primary_supply_chain_stage_fk`` is added alongside.
    Both columns coexist through the transition; the old column gets
    dropped in a later cleanup migration once all scoring code has
    migrated to the FK.
  * The existing ``company_material_exposures.supply_chain_stage``
    free-string column.  Same coexistence approach — deferred to a later
    migration (see "Deferred" below).
  * Any existing data in ``companies``.  All new columns are nullable
    with sensible defaults so existing rows remain valid.

What this migration adds
------------------------
**Block 1 — ``supply_chain_stages`` reference table.**
Canonical stage codes with bottleneck-weight per saved methodology
(Mining ×0.80, Refining ×1.20, CAM ×1.30, Cell-making ×1.40 with
extrapolated values for the remaining stages — see the seed script for
calibration notes).  ``description`` and ``display_name`` columns enable
UI tooltips without re-typing.

**Block 2 — 14 new nullable columns on ``companies``.**
``cik`` (UNIQUE), ``sic``, ``sic_description``, ``exchanges`` (JSONB),
``sec_metadata`` (JSONB — addresses, fiscalYearEnd, etc.),
``incorporated_country``, ``is_state_owned_or_influenced``,
``has_facilities``, ``operates_as_trader``, ``is_sanctioned``,
``sanctioning_jurisdictions`` (JSONB), ``has_uflpa_designation``,
``uflpa_status``, ``primary_supply_chain_stage_fk``.

**Block 3 — Two many-to-many join tables.**
``company_operating_stages`` (company × stage) for the multi-stage case
(Albemarle = mining + refining, Ganfeng = mining + refining +
precursor_production).  ``company_jv_parents`` (jv_vehicle × parent)
for the multi-parent JV case (TLEA → Tianqi + IGO).  Single-parent
subsidiaries continue to use the existing ``parent_company_id`` column;
this table is *additive* for the JV case only.

Deferred to a later migration
-----------------------------
* ``company_material_exposures.supply_chain_stage`` → FK to
  ``supply_chain_stages.stage_code``.  Reaches into scoring code path
  (geopolitical pillar's stage rollup), so kept out of this migration
  to limit blast radius.  Same Phase-1/Phase-2 approach when it lands.
* Dropping the old ``companies.supply_chain_stage`` free string.
  Requires migrating all scoring/query code first.

Revision ID: 044
Revises:     043
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "044"
down_revision: Union[str, None] = "043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Block 1: supply_chain_stages reference table ───────────────────
    op.create_table(
        "supply_chain_stages",
        sa.Column(
            "stage_code",
            sa.String(64),
            primary_key=True,
            comment=(
                "Canonical stage identifier in snake_case (e.g. 'mining', "
                "'refining', 'cathode_active_material', 'cell_making'). "
                "Referenced by companies.primary_supply_chain_stage_fk, "
                "company_operating_stages.stage_code, and "
                "(deferred migration) "
                "company_material_exposures.supply_chain_stage_fk."
            ),
        ),
        sa.Column(
            "display_name",
            sa.String(128),
            nullable=False,
            comment="UI-ready display label, e.g. 'Cathode Active Material (CAM)'.",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            comment=(
                "Long-form description of what activities count as this "
                "stage.  Intended for UI tooltips and partner-curation "
                "guidance — same text appears in the company seed template."
            ),
        ),
        sa.Column(
            "typical_facility_types",
            JSONB(),
            nullable=True,
            comment=(
                "JSON array of facility_type strings typically associated "
                "with this stage (cross-references facilities.facility_type "
                "values).  Used by partner-review UI to cross-check a "
                "company's stage list against its facility list."
            ),
        ),
        sa.Column(
            "typical_output_forms",
            JSONB(),
            nullable=True,
            comment=(
                "Advisory mapping from Axis B (this activity stage) to "
                "Axis A (product forms from the hs_code_material_mappings / "
                "facilities supply_chain_stage taxonomy: ore | concentrate | "
                "intermediate | refined | battery_grade | fabricated | "
                "scrap).  Many-to-many in practice — refining produces "
                "intermediate / refined / battery_grade depending on "
                "customer; recycling output is intermediate / refined.  Not "
                "FK-enforced; partner-review UI uses this to cross-check "
                "facility output_form against the company's activity-stage "
                "list."
            ),
        ),
        sa.Column(
            "bottleneck_weight",
            sa.Numeric(precision=4, scale=2),
            nullable=False,
            server_default=sa.text("1.0"),
            comment=(
                "Multiplier applied in stage-aware risk rollup.  Per saved "
                "methodology: Mining 0.80, Refining 1.20, CAM 1.30, "
                "Cell-making 1.40 (the four anchor values).  Other stages "
                "extrapolated and flagged for partner calibration — see "
                "seed_supply_chain_stages.py."
            ),
        ),
        sa.Column(
            "hs_chapter_hint",
            sa.String(32),
            nullable=True,
            comment=(
                "HS chapter range typically associated with this stage "
                "(e.g. '25, 26' for mining; '28, 29' for refining; '85' "
                "for cell-making).  Hint for HS-stage taxonomy derivation; "
                "not a hard constraint."
            ),
        ),
        sa.Column(
            "sort_order",
            sa.Integer(),
            nullable=False,
            comment="Ordering for UI lists. Lower = earlier in supply chain.",
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
            comment=(
                "Calibration / scoping notes — especially for stages whose "
                "bottleneck_weight is extrapolated rather than methodology-anchored."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_supply_chain_stages_sort_order",
        "supply_chain_stages",
        ["sort_order"],
    )

    # ── Block 2: 14 new columns on companies ───────────────────────────
    op.add_column(
        "companies",
        sa.Column(
            "cik",
            sa.String(10),
            nullable=True,
            comment=(
                "SEC Central Index Key, zero-padded to 10 digits "
                "(e.g. '0000915779').  UNIQUE across companies.  NULL for "
                "non-SEC filers (Ganfeng, Tianqi, CATL, LG, SK On, BYD, "
                "Trafigura, etc.).  Workstream A (SEC ingester) reads "
                "this to tie SourceDocument rows directly to Company."
            ),
        ),
    )
    op.create_index(
        "uq_companies_cik",
        "companies",
        ["cik"],
        unique=True,
        postgresql_where=sa.text("cik IS NOT NULL"),
    )

    op.add_column(
        "companies",
        sa.Column(
            "sic",
            sa.String(4),
            nullable=True,
            comment=(
                "4-digit SEC SIC code (e.g. '2812' = industrial inorganic "
                "chemicals).  Available in SEC submissions JSON.  Used as a "
                "deterministic issuer→material-bucket prior in scoring."
            ),
        ),
    )
    op.add_column(
        "companies",
        sa.Column(
            "sic_description",
            sa.String(256),
            nullable=True,
            comment="Human-readable SIC description from SEC submissions JSON.",
        ),
    )
    op.add_column(
        "companies",
        sa.Column(
            "exchanges",
            JSONB(),
            nullable=True,
            comment=(
                "JSON array of exchange codes the company is listed on, "
                "primary listing FIRST.  Examples: ['NYSE'], "
                "['SZSE', 'HKEX'], ['LSE', 'JSE', 'HKEX'].  Used by future "
                "market-data ingester and for jurisdictional context."
            ),
        ),
    )
    op.add_column(
        "companies",
        sa.Column(
            "sec_metadata",
            JSONB(),
            nullable=True,
            comment=(
                "Bag of SEC-derived fields that don't warrant first-class "
                "columns: business_address, mailing_address, fiscalYearEnd, "
                "category, entityType, phone, description, website.  "
                "Populated by Workstream A from the submissions JSON."
            ),
        ),
    )

    op.add_column(
        "companies",
        sa.Column(
            "incorporated_country",
            sa.String(2),
            nullable=True,
            comment=(
                "ISO-2 country of LEGAL INCORPORATION if different from "
                "headquarters_country.  Examples: Glencore plc is "
                "incorporated in Jersey (JE) but operationally HQ'd in "
                "Switzerland (CH); Schlumberger is incorporated in "
                "Curacao (CW) but US-listed.  NULL falls back to "
                "headquarters_country.  Drives jurisdictional risk for "
                "entities that have separated tax domicile from operational HQ."
            ),
        ),
    )

    op.add_column(
        "companies",
        sa.Column(
            "is_state_owned_or_influenced",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "TRUE if the entity is ≥30% government-owned/controlled, "
                "carries an explicit national-champion designation, or has "
                "significant SOE board influence.  Softer than "
                "ownership_type='state_owned' (which means 100% state).  "
                "Examples: CMOC (~25% via Luoyang state) = TRUE; Indonesia "
                "Battery Corp (100%) = TRUE; Norilsk Nickel (oligarchic) = "
                "TRUE; Glencore = FALSE.  Triggers geopolitical-pillar "
                "scoring multiplier."
            ),
        ),
    )

    op.add_column(
        "companies",
        sa.Column(
            "has_facilities",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment=(
                "TRUE if the company owns/operates physical production "
                "sites.  FALSE for pure traders (Trafigura, IXM, Mercuria) "
                "and financial sponsors.  Drives the three-edge company-"
                "material exposure model: TRUE → facility-derived edge; "
                "FALSE → fall back to partner-curated or supplier-graph-"
                "derived edge."
            ),
        ),
    )
    op.add_column(
        "companies",
        sa.Column(
            "operates_as_trader",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "TRUE if the company has material commodity-trading "
                "operations regardless of whether it also owns facilities.  "
                "Glencore = TRUE (integrated trader+producer); Trafigura = "
                "TRUE (pure trader); Albemarle = FALSE.  Triggers supplier-"
                "graph-derived exposure path even when has_facilities=TRUE."
            ),
        ),
    )

    op.add_column(
        "companies",
        sa.Column(
            "is_sanctioned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "TRUE if the ENTITY ITSELF appears on any sanctions list "
                "(OFAC SDN, EU consolidated, UK HM Treasury, UN).  Distinct "
                "from operating-in-a-sanctioned-country (which is captured "
                "at the facility / geography level).  Direct entity "
                "sanctions trigger maximum-severity scoring events."
            ),
        ),
    )
    op.add_column(
        "companies",
        sa.Column(
            "sanctioning_jurisdictions",
            JSONB(),
            nullable=True,
            comment=(
                "JSON array of jurisdiction codes that have sanctioned this "
                "entity: 'US', 'EU', 'UK', 'UN', 'CH', 'CA', 'AU', 'JP'.  "
                "NULL when is_sanctioned=FALSE."
            ),
        ),
    )

    op.add_column(
        "companies",
        sa.Column(
            "has_uflpa_designation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "TRUE if the entity appears on the US Uyghur Forced Labor "
                "Prevention Act (UFLPA) Entity List maintained by CBP.  "
                "Triggers the UFLPA rebuttable-presumption status multiplier "
                "in regulatory-pillar scoring."
            ),
        ),
    )
    op.add_column(
        "companies",
        sa.Column(
            "uflpa_status",
            sa.String(32),
            nullable=True,
            comment=(
                "If has_uflpa_designation=TRUE, one of: 'presumption' "
                "(default, ×1.0 multiplier), 'partial' (partial rebuttal "
                "accepted, ×0.70), 'unknown' (under CBP review, ×0.85).  "
                "NULL when has_uflpa_designation=FALSE."
            ),
        ),
    )

    op.add_column(
        "companies",
        sa.Column(
            "primary_activity_stage_fk",
            sa.String(64),
            sa.ForeignKey(
                "supply_chain_stages.stage_code", ondelete="RESTRICT"
            ),
            nullable=True,
            comment=(
                "FK to supply_chain_stages.stage_code (the activity-axis / "
                "Axis B taxonomy).  The dominant activity stage for this "
                "company (typically the bottleneck stage for integrated "
                "operators).  Distinct from facilities.supply_chain_stage "
                "and hs_code_material_mappings.supply_chain_stage, which "
                "carry the product-form / Axis A taxonomy (ore | concentrate "
                "| intermediate | refined | battery_grade | fabricated | "
                "scrap).  Coexists with the legacy free-string "
                "companies.supply_chain_stage during Phase 1 — both are "
                "populated by the loader; the legacy column gets dropped "
                "in a later cleanup once scoring code migrates."
            ),
        ),
    )
    op.create_index(
        "ix_companies_primary_activity_stage_fk",
        "companies",
        ["primary_activity_stage_fk"],
    )

    # ── Block 3: many-to-many join tables ──────────────────────────────
    op.create_table(
        "company_activity_stages",
        sa.Column(
            "company_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "stage_code",
            sa.String(64),
            sa.ForeignKey(
                "supply_chain_stages.stage_code", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        sa.Column(
            "is_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "Convenience flag: TRUE for the company's primary activity "
                "stage (matches companies.primary_activity_stage_fk).  Lets "
                "scoring queries join through one table instead of two."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "company_id", "stage_code", name="pk_company_activity_stages"
        ),
    )
    op.create_index(
        "ix_company_activity_stages_stage",
        "company_activity_stages",
        ["stage_code"],
    )

    op.create_table(
        "company_jv_parents",
        sa.Column(
            "jv_vehicle_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
            comment=(
                "The JV-vehicle Company (ownership_type='jv_vehicle')."
            ),
        ),
        sa.Column(
            "parent_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
            comment=(
                "A parent of the JV vehicle.  TLEA has two rows here — one "
                "per parent (Tianqi, IGO).  Talison Lithium has three "
                "(Tianqi via TLEA, Albemarle, IGO via TLEA)."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "jv_vehicle_id", "parent_id", name="pk_company_jv_parents"
        ),
        sa.CheckConstraint(
            "jv_vehicle_id <> parent_id",
            name="ck_jv_parent_not_self",
        ),
    )
    op.create_index(
        "ix_company_jv_parents_parent",
        "company_jv_parents",
        ["parent_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_company_jv_parents_parent", "company_jv_parents")
    op.drop_table("company_jv_parents")
    op.drop_index(
        "ix_company_activity_stages_stage", "company_activity_stages"
    )
    op.drop_table("company_activity_stages")

    op.drop_index("ix_companies_primary_activity_stage_fk", "companies")
    op.drop_column("companies", "primary_activity_stage_fk")
    op.drop_column("companies", "uflpa_status")
    op.drop_column("companies", "has_uflpa_designation")
    op.drop_column("companies", "sanctioning_jurisdictions")
    op.drop_column("companies", "is_sanctioned")
    op.drop_column("companies", "operates_as_trader")
    op.drop_column("companies", "has_facilities")
    op.drop_column("companies", "is_state_owned_or_influenced")
    op.drop_column("companies", "incorporated_country")
    op.drop_column("companies", "sec_metadata")
    op.drop_column("companies", "exchanges")
    op.drop_column("companies", "sic_description")
    op.drop_column("companies", "sic")
    op.execute("DROP INDEX IF EXISTS uq_companies_cik")
    op.drop_column("companies", "cik")

    op.drop_index(
        "ix_supply_chain_stages_sort_order", "supply_chain_stages"
    )
    op.drop_table("supply_chain_stages")
