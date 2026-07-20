"""create context_layer and context_feature tables

Revision ID: 0017_context_layers
Revises: 0016_radar_baseline_provenance
Create Date: 2026-07-18

"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0017_context_layers"
down_revision: str | None = "0016_radar_baseline_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = "('concession', 'protected_area', 'road', 'river', 'settlement', 'mill', 'port', 'other')"


def upgrade() -> None:
    op.create_table(
        "context_layer",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("source_file", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("name", name="uq_context_layer_name"),
        # Rendered ck_context_layer_kind by the naming convention.
        sa.CheckConstraint(f"kind IN {_KINDS}", name="kind"),
    )
    op.create_table(
        "context_feature",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "context_layer_id",
            sa.Integer(),
            sa.ForeignKey(
                "context_layer.id",
                ondelete="CASCADE",
                name="fk_context_feature_context_layer_id_context_layer",
            ),
            nullable=False,
        ),
        # GEOMETRY, not a single type: concessions are polygons, roads/rivers
        # are lines, mills/ports/settlements are points.
        sa.Column(
            "geometry",
            geoalchemy2.Geometry(geometry_type="GEOMETRY", srid=4326),
            nullable=False,
        ),
        sa.Column("properties", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_context_feature_context_layer_id", "context_feature", ["context_layer_id"])


def downgrade() -> None:
    op.drop_index("ix_context_feature_context_layer_id", table_name="context_feature")
    op.drop_table("context_feature")
    op.drop_table("context_layer")
