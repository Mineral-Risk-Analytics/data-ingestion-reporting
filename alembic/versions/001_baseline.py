"""Baseline schema: full initial state for the battery supply chain intelligence platform.

This is a single-shot migration — run once against an empty database.
Do NOT apply this to a database that already has tables.

Table creation order respects FK dependencies:
  1. Extensions
  2. Platform (tenants)
  3. Domain config (supply_chain_contexts)
  4. Entity tables (materials, companies, facilities)
  5. Source & ingestion pipeline
  6. Document storage
  7. Regulatory & risk events
  8. Relationship layer (all junction tables)
  9. Scoring tables
  10. Report tables
  11. Platform users & usage events
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # -------------------------------------------------------------------------
    # 1. Extensions
    # -------------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    # -------------------------------------------------------------------------
    # 2. Platform — tenants (needed before org_id FKs)
    # Note: countries and organizations tables removed — not in ads_database_schema_v1.
    # Geography is handled via ISO2 string codes throughout; parent/subsidiary
    # relationships are handled via companies.parent_company_id self-reference.
    # -------------------------------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("clerk_org_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("plan", sa.String(64), nullable=False, server_default="starter"),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clerk_org_id"),
    )
    op.create_index("ix_tenants_clerk_org_id", "tenants", ["clerk_org_id"], unique=True)

    # -------------------------------------------------------------------------
    # 3. Domain config — supply_chain_contexts
    # Externalises constants that were previously hardcoded in Python scoring:
    # pillar weights, high-concentration geographies, HS code prefixes, and
    # valid supply chain stages. One row per domain. Seed: EV battery.
    # -------------------------------------------------------------------------
    op.create_table(
        "supply_chain_contexts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("default_pillar_weights", postgresql.JSONB(), nullable=False),
        sa.Column("high_concentration_geos", postgresql.JSONB(), nullable=False),
        sa.Column("relevant_hs_code_prefixes", postgresql.JSONB(), nullable=False),
        sa.Column("supply_chain_stages", postgresql.JSONB(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_supply_chain_contexts_slug", "supply_chain_contexts", ["slug"], unique=True)

    # Seed: EV battery domain — values mirror current hardcoded scoring constants.
    op.execute(
        sa.text(
            """
            INSERT INTO supply_chain_contexts
                (slug, name, description, default_pillar_weights,
                 high_concentration_geos, relevant_hs_code_prefixes,
                 supply_chain_stages, is_active)
            VALUES (
                'ev_battery',
                'EV Battery Supply Chain',
                'Lithium-ion battery supply chain covering critical minerals '
                '(lithium, cobalt, graphite, nickel, manganese), cell '
                'manufacturing, and pack assembly for electric vehicles.',
                '{"material_concentration": 0.30, "geopolitical_trade": 0.20, "regulatory": 0.20, "operational": 0.15, "financial": 0.15}'::jsonb,
                '["CN", "CD", "RU"]'::jsonb,
                '["8507", "2825", "2836", "2604", "2602", "2501"]'::jsonb,
                '["miner", "refiner", "cell_maker", "pack_maker", "oem", "trader"]'::jsonb,
                true
            )
            """
        )
    )

    # -------------------------------------------------------------------------
    # 4. Entity tables
    # -------------------------------------------------------------------------
    op.create_table(
        "materials",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("canonical_name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(128), nullable=True),
        sa.Column("symbol_or_code", sa.String(64), nullable=True),
        sa.Column("hs_codes", postgresql.JSONB(), nullable=True),
        sa.Column("criticality_score", sa.Float(), nullable=True),
        sa.Column("primary_producing_countries", postgresql.JSONB(), nullable=True),
        sa.Column("price_unit", sa.String(20), nullable=True),
        sa.Column("is_ira_critical_mineral", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_eu_crma_critical", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_name"),
    )
    op.create_index("ix_materials_category", "materials", ["category"])

    op.create_table(
        "companies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("canonical_name", sa.String(512), nullable=False),
        sa.Column("legal_name", sa.String(512), nullable=True),
        sa.Column("supply_chain_stage", sa.String(64), nullable=True),
        sa.Column("headquarters_country", sa.String(2), nullable=True),
        sa.Column("headquarters_region", sa.String(128), nullable=True),
        sa.Column("public_ticker", sa.String(32), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("duns_number", sa.String(32), nullable=True),
        sa.Column("lei", sa.String(20), nullable=True),
        sa.Column("parent_company_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("data_confidence", sa.Float(), nullable=True),
        sa.Column("data_source", sa.String(128), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["parent_company_id"], ["companies.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_name"),
    )
    op.create_index("ix_companies_canonical_name", "companies", ["canonical_name"])
    op.create_index("ix_companies_headquarters_country", "companies", ["headquarters_country"])
    op.create_index("ix_companies_supply_chain_stage", "companies", ["supply_chain_stage"])
    op.create_index("ix_companies_public_ticker", "companies", ["public_ticker"])

    op.create_table(
        "company_aliases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("alias", sa.String(512), nullable=False),
        sa.Column("alias_type", sa.String(64), nullable=False, server_default="aka"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "alias", name="uq_company_alias"),
    )
    op.create_index("ix_company_aliases_company_id", "company_aliases", ["company_id"])
    op.create_index("ix_company_aliases_alias", "company_aliases", ["alias"])

    op.create_table(
        "facilities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("facility_type", sa.String(64), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("region", sa.String(128), nullable=True),
        sa.Column("city", sa.String(128), nullable=True),
        sa.Column("status", sa.String(64), nullable=False, server_default="operating"),
        sa.Column("capacity_notes", sa.Text(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("data_source", sa.String(128), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_facilities_company_id", "facilities", ["company_id"])
    op.create_index("ix_facilities_country", "facilities", ["country"])

    # -------------------------------------------------------------------------
    # 5. Source & ingestion pipeline
    # -------------------------------------------------------------------------
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(8), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("config_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_sources_source_type", "sources", ["source_type"])
    op.create_index("ix_sources_phase", "sources", ["phase"])

    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parameters_json", postgresql.JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("stats_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ingestion_runs_source_id", "ingestion_runs", ["source_id"])
    op.create_index("ix_ingestion_runs_status", "ingestion_runs", ["status"])

    op.create_table(
        "raw_api_payloads",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ingestion_run_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(1024), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("request_params_json", postgresql.JSONB(), nullable=True),
        sa.Column("response_body_path", sa.String(1024), nullable=True),
        sa.Column("response_body_text", sa.Text(), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_raw_api_payloads_ingestion_run_id", "raw_api_payloads", ["ingestion_run_id"])
    op.create_index("ix_raw_api_payloads_checksum", "raw_api_payloads", ["checksum"])

    # -------------------------------------------------------------------------
    # 6. Document storage
    # -------------------------------------------------------------------------
    op.create_table(
        "source_documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(512), nullable=False),
        sa.Column("title", sa.String(1024), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("document_type", sa.String(64), nullable=False, server_default="unknown"),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("raw_storage_path", sa.String(1024), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "external_id", name="uq_source_external_doc"),
    )
    op.create_index("ix_source_documents_source_id", "source_documents", ["source_id"])
    op.create_index("ix_source_documents_checksum", "source_documents", ["checksum"])

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding_id", sa.String(128), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_chunks_source_document_id", "document_chunks", ["source_document_id"])
    # 1536 = OpenAI text-embedding-3-small. SQLAlchemy has no native Vector type.
    op.execute("ALTER TABLE document_chunks ADD COLUMN embedding vector(1536);")
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding "
        "ON document_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);"
    )

    # -------------------------------------------------------------------------
    # 7. Regulatory & risk events
    # -------------------------------------------------------------------------
    op.create_table(
        "regulations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=True),
        sa.Column("regulation_key", sa.String(256), nullable=False),
        sa.Column("title", sa.String(1024), nullable=True),
        sa.Column("issuing_body", sa.String(512), nullable=True),
        sa.Column("geography", sa.String(256), nullable=True),
        sa.Column("policy_theme", sa.String(256), nullable=True),
        sa.Column("status", sa.String(128), nullable=True),
        sa.Column("publication_date", sa.Date(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("regulation_key", name="uq_regulation_key"),
    )
    op.create_index("ix_regulations_source_document_id", "regulations", ["source_document_id"])

    op.create_table(
        "risk_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("event_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("title", sa.String(1024), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("severity_score", sa.Float(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("risk_categories_json", postgresql.JSONB(), nullable=True),
        sa.Column("geography_json", postgresql.JSONB(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_risk_events_source_document_id", "risk_events", ["source_document_id"])
    op.create_index("ix_risk_events_event_type", "risk_events", ["event_type"])
    op.create_index("ix_risk_events_event_date", "risk_events", ["event_date"])
    op.create_index("ix_risk_events_content_hash", "risk_events", ["content_hash"])

    # -------------------------------------------------------------------------
    # 8. Relationship layer — all junction tables
    # -------------------------------------------------------------------------

    op.create_table(
        "company_material_exposures",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("supply_chain_stage", sa.String(64), nullable=False),
        sa.Column("exposure_score", sa.Float(), nullable=False),
        sa.Column("source_geography", sa.String(2), nullable=True),
        sa.Column("data_confidence", sa.Float(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("as_of_date", sa.Date(), nullable=True),
        sa.Column("source_document_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "material_id", "supply_chain_stage", name="uq_company_material_stage"),
    )
    op.create_index("ix_company_material_exposures_company_id", "company_material_exposures", ["company_id"])
    op.create_index("ix_company_material_exposures_material_id", "company_material_exposures", ["material_id"])

    op.create_table(
        "company_supply_relationships",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("buyer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("supplier_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=True),
        sa.Column("relationship_type", sa.String(64), nullable=False, server_default="direct"),
        sa.Column("data_confidence", sa.Float(), nullable=True),
        sa.Column("source_document_id", sa.Integer(), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["buyer_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supplier_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("buyer_id", "supplier_id", "material_id", name="uq_supply_relationship"),
    )
    op.create_index("ix_company_supply_relationships_buyer_id", "company_supply_relationships", ["buyer_id"])
    op.create_index("ix_company_supply_relationships_supplier_id", "company_supply_relationships", ["supplier_id"])

    op.create_table(
        "regulation_material_scope",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("regulation_id", sa.Integer(), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("scope_type", sa.String(64), nullable=False, server_default="covered"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["regulation_id"], ["regulations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("regulation_id", "material_id", name="uq_reg_material"),
    )
    op.create_index("ix_regulation_material_scope_regulation_id", "regulation_material_scope", ["regulation_id"])

    op.create_table(
        "regulation_geography_scope",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("regulation_id", sa.Integer(), nullable=False),
        sa.Column("country_code", sa.String(8), nullable=False),
        sa.Column("scope_type", sa.String(64), nullable=False, server_default="jurisdiction"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["regulation_id"], ["regulations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("regulation_id", "country_code", name="uq_reg_geography"),
    )
    op.create_index("ix_regulation_geography_scope_regulation_id", "regulation_geography_scope", ["regulation_id"])

    op.create_table(
        "company_regulation_exposure",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("regulation_id", sa.Integer(), nullable=False),
        sa.Column("compliance_status", sa.String(64), nullable=False, server_default="unknown"),
        sa.Column("exposure_reason", sa.Text(), nullable=True),
        sa.Column("assessed_at", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["regulation_id"], ["regulations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "regulation_id", name="uq_company_regulation"),
    )
    op.create_index("ix_company_regulation_exposure_company_id", "company_regulation_exposure", ["company_id"])
    op.create_index("ix_company_regulation_exposure_regulation_id", "company_regulation_exposure", ["regulation_id"])

    op.create_table(
        "hs_code_material_mappings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("hs_code_prefix", sa.String(16), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(512), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hs_code_prefix", "material_id", name="uq_hs_material"),
    )
    op.create_index("ix_hs_code_material_mappings_hs_code_prefix", "hs_code_material_mappings", ["hs_code_prefix"])
    op.create_index("ix_hs_code_material_mappings_material_id", "hs_code_material_mappings", ["material_id"])

    op.create_table(
        "risk_event_companies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("risk_event_id", sa.Integer(), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relevance_score", sa.Float(), nullable=False),
        sa.Column("match_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["risk_event_id"], ["risk_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("risk_event_id", "company_id", name="uq_risk_event_company"),
    )
    op.create_index("ix_risk_event_companies_risk_event_id", "risk_event_companies", ["risk_event_id"])
    op.create_index("ix_risk_event_companies_company_id", "risk_event_companies", ["company_id"])

    op.create_table(
        "risk_event_materials",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("risk_event_id", sa.Integer(), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("relevance_score", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("match_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["risk_event_id"], ["risk_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("risk_event_id", "material_id", name="uq_risk_event_material"),
    )
    op.create_index("ix_risk_event_materials_risk_event_id", "risk_event_materials", ["risk_event_id"])
    op.create_index("ix_risk_event_materials_material_id", "risk_event_materials", ["material_id"])

    op.create_table(
        "risk_event_regulations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("risk_event_id", sa.Integer(), nullable=False),
        sa.Column("regulation_id", sa.Integer(), nullable=False),
        sa.Column("relevance_score", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("match_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["risk_event_id"], ["risk_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["regulation_id"], ["regulations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("risk_event_id", "regulation_id", name="uq_risk_event_regulation"),
    )
    op.create_index("ix_risk_event_regulations_risk_event_id", "risk_event_regulations", ["risk_event_id"])
    op.create_index("ix_risk_event_regulations_regulation_id", "risk_event_regulations", ["regulation_id"])

    op.create_table(
        "risk_event_geographies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("risk_event_id", sa.Integer(), nullable=False),
        sa.Column("country_code", sa.String(2), nullable=False),
        sa.Column("geography_context", sa.String(64), nullable=False, server_default="primary"),
        sa.Column("relevance_score", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["risk_event_id"], ["risk_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("risk_event_id", "country_code", name="uq_risk_event_geography"),
    )
    op.create_index("ix_risk_event_geographies_risk_event_id", "risk_event_geographies", ["risk_event_id"])
    op.create_index("ix_risk_event_geographies_country_code", "risk_event_geographies", ["country_code"])

    op.create_table(
        "trade_flows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(16), nullable=False),
        sa.Column("reporter_country", sa.String(8), nullable=False),
        sa.Column("partner_country", sa.String(8), nullable=False),
        sa.Column("hs_code", sa.String(32), nullable=True),
        sa.Column("hs_description", sa.String(512), nullable=True),
        sa.Column("material_id", sa.Integer(), nullable=True),
        sa.Column("import_export_flag", sa.String(16), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("quantity_unit", sa.String(64), nullable=True),
        sa.Column("trade_value_usd", sa.Float(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trade_flows_source_document_id", "trade_flows", ["source_document_id"])
    op.create_index("ix_trade_flows_period", "trade_flows", ["period"])
    op.create_index("ix_trade_flows_hs_code", "trade_flows", ["hs_code"])

    op.create_table(
        "commodity_prices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("price_date", sa.Date(), nullable=False),
        sa.Column("price_usd", sa.Float(), nullable=False),
        sa.Column("price_unit", sa.String(20), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("material_id", "price_date", "source", name="uq_commodity_price"),
    )
    op.create_index("ix_commodity_prices_material_id", "commodity_prices", ["material_id"])
    op.create_index("ix_commodity_prices_price_date", "commodity_prices", ["price_date"])

    # -------------------------------------------------------------------------
    # 9. Scoring tables
    # -------------------------------------------------------------------------
    op.create_table(
        "company_scores",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("material_concentration_risk_score", sa.Float(), nullable=True),
        sa.Column("geopolitical_trade_risk_score", sa.Float(), nullable=True),
        sa.Column("regulatory_risk_score", sa.Float(), nullable=True),
        sa.Column("operational_risk_score", sa.Float(), nullable=True),
        sa.Column("financial_pressure_score", sa.Float(), nullable=True),
        sa.Column("overall_risk_score", sa.Float(), nullable=True),
        sa.Column("rationale_json", postgresql.JSONB(), nullable=True),
        sa.Column("scoring_version", sa.String(32), nullable=False, server_default="2.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_company_scores_company_id", "company_scores", ["company_id"])
    op.create_index("ix_company_scores_as_of_date", "company_scores", ["as_of_date"])

    op.create_table(
        "material_scores",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("material_concentration_score", sa.Float(), nullable=True),
        sa.Column("geopolitical_trade_score", sa.Float(), nullable=True),
        sa.Column("regulatory_compliance_score", sa.Float(), nullable=True),
        sa.Column("operational_score", sa.Float(), nullable=True),
        sa.Column("financial_pressure_score", sa.Float(), nullable=True),
        sa.Column("overall_risk_score", sa.Float(), nullable=True),
        sa.Column("company_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rationale_json", postgresql.JSONB(), nullable=True),
        sa.Column("scoring_version", sa.String(32), nullable=False, server_default="2.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_material_scores_material_id", "material_scores", ["material_id"])
    op.create_index("ix_material_scores_as_of_date", "material_scores", ["as_of_date"])

    op.create_table(
        "geography_scores",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("geography_code", sa.String(2), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("geopolitical_trade_score", sa.Float(), nullable=True),
        sa.Column("regulatory_compliance_score", sa.Float(), nullable=True),
        sa.Column("operational_score", sa.Float(), nullable=True),
        sa.Column("overall_risk_score", sa.Float(), nullable=True),
        sa.Column("company_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rationale_json", postgresql.JSONB(), nullable=True),
        sa.Column("scoring_version", sa.String(32), nullable=False, server_default="2.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_geography_scores_geography_code", "geography_scores", ["geography_code"])
    op.create_index("ix_geography_scores_as_of_date", "geography_scores", ["as_of_date"])

    # -------------------------------------------------------------------------
    # 10. Report tables
    # -------------------------------------------------------------------------
    op.create_table(
        "report_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("focus_type", sa.String(64), nullable=False),
        sa.Column("audience_type", sa.String(64), nullable=False),
        sa.Column("is_platform_template", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("weight_overrides", postgresql.JSONB(), nullable=True),
        sa.Column("sections_config", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["org_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_report_templates_org_id", "report_templates", ["org_id"])
    op.create_index("ix_report_templates_focus_type", "report_templates", ["focus_type"])

    op.create_table(
        "report_template_focus_entities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["template_id"], ["report_templates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_report_template_focus_entities_template_id", "report_template_focus_entities", ["template_id"])

    op.create_table(
        "report_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("report_type", sa.String(64), nullable=False),
        sa.Column("audience_type", sa.String(64), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("parameters_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["template_id"], ["report_templates.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_report_runs_report_type", "report_runs", ["report_type"])
    op.create_index("ix_report_runs_org_id", "report_runs", ["org_id"])

    op.create_table(
        "report_insights",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_run_id", sa.Integer(), nullable=False),
        sa.Column("insight_type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("related_company_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("related_material_id", sa.Integer(), nullable=True),
        sa.Column("related_regulation_id", sa.Integer(), nullable=True),
        sa.Column("related_geography_code", sa.String(2), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["related_company_id"], ["companies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["related_material_id"], ["materials.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["related_regulation_id"], ["regulations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["report_run_id"], ["report_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_report_insights_report_run_id", "report_insights", ["report_run_id"])

    op.create_table(
        "analyst_notes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("note_type", sa.String(64), nullable=False),
        sa.Column("note_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analyst_notes_entity", "analyst_notes", ["entity_type", "entity_id"])

    # -------------------------------------------------------------------------
    # 11. Platform users & usage events
    # -------------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clerk_user_id", sa.String(128), nullable=False),
        sa.Column("email", sa.String(512), nullable=False),
        sa.Column("role", sa.String(64), nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clerk_user_id"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.create_index("ix_users_clerk_user_id", "users", ["clerk_user_id"], unique=True)
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "usage_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_usage_events_tenant_id", "usage_events", ["tenant_id"])
    op.create_index("ix_usage_events_event_type", "usage_events", ["event_type"])


def downgrade() -> None:
    # Drop in reverse FK dependency order
    op.drop_table("usage_events")
    op.drop_table("users")
    op.drop_table("analyst_notes")
    op.drop_table("report_insights")
    op.drop_table("report_runs")
    op.drop_table("report_template_focus_entities")
    op.drop_table("report_templates")
    op.drop_table("geography_scores")
    op.drop_table("material_scores")
    op.drop_table("company_scores")
    op.drop_table("commodity_prices")
    op.drop_table("trade_flows")
    op.drop_table("risk_event_geographies")
    op.drop_table("risk_event_regulations")
    op.drop_table("risk_event_materials")
    op.drop_table("risk_event_companies")
    op.drop_table("hs_code_material_mappings")
    op.drop_table("company_regulation_exposure")
    op.drop_table("regulation_geography_scope")
    op.drop_table("regulation_material_scope")
    op.drop_table("company_supply_relationships")
    op.drop_table("company_material_exposures")
    op.drop_table("risk_events")
    op.drop_table("regulations")
    op.drop_table("document_chunks")
    op.drop_table("source_documents")
    op.drop_table("raw_api_payloads")
    op.drop_table("ingestion_runs")
    op.drop_table("sources")
    op.drop_table("facilities")
    op.drop_table("company_aliases")
    op.drop_table("companies")
    op.drop_table("materials")
    op.drop_table("supply_chain_contexts")
    op.drop_table("tenants")
    # Leave pgvector and pgcrypto extensions in place
