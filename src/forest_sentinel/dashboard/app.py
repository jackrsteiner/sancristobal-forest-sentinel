"""FastAPI dashboard backend.

Read-only JSON/GeoJSON endpoints over the PostGIS catalog that let a user answer the six
core README "Product Deliverable" questions for a disturbance event: where (geometry), when
first detected, size (footprint area), expansion rate (footprint growth over the timeline),
status, and supporting evidence (the source change rasters); the README's remaining
deliverable questions arrive with later slices (see ``docs/architecture.md`` §2). The map UI
(a static Leaflet page) consumes these endpoints.

The database session is provided by the ``get_session`` dependency so tests can override it
with a transactional test session; no Earth Engine or storage access happens here.
"""

import json
import os
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from geoalchemy2 import Geography
from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import Engine, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forest_sentinel.aoi import (
    AOIS_DIR_ENV_VAR,
    DEFAULT_AOIS_DIR,
    AoiConfigError,
    load_aoi_config_document,
    persist_aoi,
)
from forest_sentinel.db import get_engine
from forest_sentinel.events import footprint_area_m2
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    DisturbanceCandidate,
    DisturbanceEvent,
    EventObservation,
    MethodologyVersion,
    PipelineRun,
    PipelineRunEvent,
)
from forest_sentinel.storage import StorageError, sanitize_path_component

# How many runs / progress events the runs endpoints return.
RUNS_LIMIT = 20
RUN_EVENTS_LIMIT = 50

# Set to "0" to disable uploads (vm_setup.sh does this when the dashboard port
# is opened to the world: a public dashboard must stay read-only).
AOI_UPLOADS_ENV_VAR = "FOREST_SENTINEL_AOI_UPLOADS"


@lru_cache(maxsize=1)
def _engine() -> Engine:
    return get_engine()


def get_session() -> Iterator[Session]:
    """Yield a database session for one request (overridable in tests)."""
    with Session(_engine()) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]

_INDEX_HTML = Path(__file__).parent / "static" / "index.html"


def create_app() -> FastAPI:
    app = FastAPI(title="Open Forest Sentinel", description="Forest disturbance dashboard.")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        """The Leaflet map page that consumes the JSON/GeoJSON API."""
        return _INDEX_HTML.read_text()

    @app.get("/api/aois")
    def list_aois(session: SessionDep) -> list[dict[str, Any]]:
        """AOIs with summary metrics (event counts)."""
        rows = session.execute(
            select(Aoi.id, Aoi.name, func.count(DisturbanceEvent.id))
            .outerjoin(DisturbanceEvent, DisturbanceEvent.aoi_id == Aoi.id)
            .group_by(Aoi.id, Aoi.name)
            .order_by(Aoi.name)
        ).all()
        return [
            {"id": aoi_id, "name": name, "event_count": event_count}
            for aoi_id, name, event_count in rows
        ]

    @app.post("/api/aois", status_code=201)
    def upload_aoi(session: SessionDep, document: Annotated[Any, Body()]) -> dict[str, Any]:
        """Register a new AOI from an uploaded GeoJSON document.

        The document is the same single-feature GeoJSON accepted as a file; it is
        validated, written into the AOIs directory (so the scheduled run picks it
        up exactly like a committed file), and persisted as an ``aoi`` row (visible
        in the dropdown immediately; processed on the next pipeline run). Uploads
        are instance-local — commit the written file to the instance repo to
        survive a teardown/redeploy. A name that already exists is rejected (409):
        stored geometry is pinned to the name, so a new footprint needs a new name.

        The body must be JSON (not multipart): a cross-origin browser POST with
        ``Content-Type: application/json`` requires a CORS preflight, and no CORS
        middleware is configured — which structurally blocks CSRF while the
        dashboard is reachable through the SSH tunnel.
        """
        if os.environ.get(AOI_UPLOADS_ENV_VAR, "1") == "0":
            raise HTTPException(
                status_code=403, detail="AOI uploads are disabled on this dashboard"
            )
        try:
            config = load_aoi_config_document(document)
            filename = f"{sanitize_path_component(config.name)}.geojson"
        except (AoiConfigError, StorageError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        aois_dir = Path(os.environ.get(AOIS_DIR_ENV_VAR, DEFAULT_AOIS_DIR))
        target = aois_dir / filename
        if target.exists():
            raise HTTPException(
                status_code=409, detail=f"an AOI file named {filename!r} already exists"
            )
        aois_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, indent=2) + "\n")
        try:
            aoi = persist_aoi(session, config)
            session.commit()
        except IntegrityError:
            session.rollback()
            target.unlink(missing_ok=True)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"an AOI named {config.name!r} already exists; stored geometry is "
                    "pinned to the name — upload under a new name to monitor a new footprint"
                ),
            ) from None
        return {"id": aoi.id, "name": aoi.name, "file": str(target)}

    @app.get("/api/aois/{aoi_id}/events")
    def aoi_events(aoi_id: int, session: SessionDep) -> dict[str, Any]:
        """Events for an AOI as a GeoJSON FeatureCollection.

        One aggregate query: per-event observation count, latest detection area, and
        footprint area come back with the events instead of a timeline fetch per event.
        """
        if session.get(Aoi, aoi_id) is None:
            raise HTTPException(status_code=404, detail=f"AOI {aoi_id} not found")

        measurements = (
            select(
                EventObservation.event_id,
                EventObservation.area_m2,
                func.count().over(partition_by=EventObservation.event_id).label("count"),
                func.row_number()
                .over(
                    partition_by=EventObservation.event_id,
                    order_by=(EventObservation.observed_at.desc(), EventObservation.id.desc()),
                )
                .label("recency"),
            )
        ).subquery()
        latest = (
            select(measurements.c.event_id, measurements.c.area_m2, measurements.c.count)
            .where(measurements.c.recency == 1)
            .subquery()
        )
        rows = session.execute(
            select(
                DisturbanceEvent,
                func.ST_Area(cast(DisturbanceEvent.geometry, Geography)),
                func.coalesce(latest.c.count, 0),
                latest.c.area_m2,
            )
            .outerjoin(latest, latest.c.event_id == DisturbanceEvent.id)
            .where(DisturbanceEvent.aoi_id == aoi_id)
            .order_by(DisturbanceEvent.first_detected_at)
        ).all()
        return {
            "type": "FeatureCollection",
            "features": [
                _event_feature(event, footprint_area, count, latest_area)
                for event, footprint_area, count, latest_area in rows
            ],
        }

    @app.get("/api/aois/{aoi_id}/runs")
    def aoi_runs(aoi_id: int, session: SessionDep) -> list[dict[str, Any]]:
        """Recent pipeline runs for an AOI, newest first.

        Runs commit their progress events live, so polling this endpoint shows
        an executing run advancing batch by batch; ``last_event_at`` lets the
        UI flag a ``running`` row whose run actually died (stale heartbeat).
        """
        if session.get(Aoi, aoi_id) is None:
            raise HTTPException(status_code=404, detail=f"AOI {aoi_id} not found")
        rows = session.execute(
            select(PipelineRun, MethodologyVersion, func.max(PipelineRunEvent.occurred_at))
            .outerjoin(PipelineRunEvent, PipelineRunEvent.run_id == PipelineRun.id)
            .outerjoin(
                MethodologyVersion,
                MethodologyVersion.id == PipelineRun.methodology_version_id,
            )
            .where(PipelineRun.aoi_id == aoi_id)
            .group_by(PipelineRun.id, MethodologyVersion.id)
            .order_by(PipelineRun.started_at.desc())
            .limit(RUNS_LIMIT)
        ).all()
        return [
            _run_summary(run, last_event_at, methodology)
            for run, methodology, last_event_at in rows
        ]

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: int, session: SessionDep) -> dict[str, Any]:
        """One run with the tail of its progress events (oldest first).

        Includes the full methodology ``parameters`` — the run's non-data inputs
        (threshold, min area, baseline window, EE script version, ...) — so the
        provenance behind the results is inspectable from the dashboard.
        """
        run = session.get(PipelineRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        methodology = (
            session.get(MethodologyVersion, run.methodology_version_id)
            if run.methodology_version_id is not None
            else None
        )
        last_event_at = session.execute(
            select(func.max(PipelineRunEvent.occurred_at)).where(PipelineRunEvent.run_id == run_id)
        ).scalar_one()
        detail = {**_run_summary(run, last_event_at, methodology)}
        if methodology is not None:
            detail["methodology"]["parameters"] = methodology.parameters
        detail["events"] = _run_events(session, run_id)
        return detail

    @app.get("/api/events/{event_id}")
    def event_detail(event_id: int, session: SessionDep) -> dict[str, Any]:
        """One event with its measurement timeline and supporting evidence."""
        event = session.get(DisturbanceEvent, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail=f"event {event_id} not found")
        timeline = _timeline(session, event_id)
        return {
            "id": event.id,
            "aoi_id": event.aoi_id,
            "status": event.status,
            "first_detected_at": event.first_detected_at,
            "last_detected_at": event.last_detected_at,
            "geometry": mapping(to_shape(event.geometry)),
            "footprint_area_m2": footprint_area_m2(session, event.geometry),
            "timeline": timeline,
            "evidence": _evidence(session, event_id),
        }

    return app


def _event_feature(
    event: DisturbanceEvent,
    footprint_area: float,
    observation_count: int,
    latest_area: float | None,
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": mapping(to_shape(event.geometry)),
        "properties": {
            "id": event.id,
            "status": event.status,
            "first_detected_at": event.first_detected_at,
            "last_detected_at": event.last_detected_at,
            "observation_count": observation_count,
            # Cumulative unioned footprint vs the latest single-scene detection.
            "footprint_area_m2": footprint_area,
            "latest_area_m2": latest_area,
        },
    }


def _timeline(session: Session, event_id: int) -> list[dict[str, Any]]:
    observations = (
        session.execute(
            select(EventObservation)
            .where(EventObservation.event_id == event_id)
            .order_by(EventObservation.observed_at)
        )
        .scalars()
        .all()
    )
    return [
        {
            "observed_at": obs.observed_at,
            "area_m2": obs.area_m2,
            "growth_m2": obs.growth_m2,
            "candidate_id": obs.disturbance_candidate_id,
        }
        for obs in observations
    ]


def _run_summary(
    run: PipelineRun, last_event_at: Any, methodology: MethodologyVersion | None
) -> dict[str, Any]:
    return {
        "id": run.id,
        "aoi_id": run.aoi_id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": run.status,
        "since": run.since,
        "until": run.until,
        "summary": run.summary,
        "last_event_at": last_event_at,
        "methodology": (
            {"id": methodology.id, "name": methodology.name, "version": methodology.version}
            if methodology is not None
            else None
        ),
    }


def _run_events(session: Session, run_id: int) -> list[dict[str, Any]]:
    # The most recent events, returned oldest-first for chronological display.
    tail = (
        session.execute(
            select(PipelineRunEvent)
            .where(PipelineRunEvent.run_id == run_id)
            .order_by(PipelineRunEvent.id.desc())
            .limit(RUN_EVENTS_LIMIT)
        )
        .scalars()
        .all()
    )
    return [
        {
            "occurred_at": event.occurred_at,
            "stage": event.stage,
            "batch_index": event.batch_index,
            "batch_total": event.batch_total,
            "exports": event.exports,
            "outcome": event.outcome,
            "message": event.message,
        }
        for event in reversed(tail)
    ]


def _evidence(session: Session, event_id: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(ChangeRaster.change_type, ChangeRaster.cog_path, DisturbanceCandidate.id)
        .join(
            DisturbanceCandidate,
            DisturbanceCandidate.change_raster_id == ChangeRaster.id,
        )
        .join(
            EventObservation,
            EventObservation.disturbance_candidate_id == DisturbanceCandidate.id,
        )
        .where(EventObservation.event_id == event_id)
        .order_by(DisturbanceCandidate.id)
    ).all()
    return [
        {"change_type": change_type, "cog_path": cog_path, "candidate_id": candidate_id}
        for change_type, cog_path, candidate_id in rows
    ]


app = create_app()
