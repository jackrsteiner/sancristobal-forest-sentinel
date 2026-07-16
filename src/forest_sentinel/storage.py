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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from forest_sentinel import earthengine

COG_ROOT_ENV_VAR = "FOREST_SENTINEL_COG_ROOT"
GCS_STAGING_BUCKET_ENV_VAR = "FOREST_SENTINEL_GCS_STAGING_BUCKET"
DEFAULT_COG_ROOT = "data/cogs/"
# A stuck (non-terminal) EE task must not wedge the pipeline forever; generous
# because large-AOI exports can legitimately take a long time.
DEFAULT_EXPORT_TIMEOUT_SECONDS = 3600.0

_SAFE_COMPONENT = re.compile(r"[^a-z0-9._-]+")


class StorageError(RuntimeError):
    """Raised when a COG cannot be exported, staged, or copied to local disk."""


class StorageConfigurationError(StorageError):
    """Raised when storage cannot be *built* (missing configuration).

    Distinct from run-time export failures so callers can tell "fix your
    environment" apart from "this export failed".
    """


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


@dataclass(frozen=True)
class ExportRequest:
    """One image to export as part of a batch."""

    image: Any
    key: CogKey
    scale: int | None = None
    region: Any = None


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

    def export_images(self, requests: Sequence[ExportRequest]) -> list[Path | StorageError]: ...


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
        timeout_seconds: float | None = DEFAULT_EXPORT_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._root = root
        self._staging_bucket_name = staging_bucket_name
        self._staging = staging
        self._ee = ee_module
        self._poll_interval = poll_interval_seconds
        self._timeout = timeout_seconds
        self._sleep = sleep

    def path_for(self, key: CogKey) -> Path:
        return self._root / key.relative_path()

    def export_image(
        self, image: Any, key: CogKey, *, scale: int | None = None, region: Any = None
    ) -> Path:
        result = self.export_images(
            [ExportRequest(image=image, key=key, scale=scale, region=region)]
        )[0]
        if isinstance(result, StorageError):
            raise result
        return result

    def export_images(self, requests: Sequence[ExportRequest]) -> list[Path | StorageError]:
        """Submit several exports at once and poll them as a group.

        Earth Engine runs batch tasks concurrently (up to the account tier's
        limit), so overlapping their queue waits — instead of waiting out each
        task before submitting the next — is the main wall-clock lever
        (``docs/scaling.md`` §3.3). Failures are per item: the returned list
        holds, in request order, a local Path for each success and a
        ``StorageError`` for each failure, so callers keep per-observation
        isolation.
        """
        results: list[Path | StorageError] = [
            StorageError("export was not submitted") for _ in requests
        ]
        pending: dict[int, Any] = {}
        for i, request in enumerate(requests):
            prefix = request.key.gcs_prefix()
            try:
                pending[i] = self._ee.start_image_export_to_gcs(
                    request.image,
                    bucket=self._staging_bucket_name,
                    file_name_prefix=prefix,
                    scale=request.scale,
                    region=request.region,
                )
            except earthengine.EarthEngineError as exc:
                results[i] = StorageError(f"failed to submit export {prefix!r}: {exc}")

        # The timeout guards against a *stuck* batch: it resets whenever any task
        # reaches a terminal state, so a slow-but-moving EE queue does not trip it.
        waited_since_progress = 0.0
        while pending:
            progressed = False
            for i in list(pending):
                key = requests[i].key
                prefix = key.gcs_prefix()
                try:
                    state = self._ee.export_task_state(pending[i])
                except earthengine.EarthEngineError as exc:
                    results[i] = StorageError(f"task state for export {prefix!r} failed: {exc}")
                    del pending[i]
                    progressed = True
                    continue
                if state == earthengine.TASK_STATE_COMPLETED:
                    try:
                        results[i] = self._download_and_clear(key)
                    except StorageError as exc:
                        results[i] = exc
                    del pending[i]
                    progressed = True
                elif self._ee.is_terminal_failure(state):
                    results[i] = StorageError(
                        f"Earth Engine export {prefix!r} ended in state {state}"
                    )
                    del pending[i]
                    progressed = True
            if not pending:
                break
            if progressed:
                waited_since_progress = 0.0
            if self._timeout is not None and waited_since_progress >= self._timeout:
                for i in list(pending):
                    prefix = requests[i].key.gcs_prefix()
                    results[i] = StorageError(
                        f"Earth Engine export {prefix!r} timed out after {self._timeout:.0f}s "
                        "without progress"
                    )
                    del pending[i]
                break
            self._sleep(self._poll_interval)
            waited_since_progress += self._poll_interval
        return results

    def _download_and_clear(self, key: CogKey) -> Path:
        blob_name = f"{key.gcs_prefix()}.tif"
        destination = self.path_for(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Staging errors (missing object, GCS hiccup) must surface as StorageError so
        # the pipeline's per-observation isolation applies, not as raw GCS exceptions.
        try:
            self._staging.download_to(blob_name, destination)
            self._staging.delete(blob_name)
        except Exception as exc:
            raise StorageError(f"staging copy/clear failed for {blob_name!r}: {exc}") from exc
        return destination


def local_disk_storage_from_env(staging: StagingBucket | None = None) -> LocalDiskStorage:
    """Build a ``LocalDiskStorage`` from the configured environment variables."""
    root = Path(os.environ.get(COG_ROOT_ENV_VAR, DEFAULT_COG_ROOT))
    bucket_name = os.environ.get(GCS_STAGING_BUCKET_ENV_VAR)
    if not bucket_name:
        raise StorageConfigurationError(f"{GCS_STAGING_BUCKET_ENV_VAR} is not set")
    if staging is None:
        staging = GcsStagingBucket(bucket_name)
    return LocalDiskStorage(root, bucket_name, staging)
