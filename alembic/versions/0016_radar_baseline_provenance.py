"""add baseline_source_scene_ids to change_raster (radar provenance)

Revision ID: 0016_radar_baseline_provenance
Revises: 0015_sensor_source
Create Date: 2026-07-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0016_radar_baseline_provenance"
down_revision: str | None = "0015_sensor_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: optical deltas keep recording their baseline via
    # change_raster_source; radar deltas record their scene-id recipe here.
    op.add_column("change_raster", sa.Column("baseline_source_scene_ids", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("change_raster", "baseline_source_scene_ids")
