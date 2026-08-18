"""Breadth discount + direct/broad distinction on event-material links.

Nicole 2026-07-15: GTA events average 7.9 material tags (20,389 links on
2,576 events) because broad measures — omnibus tariff lists, cleantech
subsidy schemes, export-duty schedules — match dozens of tracked HS
codes.  Per material, such an event is weak evidence; and on material
pages it is noise the partner has to sift through.  Two changes:

``risk_event_materials.is_direct``
    TRUE when the event is tagged to <= 3 materials (live distribution
    is bimodal: 43%% of GTA events are single-material; 609 events carry
    13+ tags; nothing meaningful in between changes at the boundary).
    manual_walkthrough links are EXEMPT and stay direct regardless of
    count — a human deliberately attributed those materials (1 live
    event with 4 tags), unlike mechanical HS fan-out.
    Material-scoped UI surfaces (material pages, per-material counts,
    evidence drill-downs) show direct links only; broad events remain
    fully visible in the cross-material "industry events" surface.
    Title-mention rescue was evaluated and rejected: on >3-material
    events the material name effectively never appears in the title
    (56/19,225 links matched, nearly all substring false positives).

Relevance breadth discount (data backfill, GTA + IEA only)
    ``relevance_score *= min(1.0, 3.0/n)`` where n = number of material
    links on the event — applied to BOTH risk_event_materials and
    risk_event_hs_mappings rows, so the discount also damps the L0
    tariff/export drip from recurring omnibus measures.  n=1-3 keeps
    full weight; n=6 -> 0.5; n=34 -> 0.09.  Manual-walkthrough links
    are NOT touched (human-graded relevance per the partner workflow);
    SEC / FedReg / EUR-Lex are not touched either (out of scope here —
    97%% of FedReg events are single-material anyway).
    Scoring needs no changes: junction relevance already flows through
    the event-impact formula (EventWithRelevance /
    relevance_score_to_multiplier; 11.7-HS-F4 for the HS side).

The GTA and IEA ingesters apply the same rule at write time from this
migration forward, so re-ingests produce identical values (idempotent
with the backfill).

Revision ID: 056
Revises: 055
"""

from alembic import op
import sqlalchemy as sa

revision = "056"
down_revision = "055"
branch_labels = None
depends_on = None

_BREADTH_SOURCES = ("Global Trade Alert", "IEA Critical Minerals Policy Tracker")


def upgrade() -> None:
    op.add_column(
        "risk_event_materials",
        sa.Column(
            "is_direct",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.create_index(
        "ix_risk_event_materials_is_direct",
        "risk_event_materials",
        ["is_direct"],
    )

    # ── Backfill is_direct (breadth rule; manual_walkthrough exempt) ────
    op.execute(
        """
        WITH counts AS (
            SELECT risk_event_id, count(*) AS n
            FROM risk_event_materials
            GROUP BY risk_event_id
        )
        UPDATE risk_event_materials rem
        SET is_direct = (c.n <= 3)
        FROM counts c, risk_events e, source_documents sd, sources s
        WHERE c.risk_event_id = rem.risk_event_id
          AND e.id = rem.risk_event_id
          AND sd.id = e.source_document_id
          AND s.id = sd.source_id
          AND s.name != 'manual_walkthrough'
        """
    )

    # ── Backfill breadth-discounted relevance (GTA + IEA only) ──────────
    src_list = ", ".join(f"'{s}'" for s in _BREADTH_SOURCES)
    op.execute(
        f"""
        WITH counts AS (
            SELECT rem.risk_event_id, count(*) AS n
            FROM risk_event_materials rem
            GROUP BY rem.risk_event_id
        ),
        scoped AS (
            SELECT e.id AS event_id, c.n
            FROM risk_events e
            JOIN source_documents sd ON sd.id = e.source_document_id
            JOIN sources s ON s.id = sd.source_id
            JOIN counts c ON c.risk_event_id = e.id
            WHERE s.name IN ({src_list}) AND c.n > 3
        )
        UPDATE risk_event_materials rem
        SET relevance_score = rem.relevance_score * (3.0 / sc.n)
        FROM scoped sc
        WHERE sc.event_id = rem.risk_event_id
        """
    )
    op.execute(
        f"""
        WITH counts AS (
            SELECT rem.risk_event_id, count(*) AS n
            FROM risk_event_materials rem
            GROUP BY rem.risk_event_id
        ),
        scoped AS (
            SELECT e.id AS event_id, c.n
            FROM risk_events e
            JOIN source_documents sd ON sd.id = e.source_document_id
            JOIN sources s ON s.id = sd.source_id
            JOIN counts c ON c.risk_event_id = e.id
            WHERE s.name IN ({src_list}) AND c.n > 3
        )
        UPDATE risk_event_hs_mappings ehm
        SET relevance_score = ehm.relevance_score * (3.0 / sc.n)
        FROM scoped sc
        WHERE sc.event_id = ehm.risk_event_id
        """
    )


def downgrade() -> None:
    # Relevance discounts are NOT reversible (original values folded in) —
    # a re-ingest (reset-events + reingest-all-events) restores them.
    op.drop_index(
        "ix_risk_event_materials_is_direct", table_name="risk_event_materials"
    )
    op.drop_column("risk_event_materials", "is_direct")
