"""The single seam onto Google Earth Engine.

Every ``ee.*`` call in the codebase lives here, wrapped in small functions that take
and return plain Python (dicts, lists, opaque task/image handles). Pipeline modules
(`hls`, `indices`, `change`, `candidates`, `qa`, `storage`) call these helpers and keep
their own logic free of EE objects, so tests can stub this module instead of standing
up a live Earth Engine session.

Authentication: a run needs a GCP service account with Earth Engine access and an
EE-registered Cloud project. ``initialize`` reads the project from
``FOREST_SENTINEL_GEE_PROJECT`` (overridable per call); credentials come from the
ambient environment (``GOOGLE_APPLICATION_CREDENTIALS`` / ``earthengine authenticate``).
"""

import os
from typing import Any

import ee

GEE_PROJECT_ENV_VAR = "FOREST_SENTINEL_GEE_PROJECT"

# Terminal Earth Engine batch-task states.
TASK_STATE_COMPLETED = "COMPLETED"
_TASK_STATES_TERMINAL_FAILURE = frozenset({"FAILED", "CANCELLED", "CANCEL_REQUESTED"})


class EarthEngineError(RuntimeError):
    """Raised for Earth Engine operations that fail (e.g. a failed export task)."""


def initialize(project: str | None = None) -> None:
    """Initialize the Earth Engine client for ``project`` (or the configured default)."""
    resolved = project or os.environ.get(GEE_PROJECT_ENV_VAR)
    ee.Initialize(project=resolved)


def start_image_export_to_gcs(
    image: Any,
    *,
    bucket: str,
    file_name_prefix: str,
    scale: int | None = None,
    region: Any = None,
) -> Any:
    """Submit an ``Export.image.toCloudStorage`` batch task and return its handle.

    Earth Engine writes the COG to ``gs://{bucket}/{file_name_prefix}.tif``.
    """
    task = ee.batch.Export.image.toCloudStorage(
        image=image,
        bucket=bucket,
        fileNamePrefix=file_name_prefix,
        scale=scale,
        region=region,
        fileFormat="GeoTIFF",
        formatOptions={"cloudOptimized": True},
    )
    task.start()
    return task


def export_task_state(task: Any) -> str:
    """Return the current state string of an export task."""
    state: str = task.status()["state"]
    return state


def is_terminal_failure(state: str) -> bool:
    """True if a task state is a terminal failure (no point polling further)."""
    return state in _TASK_STATES_TERMINAL_FAILURE
