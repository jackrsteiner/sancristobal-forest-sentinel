"""Change products (ΔNBR / ΔNDVI) against a trailing-median baseline, computed in EE.

For a current observation, the baseline is the per-pixel **median** of the index over a
trailing window of prior observations (``ImageCollection.median()``); the change product is
``current − baseline`` (``docs/architecture.md`` §4a). The delta is exported as a COG through
the storage seam and recorded as a ``change_raster`` with provenance to the source observation,
the methodology version, and every contributing ``index_raster`` (current + baseline).
"""

from dataclasses import dataclass
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel import earthengine, indices
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    ChangeRasterSource,
    IndexRaster,
    MethodologyVersion,
    Observation,
)
from forest_sentinel.storage import CogKey, Storage

DEFAULT_BASELINE_WINDOW = 5

# Change product -> the index it is derived from.
CHANGE_TYPES: dict[str, str] = {
    "delta_nbr": "NBR",
    "delta_ndvi": "NDVI",
}


@dataclass(frozen=True)
class ChangeProduct:
    """A persisted change raster plus the EE delta image it was computed from.

    The pipeline (#42) reuses ``delta_image`` for candidate extraction (#41) without
    rebuilding it.
    """

    change_type: str
    change_raster: ChangeRaster
    delta_image: Any


def compute_change_products_for_observation(
    session: Session,
    *,
    aoi: Aoi,
    observation: Observation,
    methodology: MethodologyVersion,
    storage: Storage,
    baseline_window: int = DEFAULT_BASELINE_WINDOW,
    scale: int = indices.DEFAULT_SCALE_METERS,
    ee_module: Any = earthengine,
) -> list[ChangeProduct]:
    """Compute and persist ΔNBR/ΔNDVI for one observation against its trailing baseline.

    An observation with no prior observations in the AOI has no baseline and is skipped
    (the next run, with one more prior observation, produces deltas).
    """
    region = mapping(to_shape(aoi.geometry))
    date = observation.acquired_at.date().isoformat()
    results: list[ChangeProduct] = []

    for change_type, index_type in CHANGE_TYPES.items():
        baseline_obs = (
            session.execute(
                select(Observation)
                .where(Observation.aoi_id == aoi.id)
                .where(Observation.acquired_at < observation.acquired_at)
                .order_by(Observation.acquired_at.desc())
                .limit(baseline_window)
            )
            .scalars()
            .all()
        )
        if not baseline_obs:
            continue

        current_image = indices.build_index_image(observation, index_type, ee_module=ee_module)
        baseline_images = [
            indices.build_index_image(prior, index_type, ee_module=ee_module)
            for prior in baseline_obs
        ]
        baseline_median = ee_module.median_of(baseline_images)
        delta = ee_module.subtract(current_image, baseline_median)

        # As with index COGs (see indices.py), the scene id and AOI id keep paths
        # collision-free.
        key = CogKey(
            aoi=f"{aoi.id}-{aoi.name}",
            product=change_type,
            date=date,
            filename=f"{change_type}-{observation.source_scene_id}.tif",
        )
        cog_path = storage.export_image(delta, key, scale=scale, region=region)

        source_obs_ids = [observation.id, *(prior.id for prior in baseline_obs)]
        index_rows = (
            session.execute(
                select(IndexRaster)
                .where(IndexRaster.observation_id.in_(source_obs_ids))
                .where(IndexRaster.index_type == index_type)
                .where(IndexRaster.methodology_version_id == methodology.id)
            )
            .scalars()
            .all()
        )
        current_index = next(
            (row for row in index_rows if row.observation_id == observation.id), None
        )
        fraction = current_index.valid_pixel_fraction if current_index is not None else None

        change = _upsert_change_raster(
            session,
            observation_id=observation.id,
            methodology_version_id=methodology.id,
            change_type=change_type,
            cog_path=str(cog_path),
            baseline_window=baseline_window,
            valid_pixel_fraction=fraction,
        )
        session.flush()
        _replace_sources(session, change.id, [row.id for row in index_rows])
        results.append(
            ChangeProduct(change_type=change_type, change_raster=change, delta_image=delta)
        )

    session.flush()
    return results


def _upsert_change_raster(
    session: Session,
    *,
    observation_id: int,
    methodology_version_id: int,
    change_type: str,
    cog_path: str,
    baseline_window: int,
    valid_pixel_fraction: float | None,
) -> ChangeRaster:
    existing = session.execute(
        select(ChangeRaster)
        .where(ChangeRaster.observation_id == observation_id)
        .where(ChangeRaster.change_type == change_type)
        .where(ChangeRaster.methodology_version_id == methodology_version_id)
    ).scalar_one_or_none()
    if existing is not None:
        existing.cog_path = cog_path
        existing.baseline_window = baseline_window
        existing.valid_pixel_fraction = valid_pixel_fraction
        return existing
    created = ChangeRaster(
        observation_id=observation_id,
        methodology_version_id=methodology_version_id,
        change_type=change_type,
        cog_path=cog_path,
        baseline_window=baseline_window,
        valid_pixel_fraction=valid_pixel_fraction,
    )
    session.add(created)
    return created


def _replace_sources(session: Session, change_raster_id: int, index_raster_ids: list[int]) -> None:
    for source in session.execute(
        select(ChangeRasterSource).where(ChangeRasterSource.change_raster_id == change_raster_id)
    ).scalars():
        session.delete(source)
    session.flush()
    for index_raster_id in index_raster_ids:
        session.add(
            ChangeRasterSource(change_raster_id=change_raster_id, index_raster_id=index_raster_id)
        )
