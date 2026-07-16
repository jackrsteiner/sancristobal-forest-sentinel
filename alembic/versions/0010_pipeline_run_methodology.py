"""add methodology_version_id to pipeline_run

Revision ID: 0010_pipeline_run_methodology
Revises: 0009_pipeline_run
Create Date: 2026-07-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_pipeline_run_methodology"
down_revision: str | None = "0009_pipeline_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipeline_run",
        sa.Column("methodology_version_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_pipeline_run_methodology",
        "pipeline_run",
        "methodology_version",
        ["methodology_version_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_pipeline_run_methodology", "pipeline_run", type_="foreignkey")
    op.drop_column("pipeline_run", "methodology_version_id")
