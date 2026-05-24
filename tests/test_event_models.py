from datetime import UTC, datetime

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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

_SQUARE = MultiPolygon([Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])])


def _aoi_and_methodology(session: Session) -> tuple[Aoi, MethodologyVersion]:
    aoi = Aoi(name="Test AOI", geometry=from_shape(_SQUARE, srid=4326))
    session.add(aoi)
    session.flush()
    methodology = get_or_create_methodology_version(
        session, name="optical-change", version="1.0.0", parameters={}
    )
    return aoi, methodology


def _candidate(session: Session, aoi: Aoi, methodology: MethodologyVersion) -> DisturbanceCandidate:
    obs = Observation(
        aoi_id=aoi.id,
        sensor="HLSL30",
        acquired_at=datetime(2026, 1, 6, tzinfo=UTC),
        source_scene_id="scene-6",
    )
    session.add(obs)
    session.flush()
    change = ChangeRaster(
        observation_id=obs.id,
        methodology_version_id=methodology.id,
        change_type="delta_nbr",
        cog_path="/x.tif",
        baseline_window=5,
    )
    session.add(change)
    session.flush()
    candidate = DisturbanceCandidate(
        change_raster_id=change.id,
        methodology_version_id=methodology.id,
        geometry=from_shape(Polygon([(0, 0), (0.1, 0), (0.1, 0.1), (0, 0.1), (0, 0)]), srid=4326),
        detected_at=datetime(2026, 1, 6, tzinfo=UTC),
        area_m2=10_000.0,
    )
    session.add(candidate)
    session.flush()
    return candidate


def _event(session: Session, aoi: Aoi, methodology: MethodologyVersion) -> DisturbanceEvent:
    event = DisturbanceEvent(
        aoi_id=aoi.id,
        methodology_version_id=methodology.id,
        geometry=from_shape(_SQUARE, srid=4326),
        status="new",
        first_detected_at=datetime(2026, 1, 6, tzinfo=UTC),
        last_detected_at=datetime(2026, 1, 6, tzinfo=UTC),
    )
    session.add(event)
    session.flush()
    return event


def test_event_round_trips(db_session: Session) -> None:
    aoi, methodology = _aoi_and_methodology(db_session)
    event = _event(db_session, aoi, methodology)
    db_session.commit()

    stored = db_session.execute(select(DisturbanceEvent)).scalar_one()
    assert stored.id == event.id
    assert stored.aoi_id == aoi.id
    assert stored.status == "new"


def test_event_observation_round_trips(db_session: Session) -> None:
    aoi, methodology = _aoi_and_methodology(db_session)
    event = _event(db_session, aoi, methodology)
    candidate = _candidate(db_session, aoi, methodology)
    db_session.add(
        EventObservation(
            event_id=event.id,
            disturbance_candidate_id=candidate.id,
            observed_at=candidate.detected_at,
            area_m2=candidate.area_m2,
            growth_m2=None,
        )
    )
    db_session.commit()

    stored = db_session.execute(select(EventObservation)).scalar_one()
    assert stored.event_id == event.id
    assert stored.disturbance_candidate_id == candidate.id
    assert stored.area_m2 == 10_000.0
    assert stored.growth_m2 is None


def test_a_candidate_maps_to_at_most_one_event_observation(db_session: Session) -> None:
    aoi, methodology = _aoi_and_methodology(db_session)
    event = _event(db_session, aoi, methodology)
    candidate = _candidate(db_session, aoi, methodology)
    common = {
        "event_id": event.id,
        "disturbance_candidate_id": candidate.id,
        "observed_at": candidate.detected_at,
        "area_m2": candidate.area_m2,
    }
    db_session.add(EventObservation(**common))
    db_session.flush()
    db_session.add(EventObservation(**common))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_deleting_event_cascades_to_observations(db_session: Session) -> None:
    aoi, methodology = _aoi_and_methodology(db_session)
    event = _event(db_session, aoi, methodology)
    candidate = _candidate(db_session, aoi, methodology)
    db_session.add(
        EventObservation(
            event_id=event.id,
            disturbance_candidate_id=candidate.id,
            observed_at=candidate.detected_at,
            area_m2=candidate.area_m2,
        )
    )
    db_session.commit()

    db_session.delete(event)
    db_session.commit()
    assert db_session.execute(select(EventObservation)).scalars().all() == []
