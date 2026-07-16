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
    """Initialize the Earth Engine client for ``project`` (or the configured default).

    Raises ``EarthEngineError`` when initialization fails (missing/invalid credentials,
    unregistered project), so callers can report it without a raw EE traceback.
    """
    resolved = project or os.environ.get(GEE_PROJECT_ENV_VAR)
    try:
        ee.Initialize(project=resolved)
    except ee.EEException as exc:
        raise EarthEngineError(
            f"Earth Engine initialization failed for project {resolved!r}: {exc}"
        ) from exc


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
    # Export.image does not accept a raw GeoJSON dict for ``region`` (the AOI is
    # always a MultiPolygon mapping) — it must be wrapped in ee.Geometry, as the
    # other call sites in this module already do.
    task = ee.batch.Export.image.toCloudStorage(
        image=image,
        bucket=bucket,
        fileNamePrefix=file_name_prefix,
        scale=scale,
        region=ee.Geometry(region) if region is not None else None,
        fileFormat="GeoTIFF",
        formatOptions={"cloudOptimized": True},
    )
    try:
        task.start()
    except ee.EEException as exc:
        raise EarthEngineError(f"failed to submit export {file_name_prefix!r}: {exc}") from exc
    return task


def export_task_state(task: Any) -> str:
    """Return the current state string of an export task.

    Wrapped like every other server-touching helper: a transient API failure during
    the (long) poll loop must surface as ``EarthEngineError`` so the pipeline's
    per-observation isolation applies.
    """
    try:
        status = task.status()
    except ee.EEException as exc:
        raise EarthEngineError(f"failed to read export task state: {exc}") from exc
    state: str = status["state"]
    return state


def list_image_properties(
    collection_id: str,
    region: Any,
    since: str,
    until: str,
) -> list[dict[str, Any]]:
    """Enumerate images in ``collection_id`` intersecting ``region`` over [since, until).

    ``region`` is a GeoJSON geometry dict. Returns one ``{"id", "properties"}`` dict per
    image (``getInfo`` over the filtered collection) — plain Python the caller turns into
    ``observation`` rows.
    """
    collection = (
        ee.ImageCollection(collection_id).filterBounds(ee.Geometry(region)).filterDate(since, until)
    )
    try:
        info = collection.getInfo() or {}
    except ee.EEException as exc:
        raise EarthEngineError(f"listing {collection_id!r} failed: {exc}") from exc
    features = info.get("features", [])
    return [
        {"id": feature.get("id"), "properties": feature.get("properties", {})}
        for feature in features
    ]


def is_terminal_failure(state: str) -> bool:
    """True if a task state is a terminal failure (no point polling further)."""
    return state in _TASK_STATES_TERMINAL_FAILURE


# HLS Fmask QA bit positions (HLS v2.0 ``Fmask`` band).
FMASK_BIT_CLOUD = 1
FMASK_BIT_CLOUD_SHADOW = 3
FMASK_BIT_SNOW_ICE = 4
FMASK_AEROSOL_SHIFT = 6
FMASK_AEROSOL_HIGH = 0b11


def apply_fmask_mask(image: Any, fmask_band: str = "Fmask") -> Any:
    """Return ``image`` with cloud / cloud-shadow / snow-ice / high-aerosol pixels masked.

    Mirrors :func:`forest_sentinel.qa.fmask_clear` as an Earth Engine band expression.
    """
    fmask = image.select(fmask_band)
    cloud = fmask.bitwiseAnd(1 << FMASK_BIT_CLOUD).neq(0)
    shadow = fmask.bitwiseAnd(1 << FMASK_BIT_CLOUD_SHADOW).neq(0)
    snow = fmask.bitwiseAnd(1 << FMASK_BIT_SNOW_ICE).neq(0)
    high_aerosol = fmask.rightShift(FMASK_AEROSOL_SHIFT).bitwiseAnd(0b11).eq(FMASK_AEROSOL_HIGH)
    bad = cloud.Or(shadow).Or(snow).Or(high_aerosol)
    return image.updateMask(bad.Not())


def valid_pixel_fraction(image: Any, band: str, region: Any, scale: int) -> float:
    """Fraction of unmasked pixels of ``band`` within ``region`` (mean of the mask)."""
    mask = image.select(band).mask()
    reduced = mask.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=ee.Geometry(region), scale=scale, maxPixels=1e10
    )
    try:
        value = reduced.get(band).getInfo()
    except ee.EEException as exc:
        raise EarthEngineError(f"valid-pixel reduction for band {band!r} failed: {exc}") from exc
    return float(value) if value is not None else 0.0


def image_by_id(image_id: str) -> Any:
    """Return the Earth Engine image with the given asset id."""
    return ee.Image(image_id)


def normalized_difference(image: Any, bands: list[str]) -> Any:
    """``(bands[0] - bands[1]) / (bands[0] + bands[1])`` as an EE image."""
    return image.normalizedDifference(bands)


def median_of(images: list[Any]) -> Any:
    """Per-pixel median across ``images`` (the trailing-baseline reduction)."""
    return ee.ImageCollection(images).median()


def subtract(image: Any, other: Any) -> Any:
    """``image - other`` as an EE image (the change product)."""
    return image.subtract(other)


def _feature_with_area(feature: Any) -> Any:
    """Tag a vector feature with its area in m² (native projection)."""
    return feature.set("area_m2", feature.area(maxError=1))


def threshold_and_vectorize(
    delta_image: Any,
    *,
    threshold: float,
    scale: int,
    region: Any,
    min_area_m2: float,
) -> list[dict[str, Any]]:
    """Threshold a change image, polygonize the mask, and return candidate features.

    Disturbance is a drop beyond ``threshold`` (``delta < threshold``). The mask is
    polygonized with ``reduceToVectors``; each polygon is tagged with its area and the
    collection is filtered to ``area_m2 >= min_area_m2``. Returns the GeoJSON features
    (WGS 84 geometry + ``area_m2`` property) — plain Python the caller persists.
    """
    mask = delta_image.lt(threshold).selfMask()
    vectors = mask.reduceToVectors(
        geometry=ee.Geometry(region),
        scale=scale,
        geometryType="polygon",
        eightConnected=False,
        maxPixels=1e10,
    )
    with_area = vectors.map(_feature_with_area)
    filtered = with_area.filter(ee.Filter.gte("area_m2", min_area_m2))
    try:
        info = filtered.getInfo() or {}
    except ee.EEException as exc:
        raise EarthEngineError(f"candidate vectorization failed: {exc}") from exc
    features: list[dict[str, Any]] = info.get("features", [])
    return features
