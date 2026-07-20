"""`manual_review` (#101): append-only reviewer opinions recorded alongside —
never mutating — the automatic event status."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forest_sentinel.events import track_events_for_aoi
from forest_sentinel.models import REVIEW_OPINIONS, DisturbanceEvent, ManualReview
from tests.fakes import make_aoi, make_candidate, make_methodology, make_review

_PATCH = [(0.1, 0.1), (0.2, 0.1), (0.2, 0.2), (0.1, 0.2), (0.1, 0.1)]


def _seed_event(session: Session) -> DisturbanceEvent:
    aoi = make_aoi(session)
    methodology = make_methodology(session)
    make_candidate(session, aoi, methodology, day=1, ring=_PATCH, area_m2=10_000.0)
    track_events_for_aoi(session, aoi=aoi)
    session.flush()
    return session.execute(select(DisturbanceEvent)).scalar_one()


def test_reviews_round_trip_and_accumulate(db_session: Session) -> None:
    event = _seed_event(db_session)

    make_review(db_session, event, opinion="uncertain", notes="cloud shadow?", reviewer="jack")
    make_review(db_session, event, opinion="confirmed", notes="visible in later scene")
    db_session.commit()

    rows = db_session.execute(select(ManualReview).order_by(ManualReview.id)).scalars().all()
    assert [row.opinion for row in rows] == ["uncertain", "confirmed"]
    assert rows[0].reviewer == "jack"
    assert rows[1].reviewer is None
    assert all(row.event_id == event.id for row in rows)
    assert all(row.created_at is not None for row in rows)
    # The opinion never touches the machine-owned status.
    assert event.status == "new"


@pytest.mark.parametrize("opinion", REVIEW_OPINIONS)
def test_every_documented_opinion_is_accepted(db_session: Session, opinion: str) -> None:
    event = _seed_event(db_session)
    review = make_review(db_session, event, opinion=opinion)
    db_session.commit()
    assert review.id is not None


def test_invalid_opinion_is_rejected(db_session: Session) -> None:
    event = _seed_event(db_session)
    db_session.add(ManualReview(event_id=event.id, opinion="looks-bad"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_deleting_an_event_cascades_its_reviews(db_session: Session) -> None:
    event = _seed_event(db_session)
    make_review(db_session, event, opinion="confirmed")
    db_session.execute(select(ManualReview).where(ManualReview.event_id == event.id)).scalar_one()

    # Event deletion happens through aoi_admin's FK-ordered teardown in
    # production; the schema-level ON DELETE CASCADE is the safety net pinned
    # here. event_observation rows reference the event without a cascade, so
    # remove them first exactly as delete_aoi does.
    from forest_sentinel.models import EventObservation

    for row in db_session.execute(select(EventObservation)).scalars():
        db_session.delete(row)
    db_session.flush()
    db_session.delete(event)
    db_session.flush()

    assert db_session.execute(select(ManualReview)).scalars().all() == []
