"""Per-AOI administration: inventory and full teardown of one AOI's data (#83).

Deleting an AOI removes its rows across every table plus its COG directory.
The deletes run in one transaction in dependency order — notably events before
change rasters: ``event_observation`` references ``disturbance_candidate``
without a cascade, so candidates can only go (via their change raster's
cascade) once the event side has been deleted.
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session

from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    DisturbanceCandidate,
    DisturbanceEvent,
    EventObservation,
    IndexRaster,
    Observation,
    PipelineRun,
    PipelineRunEvent,
    QualityMask,
)
from forest_sentinel.storage import COG_ROOT_ENV_VAR, DEFAULT_COG_ROOT, sanitize_path_component


@dataclass(frozen=True)
class AoiInventory:
    """What deleting an AOI would remove (rows per table + its COG directory)."""

    observations: int
    quality_masks: int
    index_rasters: int
    change_rasters: int
    candidates: int
    events: int
    event_observations: int
    runs: int
    run_events: int
    cog_directory: Path


def cog_directory(aoi: Aoi) -> Path:
    """The AOI's COG tree, matching the path ``CogKey`` builds for exports."""
    root = Path(os.environ.get(COG_ROOT_ENV_VAR, DEFAULT_COG_ROOT))
    return root / sanitize_path_component(f"{aoi.id}-{aoi.name}")


def inventory_aoi(session: Session, aoi: Aoi) -> AoiInventory:
    """Count every row that :func:`delete_aoi` would remove."""
    obs_ids = select(Observation.id).where(Observation.aoi_id == aoi.id).scalar_subquery()
    raster_ids = (
        select(ChangeRaster.id).where(ChangeRaster.observation_id.in_(obs_ids)).scalar_subquery()
    )
    event_ids = select(DisturbanceEvent.id).where(DisturbanceEvent.aoi_id == aoi.id)
    run_ids = select(PipelineRun.id).where(PipelineRun.aoi_id == aoi.id)

    def count(query: Select[tuple[int]]) -> int:
        return session.execute(select(func.count()).select_from(query.subquery())).scalar_one()

    return AoiInventory(
        observations=count(select(Observation.id).where(Observation.aoi_id == aoi.id)),
        quality_masks=count(
            select(QualityMask.observation_id).where(QualityMask.observation_id.in_(obs_ids))
        ),
        index_rasters=count(select(IndexRaster.id).where(IndexRaster.observation_id.in_(obs_ids))),
        change_rasters=count(
            select(ChangeRaster.id).where(ChangeRaster.observation_id.in_(obs_ids))
        ),
        candidates=count(
            select(DisturbanceCandidate.id).where(
                DisturbanceCandidate.change_raster_id.in_(raster_ids)
            )
        ),
        events=count(event_ids),
        event_observations=count(
            select(EventObservation.id).where(
                EventObservation.event_id.in_(event_ids.scalar_subquery())
            )
        ),
        runs=count(run_ids),
        run_events=count(
            select(PipelineRunEvent.id).where(
                PipelineRunEvent.run_id.in_(run_ids.scalar_subquery())
            )
        ),
        cog_directory=cog_directory(aoi),
    )


def delete_aoi(session: Session, aoi: Aoi) -> None:
    """Delete the AOI and every dependent row, in one transaction.

    The caller commits. Order matters: events first (their ``event_observation``
    rows cascade, freeing the candidates), then change rasters (cascading
    sources + candidates), then index rasters / quality masks / observations,
    then runs (cascading run events), then the AOI row itself.
    """
    obs_ids = select(Observation.id).where(Observation.aoi_id == aoi.id).scalar_subquery()
    raster_ids = (
        select(ChangeRaster.id).where(ChangeRaster.observation_id.in_(obs_ids)).scalar_subquery()
    )

    session.execute(
        delete(DisturbanceEvent).where(DisturbanceEvent.aoi_id == aoi.id),
        execution_options={"synchronize_session": False},
    )
    session.execute(
        delete(DisturbanceCandidate).where(DisturbanceCandidate.change_raster_id.in_(raster_ids)),
        execution_options={"synchronize_session": False},
    )
    session.execute(
        delete(ChangeRaster).where(ChangeRaster.observation_id.in_(obs_ids)),
        execution_options={"synchronize_session": False},
    )
    session.execute(
        delete(IndexRaster).where(IndexRaster.observation_id.in_(obs_ids)),
        execution_options={"synchronize_session": False},
    )
    session.execute(
        delete(QualityMask).where(QualityMask.observation_id.in_(obs_ids)),
        execution_options={"synchronize_session": False},
    )
    session.execute(
        delete(Observation).where(Observation.aoi_id == aoi.id),
        execution_options={"synchronize_session": False},
    )
    session.execute(
        delete(PipelineRun).where(PipelineRun.aoi_id == aoi.id),
        execution_options={"synchronize_session": False},
    )
    session.execute(
        delete(Aoi).where(Aoi.id == aoi.id),
        execution_options={"synchronize_session": False},
    )
    session.flush()


def remove_cog_directory(directory: Path) -> bool:
    """Remove an AOI's COG tree from disk; True if something was removed.

    Takes the path (from :func:`cog_directory` / ``AoiInventory.cog_directory``)
    rather than the ORM row: by the time cleanup runs the row is deleted and
    committed, so its attributes are no longer loadable.
    """
    if directory.is_dir():
        shutil.rmtree(directory)
        return True
    return False
