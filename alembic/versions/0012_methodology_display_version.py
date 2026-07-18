"""add a semantic display_version to methodology_version

Revision ID: 0012_methodology_display_version
Revises: 0011_candidate_statistics
Create Date: 2026-07-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_methodology_display_version"
down_revision: str | None = "0011_candidate_statistics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("methodology_version", sa.Column("display_version", sa.String(), nullable=True))
    op.create_unique_constraint(
        "uq_methodology_version_name_display", "methodology_version", ["name", "display_version"]
    )

    # Backfill existing rows with the same bump rule new mints use (see
    # forest_sentinel.methodology.next_display_version): per name in mint (id)
    # order, starting at 1.0.0 — a changed ee_script_version bumps the minor
    # version, any other parameter change bumps the patch version.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, name, parameters FROM methodology_version ORDER BY name, id")
    ).mappings()
    latest: dict[str, tuple[int, int, int, object]] = {}
    for row in rows:
        script = (row["parameters"] or {}).get("ee_script_version")
        if row["name"] not in latest:
            version = (1, 0, 0)
        else:
            major, minor, patch, previous_script = latest[row["name"]]
            version = (
                (major, minor + 1, 0)
                if script != previous_script
                else (
                    major,
                    minor,
                    patch + 1,
                )
            )
        latest[row["name"]] = (*version, script)
        bind.execute(
            sa.text("UPDATE methodology_version SET display_version = :v WHERE id = :id"),
            {"v": "{}.{}.{}".format(*version), "id": row["id"]},
        )


def downgrade() -> None:
    op.drop_constraint("uq_methodology_version_name_display", "methodology_version", type_="unique")
    op.drop_column("methodology_version", "display_version")
