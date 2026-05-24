from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text


def test_migrations_create_aoi_table(alembic_config: Config, clean_database: Engine) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    assert "aoi" in inspector.get_table_names()

    columns = {column["name"] for column in inspector.get_columns("aoi")}
    assert {"id", "name", "geometry", "created_at"} <= columns

    # The geometry column is registered with PostGIS as a MULTIPOLYGON in EPSG:4326.
    with clean_database.connect() as connection:
        row = connection.execute(
            text(
                "SELECT type, srid FROM geometry_columns "
                "WHERE f_table_name = 'aoi' AND f_geometry_column = 'geometry'"
            )
        ).one()
    assert row[0] == "MULTIPOLYGON"
    assert row[1] == 4326


def test_downgrade_removes_aoi_table(alembic_config: Config, clean_database: Engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    assert "aoi" not in inspect(clean_database).get_table_names()


def test_migrations_create_observation_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    assert "observation" in inspector.get_table_names()

    columns = {column["name"] for column in inspector.get_columns("observation")}
    assert {
        "id",
        "aoi_id",
        "sensor",
        "acquired_at",
        "source_scene_id",
        "cloud_cover_percent",
        "created_at",
    } <= columns

    unique_constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("observation")
    }
    assert "uq_observation_aoi_id_source_scene_id" in unique_constraints


def test_downgrade_removes_observation_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    assert "observation" not in inspect(clean_database).get_table_names()


def test_migrations_create_methodology_version_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    assert "methodology_version" in inspector.get_table_names()

    columns = {column["name"] for column in inspector.get_columns("methodology_version")}
    assert {"id", "name", "version", "parameters", "created_at"} <= columns

    unique_constraints = {
        c["name"] for c in inspector.get_unique_constraints("methodology_version")
    }
    assert "uq_methodology_version_name_version" in unique_constraints


def test_downgrade_removes_methodology_version_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    assert "methodology_version" not in inspect(clean_database).get_table_names()


def test_migrations_create_quality_mask_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    assert "quality_mask" in inspector.get_table_names()

    columns = {column["name"] for column in inspector.get_columns("quality_mask")}
    assert {"observation_id", "valid_pixel_fraction", "parameters", "created_at"} <= columns


def test_downgrade_removes_quality_mask_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    assert "quality_mask" not in inspect(clean_database).get_table_names()


def test_migrations_create_index_raster_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    assert "index_raster" in inspector.get_table_names()

    columns = {column["name"] for column in inspector.get_columns("index_raster")}
    assert {
        "id",
        "observation_id",
        "methodology_version_id",
        "index_type",
        "cog_path",
        "valid_pixel_fraction",
    } <= columns


def test_downgrade_removes_index_raster_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    assert "index_raster" not in inspect(clean_database).get_table_names()


def test_migrations_create_change_raster_tables(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    tables = set(inspector.get_table_names())
    assert {"change_raster", "change_raster_source"} <= tables

    columns = {column["name"] for column in inspector.get_columns("change_raster")}
    assert {
        "id",
        "observation_id",
        "methodology_version_id",
        "change_type",
        "cog_path",
        "baseline_window",
        "valid_pixel_fraction",
    } <= columns


def test_downgrade_removes_change_raster_tables(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    tables = set(inspect(clean_database).get_table_names())
    assert "change_raster" not in tables
    assert "change_raster_source" not in tables


def test_migrations_create_disturbance_candidate_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    assert "disturbance_candidate" in inspector.get_table_names()

    columns = {column["name"] for column in inspector.get_columns("disturbance_candidate")}
    assert {
        "id",
        "change_raster_id",
        "methodology_version_id",
        "geometry",
        "detected_at",
        "area_m2",
    } <= columns

    # The geometry column is registered with PostGIS as a POLYGON in EPSG:4326.
    with clean_database.connect() as connection:
        row = connection.execute(
            text(
                "SELECT type, srid FROM geometry_columns "
                "WHERE f_table_name = 'disturbance_candidate' AND f_geometry_column = 'geometry'"
            )
        ).one()
    assert row[0] == "POLYGON"
    assert row[1] == 4326


def test_downgrade_removes_disturbance_candidate_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    assert "disturbance_candidate" not in inspect(clean_database).get_table_names()
