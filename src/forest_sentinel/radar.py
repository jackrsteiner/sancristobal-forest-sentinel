"""Radar backscatter change products (E16, #116): VV dB delta vs trailing median.

Mirrors ``change.py`` for Sentinel-1 GRD: for a current radar observation, the
baseline is the per-pixel **median** of the VV backscatter (dB — the
``COPERNICUS/S1_GRD`` catalog is already log-scaled) over a trailing window of
prior scenes with the **same orbit direction** (viewing geometry changes
backscatter independently of the ground, so cross-orbit baselines would flag
geometry, not disturbance). The delta ``current − baseline`` is exported as a
COG through the storage seam and recorded as a ``change_raster`` with
``change_type="delta_vv_db"`` — the README's ``radar_change_raster`` domain
object realized through the existing FK graph. The trailing median doubles as
speckle mitigation.

Baseline provenance: radar has no per-scene index rasters, so the recipe is
recorded as ``baseline_source_scene_ids`` (ordered) on the row rather than
through ``change_raster_source``. Freeze/reuse semantics match optical: a
raster whose candidates are tracked into events is never recomputed, and an
existing row+COG is reused without an export.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel import earthengine, indices
from forest_sentinel.candidates import change_raster_is_frozen
from forest_sentinel.change import DEFAULT_BASELINE_WINDOW, ChangeProduct
from forest_sentinel.models import Aoi, ChangeRaster, MethodologyVersion, Observation
from forest_sentinel.sentinel1 import S1_COLLECTION, S1_SENSOR
from forest_sentinel.storage import CogKey, Storage

RADAR_CHANGE_TYPE = "delta_vv_db"
_VV = "VV"

# Documented default: a VV backscatter drop of 3 dB or more vs the trailing
# median is a candidate. Overridable via the radar methodology parameters.
DEFAULT_DELTA_VV_DB_THRESHOLD = -3.0
_DB_THRESHOLD_PARAM = "delta_vv_db_threshold"


def resolve_db_threshold(methodology: MethodologyVersion, override: float | None = None) -> float:
    """dB threshold from the override, else methodology parameters, else default."""
    if override is not None:
        return override
    value = methodology.parameters.get(_DB_THRESHOLD_PARAM)
    return float(value) if value is not None else DEFAULT_DELTA_VV_DB_THRESHOLD


def compute_radar_change_for_observation(
    session: Session,
    *,
    aoi: Aoi,
    observation: Observation,
    methodology: MethodologyVersion,
    storage: Storage,
    baseline_window: int = DEFAULT_BASELINE_WINDOW,
    scale: int = indices.DEFAULT_SCALE_METERS,
    ee_module: Any = earthengine,
    on_export_submit: Callable[[int], None] | None = None,
) -> list[ChangeProduct]:
    """Compute and persist the VV dB delta for one radar observation.

    Returns the same ``ChangeProduct`` shape the optical stage produces, so
    candidate extraction downstream is source-agnostic. An observation with no
    same-orbit priors has no baseline and is skipped (the next run, with one
    more scene recorded, produces the delta).
    """
    existing = session.execute(
        select(ChangeRaster)
        .where(ChangeRaster.observation_id == observation.id)
        .where(ChangeRaster.change_type == RADAR_CHANGE_TYPE)
        .where(ChangeRaster.raster_lineage_id == methodology.raster_lineage_id)
    ).scalar_one_or_none()
    # Frozen: event evidence, never recomputed (matching change.py).
    if existing is not None and change_raster_is_frozen(session, existing.id):
        return [
            ChangeProduct(change_type=RADAR_CHANGE_TYPE, change_raster=existing, delta_image=None)
        ]
    # Reused (#77): row + COG already persisted; the recorded baseline stands.
    if existing is not None and Path(existing.cog_path).exists():
        return [
            ChangeProduct(
                change_type=RADAR_CHANGE_TYPE,
                change_raster=existing,
                delta_image=None,
                reused=True,
            )
        ]

    baseline_observations = list(
        session.execute(
            select(Observation)
            .where(Observation.aoi_id == aoi.id)
            .where(Observation.sensor == S1_SENSOR)
            .where(Observation.orbit_direction == observation.orbit_direction)
            .where(Observation.acquired_at < observation.acquired_at)
            .order_by(Observation.acquired_at.desc())
            .limit(baseline_window)
        )
        .scalars()
        .all()
    )
    if not baseline_observations:
        return []

    def vv_image(obs: Observation) -> Any:
        image = ee_module.image_by_id(f"{S1_COLLECTION}/{obs.source_scene_id}")
        return ee_module.select_band(image, _VV)

    current = vv_image(observation)
    baseline_median = ee_module.median_of([vv_image(prior) for prior in baseline_observations])
    delta = ee_module.subtract(current, baseline_median)

    region = mapping(to_shape(aoi.geometry))
    current_scene = ee_module.image_by_id(f"{S1_COLLECTION}/{observation.source_scene_id}")
    observation_region = indices.clipped_region(current_scene, region, ee_module=ee_module)

    key = CogKey(
        aoi=f"{aoi.id}-{aoi.name}",
        product=RADAR_CHANGE_TYPE,
        date=observation.acquired_at.date().isoformat(),
        filename=f"{RADAR_CHANGE_TYPE}-{observation.source_scene_id}.tif",
    )
    if on_export_submit is not None:
        on_export_submit(1)
    cog_path = storage.export_image(delta, key, scale=scale, region=observation_region)

    baseline_scene_ids = [prior.source_scene_id for prior in baseline_observations]
    if existing is not None:
        existing.cog_path = str(cog_path)
        existing.baseline_window = baseline_window
        existing.baseline_source_scene_ids = baseline_scene_ids
        raster = existing
    else:
        raster = ChangeRaster(
            observation_id=observation.id,
            raster_lineage_id=methodology.raster_lineage_id,
            change_type=RADAR_CHANGE_TYPE,
            cog_path=str(cog_path),
            baseline_window=baseline_window,
            valid_pixel_fraction=None,
            baseline_source_scene_ids=baseline_scene_ids,
        )
        session.add(raster)
    session.flush()
    return [
        ChangeProduct(
            change_type=RADAR_CHANGE_TYPE,
            change_raster=raster,
            delta_image=delta,
            region=observation_region,
        )
    ]
