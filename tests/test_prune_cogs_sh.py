"""The prune wrapper (scripts/prune_cogs.sh, #80), exercised with a stubbed
``uv`` on PATH, plus the contract that vm_setup.sh installs the prune systemd
units alongside the pipeline ones."""

import os
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _run(
    tmp_path: Path, *args: str, env_file_lines: str = "", overrides_lines: str | None = None
) -> tuple[int, list[str]]:
    """Run the wrapper with a recording `uv` stub; returns (exit code, invocations)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    stub = bin_dir / "uv"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$* COG_RETENTION_DAYS=${{COG_RETENTION_DAYS:-}}" >> "{calls}"\n'
        "exit 0\n"
    )
    stub.chmod(0o755)

    env_file = tmp_path / "test.env"
    env_file.write_text(env_file_lines)
    overrides_file = tmp_path / "overrides.env"
    if overrides_lines is not None:
        overrides_file.write_text(overrides_lines)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        # The script prepends $HOME/.local/bin (where the real uv lives on the
        # VM) — point HOME elsewhere so the stub wins.
        HOME=str(tmp_path),
        ENV_FILE=str(env_file),
        # Isolated from any real config/overrides.env in the checkout (#162).
        OVERRIDES_FILE=str(overrides_file),
    )
    env.pop("COG_RETENTION_DAYS", None)
    result = subprocess.run(
        ["bash", str(SCRIPTS / "prune_cogs.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    return result.returncode, lines


def test_wrapper_loads_env_and_invokes_the_prune_cli(tmp_path: Path) -> None:
    code, calls = _run(tmp_path, env_file_lines="COG_RETENTION_DAYS=42\n")
    assert code == 0
    assert calls == ["run forest-sentinel cogs prune COG_RETENTION_DAYS=42"]


def test_overrides_beat_the_env_file(tmp_path: Path) -> None:
    """#162: a dashboard COG_RETENTION_DAYS edit applies on the next prune."""
    code, calls = _run(
        tmp_path, env_file_lines="COG_RETENTION_DAYS=42\n", overrides_lines="COG_RETENTION_DAYS=7\n"
    )
    assert code == 0
    assert calls == ["run forest-sentinel cogs prune COG_RETENTION_DAYS=7"]


def test_wrapper_forwards_extra_arguments(tmp_path: Path) -> None:
    code, calls = _run(tmp_path, "--dry-run")
    assert code == 0
    assert calls == ["run forest-sentinel cogs prune --dry-run COG_RETENTION_DAYS="]


def test_vm_setup_installs_the_prune_units() -> None:
    """vm_setup.sh must render/copy both prune units and enable the timer."""
    setup = (SCRIPTS / "vm_setup.sh").read_text()
    assert "scripts/systemd/forest-sentinel-prune.service" in setup
    assert "scripts/systemd/forest-sentinel-prune.timer" in setup
    assert "systemctl enable --now forest-sentinel-prune.timer" in setup


def test_prune_service_unit_is_a_templated_oneshot_running_the_wrapper() -> None:
    unit = (SCRIPTS / "systemd" / "forest-sentinel-prune.service").read_text()
    assert "Type=oneshot" in unit
    assert "User=@USER@" in unit
    assert "EnvironmentFile=@APP_DIR@/.env" in unit
    assert "ExecStart=@APP_DIR@/scripts/prune_cogs.sh" in unit
    # The same sed vm_setup.sh runs must leave no placeholder behind.
    rendered = unit.replace("@APP_DIR@", "/home/ofs/app").replace("@USER@", "ofs")
    assert "@" not in rendered


def test_prune_timer_fires_daily_and_catches_up() -> None:
    # The schedule is a vm_setup.sh-rendered token since bead 7.5 (#139); the
    # 02:30 default lives in vm_setup.sh and is contract-tested in
    # tests/test_settings.py.
    timer = (SCRIPTS / "systemd" / "forest-sentinel-prune.timer").read_text()
    assert "OnCalendar=@PRUNE_SCHEDULE@" in timer
    assert "Persistent=true" in timer
