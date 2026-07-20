"""record the AOI geometry hash on pipeline runs

Revision ID: 0019_run_aoi_geometry_hash
Revises: 0018_event_context
Create Date: 2026-07-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_run_aoi_geometry_hash"
down_revision: str | None = "0018_event_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: rows predating the column keep NULL — backfilling would claim
    # knowledge of a footprint the run may not have actually scanned with.
    op.add_column("pipeline_run", sa.Column("aoi_geometry_hash", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("pipeline_run", "aoi_geometry_hash")
