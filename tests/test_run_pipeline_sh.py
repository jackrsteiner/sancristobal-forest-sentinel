"""The multi-AOI wrapper loop in scripts/run_pipeline.sh (#81), exercised with a
stubbed ``uv`` on PATH: each configured AOI gets its own CLI invocation, one
failure doesn't stop the loop (but the exit code reflects it), and a file
reachable both via AOI_PATH and the aois/ directory runs once."""

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_pipeline.sh"


def _run(tmp_path: Path, *, aoi_path: str, fail_marker: str = "@@none@@") -> tuple[int, list[str]]:
    """Run the wrapper with a recording `uv` stub; returns (exit code, invocations)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    stub = bin_dir / "uv"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{calls}"\n'
        f'case "$*" in *"{fail_marker}"*) exit 1 ;; esac\n'
        "exit 0\n"
    )
    stub.chmod(0o755)

    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        # The script prepends $HOME/.local/bin (where the real uv lives on the
        # VM) — point HOME elsewhere so the stub wins.
        HOME=str(tmp_path),
        ENV_FILE=str(tmp_path / "empty.env"),
        AOI_PATH=aoi_path,
        FOREST_SENTINEL_AOIS_DIR=str(tmp_path / "aois"),
    )
    (tmp_path / "empty.env").touch()
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    return result.returncode, lines


def _geojson(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "Feature"}))


def test_runs_aoi_path_plus_every_file_in_aois_dir(tmp_path: Path) -> None:
    _geojson(tmp_path / "legacy.geojson")
    _geojson(tmp_path / "aois" / "alpha.geojson")
    _geojson(tmp_path / "aois" / "beta.geojson")

    code, calls = _run(tmp_path, aoi_path=str(tmp_path / "legacy.geojson"))
    assert code == 0
    aoi_args = [call.split("--aoi ")[1].split(" --since")[0] for call in calls]
    assert aoi_args == [
        str(tmp_path / "legacy.geojson"),
        str(tmp_path / "aois" / "alpha.geojson"),
        str(tmp_path / "aois" / "beta.geojson"),
    ]
    assert all("run --aoi" in call and "--since" in call and "--until" in call for call in calls)


def test_aoi_path_inside_aois_dir_runs_once(tmp_path: Path) -> None:
    _geojson(tmp_path / "aois" / "alpha.geojson")

    code, calls = _run(tmp_path, aoi_path=str(tmp_path / "aois" / "alpha.geojson"))
    assert code == 0
    assert len(calls) == 1


def test_one_failing_aoi_does_not_stop_the_loop_but_fails_the_run(tmp_path: Path) -> None:
    _geojson(tmp_path / "aois" / "alpha.geojson")
    _geojson(tmp_path / "aois" / "beta.geojson")

    code, calls = _run(tmp_path, aoi_path=str(tmp_path / "nope.geojson"), fail_marker="alpha")
    assert code == 1  # the scheduler still learns something failed...
    assert len(calls) == 2  # ...but beta ran after alpha's failure


def test_no_aoi_files_falls_back_to_single_aoi_path(tmp_path: Path) -> None:
    # Neither AOI_PATH nor aois/ exists: the CLI is invoked once with AOI_PATH
    # unchanged (its missing-file error path is the CLI's to report).
    code, calls = _run(tmp_path, aoi_path=str(tmp_path / "missing.geojson"))
    assert code == 0  # stub exits 0; the real CLI would report the missing file
    assert len(calls) == 1
    assert str(tmp_path / "missing.geojson") in calls[0]
