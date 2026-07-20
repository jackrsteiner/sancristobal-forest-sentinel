"""create sensor_source registry; add orbit fields to observation

Revision ID: 0015_sensor_source
Revises: 0014_confidence_assessment
Create Date: 2026-07-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0015_sensor_source"
down_revision: str | None = "0014_confidence_assessment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The datasets behind every observation.sensor value written so far, plus the
# Sentinel-1 source Slice 5 introduces.
_SEED = (
    ("HLSL30", "optical", "NASA/HLS/HLSL30/v002", '{"platform": "Landsat 8/9"}'),
    ("HLSS30", "optical", "NASA/HLS/HLSS30/v002", '{"platform": "Sentinel-2"}'),
    ("S1GRD", "radar", "COPERNICUS/S1_GRD", '{"mode": "IW", "polarisation": "VV"}'),
)


def upgrade() -> None:
    op.create_table(
        "sensor_source",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("collection", sa.String(), nullable=False),
        sa.Column("details", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("name", name="uq_sensor_source_name"),
        # Rendered ck_sensor_source_kind by the naming convention.
        sa.CheckConstraint("kind IN ('optical', 'radar')", name="kind"),
    )
    bind = op.get_bind()
    for name, kind, collection, details in _SEED:
        bind.execute(
            sa.text(
                "INSERT INTO sensor_source (name, kind, collection, details) "
                "VALUES (:n, :k, :c, CAST(:d AS jsonb))"
            ),
            {"n": name, "k": kind, "c": collection, "d": details},
        )

    op.add_column("observation", sa.Column("orbit_direction", sa.String(), nullable=True))
    op.add_column("observation", sa.Column("relative_orbit", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("observation", "relative_orbit")
    op.drop_column("observation", "orbit_direction")
    op.drop_table("sensor_source")
