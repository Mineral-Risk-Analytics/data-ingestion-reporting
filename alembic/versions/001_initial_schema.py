"""Initial schema: sources, documents, supply chain, regulatory, reporting."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "countries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("iso2", sa.String(length=2), nullable=False),
        sa.Column("iso3", sa.String(length=3), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("region", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_countries_iso2", "countries", ["iso2"], unique=True)

    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("org_type", sa.String(length=64), nullable=False),
        sa.Column("country_id", sa.Integer(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["country_id"], ["countries.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_organizations_name", "organizations", ["name"])
    op.create_index("ix_organizations_org_type", "organizations", ["org_type"])

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("phase", sa.String(length=8), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("config_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parameters_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("stats_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ingestion_runs_source_id", "ingestion_runs", ["source_id"])
    op.create_index("ix_ingestion_runs_status", "ingestion_runs", ["status"])

    op.create_table(
        "raw_api_payloads",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ingestion_run_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(length=1024), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("request_params_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response_body_path", sa.String(length=1024), nullable=True),
        sa.Column("response_body_text", sa.Text(), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_raw_api_payloads_ingestion_run_id", "raw_api_payloads", ["ingestion_run_id"])
    op.create_index("ix_raw_api_payloads_source_id", "raw_api_payloads", ["source_id"])
    op.create_index("ix_raw_api_payloads_checksum", "raw_api_payloads", ["checksum"])

    op.create_table(
        "materials",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("canonical_name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("symbol_or_code", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_name"),
    )
    op.create_index("ix_materials_category", "materials", ["category"])

    op.create_table(
        "suppliers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("canonical_name", sa.String(length=512), nullable=False),
        sa.Column(
            "supplier_type",
            sa.String(length=64),
            nullable=False,
            server_default="other",
        ),
        sa.Column("headquarters_country", sa.String(length=2), nullable=True),
        sa.Column("headquarters_region", sa.String(length=128), nullable=True),
        sa.Column("public_ticker", sa.String(length=32), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_name"),
    )
    op.create_index("ix_suppliers_headquarters_country", "suppliers", ["headquarters_country"])
    op.create_index("ix_suppliers_public_ticker", "suppliers", ["public_ticker"])

    op.create_table(
        "supplier_aliases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(length=512), nullable=False),
        sa.Column(
            "alias_type",
            sa.String(length=64),
            nullable=False,
            server_default="aka",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("supplier_id", "alias", name="uq_supplier_alias"),
    )
    op.create_index("ix_supplier_aliases_supplier_id", "supplier_aliases", ["supplier_id"])
    op.create_index("ix_supplier_aliases_alias", "supplier_aliases", ["alias"])

    op.create_table(
        "source_documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column(
            "document_type",
            sa.String(length=64),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("raw_storage_path", sa.String(length=1024), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
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
        sa.Column("embedding_id", sa.String(length=128), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_chunks_source_document_id", "document_chunks", ["source_document_id"])

    op.create_table(
        "entity_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("from_entity_type", sa.String(length=64), nullable=False),
        sa.Column("from_entity_id", sa.Integer(), nullable=False),
        sa.Column("to_entity_type", sa.String(length=64), nullable=False),
        sa.Column("to_entity_id", sa.Integer(), nullable=False),
        sa.Column("link_type", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_entity_links_from", "entity_links", ["from_entity_type", "from_entity_id"])
    op.create_index("ix_entity_links_to", "entity_links", ["to_entity_type", "to_entity_id"])

    op.create_table(
        "trade_flows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(length=16), nullable=False),
        sa.Column("reporter_country", sa.String(length=8), nullable=False),
        sa.Column("partner_country", sa.String(length=8), nullable=False),
        sa.Column("hs_code", sa.String(length=32), nullable=True),
        sa.Column("hs_description", sa.String(length=512), nullable=True),
        sa.Column("material_id", sa.Integer(), nullable=True),
        sa.Column("import_export_flag", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("quantity_unit", sa.String(length=64), nullable=True),
        sa.Column("trade_value_usd", sa.Float(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trade_flows_source_document_id", "trade_flows", ["source_document_id"])
    op.create_index("ix_trade_flows_period", "trade_flows", ["period"])
    op.create_index("ix_trade_flows_hs_code", "trade_flows", ["hs_code"])

    op.create_table(
        "regulations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=False),
        sa.Column("regulation_key", sa.String(length=256), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=True),
        sa.Column("issuing_body", sa.String(length=512), nullable=True),
        sa.Column("geography", sa.String(length=256), nullable=True),
        sa.Column("policy_theme", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=128), nullable=True),
        sa.Column("publication_date", sa.Date(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_document_id", "regulation_key", name="uq_regulation_doc_key"),
    )
    op.create_index("ix_regulations_source_document_id", "regulations", ["source_document_id"])

    op.create_table(
        "risk_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("event_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("severity_score", sa.Float(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("risk_categories_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("geography_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_risk_events_source_document_id", "risk_events", ["source_document_id"])
    op.create_index("ix_risk_events_event_type", "risk_events", ["event_type"])
    op.create_index("ix_risk_events_event_date", "risk_events", ["event_date"])

    op.create_table(
        "supplier_material_exposure",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("exposure_type", sa.String(length=64), nullable=False),
        sa.Column("exposure_score", sa.Float(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("source_document_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_supplier_material_exposure_supplier_id", "supplier_material_exposure", ["supplier_id"]
    )
    op.create_index(
        "ix_supplier_material_exposure_material_id", "supplier_material_exposure", ["material_id"]
    )

    op.create_table(
        "supplier_scores",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("material_risk_score", sa.Float(), nullable=True),
        sa.Column("regulatory_risk_score", sa.Float(), nullable=True),
        sa.Column("financial_pressure_score", sa.Float(), nullable=True),
        sa.Column("operational_risk_score", sa.Float(), nullable=True),
        sa.Column("overall_risk_score", sa.Float(), nullable=True),
        sa.Column("rationale_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_supplier_scores_supplier_id", "supplier_scores", ["supplier_id"])
    op.create_index("ix_supplier_scores_as_of_date", "supplier_scores", ["as_of_date"])

    op.create_table(
        "report_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_type", sa.String(length=64), nullable=False),
        sa.Column("audience_type", sa.String(length=64), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("parameters_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_report_runs_report_type", "report_runs", ["report_type"])

    op.create_table(
        "report_insights",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_run_id", sa.Integer(), nullable=False),
        sa.Column("insight_type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("related_supplier_id", sa.Integer(), nullable=True),
        sa.Column("related_material_id", sa.Integer(), nullable=True),
        sa.Column("related_regulation_id", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["related_material_id"], ["materials.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["related_regulation_id"], ["regulations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["related_supplier_id"], ["suppliers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["report_run_id"], ["report_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_report_insights_report_run_id", "report_insights", ["report_run_id"])

    op.create_table(
        "analyst_notes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("note_type", sa.String(length=64), nullable=False),
        sa.Column("note_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analyst_notes_entity", "analyst_notes", ["entity_type", "entity_id"])


def downgrade() -> None:
    op.drop_index("ix_analyst_notes_entity", table_name="analyst_notes")
    op.drop_table("analyst_notes")
    op.drop_index("ix_report_insights_report_run_id", table_name="report_insights")
    op.drop_table("report_insights")
    op.drop_index("ix_report_runs_report_type", table_name="report_runs")
    op.drop_table("report_runs")
    op.drop_index("ix_supplier_scores_as_of_date", table_name="supplier_scores")
    op.drop_index("ix_supplier_scores_supplier_id", table_name="supplier_scores")
    op.drop_table("supplier_scores")
    op.drop_index("ix_supplier_material_exposure_material_id", table_name="supplier_material_exposure")
    op.drop_index("ix_supplier_material_exposure_supplier_id", table_name="supplier_material_exposure")
    op.drop_table("supplier_material_exposure")
    op.drop_index("ix_risk_events_event_date", table_name="risk_events")
    op.drop_index("ix_risk_events_event_type", table_name="risk_events")
    op.drop_index("ix_risk_events_source_document_id", table_name="risk_events")
    op.drop_table("risk_events")
    op.drop_index("ix_regulations_source_document_id", table_name="regulations")
    op.drop_table("regulations")
    op.drop_index("ix_trade_flows_hs_code", table_name="trade_flows")
    op.drop_index("ix_trade_flows_period", table_name="trade_flows")
    op.drop_index("ix_trade_flows_source_document_id", table_name="trade_flows")
    op.drop_table("trade_flows")
    op.drop_index("ix_entity_links_to", table_name="entity_links")
    op.drop_index("ix_entity_links_from", table_name="entity_links")
    op.drop_table("entity_links")
    op.drop_index("ix_document_chunks_source_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_source_documents_checksum", table_name="source_documents")
    op.drop_index("ix_source_documents_source_id", table_name="source_documents")
    op.drop_table("source_documents")
    op.drop_index("ix_supplier_aliases_alias", table_name="supplier_aliases")
    op.drop_index("ix_supplier_aliases_supplier_id", table_name="supplier_aliases")
    op.drop_table("supplier_aliases")
    op.drop_index("ix_suppliers_public_ticker", table_name="suppliers")
    op.drop_index("ix_suppliers_headquarters_country", table_name="suppliers")
    op.drop_table("suppliers")
    op.drop_index("ix_materials_category", table_name="materials")
    op.drop_table("materials")
    op.drop_index("ix_raw_api_payloads_checksum", table_name="raw_api_payloads")
    op.drop_index("ix_raw_api_payloads_source_id", table_name="raw_api_payloads")
    op.drop_index("ix_raw_api_payloads_ingestion_run_id", table_name="raw_api_payloads")
    op.drop_table("raw_api_payloads")
    op.drop_index("ix_ingestion_runs_status", table_name="ingestion_runs")
    op.drop_index("ix_ingestion_runs_source_id", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")
    op.drop_index("ix_sources_phase", table_name="sources")
    op.drop_index("ix_sources_source_type", table_name="sources")
    op.drop_table("sources")
    op.drop_index("ix_organizations_org_type", table_name="organizations")
    op.drop_index("ix_organizations_name", table_name="organizations")
    op.drop_table("organizations")
    op.drop_index("ix_countries_iso2", table_name="countries")
    op.drop_table("countries")
