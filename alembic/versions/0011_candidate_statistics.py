"""add per-candidate change statistics to disturbance_candidate

Revision ID: 0011_candidate_statistics
Revises: 0010_pipeline_run_methodology
Create Date: 2026-07-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_candidate_statistics"
down_revision: str | None = "0010_pipeline_run_methodology"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: rows extracted before these columns existed stay null — statistics
    # are captured only at extraction time, never backfilled from stored COGs.
    op.add_column("disturbance_candidate", sa.Column("delta_mean", sa.Float(), nullable=True))
    op.add_column("disturbance_candidate", sa.Column("delta_min", sa.Float(), nullable=True))
    op.add_column(
        "disturbance_candidate", sa.Column("valid_pixel_fraction", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("disturbance_candidate", "valid_pixel_fraction")
    op.drop_column("disturbance_candidate", "delta_min")
    op.drop_column("disturbance_candidate", "delta_mean")
