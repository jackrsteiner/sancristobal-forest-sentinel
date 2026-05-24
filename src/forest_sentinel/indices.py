"""Per-observation vegetation/disturbance indices (NBR, NDVI), computed in Earth Engine.

For each observation this module rebuilds the HLS image, applies Fmask masking (#54),
computes NBR and NDVI as ``normalizedDifference`` band expressions, exports each as a COG
through the storage seam (#36), and records an ``index_raster`` row with provenance to the
source observation and the methodology version.

    NBR  = (NIR - SWIR2) / (NIR + SWIR2)
    NDVI = (NIR - RED)   / (NIR + RED)
"""

from dataclasses import dataclass
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel import earthengine, qa
from forest_sentinel.hls import HLS_COLLECTIONS
from forest_sentinel.models import Aoi, IndexRaster, MethodologyVersion, Observation
from forest_sentinel.storage import CogKey, Storage

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


def index_bands(sensor: str) -> dict[str, list[str]]:
    """The ``normalizedDifference`` band pairs per index for ``sensor``."""
    try:
        bands = SENSOR_BANDS[sensor]
    except KeyError as exc:
        raise ValueError(f"unsupported sensor for index computation: {sensor!r}") from exc
    return {
        "NBR": [bands.nir, bands.swir2],
        "NDVI": [bands.nir, bands.red],
    }


def _masked_image(observation: Observation, *, ee_module: Any) -> Any:
    """Rebuild the Fmask-masked HLS image for an observation."""
    if observation.sensor not in SENSOR_BANDS:
        raise ValueError(f"unsupported sensor for index computation: {observation.sensor!r}")
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
    masked = _masked_image(observation, ee_module=ee_module)
    return ee_module.normalized_difference(masked, nd_bands)


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
    bands = SENSOR_BANDS.get(observation.sensor)
    if bands is None:
        raise ValueError(f"unsupported sensor for index computation: {observation.sensor!r}")

    region = mapping(to_shape(aoi.geometry))
    masked = _masked_image(observation, ee_module=ee_module)

    fraction = qa.measure_valid_fraction(masked, bands.red, region, scale, ee_module=ee_module)
    qa.record_quality_mask(session, observation_id=observation.id, valid_pixel_fraction=fraction)

    date = observation.acquired_at.date().isoformat()
    results: list[IndexRaster] = []
    for index_type, nd_bands in index_bands(observation.sensor).items():
        index_image = ee_module.normalized_difference(masked, nd_bands)
        key = CogKey(
            aoi=aoi.name,
            product=index_type,
            date=date,
            filename=f"{index_type.lower()}.tif",
        )
        cog_path = storage.export_image(index_image, key, scale=scale, region=region)
        results.append(
            _upsert_index_raster(
                session,
                observation_id=observation.id,
                methodology_version_id=methodology.id,
                index_type=index_type,
                cog_path=str(cog_path),
                valid_pixel_fraction=fraction,
            )
        )
    session.flush()
    return results


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
