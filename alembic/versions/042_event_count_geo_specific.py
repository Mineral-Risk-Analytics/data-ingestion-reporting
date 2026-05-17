"""Add ``material_geography_risk_scores.event_count_geo_specific``.

Background
----------
``event_count`` on this table has always stored the **union** of
material-anchored and geography-anchored event lists (see
``market_aggregator._score_market_for_geography`` — ``total_event_count``
is the size of ``dedup(material_trade_events, geo_trade_events, ...)``).

Because the material-anchored half returns every event tagged to the
material regardless of geography, every country inherits the full
per-material event pool plus a thin per-country sliver from the
geo-anchored half.  The visible symptom (2026-05-12): for Natural
Graphite, all 113 country rows had ``event_count`` in 117–158 — the
floor (~115) is the material-anchored count; per-country variance is
≤10% on top of that.

That's the correct number to expose to the **scoring pillars** —
geographically-agnostic material policies (EU CRMA, US IRA) genuinely
affect every country — but it's the wrong number to show in the UI
"Events" column or to use in a "this country has material exposure"
filter.  Analysts read ``event_count`` as "events about this material
in this country", which is the **intersection**
``RiskEventMaterial ∩ RiskEventGeography``.  That intersection produces
sensibly differentiated counts: for Graphite, CN=66, JP=23, UA=9,
AU=8, NZ=3, CD=2 — an ordering that matches actual supply-chain
salience and is 3–30× smaller than the stored union.

This migration adds ``event_count_geo_specific`` so we can store both:

  - ``event_count`` (existing column, unchanged semantics): size of the
    event union consumed by the pillar sub-input derivation.  Preserves
    the audit trail — the column reflects what was actually fed into
    the score.

  - ``event_count_geo_specific`` (new, nullable): size of the
    intersection — events tagged to BOTH this material AND this
    country.  This is what the UI ``Events`` column will display and
    what the Country-scores filter will key off.

The column is nullable on purpose: existing pre-migration rows have no
intersection value computed.  The next ``POST /market/rescore`` run
will populate it for every (material, geography) pair the aggregator
touches.  A null in the UI signals "stale — rescore needed", which is
preferable to silently displaying a misleading value.

Revision ID: 042
Revises:     041
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "042"
down_revision: Union[str, None] = "041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "material_geography_risk_scores",
        sa.Column(
            "event_count_geo_specific",
            sa.Integer(),
            nullable=True,
            comment=(
                "Count of events at the intersection "
                "RiskEventMaterial ∩ RiskEventGeography for this "
                "(material_id, geography_code) pair, within each "
                "category's lookback window.  Distinct from "
                "``event_count`` which stores the UNION consumed by the "
                "pillar sub-input derivation.  NULL on rows produced "
                "before migration 042 — re-run ``POST /market/rescore`` "
                "to populate."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "material_geography_risk_scores", "event_count_geo_specific"
    )
