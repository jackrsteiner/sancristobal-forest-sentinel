"""create disturbance_candidate table

Revision ID: 0007_disturbance_candidate
Revises: 0006_change_raster
Create Date: 2026-05-24

"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision: str = "0007_disturbance_candidate"
down_revision: str | None = "0006_change_raster"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "disturbance_candidate",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("change_raster_id", sa.Integer(), nullable=False),
        sa.Column("methodology_version_id", sa.Integer(), nullable=False),
        sa.Column(
            "geometry",
            geoalchemy2.Geometry(geometry_type="POLYGON", srid=4326),
            nullable=False,
        ),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("area_m2", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["change_raster_id"],
            ["change_raster.id"],
            name="fk_disturbance_candidate_change_raster_id_change_raster",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["methodology_version_id"],
            ["methodology_version.id"],
            name="fk_disturbance_candidate_methodology",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_disturbance_candidate"),
    )
    op.create_index(
        "ix_disturbance_candidate_change_raster_id",
        "disturbance_candidate",
        ["change_raster_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_disturbance_candidate_change_raster_id", table_name="disturbance_candidate")
    op.drop_table("disturbance_candidate")
