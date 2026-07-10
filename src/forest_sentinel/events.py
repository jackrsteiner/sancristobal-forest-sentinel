"""Track disturbance candidates into events over time (E7, Slice 2).

Algorithm — **spatial overlap**: candidates are processed in detection order; a candidate
whose geometry intersects an existing event's footprint extends that event (a new
``event_observation`` plus the unioned geometry and refreshed dates), otherwise it starts a
new event. Tracking is **incremental and idempotent**: only candidates not yet linked to an
``event_observation`` are processed, so re-running adds nothing.

The candidate→event linkage is the resolved design from the Slice 2 planning pass. A candidate
that intersects several events is attached to the earliest; merging multiple events into one is
a deliberate non-goal of this slice.
"""

from dataclasses import dataclass

from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon
from shapely.geometry.base import BaseGeometry
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forest_sentinel.models import (
    AOI_SRID,
    Aoi,
    ChangeRaster,
    DisturbanceCandidate,
    DisturbanceEvent,
    EventObservation,
    Observation,
)

EVENT_STATUS_NEW = "new"
EVENT_STATUS_ONGOING = "ongoing"


@dataclass(frozen=True)
class TrackingResult:
    """Counts from one tracking pass."""

    events_created: int
    events_extended: int  # observations that attached to a pre-existing event
    observations_added: int


def track_events_for_aoi(session: Session, *, aoi: Aoi) -> TrackingResult:
    """Track this AOI's not-yet-tracked candidates into disturbance events."""
    linked = select(EventObservation.disturbance_candidate_id)
    candidates = (
        session.execute(
            select(DisturbanceCandidate)
            .join(ChangeRaster, DisturbanceCandidate.change_raster_id == ChangeRaster.id)
            .join(Observation, ChangeRaster.observation_id == Observation.id)
            .where(Observation.aoi_id == aoi.id)
            .where(DisturbanceCandidate.id.not_in(linked))
            .order_by(DisturbanceCandidate.detected_at, DisturbanceCandidate.id)
        )
        .scalars()
        .all()
    )

    created = 0
    extended = 0
    for candidate in candidates:
        candidate_shape = to_shape(candidate.geometry)
        event = _find_overlapping_event(session, aoi, candidate)
        if event is None:
            event = _create_event(session, aoi, candidate, candidate_shape)
            growth = None
            created += 1
        else:
            growth = _extend_event(session, event, candidate, candidate_shape)
            extended += 1

        session.add(
            EventObservation(
                event_id=event.id,
                disturbance_candidate_id=candidate.id,
                observed_at=candidate.detected_at,
                area_m2=candidate.area_m2,
                growth_m2=growth,
            )
        )
        session.flush()

    return TrackingResult(
        events_created=created,
        events_extended=extended,
        observations_added=created + extended,
    )


def _find_overlapping_event(
    session: Session, aoi: Aoi, candidate: DisturbanceCandidate
) -> DisturbanceEvent | None:
    # A candidate may intersect several events (e.g. a disturbance growing to bridge
    # two previously separate ones); it attaches to the earliest.
    return (
        session.execute(
            select(DisturbanceEvent)
            .where(DisturbanceEvent.aoi_id == aoi.id)
            .where(func.ST_Intersects(DisturbanceEvent.geometry, candidate.geometry))
            .order_by(DisturbanceEvent.first_detected_at, DisturbanceEvent.id)
            .limit(1)
        )
        .scalars()
        .first()
    )


def _create_event(
    session: Session, aoi: Aoi, candidate: DisturbanceCandidate, candidate_shape: BaseGeometry
) -> DisturbanceEvent:
    event = DisturbanceEvent(
        aoi_id=aoi.id,
        methodology_version_id=candidate.methodology_version_id,
        geometry=from_shape(_as_multipolygon(candidate_shape), srid=AOI_SRID),
        status=EVENT_STATUS_NEW,
        first_detected_at=candidate.detected_at,
        last_detected_at=candidate.detected_at,
    )
    session.add(event)
    session.flush()
    return event


def _extend_event(
    session: Session,
    event: DisturbanceEvent,
    candidate: DisturbanceCandidate,
    candidate_shape: BaseGeometry,
) -> float | None:
    previous = (
        session.execute(
            select(EventObservation)
            .where(EventObservation.event_id == event.id)
            .order_by(EventObservation.observed_at.desc(), EventObservation.id.desc())
        )
        .scalars()
        .first()
    )
    growth = candidate.area_m2 - previous.area_m2 if previous is not None else None

    merged = to_shape(event.geometry).union(candidate_shape)
    event.geometry = from_shape(_as_multipolygon(merged), srid=AOI_SRID)
    event.first_detected_at = min(event.first_detected_at, candidate.detected_at)
    event.last_detected_at = max(event.last_detected_at, candidate.detected_at)
    event.status = EVENT_STATUS_ONGOING
    return growth


def _as_multipolygon(geometry: BaseGeometry) -> MultiPolygon:
    """Coerce a polygonal geometry to a ``MultiPolygon`` for storage."""
    if geometry.geom_type == "MultiPolygon":
        return geometry
    if geometry.geom_type == "Polygon":
        return MultiPolygon([geometry])
    parts = [part for part in getattr(geometry, "geoms", []) if part.geom_type == "Polygon"]
    return MultiPolygon(parts)
