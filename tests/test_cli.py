from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from forest_sentinel import earthengine, indices, pipeline, qa, storage
from forest_sentinel.candidates import DEFAULT_DELTA_NBR_THRESHOLD, DEFAULT_MIN_AREA_M2
from forest_sentinel.cli import main
from forest_sentinel.models import Aoi, MethodologyVersion
from forest_sentinel.pipeline import PipelineSummary

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SAMPLE_AOI = EXAMPLES / "aoi-sample.geojson"


def test_run_persists_aoi_and_reports(
    migrated_database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["run", "--aoi", str(SAMPLE_AOI)])
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "Example AOI" in output
    assert "id=" in output
    assert "Total AOIs in database: 1" in output

    with Session(migrated_database) as session:
        rows = session.execute(select(Aoi)).scalars().all()
    assert [row.name for row in rows] == ["Example AOI"]


def test_run_with_bad_config_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["run", "--aoi", str(tmp_path / "missing.geojson")])
    assert exit_code == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--since", "--until"])
def test_lone_window_flag_is_a_usage_error(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    """A single window flag used to fall through silently to the Slice 0 load
    (audit BUG-8); it must be rejected as a usage error."""
    with pytest.raises(SystemExit) as excinfo:
        main(["run", "--aoi", str(SAMPLE_AOI), flag, "2026-01-01"])
    assert excinfo.value.code == 2
    assert "--since and --until must be provided together" in capsys.readouterr().err


def test_run_with_duplicate_aoi_exits_nonzero(
    migrated_database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "--aoi", str(SAMPLE_AOI)]) == 0
    capsys.readouterr()

    exit_code = main(["run", "--aoi", str(SAMPLE_AOI)])
    assert exit_code == 1
    assert "already exists" in capsys.readouterr().err


def test_run_reports_database_connection_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FOREST_SENTINEL_DATABASE_URL",
        "postgresql+psycopg://nobody:nobody@localhost:1/nowhere",
    )
    exit_code = main(["run", "--aoi", str(SAMPLE_AOI)])
    assert exit_code == 1
    assert "could not connect to the database" in capsys.readouterr().err


def test_pipeline_mode_runs_and_reports_summary(
    migrated_database: Engine,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_pipeline(session: object, **kwargs: object) -> PipelineSummary:
        captured.update(kwargs)
        return PipelineSummary(
            observations_discovered=6,
            observations_recorded=6,
            observations_skipped=0,
            index_rasters=12,
            change_rasters=10,
            candidates=5,
            events_created=1,
            event_observations=5,
        )

    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.setattr(storage, "local_disk_storage_from_env", lambda: object())
    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)

    exit_code = main(
        ["run", "--aoi", str(SAMPLE_AOI), "--since", "2026-01-01", "--until", "2026-02-01"]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Ran Slice 1 pipeline" in out
    assert "Disturbance candidates: 5" in out
    assert "Disturbance events: 1 created" in out
    # The configured window was threaded through to the pipeline.
    assert str(captured["since"]) == "2026-01-01"
    assert str(captured["until"]) == "2026-02-01"


def test_pipeline_mode_reports_export_failures_with_nonzero_exit(
    migrated_database: Engine,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial results are committed, but skipped exports must alert the scheduler
    (re-audit R4)."""
    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.setattr(storage, "local_disk_storage_from_env", lambda: object())
    monkeypatch.setattr(
        pipeline,
        "run_pipeline",
        lambda session, **kwargs: PipelineSummary(3, 3, 0, 4, 2, 1, 1, 1, export_failures=1),
    )
    exit_code = main(
        ["run", "--aoi", str(SAMPLE_AOI), "--since", "2026-01-01", "--until", "2026-02-01"]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Disturbance events: 1 created" in captured.out  # summary still printed
    assert "1 observation(s) skipped" in captured.err


def test_pipeline_mode_defaults_are_resolved_and_recorded(
    migrated_database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting --threshold/--min-area must use and record the documented defaults,
    not store nulls that crash candidate extraction (audit BUG-1)."""
    captured: dict[str, object] = {}

    def fake_run_pipeline(session: object, **kwargs: object) -> PipelineSummary:
        captured.update(kwargs)
        return PipelineSummary(0, 0, 0, 0, 0, 0, 0, 0)

    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.setattr(storage, "local_disk_storage_from_env", lambda: object())
    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)

    args = ["run", "--aoi", str(SAMPLE_AOI), "--since", "2026-01-01", "--until", "2026-02-01"]
    assert main(args) == 0

    # The resolved defaults are threaded into the pipeline...
    assert captured["threshold"] == DEFAULT_DELTA_NBR_THRESHOLD
    assert captured["min_area_m2"] == DEFAULT_MIN_AREA_M2
    # ...and recorded in the methodology provenance (no nulls), together with the
    # scale and mask categories that also shape the output.
    with Session(migrated_database) as session:
        methodology = session.execute(select(MethodologyVersion)).scalar_one()
    assert methodology.parameters["delta_nbr_threshold"] == DEFAULT_DELTA_NBR_THRESHOLD
    assert methodology.parameters["min_area_m2"] == DEFAULT_MIN_AREA_M2
    assert methodology.parameters["scale_m"] == indices.DEFAULT_SCALE_METERS
    assert methodology.parameters["masked_categories"] == list(qa.MASK_CATEGORIES)


def test_pipeline_mode_reuses_existing_aoi(
    migrated_database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.setattr(storage, "local_disk_storage_from_env", lambda: object())
    monkeypatch.setattr(
        pipeline,
        "run_pipeline",
        lambda session, **kwargs: PipelineSummary(0, 0, 0, 0, 0, 0, 0, 0),
    )
    args = ["run", "--aoi", str(SAMPLE_AOI), "--since", "2026-01-01", "--until", "2026-02-01"]
    assert main(args) == 0
    assert main(args) == 0  # idempotent: re-running reuses the AOI row, no duplicate error

    with Session(migrated_database) as session:
        assert len(session.execute(select(Aoi)).scalars().all()) == 1


def test_pipeline_mode_reports_earth_engine_failure(
    migrated_database: Engine,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_initialize(project: str | None = None) -> None:
        raise earthengine.EarthEngineError("Earth Engine initialization failed: no credentials")

    monkeypatch.setattr(earthengine, "initialize", failing_initialize)
    exit_code = main(
        ["run", "--aoi", str(SAMPLE_AOI), "--since", "2026-01-01", "--until", "2026-02-01"]
    )
    assert exit_code == 1
    assert "Earth Engine initialization failed" in capsys.readouterr().err


def test_pipeline_mode_reports_methodology_mismatch_for_explicit_version(
    migrated_database: Engine,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinning a version that exists with different parameters is a user error and
    must print the 'bump the version' guidance, not a traceback (audit BUG-13)."""
    with Session(migrated_database) as session:
        session.add(
            MethodologyVersion(name="optical-change", version="1.0.0", parameters={"other": 1})
        )
        session.commit()

    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.setattr(storage, "local_disk_storage_from_env", lambda: object())
    exit_code = main(
        [
            "run",
            "--aoi",
            str(SAMPLE_AOI),
            "--since",
            "2026-01-01",
            "--until",
            "2026-02-01",
            "--methodology-version",
            "1.0.0",
        ]
    )
    assert exit_code == 1
    assert "bump the version" in capsys.readouterr().err


def test_pipeline_mode_auto_mints_methodology_when_parameters_change(
    migrated_database: Engine,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit --methodology-version, changed parameters mint a new
    content-addressed version instead of erroring — the knobs in instance.env are
    usable on a live instance."""
    with Session(migrated_database) as session:
        session.add(
            MethodologyVersion(name="optical-change", version="1.0.0", parameters={"other": 1})
        )
        session.commit()

    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.setattr(storage, "local_disk_storage_from_env", lambda: object())
    monkeypatch.setattr(
        pipeline,
        "run_pipeline",
        lambda session, **kwargs: PipelineSummary(0, 0, 0, 0, 0, 0, 0, 0),
    )
    exit_code = main(
        ["run", "--aoi", str(SAMPLE_AOI), "--since", "2026-01-01", "--until", "2026-02-01"]
    )
    assert exit_code == 0
    assert "Methodology: optical-change @ auto-" in capsys.readouterr().out
    with Session(migrated_database) as session:
        versions = session.execute(select(MethodologyVersion.version)).scalars().all()
    assert "1.0.0" in versions
    assert any(version.startswith("auto-") for version in versions)


def test_pipeline_mode_reports_concurrent_creation_race(
    migrated_database: Engine,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-AOI lock can't cover the AOI/methodology creation itself; a losing
    first-ever run must get a friendly message, not a traceback (re-audit round 3,
    finding 3)."""
    from sqlalchemy.exc import IntegrityError

    from forest_sentinel import cli

    def racing_get_or_create(session: object, config: object) -> object:
        raise IntegrityError("INSERT INTO aoi ...", {}, Exception("duplicate key uq_aoi_name"))

    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.setattr(storage, "local_disk_storage_from_env", lambda: object())
    monkeypatch.setattr(cli, "get_or_create_aoi", racing_get_or_create)
    exit_code = main(
        ["run", "--aoi", str(SAMPLE_AOI), "--since", "2026-01-01", "--until", "2026-02-01"]
    )
    assert exit_code == 1
    assert "concurrent run created this AOI" in capsys.readouterr().err


def test_pipeline_mode_reports_storage_misconfiguration(
    migrated_database: Engine,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.delenv("FOREST_SENTINEL_GCS_STAGING_BUCKET", raising=False)
    exit_code = main(
        ["run", "--aoi", str(SAMPLE_AOI), "--since", "2026-01-01", "--until", "2026-02-01"]
    )
    assert exit_code == 1
    assert "storage is not configured" in capsys.readouterr().err
