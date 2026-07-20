"""create confidence_assessment table

Revision ID: 0014_confidence_assessment
Revises: 0013_manual_review
Create Date: 2026-07-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0014_confidence_assessment"
down_revision: str | None = "0013_manual_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "confidence_assessment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "event_id",
            sa.Integer(),
            sa.ForeignKey("disturbance_event.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "pipeline_run_id",
            sa.Integer(),
            sa.ForeignKey("pipeline_run.id", name="fk_confidence_assessment_run"),
            nullable=True,
        ),
        sa.Column("level", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("inputs", JSONB(), nullable=False),
        sa.Column("rule_version", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # Rendered ck_confidence_assessment_level by the naming convention.
        sa.CheckConstraint("level IN ('low', 'medium', 'high')", name="level"),
    )
    op.create_index("ix_confidence_assessment_event_id", "confidence_assessment", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_confidence_assessment_event_id", table_name="confidence_assessment")
    op.drop_table("confidence_assessment")
