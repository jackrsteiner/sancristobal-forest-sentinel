"""Per-observation vegetation/disturbance indices (NBR, NDVI), computed in Earth Engine.

For each observation this module rebuilds the HLS image, applies Fmask masking (#54),
computes NBR and NDVI as ``normalizedDifference`` band expressions, exports each as a COG
through the storage seam (#36), and records an ``index_raster`` row with provenance to the
source observation and the methodology version.

    NBR  = (NIR - SWIR2) / (NIR + SWIR2)
    NDVI = (NIR - RED)   / (NIR + RED)
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel import earthengine, qa
from forest_sentinel.earthengine import EarthEngineError
from forest_sentinel.hls import HLS_COLLECTIONS
from forest_sentinel.models import Aoi, IndexRaster, MethodologyVersion, Observation
from forest_sentinel.storage import CogKey, ExportRequest, Storage, StorageError

# Native export resolution for HLS (metres).
DEFAULT_SCALE_METERS = 30


@dataclass(frozen=True)
class BandMapping:
    """Per-sensor HLS band names for the reflectance bands the indices need."""

    red: str
    nir: str
    swir2: str


# HLS v2.0 band names differ between the Landsat- and Sentinel-derived collections.
SENSOR_BANDS: dict[str, BandMapping] = {
    "HLSL30": BandMapping(red="B4", nir="B5", swir2="B7"),
    "HLSS30": BandMapping(red="B4", nir="B8A", swir2="B12"),
}
# Reverse of HLS_COLLECTIONS: sensor -> EE collection id.
SENSOR_COLLECTIONS: dict[str, str] = {sensor: cid for cid, sensor in HLS_COLLECTIONS.items()}


def _require_bands(sensor: str) -> BandMapping:
    """The band mapping for ``sensor``, or a ``ValueError`` for unknown sensors."""
    try:
        return SENSOR_BANDS[sensor]
    except KeyError as exc:
        raise ValueError(f"unsupported sensor for index computation: {sensor!r}") from exc


def index_bands(sensor: str) -> dict[str, list[str]]:
    """The ``normalizedDifference`` band pairs per index for ``sensor``."""
    bands = _require_bands(sensor)
    return {
        "NBR": [bands.nir, bands.swir2],
        "NDVI": [bands.nir, bands.red],
    }


def build_masked_image(observation: Observation, *, ee_module: Any = earthengine) -> Any:
    """Rebuild the Fmask-masked HLS image for an observation.

    Public so the change-product module can build each observation's masked image once
    and derive both indices from it, instead of re-masking per index type.
    """
    _require_bands(observation.sensor)
    collection_id = SENSOR_COLLECTIONS[observation.sensor]
    image = ee_module.image_by_id(f"{collection_id}/{observation.source_scene_id}")
    return qa.mask_image(image, ee_module=ee_module)


def build_index_image(
    observation: Observation, index_type: str, *, ee_module: Any = earthengine
) -> Any:
    """Build the masked NBR/NDVI EE image for an observation (no export, no persistence).

    Reused by the change-product baseline (#40), which needs the index images themselves.
    """
    nd_bands = index_bands(observation.sensor)[index_type]
    masked = build_masked_image(observation, ee_module=ee_module)
    return ee_module.normalized_difference(masked, nd_bands)


@dataclass
class IndexStageOutcome:
    """Per-chunk results of the index stage, keyed by observation id."""

    rasters: dict[int, list[IndexRaster]] = field(default_factory=dict)
    exported: int = 0
    reused: int = 0
    failures: dict[int, Exception] = field(default_factory=dict)


def compute_indices_for_observation(
    session: Session,
    *,
    aoi: Aoi,
    observation: Observation,
    methodology: MethodologyVersion,
    storage: Storage,
    scale: int = DEFAULT_SCALE_METERS,
    ee_module: Any = earthengine,
) -> list[IndexRaster]:
    """Compute and persist NBR + NDVI index rasters for one observation."""
    outcome = compute_indices_for_observations(
        session,
        aoi=aoi,
        observations=[observation],
        methodology=methodology,
        storage=storage,
        scale=scale,
        ee_module=ee_module,
    )
    if observation.id in outcome.failures:
        raise outcome.failures[observation.id]
    return outcome.rasters.get(observation.id, [])


def compute_indices_for_observations(
    session: Session,
    *,
    aoi: Aoi,
    observations: list[Observation],
    methodology: MethodologyVersion,
    storage: Storage,
    scale: int = DEFAULT_SCALE_METERS,
    ee_module: Any = earthengine,
    on_export_submit: Callable[[int], None] | None = None,
) -> IndexStageOutcome:
    """Compute and persist NBR + NDVI rasters for a chunk of observations.

    Two cost levers live here (``docs/scaling.md`` §3.1/§3.3): an index raster
    whose row *and* COG file already exist under this methodology is **reused**
    (no export, no valid-fraction measurement — the stored fraction stands), and
    everything that does need exporting across the chunk is submitted to Earth
    Engine as **one batch** so the tasks progress through EE's queue
    concurrently. Failures stay per-observation: a failed export marks only its
    observation failed (in ``failures``); sibling artifacts that succeeded are
    persisted, exactly like the pre-batch behavior under upserts.

    ``on_export_submit`` (if given) is called with the number of export requests
    immediately before they are handed to Earth Engine — i.e. only when the
    chunk actually submits work — so the caller can record/log the pending
    batch before the long queue wait begins.
    """
    region = mapping(to_shape(aoi.geometry))
    outcome = IndexStageOutcome()
    requests: list[ExportRequest] = []
    # Parallel to `requests`: which (observation, index type, fraction) each export lands.
    slots: list[tuple[Observation, str, float]] = []

    for observation in observations:
        try:
            bands = _require_bands(observation.sensor)
            date = observation.acquired_at.date().isoformat()
            reusable: dict[str, IndexRaster] = {}
            missing: dict[str, list[str]] = {}
            for index_type, nd_bands in index_bands(observation.sensor).items():
                existing = _get_index_raster(
                    session,
                    observation_id=observation.id,
                    methodology_version_id=methodology.id,
                    index_type=index_type,
                )
                if existing is not None and Path(existing.cog_path).exists():
                    reusable[index_type] = existing
                else:
                    missing[index_type] = nd_bands

            outcome.rasters[observation.id] = list(reusable.values())
            outcome.reused += len(reusable)
            if not missing:
                continue

            masked = build_masked_image(observation, ee_module=ee_module)
            fraction = qa.measure_valid_fraction(
                masked, bands.red, region, scale, ee_module=ee_module
            )
            qa.record_quality_mask(
                session, observation_id=observation.id, valid_pixel_fraction=fraction
            )
            for index_type, nd_bands in missing.items():
                index_image = ee_module.normalized_difference(masked, nd_bands)
                # The scene id keeps same-day observations (both sensors, adjacent
                # tiles) from exporting to the same path and silently overwriting
                # each other; the AOI id keeps distinct AOI names that sanitize
                # identically from sharing a tree.
                key = CogKey(
                    aoi=f"{aoi.id}-{aoi.name}",
                    product=index_type,
                    date=date,
                    filename=f"{index_type.lower()}-{observation.source_scene_id}.tif",
                )
                requests.append(ExportRequest(index_image, key, scale=scale, region=region))
                slots.append((observation, index_type, fraction))
        except (StorageError, EarthEngineError) as exc:
            outcome.failures[observation.id] = exc
            outcome.rasters.pop(observation.id, None)

    if requests and on_export_submit is not None:
        on_export_submit(len(requests))
    export_results = storage.export_images(requests) if requests else []
    for (observation, index_type, fraction), result in zip(slots, export_results, strict=True):
        if observation.id in outcome.failures:
            continue
        if isinstance(result, StorageError):
            outcome.failures[observation.id] = result
            continue
        raster = _upsert_index_raster(
            session,
            observation_id=observation.id,
            methodology_version_id=methodology.id,
            index_type=index_type,
            cog_path=str(result),
            valid_pixel_fraction=fraction,
        )
        outcome.rasters.setdefault(observation.id, []).append(raster)
        outcome.exported += 1

    session.flush()
    return outcome


def _get_index_raster(
    session: Session,
    *,
    observation_id: int,
    methodology_version_id: int,
    index_type: str,
) -> IndexRaster | None:
    return session.execute(
        select(IndexRaster)
        .where(IndexRaster.observation_id == observation_id)
        .where(IndexRaster.index_type == index_type)
        .where(IndexRaster.methodology_version_id == methodology_version_id)
    ).scalar_one_or_none()


def _upsert_index_raster(
    session: Session,
    *,
    observation_id: int,
    methodology_version_id: int,
    index_type: str,
    cog_path: str,
    valid_pixel_fraction: float,
) -> IndexRaster:
    existing = session.execute(
        select(IndexRaster)
        .where(IndexRaster.observation_id == observation_id)
        .where(IndexRaster.index_type == index_type)
        .where(IndexRaster.methodology_version_id == methodology_version_id)
    ).scalar_one_or_none()
    if existing is not None:
        existing.cog_path = cog_path
        existing.valid_pixel_fraction = valid_pixel_fraction
        return existing
    created = IndexRaster(
        observation_id=observation_id,
        methodology_version_id=methodology_version_id,
        index_type=index_type,
        cog_path=cog_path,
        valid_pixel_fraction=valid_pixel_fraction,
    )
    session.add(created)
    return created
