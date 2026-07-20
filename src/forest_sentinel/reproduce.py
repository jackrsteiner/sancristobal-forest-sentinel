"""Re-export a pruned raster from its recorded provenance (#94).

The retention policy (#80) deletes COG files but keeps their catalog rows as the
reproduction recipe (``docs/architecture.md`` §7): scene ids, the methodology's
parameters (EE script version, scale, mask categories), and — for change
rasters — the exact ``change_raster_source`` set. This module rebuilds the image
in Earth Engine from those records and re-exports it to the raster's recorded
``cog_path`` via the storage seam. Database rows are never modified.

Two documented caveats bound the reproducibility claim to "same conclusions",
not bit-identical output:

- The original export region (scene ∩ AOI at run time) is not stored; it is
  re-derived from the scene footprint and the AOI's current geometry.
- The claim hinges on the recorded ``ee_script_version`` matching the running
  code's pin — a mismatched (or unrecorded) version is refused unless the
  caller forces it, and forcing logs a loud warning.
"""

import logging
from pathlib import Path
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel import earthengine, indices
from forest_sentinel.change import CHANGE_TYPES
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    ChangeRasterSource,
    IndexRaster,
    Observation,
    RasterLineage,
)
from forest_sentinel.sentinel1 import S1_COLLECTION
from forest_sentinel.storage import CogKey, Storage

logger = logging.getLogger(__name__)

# Rasters key on the raster lineage (Finding 1); its pin is the raster-stage
# script version (Finding 4). Lineages derived from parameter sets that predate
# the split carry the old ee_script_version value under this key.
_SCRIPT_VERSION_PARAM = "raster_script_version"
_SCALE_PARAM = "scale_m"
_RADAR_CHANGE_TYPE = "delta_vv_db"
_VV_BAND = "VV"


class ReproduceError(RuntimeError):
    """Raised when a raster cannot be reproduced from its recorded provenance."""


def reproduce_index_raster(
    session: Session,
    *,
    raster: IndexRaster,
    storage: Storage,
    current_script_version: str,
    force_version: bool = False,
    ee_module: Any = earthengine,
) -> Path:
    """Rebuild one index raster's image in EE and re-export it to its ``cog_path``."""
    lineage = _lineage(session, raster.raster_lineage_id)
    _check_script_version(lineage, current_script_version, force=force_version)
    observation, aoi = _observation_and_aoi(session, raster.observation_id)

    masked = indices.build_masked_image(observation, ee_module=ee_module)
    nd_bands = indices.index_bands(observation.sensor)[raster.index_type]
    image = ee_module.normalized_difference(masked, nd_bands)
    region = _rederived_region(masked, aoi, ee_module=ee_module)
    key = CogKey(
        aoi=f"{aoi.id}-{aoi.name}",
        product=raster.index_type,
        date=observation.acquired_at.date().isoformat(),
        filename=f"{raster.index_type.lower()}-{observation.source_scene_id}.tif",
    )
    _check_destination(storage, key, raster.cog_path)
    return storage.export_image(image, key, scale=_scale(lineage), region=region)


def reproduce_change_raster(
    session: Session,
    *,
    raster: ChangeRaster,
    storage: Storage,
    current_script_version: str,
    force_version: bool = False,
    ee_module: Any = earthengine,
) -> Path:
    """Rebuild one change raster against its *recorded* baseline and re-export it.

    The baseline is reconstructed from the raster's ``change_raster_source`` rows —
    the exact prior observations reduced into the original median — not from "the
    priors indexed now", so reproduction matches the recorded provenance even after
    later runs added newer index rasters.
    """
    lineage = _lineage(session, raster.raster_lineage_id)
    _check_script_version(lineage, current_script_version, force=force_version)

    delta, region, observation, aoi = rebuild_change_delta(
        session, raster=raster, ee_module=ee_module
    )
    key = CogKey(
        aoi=f"{aoi.id}-{aoi.name}",
        product=raster.change_type,
        date=observation.acquired_at.date().isoformat(),
        filename=f"{raster.change_type}-{observation.source_scene_id}.tif",
    )
    _check_destination(storage, key, raster.cog_path)
    return storage.export_image(delta, key, scale=_scale(lineage), region=region)


def rebuild_change_delta(
    session: Session,
    *,
    raster: ChangeRaster,
    ee_module: Any = earthengine,
) -> tuple[Any, Any, Observation, Aoi]:
    """Rebuild a change raster's delta image from recorded provenance — no export.

    Returns ``(delta_image, scene ∩ AOI region, observation, aoi)``. Optical
    deltas reconstruct their baseline from ``change_raster_source``; radar
    (``delta_vv_db``) from the recorded ``baseline_source_scene_ids``. This is
    what candidate re-extraction under a new detection layer reduces over
    (Finding 1): the raster lineage's math, replayed exactly, with no new COG.
    """
    observation, aoi = _observation_and_aoi(session, raster.observation_id)

    if raster.change_type == _RADAR_CHANGE_TYPE:
        scene_ids = raster.baseline_source_scene_ids or []
        if not scene_ids:
            raise ReproduceError(
                f"change_raster {raster.id} records no baseline scene ids; "
                "its provenance cannot reproduce the trailing median"
            )

        def vv_image(scene_id: str) -> Any:
            return ee_module.select_band(
                ee_module.image_by_id(f"{S1_COLLECTION}/{scene_id}"), _VV_BAND
            )

        current = vv_image(observation.source_scene_id)
        baseline = ee_module.median_of([vv_image(scene_id) for scene_id in scene_ids])
        delta = ee_module.subtract(current, baseline)
        scene = ee_module.image_by_id(f"{S1_COLLECTION}/{observation.source_scene_id}")
        region = _rederived_region(scene, aoi, ee_module=ee_module)
        return delta, region, observation, aoi

    index_type = CHANGE_TYPES.get(raster.change_type)
    if index_type is None:
        raise ReproduceError(f"change_raster {raster.id} has unknown type {raster.change_type!r}")

    baseline_observations = list(
        session.execute(
            select(Observation)
            .join(IndexRaster, IndexRaster.observation_id == Observation.id)
            .join(ChangeRasterSource, ChangeRasterSource.index_raster_id == IndexRaster.id)
            .where(ChangeRasterSource.change_raster_id == raster.id)
            .where(IndexRaster.index_type == index_type)
            .where(Observation.id != raster.observation_id)
            .order_by(Observation.acquired_at)
        )
        .scalars()
        .all()
    )
    if not baseline_observations:
        raise ReproduceError(
            f"change_raster {raster.id} records no baseline sources for {index_type}; "
            "its provenance cannot reproduce the trailing median"
        )

    masked = indices.build_masked_image(observation, ee_module=ee_module)
    nd_bands = indices.index_bands(observation.sensor)[index_type]
    current_image = ee_module.normalized_difference(masked, nd_bands)
    baseline_images = [
        indices.build_index_image(prior, index_type, ee_module=ee_module)
        for prior in baseline_observations
    ]
    delta = ee_module.subtract(current_image, ee_module.median_of(baseline_images))
    region = _rederived_region(masked, aoi, ee_module=ee_module)
    return delta, region, observation, aoi


def _lineage(session: Session, raster_lineage_id: int) -> RasterLineage:
    lineage = session.get(RasterLineage, raster_lineage_id)
    if lineage is None:
        raise ReproduceError(f"raster lineage {raster_lineage_id} not found")
    return lineage


def _check_script_version(lineage: RasterLineage, current: str, *, force: bool) -> None:
    """Refuse (or, forced, loudly warn) when the recorded raster pin differs.

    "Same conclusions" reproducibility hinges on the recorded
    ``raster_script_version`` — running today's band math against a row produced
    by other code silently yields a different product under the recorded
    identity.
    """
    recorded = lineage.parameters.get(_SCRIPT_VERSION_PARAM)
    if recorded == current:
        return
    message = (
        f"recorded raster_script_version {recorded!r} does not match the running "
        f"code's {current!r}; the reproduced raster may not match the recorded "
        "provenance"
    )
    if not force:
        raise ReproduceError(f"{message} (pass --force-version to reproduce anyway)")
    logger.warning("%s (forced)", message)


def _observation_and_aoi(session: Session, observation_id: int) -> tuple[Observation, Aoi]:
    observation = session.get(Observation, observation_id)
    if observation is None:
        raise ReproduceError(f"observation {observation_id} not found")
    aoi = session.get(Aoi, observation.aoi_id)
    if aoi is None:
        raise ReproduceError(f"AOI {observation.aoi_id} not found")
    return observation, aoi


def _rederived_region(masked_image: Any, aoi: Aoi, *, ee_module: Any) -> Any:
    """Scene ∩ AOI, re-derived — the original run's region is not stored (caveat above)."""
    return indices.clipped_region(
        masked_image, mapping(to_shape(aoi.geometry)), ee_module=ee_module
    )


def _scale(lineage: RasterLineage) -> int:
    value = lineage.parameters.get(_SCALE_PARAM)
    return int(value) if value is not None else indices.DEFAULT_SCALE_METERS


def _check_destination(storage: Storage, key: CogKey, recorded_path: str) -> None:
    """The reconstructed key must land exactly on the recorded ``cog_path``.

    A mismatch means the store layout (or the AOI's name) changed since the row
    was written; exporting anywhere but the recorded path would strand a file the
    catalog doesn't point at.
    """
    derived = storage.path_for(key)
    if derived != Path(recorded_path):
        raise ReproduceError(
            f"reconstructed export path {derived} does not match the recorded "
            f"cog_path {recorded_path}; refusing to export to a location the "
            "catalog does not reference"
        )
