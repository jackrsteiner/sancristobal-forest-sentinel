"""split the methodology into raster lineage + detection layers

Revision ID: 0020_raster_lineage
Revises: 0019_run_aoi_geometry_hash
Create Date: 2026-07-19

The raster-shaping parameters (script pin, collections, scale, masked
categories, baseline window) hash into a content-addressed ``raster_lineage``,
and index/change rasters re-key from the full methodology onto it — so a
detection-parameter change (threshold, min area, forest mask) reuses every COG
(config-inventory Finding 1). The backfill derives each existing methodology's
lineage from its stored parameters with the pre-split ``ee_script_version``
doubling as the raster pin, which makes backfilled lineages content-match the
ones new runs derive: existing rasters stay reusable across this upgrade.
"""

import hashlib
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0020_raster_lineage"
down_revision: str | None = "0019_run_aoi_geometry_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of forest_sentinel.methodology.raster_parameters/auto_version at
# the time of this migration — migrations must not drift with the code.
_RASTER_PARAM_KEYS = (
    "raster_script_version",
    "collections",
    "collection",
    "metric",
    "orbit_policy",
    "scale_m",
    "masked_categories",
    "baseline_window",
)


def _raster_subset(parameters: dict[str, Any]) -> dict[str, Any]:
    subset = {key: parameters[key] for key in _RASTER_PARAM_KEYS if key in parameters}
    if "raster_script_version" not in subset and "ee_script_version" in parameters:
        subset["raster_script_version"] = parameters["ee_script_version"]
    return subset


def _auto_version(parameters: dict[str, Any]) -> str:
    canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    return "auto-" + hashlib.sha256(canonical.encode()).hexdigest()[:10]


def upgrade() -> None:
    op.create_table(
        "raster_lineage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("parameters", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "version", name="uq_raster_lineage_name_version"),
    )

    op.add_column(
        "methodology_version", sa.Column("raster_lineage_id", sa.Integer(), nullable=True)
    )
    op.add_column("index_raster", sa.Column("raster_lineage_id", sa.Integer(), nullable=True))
    op.add_column("change_raster", sa.Column("raster_lineage_id", sa.Integer(), nullable=True))

    # Backfill: derive each methodology's lineage from its stored parameters.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, name, parameters FROM methodology_version ORDER BY id")
    ).all()
    lineage_ids: dict[tuple[str, str], int] = {}
    for row_id, name, parameters in rows:
        subset = _raster_subset(parameters)
        version = _auto_version(subset)
        key = (name, version)
        if key not in lineage_ids:
            existing = bind.execute(
                sa.text("SELECT id FROM raster_lineage WHERE name = :name AND version = :version"),
                {"name": name, "version": version},
            ).scalar()
            if existing is None:
                existing = bind.execute(
                    sa.text(
                        "INSERT INTO raster_lineage (name, version, parameters) "
                        "VALUES (:name, :version, CAST(:parameters AS JSONB)) RETURNING id"
                    ),
                    {"name": name, "version": version, "parameters": json.dumps(subset)},
                ).scalar()
            lineage_ids[key] = existing
        bind.execute(
            sa.text("UPDATE methodology_version SET raster_lineage_id = :lineage WHERE id = :id"),
            {"lineage": lineage_ids[key], "id": row_id},
        )

    for table in ("index_raster", "change_raster"):
        bind.execute(
            sa.text(
                f"UPDATE {table} SET raster_lineage_id = ("  # noqa: S608 - fixed table names
                "SELECT raster_lineage_id FROM methodology_version "
                f"WHERE methodology_version.id = {table}.methodology_version_id)"
            )
        )

    op.alter_column("methodology_version", "raster_lineage_id", nullable=False)
    op.alter_column("index_raster", "raster_lineage_id", nullable=False)
    op.alter_column("change_raster", "raster_lineage_id", nullable=False)
    op.create_foreign_key(
        "fk_methodology_version_raster_lineage",
        "methodology_version",
        "raster_lineage",
        ["raster_lineage_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_index_raster_raster_lineage",
        "index_raster",
        "raster_lineage",
        ["raster_lineage_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_change_raster_raster_lineage",
        "change_raster",
        "raster_lineage",
        ["raster_lineage_id"],
        ["id"],
    )

    # Extraction markers: "has methodology M extracted from raster R?" cannot be
    # answered by counting candidates (an extraction can yield zero). Pre-split,
    # every committed non-frozen change raster implied extracted candidates under
    # its own methodology — backfill that invariant before the column drops.
    op.create_table(
        "candidate_extraction",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("change_raster_id", sa.Integer(), nullable=False),
        sa.Column("methodology_version_id", sa.Integer(), nullable=False),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["change_raster_id"], ["change_raster.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["methodology_version_id"],
            ["methodology_version.id"],
            name="fk_candidate_extraction_methodology",
        ),
        sa.UniqueConstraint(
            "change_raster_id",
            "methodology_version_id",
            name="uq_candidate_extraction_identity",
        ),
    )
    bind.execute(
        sa.text(
            "INSERT INTO candidate_extraction (change_raster_id, methodology_version_id) "
            "SELECT id, methodology_version_id FROM change_raster"
        )
    )

    # Re-key the artifact identities from the full methodology onto the lineage,
    # then drop the methodology FK columns — candidates/events keep theirs.
    op.drop_constraint("uq_index_raster_identity", "index_raster", type_="unique")
    op.create_unique_constraint(
        "uq_index_raster_identity",
        "index_raster",
        ["observation_id", "index_type", "raster_lineage_id"],
    )
    op.drop_constraint("uq_change_raster_identity", "change_raster", type_="unique")
    op.create_unique_constraint(
        "uq_change_raster_identity",
        "change_raster",
        ["observation_id", "change_type", "raster_lineage_id"],
    )
    op.drop_column("index_raster", "methodology_version_id")
    op.drop_column("change_raster", "methodology_version_id")


def downgrade() -> None:
    op.drop_table("candidate_extraction")
    # Restore the methodology FK columns, repointing each raster at the first
    # methodology of its lineage (the exact original is unrecoverable when
    # several methodologies shared one lineage — they shared the artifact too).
    op.add_column("index_raster", sa.Column("methodology_version_id", sa.Integer(), nullable=True))
    op.add_column("change_raster", sa.Column("methodology_version_id", sa.Integer(), nullable=True))
    bind = op.get_bind()
    for table in ("index_raster", "change_raster"):
        bind.execute(
            sa.text(
                f"UPDATE {table} SET methodology_version_id = ("  # noqa: S608 - fixed table names
                "SELECT MIN(methodology_version.id) FROM methodology_version "
                f"WHERE methodology_version.raster_lineage_id = {table}.raster_lineage_id)"
            )
        )
    op.alter_column("index_raster", "methodology_version_id", nullable=False)
    op.alter_column("change_raster", "methodology_version_id", nullable=False)
    op.create_foreign_key(
        "index_raster_methodology_version_id_fkey",
        "index_raster",
        "methodology_version",
        ["methodology_version_id"],
        ["id"],
    )
    op.create_foreign_key(
        "change_raster_methodology_version_id_fkey",
        "change_raster",
        "methodology_version",
        ["methodology_version_id"],
        ["id"],
    )
    op.drop_constraint("uq_index_raster_identity", "index_raster", type_="unique")
    op.create_unique_constraint(
        "uq_index_raster_identity",
        "index_raster",
        ["observation_id", "index_type", "methodology_version_id"],
    )
    op.drop_constraint("uq_change_raster_identity", "change_raster", type_="unique")
    op.create_unique_constraint(
        "uq_change_raster_identity",
        "change_raster",
        ["observation_id", "change_type", "methodology_version_id"],
    )
    op.drop_column("index_raster", "raster_lineage_id")
    op.drop_column("change_raster", "raster_lineage_id")
    op.drop_constraint(
        "fk_methodology_version_raster_lineage", "methodology_version", type_="foreignkey"
    )
    op.drop_column("methodology_version", "raster_lineage_id")
    op.drop_table("raster_lineage")
