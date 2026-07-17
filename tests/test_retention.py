"""COG retention pruning (#80): catalog-date selection, the window floor, and
the `forest-sentinel cogs prune` CLI wiring. Pure filesystem + environment —
no database, no Earth Engine."""

from datetime import date
from pathlib import Path

import pytest

from forest_sentinel.cli import main
from forest_sentinel.retention import (
    FLOOR_MARGIN_DAYS,
    effective_retention_days,
    prune_cogs,
)

TODAY = date(2026, 7, 17)


def _cog(root: Path, aoi: str, product: str, day: str, name: str, size: int = 10) -> Path:
    path = root / aoi / product / day / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_effective_retention_floors_at_window_plus_margin() -> None:
    assert effective_retention_days(90, 30) == (90, False)
    assert effective_retention_days(30, 30) == (30 + FLOOR_MARGIN_DAYS, True)
    assert effective_retention_days(44, 30) == (44, False)  # exactly at the floor


def test_prunes_only_files_older_than_retention(tmp_path: Path) -> None:
    old = _cog(tmp_path, "1-aoi", "nbr", "2026-01-01", "old.tif", size=100)
    fresh = _cog(tmp_path, "1-aoi", "nbr", "2026-07-01", "fresh.tif")

    report = prune_cogs(tmp_path, retention_days=90, window_days=30, today=TODAY)

    assert report.pruned == [old]
    assert report.pruned_bytes == 100
    assert report.kept == 1
    assert not old.exists()
    assert fresh.exists()


def test_age_is_the_catalog_date_not_mtime(tmp_path: Path) -> None:
    """A re-downloaded old raster (fresh mtime, old path date) is still pruned."""
    old = _cog(tmp_path, "1-aoi", "nbr", "2026-01-01", "old.tif")
    # mtime is "now" by construction — only the path date says it is old.
    report = prune_cogs(tmp_path, retention_days=90, window_days=30, today=TODAY)
    assert report.pruned == [old]


def test_files_at_the_boundary_are_kept(tmp_path: Path) -> None:
    boundary = _cog(tmp_path, "1-aoi", "nbr", "2026-04-18", "edge.tif")  # exactly 90 days
    report = prune_cogs(tmp_path, retention_days=90, window_days=30, today=TODAY)
    assert report.pruned == []
    assert boundary.exists()


def test_floor_protects_the_active_window(tmp_path: Path) -> None:
    """COG_RETENTION_DAYS below WINDOW_DAYS + margin must not prune in-window files."""
    in_window = _cog(tmp_path, "1-aoi", "nbr", "2026-07-01", "in-window.tif")
    past_floor = _cog(tmp_path, "1-aoi", "nbr", "2026-05-01", "past-floor.tif")

    report = prune_cogs(tmp_path, retention_days=1, window_days=30, today=TODAY)

    assert report.floor_applied
    assert report.effective_retention_days == 30 + FLOOR_MARGIN_DAYS
    assert in_window.exists()
    assert report.pruned == [past_floor]


def test_non_catalog_files_are_left_alone(tmp_path: Path) -> None:
    not_a_date = _cog(tmp_path, "1-aoi", "nbr", "not-a-date", "weird.tif")
    shallow = tmp_path / "loose.tif"
    shallow.write_bytes(b"x")
    ancient = _cog(tmp_path, "1-aoi", "nbr", "2020-01-01", "keep.txt")  # not .tif

    report = prune_cogs(tmp_path, retention_days=90, window_days=30, today=TODAY)

    assert report.pruned == []
    assert report.unrecognized == 1  # only the bad-date .tif is counted
    assert not_a_date.exists() and shallow.exists() and ancient.exists()


def test_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    old = _cog(tmp_path, "1-aoi", "nbr", "2026-01-01", "old.tif")
    report = prune_cogs(tmp_path, retention_days=90, window_days=30, today=TODAY, dry_run=True)
    assert report.pruned == [old]
    assert old.exists()


def test_emptied_directories_are_removed(tmp_path: Path) -> None:
    _cog(tmp_path, "1-aoi", "nbr", "2026-01-01", "old.tif")
    keep = _cog(tmp_path, "1-aoi", "ndvi", "2026-07-01", "fresh.tif")

    prune_cogs(tmp_path, retention_days=90, window_days=30, today=TODAY)

    assert not (tmp_path / "1-aoi" / "nbr").exists()  # date + product dirs collapsed
    assert keep.exists()
    assert tmp_path.exists()  # the root itself is never removed


def test_missing_root_is_a_no_op(tmp_path: Path) -> None:
    report = prune_cogs(tmp_path / "absent", retention_days=90, window_days=30, today=TODAY)
    assert report.pruned == [] and report.kept == 0


# --- CLI wiring -------------------------------------------------------------


def _prune_env(monkeypatch: pytest.MonkeyPatch, root: Path, **env: str) -> None:
    monkeypatch.setenv("FOREST_SENTINEL_COG_ROOT", str(root))
    for name in ("COG_RETENTION_DAYS", "WINDOW_DAYS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_cli_prune_is_disabled_without_retention_setting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    survivor = _cog(tmp_path, "1-aoi", "nbr", "2020-01-01", "ancient.tif")
    _prune_env(monkeypatch, tmp_path)

    assert main(["cogs", "prune"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert survivor.exists()


def test_cli_prune_zero_keeps_forever(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    survivor = _cog(tmp_path, "1-aoi", "nbr", "2020-01-01", "ancient.tif")
    _prune_env(monkeypatch, tmp_path, COG_RETENTION_DAYS="0")

    assert main(["cogs", "prune"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert survivor.exists()


def test_cli_prune_deletes_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _cog(tmp_path, "1-aoi", "nbr", "2020-01-01", "ancient.tif")
    fresh = _cog(tmp_path, "1-aoi", "nbr", date.today().isoformat(), "fresh.tif")
    _prune_env(monkeypatch, tmp_path, COG_RETENTION_DAYS="90", WINDOW_DAYS="30")

    assert main(["cogs", "prune"]) == 0
    out = capsys.readouterr().out
    assert "Pruned 1 COG(s)" in out
    assert str(old) in out
    assert not old.exists() and fresh.exists()


def test_cli_prune_dry_run_deletes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _cog(tmp_path, "1-aoi", "nbr", "2020-01-01", "ancient.tif")
    _prune_env(monkeypatch, tmp_path, COG_RETENTION_DAYS="90")

    assert main(["cogs", "prune", "--dry-run"]) == 0
    assert "Would prune 1 COG(s)" in capsys.readouterr().out
    assert old.exists()


def test_cli_prune_warns_when_floor_raises_the_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    in_window = _cog(tmp_path, "1-aoi", "nbr", date.today().isoformat(), "today.tif")
    _prune_env(monkeypatch, tmp_path, COG_RETENTION_DAYS="1", WINDOW_DAYS="30")

    assert main(["cogs", "prune"]) == 0
    captured = capsys.readouterr()
    assert "below the safe floor" in captured.err
    assert in_window.exists()
