"""End-to-end orchestration: discover → indices → change → candidates → events.

Because compute runs in Earth Engine, each export is an asynchronous task; the storage
seam blocks and polls each export to completion before the dependent step, so a single
``run_pipeline`` call drives the whole thread synchronously (a submit-and-return mode is a
later bead if needed). Event tracking (Slice 2) runs as the final stage. This module is
pure orchestration over the building blocks and is fully injectable, so the hallway test
runs it against stubbed EE/storage.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel import candidates, change, earthengine, events, indices
from forest_sentinel.hls import discover_observations
from forest_sentinel.models import Aoi, MethodologyVersion, Observation
from forest_sentinel.storage import Storage

# Candidates are extracted from the ΔNBR product (NBR drop = disturbance).
CANDIDATE_CHANGE_TYPE = "delta_nbr"


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

    index_count = 0
    for observation in observations:
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

    change_count = 0
    candidate_count = 0
    for observation in observations:
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
    )
