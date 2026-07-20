"""create manual_review table

Revision ID: 0013_manual_review
Revises: 0012_methodology_display_version
Create Date: 2026-07-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_manual_review"
down_revision: str | None = "0012_methodology_display_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manual_review",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "event_id",
            sa.Integer(),
            sa.ForeignKey("disturbance_event.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("opinion", sa.String(), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("reviewer", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # Rendered ck_manual_review_opinion by the naming convention alembic applies.
        sa.CheckConstraint(
            "opinion IN ('confirmed', 'false_positive', 'uncertain', 'resolved')",
            name="opinion",
        ),
    )
    op.create_index("ix_manual_review_event_id", "manual_review", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_manual_review_event_id", table_name="manual_review")
    op.drop_table("manual_review")
