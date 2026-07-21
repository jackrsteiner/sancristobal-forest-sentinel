"""Image mode (#96): the systemd wrappers run the CLI from the published
container when APP_IMAGE is set, and keep the from-source uv path (unchanged
argv) when it is blank. Exercised with recording ``uv``/``docker`` stubs on
PATH, mirroring tests/test_prune_cogs_sh.py."""

import os
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

IMAGE = "ghcr.io/example/open-forest-sentinel:abc123"


def _run_wrapper(
    tmp_path: Path, script: str, *args: str, env_file_lines: str = ""
) -> tuple[int, list[str], list[str]]:
    """Run a wrapper with recording stubs; returns (exit, uv calls, docker calls)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv_calls = tmp_path / "uv.log"
    docker_calls = tmp_path / "docker.log"
    for name, log in (("uv", uv_calls), ("docker", docker_calls)):
        stub = bin_dir / name
        stub.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{log}"\nexit 0\n')
        stub.chmod(0o755)

    env_file = tmp_path / "test.env"
    env_file.write_text(env_file_lines)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        # The scripts prepend $HOME/.local/bin (where the real uv lives on the
        # VM) — point HOME elsewhere so the stubs win.
        HOME=str(tmp_path),
        ENV_FILE=str(env_file),
        # Hermetic against INSTANCE repos: their CI checkouts carry committed
        # AOIs in config/aois/ (harvested uploads) and a committed
        # config/overrides.env (synced settings edits). Without this isolation
        # the wrapper loops over the real AOIs — observed as instance-CI-only
        # failures — and sources real overrides into the assertions.
        FOREST_SENTINEL_AOIS_DIR=str(tmp_path / "aois"),
        OVERRIDES_FILE=str(tmp_path / "overrides.env"),
    )
    env.pop("APP_IMAGE", None)
    env.pop("WINDOW_DAYS", None)
    result = subprocess.run(
        ["bash", str(SCRIPTS / script), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    def read(log: Path) -> list[str]:
        return log.read_text().splitlines() if log.exists() else []

    return result.returncode, read(uv_calls), read(docker_calls)


def test_prune_wrapper_image_mode_runs_docker_not_uv(tmp_path: Path) -> None:
    code, uv, docker = _run_wrapper(
        tmp_path,
        "prune_cogs.sh",
        "--dry-run",
        env_file_lines=f"APP_IMAGE={IMAGE}\nFOREST_SENTINEL_COG_ROOT={tmp_path}/cogs\n",
    )
    assert code == 0
    assert uv == []
    (call,) = docker
    assert call.startswith("run --rm --network host")
    assert f"--env-file {tmp_path}/test.env" in call
    # The host store is mounted at its own path — the CLI prunes the real COGs.
    assert f"-v {tmp_path}/cogs:{tmp_path}/cogs" in call
    assert call.endswith(f"{IMAGE} forest-sentinel cogs prune --dry-run")


def test_prune_wrapper_default_stays_from_source(tmp_path: Path) -> None:
    code, uv, docker = _run_wrapper(tmp_path, "prune_cogs.sh", "--dry-run")
    assert code == 0
    assert docker == []
    assert uv == ["run forest-sentinel cogs prune --dry-run"]


def test_pipeline_wrapper_image_mode_mounts_config_for_aois(tmp_path: Path) -> None:
    # AOI files live under the host's config/ — the container must see them.
    code, uv, docker = _run_wrapper(
        tmp_path,
        "run_pipeline.sh",
        env_file_lines=(
            f"APP_IMAGE={IMAGE}\nAOI_PATH={tmp_path}/missing.geojson\nWINDOW_DAYS=10\n"
        ),
    )
    assert code == 0
    assert uv == []
    (call,) = docker
    assert "-v" in call and ":/app/config" in call
    assert f"{IMAGE} forest-sentinel run --aoi {tmp_path}/missing.geojson" in call
    assert "--since" in call and "--until" in call


def test_pipeline_wrapper_default_stays_from_source(tmp_path: Path) -> None:
    code, uv, docker = _run_wrapper(
        tmp_path,
        "run_pipeline.sh",
        env_file_lines=f"AOI_PATH={tmp_path}/missing.geojson\n",
    )
    assert code == 0
    assert docker == []
    assert len(uv) == 1 and uv[0].startswith("run forest-sentinel run --aoi")


def test_dashboard_wrapper_image_mode_serves_uvicorn_from_the_container(
    tmp_path: Path,
) -> None:
    code, uv, docker = _run_wrapper(
        tmp_path,
        "serve_dashboard.sh",
        env_file_lines=f"APP_IMAGE={IMAGE}\nDASHBOARD_PORT=9001\n",
    )
    assert code == 0
    assert uv == []
    (call,) = docker
    assert call.startswith("run --rm --network host")
    assert call.endswith(
        f"{IMAGE} uvicorn forest_sentinel.dashboard.app:app --host 0.0.0.0 --port 9001"
    )


def test_dashboard_wrapper_default_serves_uvicorn_via_uv(tmp_path: Path) -> None:
    code, uv, docker = _run_wrapper(
        tmp_path, "serve_dashboard.sh", env_file_lines="DASHBOARD_PORT=9001\n"
    )
    assert code == 0
    assert docker == []
    assert uv == ["run uvicorn forest_sentinel.dashboard.app:app --host 0.0.0.0 --port 9001"]


def test_dashboard_unit_runs_the_wrapper() -> None:
    unit = (SCRIPTS / "systemd" / "forest-sentinel-dashboard.service").read_text()
    assert "ExecStart=@APP_DIR@/scripts/serve_dashboard.sh" in unit
    # The same sed vm_setup.sh runs must leave no placeholder behind.
    rendered = unit.replace("@APP_DIR@", "/home/ofs/app").replace("@USER@", "ofs")
    assert "@" not in rendered


def test_vm_setup_image_mode_pulls_and_migrates_in_the_container() -> None:
    """vm_setup.sh contract: image mode pulls APP_IMAGE, runs migrations in the
    container, and skips the source build; from-source keeps uv sync + alembic."""
    setup = (SCRIPTS / "vm_setup.sh").read_text()
    assert 'sudo docker pull "${APP_IMAGE}"' in setup
    assert '"${APP_IMAGE}" alembic upgrade head' in setup
    assert 'if [ -z "${APP_IMAGE}" ]; then\n    uv sync\nfi' in setup
    assert "uv run alembic upgrade head" in setup


def test_ci_publishes_the_image_on_main() -> None:
    """CI contract: the publish job pushes latest + SHA tags to GHCR on main
    pushes only, after smoke-testing the CLI, migrations, and dashboard."""
    workflow = (SCRIPTS.parent / ".github" / "workflows" / "ci.yml").read_text()
    assert "publish-image:" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "packages: write" in workflow
    assert "forest-sentinel --help" in workflow
    assert "alembic upgrade head" in workflow
    assert "import forest_sentinel.dashboard.app" in workflow
    assert 'docker push "${IMAGE}:${GITHUB_SHA}"' in workflow
    assert 'docker push "${IMAGE}:latest"' in workflow
