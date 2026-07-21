"""Change products (ΔNBR / ΔNDVI) against a trailing-median baseline, computed in EE.

For a current observation, the baseline is the per-pixel **median** of the index over a
trailing window of prior observations (``ImageCollection.median()``); the change product is
``current − baseline`` (``docs/architecture.md`` §4a). The delta is exported as a COG through
the storage seam and recorded as a ``change_raster`` with provenance to the source observation,
the methodology version, and every contributing ``index_raster`` (current + baseline).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
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
    ``delta_image`` is ``None``: nothing was recomputed. The pipeline skips
    extraction when the current methodology already has candidates for the
    raster; a raster reused under a *new* detection layer (same raster lineage,
    different threshold/min-area/mask — Finding 1) has none yet, and the
    pipeline rebuilds the delta from recorded provenance to extract them
    without re-exporting.

    ``region`` is the scene ∩ AOI GeoJSON the delta was exported over (#78);
    candidate extraction vectorizes over the same region. ``None`` for
    frozen/reused products — nothing downstream reduces over them.
    """

    change_type: str
    change_raster: ChangeRaster
    delta_image: Any
    reused: bool = False
    region: Any = None


@dataclass
class PendingDelta:
    """One delta awaiting export: the request plus what its persistence needs."""

    change_type: str
    index_type: str
    delta_image: Any
    request: ExportRequest
    baseline: list[Observation]


@dataclass
class ChangePlan:
    """Pass-1 outcome for one observation (#156).

    ``settled`` products (frozen/reused) are final; ``pending`` deltas still need
    their exports run. The pipeline collects many plans' requests into ONE
    ``storage.export_images`` batch so the Earth Engine queue waits overlap
    across observations, then lands each plan with ``persist_change_products``
    — keeping the per-observation checkpoint commits of the serial design.
    """

    observation: Observation
    baseline_window: int
    settled: list[ChangeProduct] = field(default_factory=list)
    pending: list[PendingDelta] = field(default_factory=list)
    region: Any = None  # scene ∩ AOI (#78); set iff ``pending`` is non-empty


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

    This is the single-observation convenience over the plan/persist split (#156):
    the pipeline batches many observations' plans into one export submission.
    """
    plan = plan_change_products(
        session,
        aoi=aoi,
        observation=observation,
        methodology=methodology,
        baseline_window=baseline_window,
        scale=scale,
        ee_module=ee_module,
    )
    export_results: list[Path | StorageError] = []
    if plan.pending:
        if on_export_submit is not None:
            on_export_submit(len(plan.pending))
        export_results = storage.export_images([item.request for item in plan.pending])
    return persist_change_products(
        session, methodology=methodology, plan=plan, export_results=export_results
    )


def plan_change_products(
    session: Session,
    *,
    aoi: Aoi,
    observation: Observation,
    methodology: MethodologyVersion,
    baseline_window: int = DEFAULT_BASELINE_WINDOW,
    scale: int = indices.DEFAULT_SCALE_METERS,
    ee_module: Any = earthengine,
) -> ChangePlan:
    """Pass 1: classify each change type and build its export request (#156).

    Frozen and reused rasters land in ``settled``; anything needing an export
    lands in ``pending`` with its delta image and request already built. Nothing
    is exported or persisted here, so many observations' plans can be submitted
    to Earth Engine together.
    """
    region = mapping(to_shape(aoi.geometry))
    date = observation.acquired_at.date().isoformat()
    plan = ChangePlan(observation=observation, baseline_window=baseline_window)

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
                    .where(IndexRaster.raster_lineage_id == methodology.raster_lineage_id)
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

    # Decide per change type — frozen / reused / needs export — and build the
    # pending export requests.
    for change_type, index_type in CHANGE_TYPES.items():
        # Frozen: the existing raster is evidence for tracked candidates. Recomputing
        # would overwrite its COG (same path) and rewrite its recorded sources with a
        # different baseline — silently invalidating the events derived from it.
        existing = _get_change_raster(
            session,
            observation_id=observation.id,
            change_type=change_type,
            raster_lineage_id=methodology.raster_lineage_id,
        )
        if existing is not None and change_raster_is_frozen(session, existing.id):
            plan.settled.append(
                ChangeProduct(change_type=change_type, change_raster=existing, delta_image=None)
            )
            continue
        # Reused (#77): row + COG already persisted by an earlier run — possibly
        # under a different detection layer of the same raster lineage (Finding 1).
        # The recorded baseline (change_raster_source) stands — it is not
        # recomputed as new prior observations arrive.
        if existing is not None and Path(existing.cog_path).exists():
            plan.settled.append(
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
        if plan.region is None:
            # Both deltas derive from the current observation, so one scene ∩ AOI
            # clip (#78) covers them; the same region feeds candidate extraction
            # via ChangeProduct.region.
            plan.region = indices.clipped_region(
                masked_image(observation), region, ee_module=ee_module
            )
        plan.pending.append(
            PendingDelta(
                change_type=change_type,
                index_type=index_type,
                delta_image=delta,
                request=ExportRequest(delta, key, scale=scale, region=plan.region),
                baseline=baseline_obs,
            )
        )

    return plan


def persist_change_products(
    session: Session,
    *,
    methodology: MethodologyVersion,
    plan: ChangePlan,
    export_results: Sequence[Path | StorageError],
) -> list[ChangeProduct]:
    """Pass 2: record ``plan``'s exported deltas (#156).

    ``export_results`` must be ``plan.pending``'s results, in order. Successes
    are persisted even when a sibling delta failed (rows are consistent under
    upserts and resume on the next run); the first failure is re-raised after,
    so the pipeline counts the observation as failed — matching the pre-split
    behavior — while siblings' committed work stands.
    """
    observation = plan.observation
    results = list(plan.settled)
    export_error: StorageError | None = None
    for pending, export_result in zip(plan.pending, export_results, strict=True):
        if isinstance(export_result, StorageError):
            export_error = export_error or export_result
            continue

        source_obs_ids = [observation.id, *(prior.id for prior in pending.baseline)]
        index_rows = (
            session.execute(
                select(IndexRaster)
                .where(IndexRaster.observation_id.in_(source_obs_ids))
                .where(IndexRaster.index_type == pending.index_type)
                .where(IndexRaster.raster_lineage_id == methodology.raster_lineage_id)
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
            raster_lineage_id=methodology.raster_lineage_id,
            change_type=pending.change_type,
            cog_path=str(export_result),
            baseline_window=plan.baseline_window,
            valid_pixel_fraction=fraction,
        )
        session.flush()
        _replace_sources(session, change.id, [row.id for row in index_rows])
        results.append(
            ChangeProduct(
                change_type=pending.change_type,
                change_raster=change,
                delta_image=pending.delta_image,
                region=plan.region,
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
    raster_lineage_id: int,
) -> ChangeRaster | None:
    return session.execute(
        select(ChangeRaster)
        .where(ChangeRaster.observation_id == observation_id)
        .where(ChangeRaster.change_type == change_type)
        .where(ChangeRaster.raster_lineage_id == raster_lineage_id)
    ).scalar_one_or_none()


def _upsert_change_raster(
    session: Session,
    *,
    observation_id: int,
    raster_lineage_id: int,
    change_type: str,
    cog_path: str,
    baseline_window: int,
    valid_pixel_fraction: float | None,
) -> ChangeRaster:
    existing = _get_change_raster(
        session,
        observation_id=observation_id,
        change_type=change_type,
        raster_lineage_id=raster_lineage_id,
    )
    if existing is not None:
        existing.cog_path = cog_path
        existing.baseline_window = baseline_window
        existing.valid_pixel_fraction = valid_pixel_fraction
        return existing
    created = ChangeRaster(
        observation_id=observation_id,
        raster_lineage_id=raster_lineage_id,
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
