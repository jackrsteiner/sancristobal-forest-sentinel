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
        "raster_lineage_id",
        "index_type",
        "cog_path",
        "valid_pixel_fraction",
    } <= columns
    assert "methodology_version_id" not in columns


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
        "raster_lineage_id",
        "change_type",
        "cog_path",
        "baseline_window",
        "valid_pixel_fraction",
    } <= columns
    assert "methodology_version_id" not in columns


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


def test_migrations_create_event_tables(alembic_config: Config, clean_database: Engine) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    tables = set(inspector.get_table_names())
    assert {"disturbance_event", "event_observation"} <= tables

    event_columns = {column["name"] for column in inspector.get_columns("disturbance_event")}
    assert {
        "id",
        "aoi_id",
        "methodology_version_id",
        "geometry",
        "status",
        "first_detected_at",
        "last_detected_at",
    } <= event_columns

    obs_columns = {column["name"] for column in inspector.get_columns("event_observation")}
    assert {
        "id",
        "event_id",
        "disturbance_candidate_id",
        "observed_at",
        "area_m2",
        "growth_m2",
    } <= (obs_columns)

    # The event geometry is registered with PostGIS as a MULTIPOLYGON in EPSG:4326.
    with clean_database.connect() as connection:
        row = connection.execute(
            text(
                "SELECT type, srid FROM geometry_columns "
                "WHERE f_table_name = 'disturbance_event' AND f_geometry_column = 'geometry'"
            )
        ).one()
    assert row[0] == "MULTIPOLYGON"
    assert row[1] == 4326


def test_downgrade_removes_event_tables(alembic_config: Config, clean_database: Engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    tables = set(inspect(clean_database).get_table_names())
    assert "disturbance_event" not in tables
    assert "event_observation" not in tables


def test_migrations_create_pipeline_run_tables(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    tables = set(inspector.get_table_names())
    assert {"pipeline_run", "pipeline_run_event"} <= tables

    run_columns = {column["name"] for column in inspector.get_columns("pipeline_run")}
    assert {
        "id",
        "aoi_id",
        "started_at",
        "finished_at",
        "status",
        "since",
        "until",
        "summary",
    } <= run_columns
    run_indexes = {index["name"] for index in inspector.get_indexes("pipeline_run")}
    assert "ix_pipeline_run_aoi_id_started_at" in run_indexes

    event_columns = {column["name"] for column in inspector.get_columns("pipeline_run_event")}
    assert {
        "id",
        "run_id",
        "occurred_at",
        "stage",
        "batch_index",
        "batch_total",
        "exports",
        "outcome",
        "message",
    } <= event_columns
    event_indexes = {index["name"] for index in inspector.get_indexes("pipeline_run_event")}
    assert "ix_pipeline_run_event_run_id" in event_indexes


def test_downgrade_removes_pipeline_run_tables(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    tables = set(inspect(clean_database).get_table_names())
    assert "pipeline_run" not in tables
    assert "pipeline_run_event" not in tables


def test_migrations_add_pipeline_run_methodology_column(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    columns = {column["name"] for column in inspector.get_columns("pipeline_run")}
    assert "methodology_version_id" in columns
    fks = {fk["name"] for fk in inspector.get_foreign_keys("pipeline_run")}
    assert "fk_pipeline_run_methodology" in fks


def test_downgrade_removes_pipeline_run_methodology_column(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "0009_pipeline_run")

    columns = {column["name"] for column in inspect(clean_database).get_columns("pipeline_run")}
    assert "methodology_version_id" not in columns


def test_migrations_add_candidate_statistics_columns(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    columns = {
        column["name"]: column
        for column in inspect(clean_database).get_columns("disturbance_candidate")
    }
    for name in ("delta_mean", "delta_min", "valid_pixel_fraction"):
        assert name in columns
        assert columns[name]["nullable"]  # pre-#95 rows stay null


def test_downgrade_removes_candidate_statistics_columns(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "0010_pipeline_run_methodology")

    columns = {
        column["name"] for column in inspect(clean_database).get_columns("disturbance_candidate")
    }
    assert columns.isdisjoint({"delta_mean", "delta_min", "valid_pixel_fraction"})


def test_migrations_backfill_methodology_display_versions(
    alembic_config: Config, clean_database: Engine
) -> None:
    """0012 labels pre-existing rows with the same bump rule new mints use."""
    from sqlalchemy import text

    command.upgrade(alembic_config, "0011_candidate_statistics")
    with clean_database.connect() as connection:
        for version, params in (
            ("1.0.0", '{"ee_script_version": "s1"}'),
            ("auto-aaa", '{"ee_script_version": "s1", "threshold": -0.3}'),
            ("auto-bbb", '{"ee_script_version": "s2"}'),
        ):
            connection.execute(
                text(
                    "INSERT INTO methodology_version (name, version, parameters) "
                    "VALUES ('optical-change', :v, CAST(:p AS jsonb))"
                ),
                {"v": version, "p": params},
            )
        connection.commit()

    command.upgrade(alembic_config, "head")

    with clean_database.connect() as connection:
        rows = connection.execute(
            text("SELECT version, display_version FROM methodology_version ORDER BY id")
        ).all()
    # Same script -> patch bump; changed script -> minor bump.
    assert [tuple(row) for row in rows] == [
        ("1.0.0", "1.0.0"),
        ("auto-aaa", "1.0.1"),
        ("auto-bbb", "1.1.0"),
    ]


def test_downgrade_removes_methodology_display_version(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "0011_candidate_statistics")

    columns = {
        column["name"] for column in inspect(clean_database).get_columns("methodology_version")
    }
    assert "display_version" not in columns


def test_migrations_create_manual_review_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    assert "manual_review" in inspector.get_table_names()

    columns = {column["name"] for column in inspector.get_columns("manual_review")}
    assert {"id", "event_id", "opinion", "notes", "reviewer", "created_at"} <= columns

    fks = inspector.get_foreign_keys("manual_review")
    assert any(
        fk["referred_table"] == "disturbance_event" and fk["options"].get("ondelete") == "CASCADE"
        for fk in fks
    )
    checks = {constraint["name"] for constraint in inspector.get_check_constraints("manual_review")}
    assert "ck_manual_review_opinion" in checks
    indexes = {index["name"] for index in inspector.get_indexes("manual_review")}
    assert "ix_manual_review_event_id" in indexes


def test_downgrade_removes_manual_review_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "0012_methodology_display_version")

    assert "manual_review" not in inspect(clean_database).get_table_names()


def test_migrations_create_confidence_assessment_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    assert "confidence_assessment" in inspector.get_table_names()

    columns = {column["name"] for column in inspector.get_columns("confidence_assessment")}
    assert {
        "id",
        "event_id",
        "pipeline_run_id",
        "level",
        "score",
        "inputs",
        "rule_version",
        "created_at",
    } <= columns

    fks = inspector.get_foreign_keys("confidence_assessment")
    assert any(
        fk["referred_table"] == "disturbance_event" and fk["options"].get("ondelete") == "CASCADE"
        for fk in fks
    )
    checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("confidence_assessment")
    }
    assert "ck_confidence_assessment_level" in checks


def test_downgrade_removes_confidence_assessment_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "0013_manual_review")

    assert "confidence_assessment" not in inspect(clean_database).get_table_names()


def test_migrations_create_sensor_source_and_orbit_fields(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    assert "sensor_source" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("sensor_source")}
    assert {"id", "name", "kind", "collection", "details", "created_at"} <= columns

    with clean_database.connect() as connection:
        rows = connection.execute(
            text("SELECT name, kind, collection FROM sensor_source ORDER BY id")
        ).all()
    assert [tuple(row) for row in rows] == [
        ("HLSL30", "optical", "NASA/HLS/HLSL30/v002"),
        ("HLSS30", "optical", "NASA/HLS/HLSS30/v002"),
        ("S1GRD", "radar", "COPERNICUS/S1_GRD"),
    ]

    observation_columns = {c["name"] for c in inspector.get_columns("observation")}
    assert {"orbit_direction", "relative_orbit"} <= observation_columns


def test_downgrade_removes_sensor_source_and_orbit_fields(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "0014_confidence_assessment")

    inspector = inspect(clean_database)
    assert "sensor_source" not in inspector.get_table_names()
    observation_columns = {c["name"] for c in inspector.get_columns("observation")}
    assert observation_columns.isdisjoint({"orbit_direction", "relative_orbit"})


def test_migrations_add_radar_baseline_provenance(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    columns = {
        column["name"]: column for column in inspect(clean_database).get_columns("change_raster")
    }
    assert "baseline_source_scene_ids" in columns
    assert columns["baseline_source_scene_ids"]["nullable"]  # optical rows stay null


def test_downgrade_removes_radar_baseline_provenance(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "0015_sensor_source")
    columns = {c["name"] for c in inspect(clean_database).get_columns("change_raster")}
    assert "baseline_source_scene_ids" not in columns


def test_migrations_create_context_tables(alembic_config: Config, clean_database: Engine) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    assert "context_layer" in inspector.get_table_names()
    assert "context_feature" in inspector.get_table_names()
    layer_columns = {c["name"] for c in inspector.get_columns("context_layer")}
    assert {"id", "name", "kind", "source_file", "created_at"} <= layer_columns
    feature_columns = {c["name"] for c in inspector.get_columns("context_feature")}
    assert {"id", "context_layer_id", "geometry", "properties", "created_at"} <= feature_columns

    # Mixed geometry types by design (polygons, lines, points), in WGS 84.
    with clean_database.connect() as connection:
        row = connection.execute(
            text(
                "SELECT type, srid FROM geometry_columns "
                "WHERE f_table_name = 'context_feature' AND f_geometry_column = 'geometry'"
            )
        ).one()
    assert row[0] == "GEOMETRY"
    assert row[1] == 4326


def test_downgrade_removes_context_tables(alembic_config: Config, clean_database: Engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "0016_radar_baseline_provenance")

    tables = inspect(clean_database).get_table_names()
    assert "context_layer" not in tables
    assert "context_feature" not in tables


def test_migrations_create_event_context_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    assert "event_context" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("event_context")}
    assert {"id", "event_id", "context_feature_id", "relation", "distance_m", "created_at"} <= (
        columns
    )


def test_downgrade_removes_event_context_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "0017_context_layers")

    assert "event_context" not in inspect(clean_database).get_table_names()


def test_migration_0020_backfills_lineages_and_repoints_rasters(
    alembic_config: Config, clean_database: Engine
) -> None:
    """0020 derives each methodology's raster lineage from its stored parameters
    (pre-split ee_script_version doubling as the raster pin), repoints rasters,
    and backfills extraction markers — existing artifacts stay reusable."""
    from sqlalchemy import text

    from forest_sentinel.methodology import auto_version, raster_parameters

    command.upgrade(alembic_config, "0019_run_aoi_geometry_hash")
    parameters = (
        '{"ee_script_version": "slice1-optical-change-v1", "scale_m": 30, '
        '"baseline_window": 5, "delta_nbr_threshold": -0.25}'
    )
    with clean_database.connect() as connection:
        connection.execute(
            text(
                "INSERT INTO aoi (name, geometry) VALUES ('A', "
                "ST_GeomFromText('MULTIPOLYGON(((0 0,1 0,1 1,0 1,0 0)))', 4326))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO observation (aoi_id, sensor, acquired_at, source_scene_id) "
                "VALUES (1, 'HLSL30', '2026-01-06T00:00:00Z', 'scene-6')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO methodology_version (name, version, parameters) "
                "VALUES ('optical-change', '1.0.0', CAST(:p AS jsonb))"
            ),
            {"p": parameters},
        )
        connection.execute(
            text(
                "INSERT INTO index_raster (observation_id, methodology_version_id, "
                "index_type, cog_path) VALUES (1, 1, 'NBR', '/cogs/nbr.tif')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO change_raster (observation_id, methodology_version_id, "
                "change_type, cog_path, baseline_window) "
                "VALUES (1, 1, 'delta_nbr', '/cogs/delta.tif', 5)"
            )
        )
        connection.commit()

    command.upgrade(alembic_config, "head")

    with clean_database.connect() as connection:
        lineage = connection.execute(
            text("SELECT id, name, version, parameters FROM raster_lineage")
        ).one()
        methodology_lineage = connection.execute(
            text("SELECT raster_lineage_id FROM methodology_version WHERE id = 1")
        ).scalar_one()
        index_lineage = connection.execute(
            text("SELECT raster_lineage_id FROM index_raster WHERE id = 1")
        ).scalar_one()
        change_lineage = connection.execute(
            text("SELECT raster_lineage_id FROM change_raster WHERE id = 1")
        ).scalar_one()
        markers = connection.execute(
            text("SELECT change_raster_id, methodology_version_id FROM candidate_extraction")
        ).all()

    assert methodology_lineage == index_lineage == change_lineage == lineage[0]
    assert lineage[1] == "optical-change"
    # The migration's frozen derivation must content-match the live code's, so
    # rasters minted before the split stay reusable after it.
    expected_subset = raster_parameters(
        {
            "ee_script_version": "slice1-optical-change-v1",
            "scale_m": 30,
            "baseline_window": 5,
            "delta_nbr_threshold": -0.25,
        }
    )
    assert lineage[3] == expected_subset
    assert lineage[2] == auto_version(expected_subset)
    # Pre-split extraction invariant carried over: raster 1 was extracted by
    # methodology 1.
    assert [tuple(marker) for marker in markers] == [(1, 1)]
