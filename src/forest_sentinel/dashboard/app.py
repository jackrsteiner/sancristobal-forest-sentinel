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

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from geoalchemy2 import Geography
from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import Engine, cast, func, select
from sqlalchemy.orm import Session

from forest_sentinel.db import get_engine
from forest_sentinel.events import footprint_area_m2
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    DisturbanceCandidate,
    DisturbanceEvent,
    EventObservation,
)


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
