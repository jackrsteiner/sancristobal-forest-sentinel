from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from forest_sentinel import earthengine, forestmask, indices, pipeline, qa, storage
from forest_sentinel.candidates import DEFAULT_DELTA_NBR_THRESHOLD, DEFAULT_MIN_AREA_M2
from forest_sentinel.cli import main
from forest_sentinel.methodology import get_or_create_methodology_version
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
    for name in (
        forestmask.SOURCE_ENV_VAR,
        forestmask.ASSET_ENV_VAR,
        forestmask.CANOPY_PCT_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)

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
    # The forest mask (#82) is a methodology input too; the default is recorded.
    assert methodology.parameters[forestmask.PARAMETER_KEY] == {
        "source": "hansen",
        "asset": forestmask.DEFAULT_HANSEN_ASSET,
        "canopy_threshold_pct": forestmask.DEFAULT_CANOPY_THRESHOLD_PCT,
    }


def test_pipeline_mode_mask_off_records_no_forest_mask_key(
    migrated_database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FOREST_SENTINEL_FOREST_MASK=none must produce the same parameter set as a
    pre-#82 run (no forest_mask key), so existing methodology lineages — and their
    already-exported artifacts — keep matching."""
    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.setattr(storage, "local_disk_storage_from_env", lambda: object())
    monkeypatch.setattr(
        pipeline, "run_pipeline", lambda session, **kwargs: PipelineSummary(0, 0, 0, 0, 0, 0, 0, 0)
    )
    monkeypatch.setenv(forestmask.SOURCE_ENV_VAR, "none")

    args = ["run", "--aoi", str(SAMPLE_AOI), "--since", "2026-01-01", "--until", "2026-02-01"]
    assert main(args) == 0

    with Session(migrated_database) as session:
        methodology = session.execute(select(MethodologyVersion)).scalar_one()
    assert forestmask.PARAMETER_KEY not in methodology.parameters


def test_pipeline_mode_rejects_bad_forest_mask_config(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(forestmask.SOURCE_ENV_VAR, "not-a-source")
    args = ["run", "--aoi", str(SAMPLE_AOI), "--since", "2026-01-01", "--until", "2026-02-01"]
    assert main(args) == 1
    assert "not-a-source" in capsys.readouterr().err


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
        get_or_create_methodology_version(
            session, name="optical-change", version="1.0.0", parameters={"other": 1}
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
        get_or_create_methodology_version(
            session, name="optical-change", version="1.0.0", parameters={"other": 1}
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
    # The at-a-glance semantic label leads; the content-addressed identity
    # follows. The seeded 1.0.0 row lacks an ee_script_version, so the freshly
    # minted parameter set reads as a script change: minor bump to v1.1.0.
    assert "Methodology: optical-change v1.1.0 (auto-" in capsys.readouterr().out
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


# --- `forest-sentinel aoi list` / `aoi delete` (#83) ---


def test_aoi_list_shows_aois_and_counts(
    migrated_database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.fakes import make_aoi

    with Session(migrated_database) as session:
        make_aoi(session, name="Listed AOI")
        session.commit()

    assert main(["aoi", "list"]) == 0
    out = capsys.readouterr().out
    assert "Listed AOI" in out
    assert "observations" in out


def test_aoi_delete_requires_yes_and_then_removes_everything(
    migrated_database: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dry-run prints the inventory and deletes nothing; --yes removes every
    dependent row (in FK order, proven against full pipeline-produced data),
    the COG directory, and the aois/ seed file — other AOIs untouched."""
    from datetime import date as date_type

    from forest_sentinel.models import DisturbanceCandidate, Observation, PipelineRun
    from forest_sentinel.pipeline import run_pipeline
    from tests.fakes import FakeStorage, make_aoi, make_methodology
    from tests.test_pipeline import _fake_ee

    cog_root = tmp_path / "cogs"
    aois_dir = tmp_path / "aois"
    aois_dir.mkdir()
    monkeypatch.setenv("FOREST_SENTINEL_COG_ROOT", str(cog_root))
    monkeypatch.setenv("FOREST_SENTINEL_AOIS_DIR", str(aois_dir))

    with migrated_database.connect() as connection, Session(bind=connection) as session:
        make_aoi(session, name="Keeper")
        target = make_aoi(session, name="Doomed AOI")
        methodology = make_methodology(session)
        session.commit()
        # Full pipeline output: observations, rasters, candidates, events, runs.
        run_pipeline(
            session,
            aoi=target,
            since=date_type(2026, 1, 1),
            until=date_type(2026, 2, 1),
            methodology=methodology,
            storage=FakeStorage(cog_root),
            ee_module=_fake_ee((1, 2, 3, 4, 5, 6)),
        )
        session.commit()
        target_id = target.id
    seed = aois_dir / "doomed-aoi.geojson"
    seed.write_text("{}")
    cog_dir = cog_root / f"{target_id}-doomed-aoi"
    assert cog_dir.is_dir()  # the fake wrote real COG files

    # Dry run: inventory printed, exit 1, nothing removed.
    assert main(["aoi", "delete", "Doomed AOI"]) == 1
    captured = capsys.readouterr()
    assert "deleting removes" in captured.out
    assert "Re-run with --yes" in captured.err
    with Session(migrated_database) as session:
        assert session.execute(select(func.count()).select_from(Observation)).scalar_one() > 0
    assert cog_dir.is_dir() and seed.is_file()

    # Real delete.
    assert main(["aoi", "delete", "Doomed AOI", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Deleted AOI 'Doomed AOI'" in out
    assert "re-create it" in out
    with Session(migrated_database) as session:
        assert session.execute(select(Aoi.name)).scalars().all() == ["Keeper"]
        for model in (Observation, DisturbanceCandidate, PipelineRun):
            assert session.execute(select(func.count()).select_from(model)).scalar_one() == 0
    assert not cog_dir.exists()
    assert not seed.exists()


def test_aoi_delete_unknown_name_errors(
    migrated_database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["aoi", "delete", "No Such AOI"]) == 1
    assert "no AOI named" in capsys.readouterr().err


def test_cogs_reproduce_unknown_raster_exits_nonzero(
    migrated_database: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.setattr(storage, "local_disk_storage_from_env", lambda: object())

    assert main(["cogs", "reproduce", "index", "999999"]) == 1
    assert "index_raster 999999 not found" in capsys.readouterr().err


def test_cogs_reproduce_dispatches_by_kind(
    migrated_database: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`cogs reproduce change <id>` routes to the change path with the build's pin."""
    from forest_sentinel import reproduce
    from forest_sentinel.cli import EE_SCRIPT_VERSION
    from forest_sentinel.models import ChangeRaster
    from tests.fakes import make_aoi, make_change_raster, make_methodology, make_observation

    with migrated_database.connect() as connection, Session(bind=connection) as session:
        aoi = make_aoi(session)
        obs = make_observation(session, aoi, day=6)
        methodology = make_methodology(session)
        change = make_change_raster(session, obs, methodology, cog_path="/data/x.tif")
        session.commit()
        change_id = change.id

    captured: dict[str, object] = {}

    def fake_reproduce_change(session: object, *, raster: ChangeRaster, **kwargs: object) -> Path:
        captured["raster_id"] = raster.id
        captured.update(kwargs)
        return tmp_path / "x.tif"

    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.setattr(storage, "local_disk_storage_from_env", lambda: object())
    monkeypatch.setattr(reproduce, "reproduce_change_raster", fake_reproduce_change)

    assert main(["cogs", "reproduce", "change", str(change_id), "--force-version"]) == 0
    assert captured["raster_id"] == change_id
    assert captured["current_script_version"] == EE_SCRIPT_VERSION
    assert captured["force_version"] is True
    assert f"Reproduced change_raster {change_id}" in capsys.readouterr().out
