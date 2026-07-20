"""Track disturbance candidates into events over time (E7, Slice 2).

Algorithm — **spatial overlap**: candidates are processed in detection order; a candidate
whose geometry intersects an existing event's footprint extends that event (a new
``event_observation`` plus the unioned geometry and refreshed dates), otherwise it starts a
new event. Tracking is **incremental and idempotent**: only candidates not yet linked to an
``event_observation`` are processed, so re-running adds nothing.

``growth_m2`` measures **footprint expansion**: the geodesic area (PostGIS
``ST_Area`` over ``geography``) the candidate added to the event's unioned footprint —
not the difference between successive detection areas, which can shrink (e.g. under
partial cloud) while the disturbance itself keeps growing.

The candidate→event linkage is the resolved design from the Slice 2 planning pass. A candidate
that intersects several events is attached to the earliest; merging multiple events into one is
a deliberate non-goal of this slice.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from geoalchemy2.elements import WKBElement
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
    QualityMask,
)

logger = logging.getLogger(__name__)

EVENT_STATUS_NEW = "new"
EVENT_STATUS_ONGOING = "ongoing"
EVENT_STATUS_RESOLVED = "resolved"

# Automatic-resolve defaults (docs/architecture.md §5.9/§7): an ongoing event
# resolves after this many days without a new detection — but only when a later
# observation was clear enough that "no detection" is evidence rather than a
# cloud gap.
DEFAULT_RESOLVED_AFTER_DAYS = 90
CLEAR_FRACTION_FLOOR = 0.5


def footprint_area_m2(session: Session, geometry: WKBElement) -> float:
    """Geodesic area of a WGS 84 footprint in m² (PostGIS ``ST_Area`` on geography).

    Goes through WKT so both DB-loaded and freshly built ``WKBElement``s bind cleanly.
    """
    wkt = to_shape(geometry).wkt
    return float(session.execute(select(func.ST_Area(func.ST_GeogFromText(wkt)))).scalar_one())


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


def apply_resolved_lifecycle(
    session: Session,
    *,
    aoi: Aoi,
    resolved_after_days: int = DEFAULT_RESOLVED_AFTER_DAYS,
    clear_fraction_floor: float = CLEAR_FRACTION_FLOOR,
    now: datetime | None = None,
) -> int:
    """Flip quiet ``ongoing`` events to ``resolved``; returns how many flipped.

    An event resolves only when BOTH hold (docs/architecture.md §5.9/§7):

    - its ``last_detected_at`` is older than ``resolved_after_days``, and
    - a later observation of the AOI exists whose ``quality_mask``
      valid-pixel fraction is at least ``clear_fraction_floor`` — a cloudy gap
      alone is absence of evidence, not evidence of absence.

    Only ``ongoing`` events participate: a single-look ``new`` event has no
    established recurrence to resolve (reviewers can record an opinion
    instead), and re-detection reopens a resolved event because candidate
    extension unconditionally sets ``ongoing``. Never touched by manual
    review — the status is machine-owned.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=resolved_after_days)
    clear_later_observation = (
        select(Observation.id)
        .join(QualityMask, QualityMask.observation_id == Observation.id)
        .where(Observation.aoi_id == aoi.id)
        .where(Observation.acquired_at > DisturbanceEvent.last_detected_at)
        .where(QualityMask.valid_pixel_fraction >= clear_fraction_floor)
        .exists()
    )
    stale = (
        session.execute(
            select(DisturbanceEvent)
            .where(DisturbanceEvent.aoi_id == aoi.id)
            .where(DisturbanceEvent.status == EVENT_STATUS_ONGOING)
            .where(DisturbanceEvent.last_detected_at < cutoff)
            .where(clear_later_observation)
        )
        .scalars()
        .all()
    )
    for event in stale:
        event.status = EVENT_STATUS_RESOLVED
    session.flush()
    if stale:
        logger.info(
            "resolved %d quiet event(s) for AOI %s (no detection in %d days, "
            "clear later look available)",
            len(stale),
            aoi.name,
            resolved_after_days,
        )
    return len(stale)


def _find_overlapping_event(
    session: Session, aoi: Aoi, candidate: DisturbanceCandidate
) -> DisturbanceEvent | None:
    # A candidate may intersect several events (e.g. a disturbance growing to bridge
    # two previously separate ones); it attaches to the earliest. Only events of the
    # same methodology version are considered: an event records a single
    # methodology_version_id as provenance, so mixing versions in one footprint would
    # falsify it — a new methodology starts new events instead.
    return (
        session.execute(
            select(DisturbanceEvent)
            .where(DisturbanceEvent.aoi_id == aoi.id)
            .where(DisturbanceEvent.methodology_version_id == candidate.methodology_version_id)
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
    merged = to_shape(event.geometry).union(candidate_shape)
    merged_geometry = from_shape(_as_multipolygon(merged), srid=AOI_SRID)
    # Footprint expansion: area the candidate added to the unioned footprint. The
    # union never shrinks; max() only absorbs floating-point noise.
    growth = max(
        0.0,
        footprint_area_m2(session, merged_geometry) - footprint_area_m2(session, event.geometry),
    )

    event.geometry = merged_geometry
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
    geoms = list(getattr(geometry, "geoms", []))
    parts = [part for part in geoms if part.geom_type == "Polygon"]
    dropped = [part.geom_type for part in geoms if part.geom_type != "Polygon"]
    if dropped:
        # A degenerate union (touching edges) can yield lines/points; dropping them
        # changes the stored footprint, so say so instead of doing it silently.
        logger.warning("event footprint union dropped non-polygon parts: %s", dropped)
    return MultiPolygon(parts)
