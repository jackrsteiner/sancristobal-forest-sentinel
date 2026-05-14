from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from forest_sentinel.cli import main
from forest_sentinel.models import Aoi

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
