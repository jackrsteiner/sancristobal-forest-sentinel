"""create event_context table

Revision ID: 0018_event_context
Revises: 0017_context_layers
Create Date: 2026-07-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_event_context"
down_revision: str | None = "0017_context_layers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_context",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "event_id",
            sa.Integer(),
            sa.ForeignKey(
                "disturbance_event.id",
                ondelete="CASCADE",
                name="fk_event_context_event_id_disturbance_event",
            ),
            nullable=False,
        ),
        sa.Column(
            "context_feature_id",
            sa.Integer(),
            sa.ForeignKey(
                "context_feature.id",
                ondelete="CASCADE",
                name="fk_event_context_context_feature_id_context_feature",
            ),
            nullable=False,
        ),
        sa.Column("relation", sa.String(), nullable=False),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # Rendered ck_event_context_relation by the naming convention.
        sa.CheckConstraint("relation IN ('contains', 'intersects', 'nearby')", name="relation"),
    )
    op.create_index("ix_event_context_event_id", "event_context", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_event_context_event_id", table_name="event_context")
    op.drop_table("event_context")
