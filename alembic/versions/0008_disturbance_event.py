"""create disturbance_event and event_observation tables

Revision ID: 0008_disturbance_event
Revises: 0007_disturbance_candidate
Create Date: 2026-05-24

"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision: str = "0008_disturbance_event"
down_revision: str | None = "0007_disturbance_candidate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "disturbance_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("aoi_id", sa.Integer(), nullable=False),
        sa.Column("methodology_version_id", sa.Integer(), nullable=False),
        sa.Column(
            "geometry",
            geoalchemy2.Geometry(geometry_type="MULTIPOLYGON", srid=4326),
            nullable=False,
        ),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("first_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["aoi_id"], ["aoi.id"], name="fk_disturbance_event_aoi_id_aoi"),
        sa.ForeignKeyConstraint(
            ["methodology_version_id"],
            ["methodology_version.id"],
            name="fk_disturbance_event_methodology",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_disturbance_event"),
    )
    op.create_table(
        "event_observation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("disturbance_candidate_id", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("area_m2", sa.Float(), nullable=False),
        sa.Column("growth_m2", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["disturbance_event.id"],
            name="fk_event_observation_event_id_disturbance_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["disturbance_candidate_id"],
            ["disturbance_candidate.id"],
            name="fk_event_observation_candidate",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_event_observation"),
        sa.UniqueConstraint(
            "disturbance_candidate_id",
            name="uq_event_observation_disturbance_candidate_id",
        ),
    )
    op.create_index("ix_event_observation_event_id", "event_observation", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_event_observation_event_id", table_name="event_observation")
    op.drop_table("event_observation")
    op.drop_table("disturbance_event")
