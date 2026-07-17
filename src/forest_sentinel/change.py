"""Change products (ΔNBR / ΔNDVI) against a trailing-median baseline, computed in EE.

For a current observation, the baseline is the per-pixel **median** of the index over a
trailing window of prior observations (``ImageCollection.median()``); the change product is
``current − baseline`` (``docs/architecture.md`` §4a). The delta is exported as a COG through
the storage seam and recorded as a ``change_raster`` with provenance to the source observation,
the methodology version, and every contributing ``index_raster`` (current + baseline).
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from forest_sentinel import earthengine, indices
from forest_sentinel.candidates import change_raster_is_frozen
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    ChangeRasterSource,
    IndexRaster,
    MethodologyVersion,
    Observation,
)
from forest_sentinel.storage import CogKey, ExportRequest, Storage, StorageError

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
    rebuilding it. For a **frozen** raster (its candidates are tracked into events)
    or a **reused** one (row + COG already persisted by an earlier run, #77)
    ``delta_image`` is ``None``: nothing was recomputed, and candidate extraction
    is skipped — a reused raster's candidates were committed in the same
    checkpoint as the raster itself.

    ``region`` is the scene ∩ AOI GeoJSON the delta was exported over (#78);
    candidate extraction vectorizes over the same region. ``None`` for
    frozen/reused products — nothing downstream reduces over them.
    """

    change_type: str
    change_raster: ChangeRaster
    delta_image: Any
    reused: bool = False
    region: Any = None


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
    on_export_submit: Callable[[int], None] | None = None,
) -> list[ChangeProduct]:
    """Compute and persist ΔNBR/ΔNDVI for one observation against its trailing baseline.

    The baseline for each index type is the trailing window of prior observations that
    have an ``index_raster`` under this methodology — so the imagery reduced into the
    median always matches the recorded ``change_raster_source`` provenance. An
    observation with no such priors has no baseline and is skipped (the next run, with
    one more indexed prior observation, produces deltas).

    ``on_export_submit`` (if given) is called with the number of export requests
    immediately before they are handed to Earth Engine — only when this observation
    actually submits work (frozen/reused deltas submit nothing).
    """
    region = mapping(to_shape(aoi.geometry))
    date = observation.acquired_at.date().isoformat()
    results: list[ChangeProduct] = []

    # Masked source images are shared across change types and built lazily (a fully
    # frozen observation needs none). The baseline is per index type: only prior
    # observations that HAVE an index raster under this methodology participate, so
    # the imagery reduced into the median always equals the recorded
    # change_raster_source provenance — an observation whose index export failed (or
    # that predates this methodology) is excluded from the math, not just the record.
    baseline_cache: dict[str, list[Observation]] = {}
    masked_images: dict[int, Any] = {}

    def baseline_for(index_type: str) -> list[Observation]:
        if index_type not in baseline_cache:
            baseline_cache[index_type] = list(
                session.execute(
                    select(Observation)
                    .join(IndexRaster, IndexRaster.observation_id == Observation.id)
                    .where(Observation.aoi_id == aoi.id)
                    .where(Observation.acquired_at < observation.acquired_at)
                    .where(IndexRaster.index_type == index_type)
                    .where(IndexRaster.methodology_version_id == methodology.id)
                    .order_by(Observation.acquired_at.desc())
                    .limit(baseline_window)
                )
                .scalars()
                .all()
            )
        return baseline_cache[index_type]

    def masked_image(obs: Observation) -> Any:
        if obs.id not in masked_images:
            masked_images[obs.id] = indices.build_masked_image(obs, ee_module=ee_module)
        return masked_images[obs.id]

    def index_image(obs: Observation, index_type: str) -> Any:
        nd_bands = indices.index_bands(obs.sensor)[index_type]
        return ee_module.normalized_difference(masked_image(obs), nd_bands)

    # Pass 1: decide per change type — frozen / reused / needs export — and build
    # the pending export requests so both deltas go to Earth Engine as one batch.
    pending: list[tuple[str, str, Any, CogKey, list[Observation]]] = []
    for change_type, index_type in CHANGE_TYPES.items():
        # Frozen: the existing raster is evidence for tracked candidates. Recomputing
        # would overwrite its COG (same path) and rewrite its recorded sources with a
        # different baseline — silently invalidating the events derived from it.
        existing = _get_change_raster(
            session,
            observation_id=observation.id,
            change_type=change_type,
            methodology_version_id=methodology.id,
        )
        if existing is not None and change_raster_is_frozen(session, existing.id):
            results.append(
                ChangeProduct(change_type=change_type, change_raster=existing, delta_image=None)
            )
            continue
        # Reused (#77): row + COG already persisted by an earlier run. The recorded
        # baseline (change_raster_source) stands — it is not recomputed as new prior
        # observations arrive; candidates were committed alongside the raster.
        if existing is not None and Path(existing.cog_path).exists():
            results.append(
                ChangeProduct(
                    change_type=change_type,
                    change_raster=existing,
                    delta_image=None,
                    reused=True,
                )
            )
            continue

        baseline_obs = baseline_for(index_type)
        if not baseline_obs:
            # No usable baseline for this index type; skip it (the next run, with one
            # more indexed prior observation, produces the delta).
            continue

        current_image = index_image(observation, index_type)
        baseline_images = [index_image(prior, index_type) for prior in baseline_obs]
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
        pending.append((change_type, index_type, delta, key, baseline_obs))

    # Pass 2: batch-export, then persist each success. Successes are recorded even
    # when a sibling delta failed (rows are consistent under upserts and resume on
    # the next run); the first failure is re-raised so the pipeline counts this
    # observation as failed, matching the pre-batch behavior.
    export_error: StorageError | None = None
    if pending:
        # Both deltas derive from the current observation, so one scene ∩ AOI
        # clip (#78) covers the batch; the same region feeds candidate
        # extraction via ChangeProduct.region.
        observation_region = indices.clipped_region(
            masked_image(observation), region, ee_module=ee_module
        )
        if on_export_submit is not None:
            on_export_submit(len(pending))
        export_results = storage.export_images(
            [
                ExportRequest(delta, key, scale=scale, region=observation_region)
                for (_, _, delta, key, _) in pending
            ]
        )
        for (change_type, index_type, delta, _key, baseline_obs), export_result in zip(
            pending, export_results, strict=True
        ):
            if isinstance(export_result, StorageError):
                export_error = export_error or export_result
                continue

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
                cog_path=str(export_result),
                baseline_window=baseline_window,
                valid_pixel_fraction=fraction,
            )
            session.flush()
            _replace_sources(session, change.id, [row.id for row in index_rows])
            results.append(
                ChangeProduct(
                    change_type=change_type,
                    change_raster=change,
                    delta_image=delta,
                    region=observation_region,
                )
            )

    session.flush()
    if export_error is not None:
        raise export_error
    return results


def _get_change_raster(
    session: Session,
    *,
    observation_id: int,
    change_type: str,
    methodology_version_id: int,
) -> ChangeRaster | None:
    return session.execute(
        select(ChangeRaster)
        .where(ChangeRaster.observation_id == observation_id)
        .where(ChangeRaster.change_type == change_type)
        .where(ChangeRaster.methodology_version_id == methodology_version_id)
    ).scalar_one_or_none()


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
    existing = _get_change_raster(
        session,
        observation_id=observation_id,
        change_type=change_type,
        methodology_version_id=methodology_version_id,
    )
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
    session.execute(
        delete(ChangeRasterSource).where(ChangeRasterSource.change_raster_id == change_raster_id)
    )
    for index_raster_id in index_raster_ids:
        session.add(
            ChangeRasterSource(change_raster_id=change_raster_id, index_raster_id=index_raster_id)
        )
