from pathlib import Path
from typing import Any

import pytest

from forest_sentinel import storage as storage_module
from forest_sentinel.storage import (
    CogKey,
    LocalDiskStorage,
    Storage,
    StorageError,
    local_disk_storage_from_env,
)


class FakeTask:
    pass


class FakeEarthEngine:
    """Stand-in for the earthengine seam with a scripted task-state sequence."""

    TASK_STATE_COMPLETED = "COMPLETED"

    def __init__(self, states: list[str]) -> None:
        self._states = iter(states)
        self.submitted: dict[str, Any] | None = None

    def start_image_export_to_gcs(self, image: Any, **kwargs: Any) -> FakeTask:
        self.submitted = {"image": image, **kwargs}
        return FakeTask()

    def export_task_state(self, task: Any) -> str:
        return next(self._states)

    def is_terminal_failure(self, state: str) -> bool:
        return state in {"FAILED", "CANCELLED", "CANCEL_REQUESTED"}


class FakeStagingBucket:
    """Records staging traffic; ``download_to`` writes a file so existence can be asserted."""

    def __init__(self) -> None:
        self.downloaded: list[tuple[str, Path]] = []
        self.deleted: list[str] = []

    def download_to(self, blob_name: str, destination: Path) -> None:
        self.downloaded.append((blob_name, destination))
        destination.write_bytes(b"fake-cog")

    def delete(self, blob_name: str) -> None:
        self.deleted.append(blob_name)


def _storage(
    tmp_path: Path, states: list[str], staging: FakeStagingBucket
) -> tuple[LocalDiskStorage, FakeEarthEngine, list[float]]:
    fake_ee = FakeEarthEngine(states)
    sleeps: list[float] = []
    store = LocalDiskStorage(
        tmp_path,
        "staging-bucket",
        staging,
        ee_module=fake_ee,
        poll_interval_seconds=0.01,
        sleep=sleeps.append,
    )
    return store, fake_ee, sleeps


def test_path_for_is_deterministic(tmp_path: Path) -> None:
    store, _, _ = _storage(tmp_path, ["COMPLETED"], FakeStagingBucket())
    key = CogKey(aoi="Solomon Islands", product="NBR", date="2026-01-02", filename="nbr.tif")
    assert store.path_for(key) == tmp_path / "solomon-islands/nbr/2026-01-02/nbr.tif"


def test_freeform_components_are_sanitized() -> None:
    key = CogKey(aoi="Solomon Islands!", product="ΔNBR", date="2026-01-02", filename="My COG.tif")
    assert key.relative_path() == "solomon-islands/nbr/2026-01-02/my-cog.tif"


def test_empty_component_after_sanitization_is_rejected() -> None:
    with pytest.raises(StorageError, match="empty"):
        CogKey(aoi="!!!", product="nbr", date="2026-01-02", filename="x.tif").relative_path()


def test_filename_must_end_in_tif() -> None:
    with pytest.raises(StorageError, match="must end in .tif"):
        CogKey(aoi="a", product="b", date="2026-01-02", filename="nbr").relative_path()


def test_gcs_prefix_strips_extension() -> None:
    key = CogKey(aoi="a", product="b", date="2026-01-02", filename="nbr.tif")
    assert key.gcs_prefix() == "a/b/2026-01-02/nbr"


def test_export_lands_cog_locally_and_clears_staging(tmp_path: Path) -> None:
    staging = FakeStagingBucket()
    store, fake_ee, _ = _storage(tmp_path, ["RUNNING", "COMPLETED"], staging)
    key = CogKey(aoi="aoi", product="nbr", date="2026-01-02", filename="nbr.tif")

    result = store.export_image("ee-image", key, scale=30, region={"type": "Polygon"})

    assert result == tmp_path / "aoi/nbr/2026-01-02/nbr.tif"
    assert result.is_file()
    # Export submitted with the staging bucket, the relative prefix, and the passed scale/region.
    assert fake_ee.submitted == {
        "image": "ee-image",
        "bucket": "staging-bucket",
        "file_name_prefix": "aoi/nbr/2026-01-02/nbr",
        "scale": 30,
        "region": {"type": "Polygon"},
    }
    # Staging object downloaded then deleted.
    assert staging.downloaded == [("aoi/nbr/2026-01-02/nbr.tif", result)]
    assert staging.deleted == ["aoi/nbr/2026-01-02/nbr.tif"]


def test_export_polls_until_completed(tmp_path: Path) -> None:
    store, _, sleeps = _storage(
        tmp_path, ["READY", "RUNNING", "RUNNING", "COMPLETED"], FakeStagingBucket()
    )
    key = CogKey(aoi="aoi", product="nbr", date="2026-01-02", filename="nbr.tif")
    store.export_image("img", key)
    assert len(sleeps) == 3


def test_export_times_out_on_stuck_task(tmp_path: Path) -> None:
    """A task that never reaches a terminal state must raise instead of polling
    forever (audit BUG-11)."""
    staging = FakeStagingBucket()
    fake_ee = FakeEarthEngine(["RUNNING"] * 100)
    sleeps: list[float] = []
    store = LocalDiskStorage(
        tmp_path,
        "staging-bucket",
        staging,
        ee_module=fake_ee,
        poll_interval_seconds=1.0,
        timeout_seconds=3.0,
        sleep=sleeps.append,
    )
    key = CogKey(aoi="aoi", product="nbr", date="2026-01-02", filename="nbr.tif")
    with pytest.raises(StorageError, match="timed out after 3s"):
        store.export_image("img", key)
    assert len(sleeps) == 3  # polled until the timeout budget was exhausted
    assert staging.downloaded == []
    assert staging.deleted == []


def test_staging_failures_surface_as_storage_errors(tmp_path: Path) -> None:
    """Raw GCS exceptions from the staged copy/clear (missing object, transient API
    error) must become StorageError so per-observation isolation applies (R2-1)."""

    class BrokenStagingBucket(FakeStagingBucket):
        def download_to(self, blob_name: str, destination: Path) -> None:
            raise RuntimeError("404 staging object not found")

    store, _, _ = _storage(tmp_path, ["COMPLETED"], BrokenStagingBucket())
    key = CogKey(aoi="aoi", product="nbr", date="2026-01-02", filename="nbr.tif")
    with pytest.raises(StorageError, match="staging copy/clear failed"):
        store.export_image("img", key)


def test_export_raises_on_failed_task(tmp_path: Path) -> None:
    staging = FakeStagingBucket()
    store, _, _ = _storage(tmp_path, ["RUNNING", "FAILED"], staging)
    key = CogKey(aoi="aoi", product="nbr", date="2026-01-02", filename="nbr.tif")
    with pytest.raises(StorageError, match="FAILED"):
        store.export_image("img", key)
    assert staging.downloaded == []
    assert staging.deleted == []


def test_local_disk_storage_satisfies_storage_protocol(tmp_path: Path) -> None:
    store, _, _ = _storage(tmp_path, ["COMPLETED"], FakeStagingBucket())
    accepts: Storage = store  # static + structural conformance
    assert callable(accepts.path_for)
    assert callable(accepts.export_image)


def test_from_env_requires_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(storage_module.GCS_STAGING_BUCKET_ENV_VAR, raising=False)
    with pytest.raises(StorageError, match="not set"):
        local_disk_storage_from_env(staging=FakeStagingBucket())


def test_from_env_uses_configured_root_and_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(storage_module.COG_ROOT_ENV_VAR, str(tmp_path / "cogs"))
    monkeypatch.setenv(storage_module.GCS_STAGING_BUCKET_ENV_VAR, "my-bucket")
    store = local_disk_storage_from_env(staging=FakeStagingBucket())
    key = CogKey(aoi="a", product="nbr", date="2026-01-02", filename="nbr.tif")
    assert store.path_for(key) == tmp_path / "cogs/a/nbr/2026-01-02/nbr.tif"
