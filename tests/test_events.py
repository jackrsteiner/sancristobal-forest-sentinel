from datetime import UTC, datetime

import pytest
from geoalchemy2.shape import to_shape
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.fakes import make_aoi, make_candidate, make_methodology

from forest_sentinel.events import (
    EVENT_STATUS_NEW,
    EVENT_STATUS_ONGOING,
    footprint_area_m2,
    track_events_for_aoi,
)
from forest_sentinel.models import (
    Aoi,
    DisturbanceEvent,
    EventObservation,
    MethodologyVersion,
)


def _aoi_and_methodology(session: Session) -> tuple[Aoi, MethodologyVersion]:
    return make_aoi(session), make_methodology(session)


# Two overlapping squares (share the 0.1..0.2 strip) and one disjoint square.
_PATCH_A = [(0.1, 0.1), (0.2, 0.1), (0.2, 0.2), (0.1, 0.2), (0.1, 0.1)]
_PATCH_A_GROWN = [(0.15, 0.1), (0.3, 0.1), (0.3, 0.2), (0.15, 0.2), (0.15, 0.1)]
_PATCH_B = [(0.6, 0.6), (0.7, 0.6), (0.7, 0.7), (0.6, 0.7), (0.6, 0.6)]


def _geodesic_area(session: Session, ring: list[tuple[float, float]]) -> float:
    """Reference geodesic area of a ring, via the same PostGIS geography math."""
    wkt = "POLYGON((" + ", ".join(f"{x} {y}" for x, y in ring) + "))"
    return float(session.execute(select(func.ST_Area(func.ST_GeogFromText(wkt)))).scalar_one())


def test_overlapping_candidates_form_one_event(db_session: Session) -> None:
    aoi, methodology = _aoi_and_methodology(db_session)
    make_candidate(db_session, aoi, methodology, day=1, ring=_PATCH_A, area_m2=10_000.0)
    make_candidate(db_session, aoi, methodology, day=8, ring=_PATCH_A_GROWN, area_m2=15_000.0)

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
    # Footprint expansion: the union's geodesic area minus the first patch's.
    expected_growth = footprint_area_m2(db_session, event.geometry) - _geodesic_area(
        db_session, _PATCH_A
    )
    growth = observations[1].growth_m2
    assert growth is not None
    assert growth == pytest.approx(expected_growth, rel=1e-6)
    assert growth > 0


def test_disjoint_candidates_form_separate_events(db_session: Session) -> None:
    aoi, methodology = _aoi_and_methodology(db_session)
    make_candidate(db_session, aoi, methodology, day=1, ring=_PATCH_A, area_m2=10_000.0)
    make_candidate(db_session, aoi, methodology, day=2, ring=_PATCH_B, area_m2=8_000.0)

    result = track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()

    assert result.events_created == 2
    events = db_session.execute(select(DisturbanceEvent)).scalars().all()
    assert len(events) == 2
    assert all(e.status == EVENT_STATUS_NEW for e in events)


def test_single_candidate_event_is_new(db_session: Session) -> None:
    aoi, methodology = _aoi_and_methodology(db_session)
    make_candidate(db_session, aoi, methodology, day=1, ring=_PATCH_A, area_m2=10_000.0)

    track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()

    event = db_session.execute(select(DisturbanceEvent)).scalar_one()
    assert event.status == EVENT_STATUS_NEW
    assert to_shape(event.geometry).geom_type == "MultiPolygon"


def test_tracking_is_idempotent(db_session: Session) -> None:
    aoi, methodology = _aoi_and_methodology(db_session)
    make_candidate(db_session, aoi, methodology, day=1, ring=_PATCH_A, area_m2=10_000.0)
    make_candidate(db_session, aoi, methodology, day=8, ring=_PATCH_A_GROWN, area_m2=15_000.0)

    first = track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()
    second = track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()

    assert first.observations_added == 2
    assert (second.events_created, second.events_extended, second.observations_added) == (0, 0, 0)
    assert len(db_session.execute(select(DisturbanceEvent)).scalars().all()) == 1
    assert len(db_session.execute(select(EventObservation)).scalars().all()) == 2


def test_contained_candidate_yields_zero_growth(db_session: Session) -> None:
    """A later, smaller detection inside the existing footprint adds no area: growth
    must be ~0, not negative (audit BUG-9)."""
    aoi, methodology = _aoi_and_methodology(db_session)
    inner = [(0.12, 0.12), (0.15, 0.12), (0.15, 0.15), (0.12, 0.15), (0.12, 0.12)]
    make_candidate(db_session, aoi, methodology, day=1, ring=_PATCH_A, area_m2=10_000.0)
    make_candidate(db_session, aoi, methodology, day=8, ring=inner, area_m2=1_000.0)

    track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()

    observations = (
        db_session.execute(select(EventObservation).order_by(EventObservation.observed_at))
        .scalars()
        .all()
    )
    growth = observations[1].growth_m2
    assert growth is not None
    assert growth == pytest.approx(0.0, abs=1.0)  # within 1 m² of zero
    assert growth >= 0.0


def test_candidate_bridging_two_events_attaches_to_the_earliest(db_session: Session) -> None:
    """A candidate intersecting several events must attach to the earliest, not crash
    with MultipleResultsFound (audit BUG-3)."""
    aoi, methodology = _aoi_and_methodology(db_session)
    make_candidate(db_session, aoi, methodology, day=1, ring=_PATCH_A, area_m2=10_000.0)
    make_candidate(db_session, aoi, methodology, day=2, ring=_PATCH_B, area_m2=8_000.0)
    track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()
    assert len(db_session.execute(select(DisturbanceEvent)).scalars().all()) == 2

    # A large square covering both existing patches bridges the two events.
    bridge = [(0.05, 0.05), (0.75, 0.05), (0.75, 0.75), (0.05, 0.75), (0.05, 0.05)]
    make_candidate(db_session, aoi, methodology, day=9, ring=bridge, area_m2=40_000.0)
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
    make_candidate(db_session, aoi, methodology, day=1, ring=_PATCH_A, area_m2=10_000.0)
    make_candidate(db_session, aoi, methodology, day=8, ring=_PATCH_A_GROWN, area_m2=15_000.0)

    track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()

    event = db_session.execute(select(DisturbanceEvent)).scalar_one()
    footprint = to_shape(event.geometry)
    # The unioned footprint spans both patches (x from 0.1 to 0.3).
    minx, _, maxx, _ = footprint.bounds
    assert minx == 0.1
    assert maxx == 0.3
