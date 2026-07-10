from datetime import UTC, datetime

from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.events import (
    EVENT_STATUS_NEW,
    EVENT_STATUS_ONGOING,
    track_events_for_aoi,
)
from forest_sentinel.methodology import get_or_create_methodology_version
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    DisturbanceCandidate,
    DisturbanceEvent,
    EventObservation,
    MethodologyVersion,
    Observation,
)


def _aoi_and_methodology(session: Session) -> tuple[Aoi, MethodologyVersion]:
    aoi = Aoi(
        name="Test AOI",
        geometry=from_shape(
            MultiPolygon([Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])]), srid=4326
        ),
    )
    session.add(aoi)
    session.flush()
    methodology = get_or_create_methodology_version(
        session, name="optical-change", version="1.0.0", parameters={}
    )
    return aoi, methodology


def _candidate(
    session: Session,
    aoi: Aoi,
    methodology: MethodologyVersion,
    *,
    day: int,
    ring: list[tuple[float, float]],
    area_m2: float,
) -> DisturbanceCandidate:
    detected = datetime(2026, 1, day, tzinfo=UTC)
    obs = Observation(
        aoi_id=aoi.id,
        sensor="HLSL30",
        acquired_at=detected,
        source_scene_id=f"scene-{day}",
    )
    session.add(obs)
    session.flush()
    change = ChangeRaster(
        observation_id=obs.id,
        methodology_version_id=methodology.id,
        change_type="delta_nbr",
        cog_path=f"/cogs/{day}.tif",
        baseline_window=5,
    )
    session.add(change)
    session.flush()
    candidate = DisturbanceCandidate(
        change_raster_id=change.id,
        methodology_version_id=methodology.id,
        geometry=from_shape(Polygon(ring), srid=4326),
        detected_at=detected,
        area_m2=area_m2,
    )
    session.add(candidate)
    session.flush()
    return candidate


# Two overlapping squares (share the 0.1..0.2 strip) and one disjoint square.
_PATCH_A = [(0.1, 0.1), (0.2, 0.1), (0.2, 0.2), (0.1, 0.2), (0.1, 0.1)]
_PATCH_A_GROWN = [(0.15, 0.1), (0.3, 0.1), (0.3, 0.2), (0.15, 0.2), (0.15, 0.1)]
_PATCH_B = [(0.6, 0.6), (0.7, 0.6), (0.7, 0.7), (0.6, 0.7), (0.6, 0.6)]


def test_overlapping_candidates_form_one_event(db_session: Session) -> None:
    aoi, methodology = _aoi_and_methodology(db_session)
    _candidate(db_session, aoi, methodology, day=1, ring=_PATCH_A, area_m2=10_000.0)
    _candidate(db_session, aoi, methodology, day=8, ring=_PATCH_A_GROWN, area_m2=15_000.0)

    result = track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()

    assert (result.events_created, result.events_extended, result.observations_added) == (1, 1, 2)
    event = db_session.execute(select(DisturbanceEvent)).scalar_one()
    assert event.status == EVENT_STATUS_ONGOING
    assert event.first_detected_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert event.last_detected_at == datetime(2026, 1, 8, tzinfo=UTC)

    observations = (
        db_session.execute(select(EventObservation).order_by(EventObservation.observed_at))
        .scalars()
        .all()
    )
    assert [o.area_m2 for o in observations] == [10_000.0, 15_000.0]
    assert observations[0].growth_m2 is None
    assert observations[1].growth_m2 == 5_000.0  # 15000 - 10000


def test_disjoint_candidates_form_separate_events(db_session: Session) -> None:
    aoi, methodology = _aoi_and_methodology(db_session)
    _candidate(db_session, aoi, methodology, day=1, ring=_PATCH_A, area_m2=10_000.0)
    _candidate(db_session, aoi, methodology, day=2, ring=_PATCH_B, area_m2=8_000.0)

    result = track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()

    assert result.events_created == 2
    events = db_session.execute(select(DisturbanceEvent)).scalars().all()
    assert len(events) == 2
    assert all(e.status == EVENT_STATUS_NEW for e in events)


def test_single_candidate_event_is_new(db_session: Session) -> None:
    aoi, methodology = _aoi_and_methodology(db_session)
    _candidate(db_session, aoi, methodology, day=1, ring=_PATCH_A, area_m2=10_000.0)

    track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()

    event = db_session.execute(select(DisturbanceEvent)).scalar_one()
    assert event.status == EVENT_STATUS_NEW
    assert to_shape(event.geometry).geom_type == "MultiPolygon"


def test_tracking_is_idempotent(db_session: Session) -> None:
    aoi, methodology = _aoi_and_methodology(db_session)
    _candidate(db_session, aoi, methodology, day=1, ring=_PATCH_A, area_m2=10_000.0)
    _candidate(db_session, aoi, methodology, day=8, ring=_PATCH_A_GROWN, area_m2=15_000.0)

    first = track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()
    second = track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()

    assert first.observations_added == 2
    assert (second.events_created, second.events_extended, second.observations_added) == (0, 0, 0)
    assert len(db_session.execute(select(DisturbanceEvent)).scalars().all()) == 1
    assert len(db_session.execute(select(EventObservation)).scalars().all()) == 2


def test_candidate_bridging_two_events_attaches_to_the_earliest(db_session: Session) -> None:
    """A candidate intersecting several events must attach to the earliest, not crash
    with MultipleResultsFound (audit BUG-3)."""
    aoi, methodology = _aoi_and_methodology(db_session)
    _candidate(db_session, aoi, methodology, day=1, ring=_PATCH_A, area_m2=10_000.0)
    _candidate(db_session, aoi, methodology, day=2, ring=_PATCH_B, area_m2=8_000.0)
    track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()
    assert len(db_session.execute(select(DisturbanceEvent)).scalars().all()) == 2

    # A large square covering both existing patches bridges the two events.
    bridge = [(0.05, 0.05), (0.75, 0.05), (0.75, 0.75), (0.05, 0.75), (0.05, 0.05)]
    _candidate(db_session, aoi, methodology, day=9, ring=bridge, area_m2=40_000.0)
    result = track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()

    assert (result.events_created, result.events_extended) == (0, 1)
    events = (
        db_session.execute(select(DisturbanceEvent).order_by(DisturbanceEvent.first_detected_at))
        .scalars()
        .all()
    )
    assert len(events) == 2
    earliest, later = events
    assert earliest.status == EVENT_STATUS_ONGOING  # the bridge attached here
    assert earliest.last_detected_at == datetime(2026, 1, 9, tzinfo=UTC)
    assert later.status == EVENT_STATUS_NEW  # untouched


def test_event_geometry_unions_extending_candidates(db_session: Session) -> None:
    aoi, methodology = _aoi_and_methodology(db_session)
    _candidate(db_session, aoi, methodology, day=1, ring=_PATCH_A, area_m2=10_000.0)
    _candidate(db_session, aoi, methodology, day=8, ring=_PATCH_A_GROWN, area_m2=15_000.0)

    track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()

    event = db_session.execute(select(DisturbanceEvent)).scalar_one()
    footprint = to_shape(event.geometry)
    # The unioned footprint spans both patches (x from 0.1 to 0.3).
    minx, _, maxx, _ = footprint.bounds
    assert minx == 0.1
    assert maxx == 0.3
