"""End-to-end orchestration: discover → indices → change → candidates → events.

Because compute runs in Earth Engine, each export is an asynchronous task; the storage
seam blocks and polls each export to completion before the dependent step, so a single
``run_pipeline`` call drives the whole thread synchronously (a submit-and-return mode is a
later bead if needed). Export failures are isolated per observation: a failing scene is
skipped (and counted in the summary) instead of starving the rest of the run, so one
persistently bad export cannot zero out a scheduled window. Event tracking (Slice 2)
runs as the final stage. This module is pure orchestration over the building blocks and
is fully injectable, so the hallway test runs it against stubbed EE/storage.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forest_sentinel import candidates, change, earthengine, events, indices
from forest_sentinel.earthengine import EarthEngineError
from forest_sentinel.hls import discover_observations
from forest_sentinel.models import Aoi, MethodologyVersion, Observation
from forest_sentinel.storage import Storage, StorageError

logger = logging.getLogger(__name__)

# Candidates are extracted from the ΔNBR product (NBR drop = disturbance).
CANDIDATE_CHANGE_TYPE = "delta_nbr"

# Namespace for the per-AOI advisory run lock (arbitrary app-wide constant).
AOI_RUN_LOCK_CLASS = 0x0F5


def _acquire_aoi_run_lock(session: Session, aoi_id: int) -> None:
    """Serialize pipeline runs per AOI for the duration of this transaction.

    Discovery is race-safe on its own (ON CONFLICT), but the later upserts
    (quality_mask, index/change rasters, candidate replacement) are read-then-write:
    a manual run alongside the systemd timer would hit duplicate-key errors or double
    candidate sets. The transaction-scoped Postgres advisory lock makes the second
    run wait until the first commits; it then sees the committed rows and skips.
    """
    session.execute(select(func.pg_advisory_xact_lock(AOI_RUN_LOCK_CLASS, aoi_id)))


@dataclass(frozen=True)
class PipelineSummary:
    """Per-stage counts from one pipeline run."""

    observations_discovered: int
    observations_recorded: int
    observations_skipped: int
    index_rasters: int
    change_rasters: int
    candidates: int
    events_created: int
    event_observations: int
    export_failures: int = 0  # observations skipped because an EE export failed


def run_pipeline(
    session: Session,
    *,
    aoi: Aoi,
    since: date,
    until: date,
    methodology: MethodologyVersion,
    storage: Storage,
    baseline_window: int = change.DEFAULT_BASELINE_WINDOW,
    threshold: float | None = None,
    min_area_m2: float | None = None,
    scale: int = indices.DEFAULT_SCALE_METERS,
    ee_module: Any = earthengine,
) -> PipelineSummary:
    """Run discover → indices → change → candidates → events for one AOI and window."""
    _acquire_aoi_run_lock(session, aoi.id)
    region = mapping(to_shape(aoi.geometry))

    discovery = discover_observations(session, aoi, since=since, until=until, ee_module=ee_module)

    # Only the window's observations are (re)processed; without this filter every
    # scheduled run would re-export the AOI's entire history. The trailing baseline
    # still draws on all prior observations (change.py queries them itself).
    observations = (
        session.execute(
            select(Observation)
            .where(Observation.aoi_id == aoi.id)
            .where(Observation.acquired_at >= datetime.combine(since, time.min, tzinfo=UTC))
            .where(Observation.acquired_at < datetime.combine(until, time.min, tzinfo=UTC))
            .order_by(Observation.acquired_at)
        )
        .scalars()
        .all()
    )

    # One bad export must not starve the run: failing observations are skipped and
    # counted; already-persisted rows for them are consistent (upserts) and the next
    # run retries.
    export_failures = 0
    failed_observation_ids: set[int] = set()

    index_count = 0
    for observation in observations:
        try:
            index_count += len(
                indices.compute_indices_for_observation(
                    session,
                    aoi=aoi,
                    observation=observation,
                    methodology=methodology,
                    storage=storage,
                    scale=scale,
                    ee_module=ee_module,
                )
            )
        except (StorageError, EarthEngineError) as exc:
            export_failures += 1
            failed_observation_ids.add(observation.id)
            logger.warning(
                "skipping observation %s: index export failed (%s)",
                observation.source_scene_id,
                exc,
            )

    change_count = 0
    candidate_count = 0
    for observation in observations:
        if observation.id in failed_observation_ids:
            continue
        try:
            products = change.compute_change_products_for_observation(
                session,
                aoi=aoi,
                observation=observation,
                methodology=methodology,
                storage=storage,
                baseline_window=baseline_window,
                scale=scale,
                ee_module=ee_module,
            )
            change_count += len(products)
            for product in products:
                if product.change_type != CANDIDATE_CHANGE_TYPE:
                    continue
                candidate_count += len(
                    candidates.extract_candidates_for_change_raster(
                        session,
                        change_raster=product.change_raster,
                        delta_image=product.delta_image,
                        region=region,
                        scale=scale,
                        threshold=threshold,
                        min_area_m2=min_area_m2,
                        ee_module=ee_module,
                    )
                )
        except (StorageError, EarthEngineError) as exc:
            export_failures += 1
            logger.warning(
                "skipping observation %s: change/candidate stage failed (%s)",
                observation.source_scene_id,
                exc,
            )

    tracking = events.track_events_for_aoi(session, aoi=aoi)

    return PipelineSummary(
        observations_discovered=discovery.discovered,
        observations_recorded=discovery.recorded,
        observations_skipped=discovery.skipped,
        index_rasters=index_count,
        change_rasters=change_count,
        candidates=candidate_count,
        events_created=tracking.events_created,
        event_observations=tracking.observations_added,
        export_failures=export_failures,
    )
