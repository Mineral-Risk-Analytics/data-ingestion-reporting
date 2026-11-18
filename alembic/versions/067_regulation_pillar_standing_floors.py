"""Regulation pillar reassignment — stock vs flow, schema only.

Revision ID: 067
Revises: 066

Pillar reassignment piece 1 (docs/design/regulation_pillar_reassignment.md,
evidence pass: docs/design/standing_measure_floor_evidence.md; decided
2026-09-20 with Nicole). The registry gains the vocabulary to distinguish
*compliance burden* (standing law imposing cost/liability on companies)
from *trade-flow interference* (bans, quotas, tariffs) — the root cause of
three defects: supply-side rows double-counting into both pillars under two
names, supply cutoffs mislabelled as compliance burden, and standing bans
vanishing from the geopolitical pillar once their announcement events age
past the 730-day evidence window while compliance keeps paying rent.

``regulations`` gains:

  pillar                       regulatory_compliance | geopolitical_trade |
                               dual. The regime's FUNCTIONAL classification
                               (display/primary grouping). The enum records
                               character; the numbers below decide the
                               arithmetic.
  standing_export_restriction  [0,1] persistent floor under the geo
                               pillar's export sub-input. 1.0 = a total,
                               fully-enforced block of all exports of the
                               material from the implementing country.
  standing_tariff_exposure     [0,1] persistent floor under the geo
                               pillar's tariff sub-input. No current row
                               uses it; first candidate is the US
                               steel/aluminium regime (decision parked).
  floor_review_date            When the floor is known to change character
                               (suspension expiry, phased ban landing —
                               three CN suspensions expire 2026-11, ZW's
                               concentrate ban lands 2027-01-01). Surfaced
                               as a workbook chore, not enforced in code.

Each standing weight feeds exactly ONE pillar, so a regime cannot
double-count by construction; ``dual`` rows carry reduced obligation
points AND a floor, each feeding its own pillar. ``status`` vocabulary
gains ``suspended`` (string column — enforced by the workbook loader,
piece 2, like the existing vocabulary).

Backfill (grandfather rule, mirroring 066): every existing row displays
and scores as a compliance regime today, so all rows are stamped
``pillar = 'regulatory_compliance'``. The workbook reclassification
(piece 4, gated on the partner review) moves the flow rows over via the
loader sync. Floors start NULL everywhere.

NOTHING reads these columns yet: the aggregator floor term
(``_derive_market_geopolitical_inputs``) is piece 3, and both affected
pillars are weight 0 under the concentration-only launch (SCORING_VERSION
5.0) — behavior is provably unchanged at migration time.
"""

from alembic import op
import sqlalchemy as sa

revision = "067"
down_revision = "066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regulations",
        sa.Column(
            "pillar",
            sa.String(64),
            nullable=True,
            comment=(
                "regulatory_compliance | geopolitical_trade | dual — the "
                "regime's functional classification (display/primary). "
                "The standing floors, not this enum, decide the arithmetic."
            ),
        ),
    )
    op.add_column(
        "regulations",
        sa.Column(
            "standing_export_restriction",
            sa.Float(),
            nullable=True,
            comment=(
                "[0,1] persistent floor under the geopolitical pillar's "
                "export sub-input; 1.0 = total, fully-enforced block of all "
                "exports of the material from the implementing country. "
                "NULL = no floor. Only enacted/effective rows contribute "
                "(loader-enforced)."
            ),
        ),
    )
    op.add_column(
        "regulations",
        sa.Column(
            "standing_tariff_exposure",
            sa.Float(),
            nullable=True,
            comment=(
                "[0,1] persistent floor under the geopolitical pillar's "
                "tariff sub-input. NULL = no floor."
            ),
        ),
    )
    op.add_column(
        "regulations",
        sa.Column(
            "floor_review_date",
            sa.Date(),
            nullable=True,
            comment=(
                "When this floor is known to change character (suspension "
                "expiry, phased ban taking effect). Workbook chore, not "
                "enforced in code."
            ),
        ),
    )

    # ── Grandfather backfill ────────────────────────────────────────────
    # Every existing row is displayed and scored as a compliance regime
    # today; stamping that makes the current state explicit instead of
    # "unclassified". The piece-4 workbook edits reclassify the flow rows
    # through the loader sync. No floor values are seeded here.
    op.execute(
        "UPDATE regulations SET pillar = 'regulatory_compliance' "
        "WHERE pillar IS NULL"
    )


def downgrade() -> None:
    op.drop_column("regulations", "floor_review_date")
    op.drop_column("regulations", "standing_tariff_exposure")
    op.drop_column("regulations", "standing_export_restriction")
    op.drop_column("regulations", "pillar")
