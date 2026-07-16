"""create pipeline_run and pipeline_run_event tables

Revision ID: 0009_pipeline_run
Revises: 0008_disturbance_event
Create Date: 2026-07-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_pipeline_run"
down_revision: str | None = "0008_disturbance_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipeline_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("aoi_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("since", sa.Date(), nullable=False),
        sa.Column("until", sa.Date(), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["aoi_id"], ["aoi.id"], name="fk_pipeline_run_aoi_id_aoi"),
        sa.PrimaryKeyConstraint("id", name="pk_pipeline_run"),
    )
    op.create_index("ix_pipeline_run_aoi_id_started_at", "pipeline_run", ["aoi_id", "started_at"])
    op.create_table(
        "pipeline_run_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("batch_index", sa.Integer(), nullable=True),
        sa.Column("batch_total", sa.Integer(), nullable=True),
        sa.Column("exports", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["pipeline_run.id"],
            name="fk_pipeline_run_event_run_id_pipeline_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pipeline_run_event"),
    )
    op.create_index("ix_pipeline_run_event_run_id", "pipeline_run_event", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_run_event_run_id", table_name="pipeline_run_event")
    op.drop_table("pipeline_run_event")
    op.drop_index("ix_pipeline_run_aoi_id_started_at", table_name="pipeline_run")
    op.drop_table("pipeline_run")
