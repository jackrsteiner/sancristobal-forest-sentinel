"""Raster storage: the single seam through which Earth Engine COGs land on disk.

Earth Engine can only export to Google Cloud Storage, but bulk raster storage must
stay on the VM's always-free local disk for $0 cost (see ``docs/architecture.md``
§4b). This module owns that bridge: submit an ``Export.image.toCloudStorage`` task,
poll it to completion, copy the finished COG from a transient GCS staging area to a
deterministic local path, and delete the staging object.

``LocalDiskStorage`` is the only implementation today. Switching the canonical store
to GCS later is a backend swap behind the ``Storage`` protocol — pipeline code (#39,
#40) never touches GCS or EE directly, only ``Storage.export_image``.
"""

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from forest_sentinel import earthengine

COG_ROOT_ENV_VAR = "FOREST_SENTINEL_COG_ROOT"
GCS_STAGING_BUCKET_ENV_VAR = "FOREST_SENTINEL_GCS_STAGING_BUCKET"
DEFAULT_COG_ROOT = "data/cogs/"

_SAFE_COMPONENT = re.compile(r"[^a-z0-9._-]+")


class StorageError(RuntimeError):
    """Raised when a COG cannot be exported, staged, or copied to local disk."""


def _sanitize(component: str) -> str:
    """Reduce a free-form name to a safe, deterministic path component."""
    cleaned = _SAFE_COMPONENT.sub("-", component.strip().lower()).strip("-")
    if not cleaned:
        raise StorageError(f"path component is empty after sanitization: {component!r}")
    return cleaned


@dataclass(frozen=True)
class CogKey:
    """A deterministic location for one exported COG.

    Layout: ``{aoi}/{product}/{date}/{filename}`` (``date`` is ``YYYY-MM-DD``,
    ``filename`` ends in ``.tif``). The same relative key is reused as the GCS
    staging prefix so a finished export is easy to locate.
    """

    aoi: str
    product: str
    date: str
    filename: str

    def relative_path(self) -> str:
        if not self.filename.endswith(".tif"):
            raise StorageError(f"COG filename must end in .tif: {self.filename!r}")
        stem = _sanitize(self.filename[: -len(".tif")])
        parts = (_sanitize(self.aoi), _sanitize(self.product), _sanitize(self.date), f"{stem}.tif")
        return "/".join(parts)

    def gcs_prefix(self) -> str:
        """The EE ``fileNamePrefix`` (Earth Engine appends ``.tif``)."""
        return self.relative_path()[: -len(".tif")]


class StagingBucket(Protocol):
    """A transient GCS staging area an EE export writes into."""

    def download_to(self, blob_name: str, destination: Path) -> None: ...

    def delete(self, blob_name: str) -> None: ...


class GcsStagingBucket:
    """``StagingBucket`` backed by a real Google Cloud Storage bucket."""

    def __init__(self, bucket_name: str, client: Any | None = None) -> None:
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        self._bucket = client.bucket(bucket_name)

    def download_to(self, blob_name: str, destination: Path) -> None:
        self._bucket.blob(blob_name).download_to_filename(str(destination))

    def delete(self, blob_name: str) -> None:
        self._bucket.blob(blob_name).delete()


class Storage(Protocol):
    """Where the pipeline lands its exported rasters."""

    def path_for(self, key: CogKey) -> Path: ...

    def export_image(
        self, image: Any, key: CogKey, *, scale: int | None = None, region: Any = None
    ) -> Path: ...


class LocalDiskStorage:
    """Canonical store on the VM filesystem, fed by EE-exported COGs via GCS staging."""

    def __init__(
        self,
        root: Path,
        staging_bucket_name: str,
        staging: StagingBucket,
        *,
        ee_module: Any = earthengine,
        poll_interval_seconds: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._root = root
        self._staging_bucket_name = staging_bucket_name
        self._staging = staging
        self._ee = ee_module
        self._poll_interval = poll_interval_seconds
        self._sleep = sleep

    def path_for(self, key: CogKey) -> Path:
        return self._root / key.relative_path()

    def export_image(
        self, image: Any, key: CogKey, *, scale: int | None = None, region: Any = None
    ) -> Path:
        prefix = key.gcs_prefix()
        task = self._ee.start_image_export_to_gcs(
            image,
            bucket=self._staging_bucket_name,
            file_name_prefix=prefix,
            scale=scale,
            region=region,
        )
        self._await_completion(task, prefix)

        blob_name = f"{prefix}.tif"
        destination = self.path_for(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._staging.download_to(blob_name, destination)
        self._staging.delete(blob_name)
        return destination

    def _await_completion(self, task: Any, prefix: str) -> None:
        state = self._ee.export_task_state(task)
        while state != earthengine.TASK_STATE_COMPLETED:
            if self._ee.is_terminal_failure(state):
                raise StorageError(f"Earth Engine export {prefix!r} ended in state {state}")
            self._sleep(self._poll_interval)
            state = self._ee.export_task_state(task)


def local_disk_storage_from_env(staging: StagingBucket | None = None) -> LocalDiskStorage:
    """Build a ``LocalDiskStorage`` from the configured environment variables."""
    root = Path(os.environ.get(COG_ROOT_ENV_VAR, DEFAULT_COG_ROOT))
    bucket_name = os.environ.get(GCS_STAGING_BUCKET_ENV_VAR)
    if not bucket_name:
        raise StorageError(f"{GCS_STAGING_BUCKET_ENV_VAR} is not set")
    if staging is None:
        staging = GcsStagingBucket(bucket_name)
    return LocalDiskStorage(root, bucket_name, staging)
