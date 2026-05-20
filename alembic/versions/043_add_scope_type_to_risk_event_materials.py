"""Add ``risk_event_materials.scope_type``.

Background
----------
``RegulationMaterialScope`` carries a ``scope_type`` value
(``banned`` / ``restricted`` / ``strategic_raw_material`` /
``covered`` / ``disclosure_required``) that today is consumed once at
ingest time — ``_attach_material_scope_to_event`` maps it to a
relevance score on the ``risk_event_materials`` junction row.  After
that point the original designation is thrown away: the junction row
only retains a ``match_reason`` string and a numeric relevance, so
the scoring pipeline can't distinguish a ``banned`` regulation from a
``covered`` one when computing impact for a given material.

The visible symptom: every regulation event lands at roughly the
same severity for every material it scopes — per-material spread
inside a single regulation is ≤16%, which is too narrow for
analysts to use the per-material breakdown as a ranking signal.

This migration adds a nullable ``scope_type`` column directly on
``risk_event_materials`` so the value carries through to the scoring
layer.  ``evidence_query.EventWithRelevance`` is being extended to
surface it, and ``evidence_aggregator._impact`` will multiply the
event severity by a scope-derived factor (1.50 for ``banned`` down
to 0.50 for ``disclosure_required``) before calling
``compute_event_impact``.  Net effect: per-material spread inside
a single regulation widens to ~3× — meaningful for ranking.

Nullable on purpose: pre-migration rows have no scope_type recorded.
``apply_scope_severity_multiplier`` treats ``None`` as a passthrough
(severity unchanged) so backwards compatibility holds.  New rows
written by ``_attach_material_scope_to_event`` and
``backfill_regulation_event_materials`` populate the column from
``RegulationMaterialScope.scope_type``.

Revision ID: 043
Revises:     042
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "043"
down_revision: Union[str, None] = "042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "risk_event_materials",
        sa.Column(
            "scope_type",
            sa.String(length=64),
            nullable=True,
            comment=(
                "Copied from RegulationMaterialScope.scope_type at the time "
                "the junction row was written.  Consumed by "
                "evidence_aggregator._impact via "
                "apply_scope_severity_multiplier so per-material event "
                "severity reflects the strength of the regulation's "
                "designation (banned > restricted > strategic_raw_material "
                "> covered > disclosure_required).  NULL for non-regulation "
                "events and for pre-migration rows."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("risk_event_materials", "scope_type")
