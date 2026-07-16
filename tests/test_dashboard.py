from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.dashboard.app import app, get_session
from forest_sentinel.events import footprint_area_m2, track_events_for_aoi
from forest_sentinel.models import Aoi, DisturbanceEvent, PipelineRun, PipelineRunEvent
from tests.fakes import make_aoi, make_candidate, make_methodology

_PATCH = [(0.1, 0.1), (0.2, 0.1), (0.2, 0.2), (0.1, 0.2), (0.1, 0.1)]
_PATCH_GROWN = [(0.15, 0.1), (0.3, 0.1), (0.3, 0.2), (0.15, 0.2), (0.15, 0.1)]


def _seed_event(session: Session) -> Aoi:
    aoi = make_aoi(session, name="Seeded AOI")
    methodology = make_methodology(session)
    for day, ring, area in ((1, _PATCH, 10_000.0), (8, _PATCH_GROWN, 15_000.0)):
        make_candidate(session, aoi, methodology, day=day, ring=ring, area_m2=area)
    track_events_for_aoi(session, aoi=aoi)
    session.flush()
    return aoi


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_aois_reports_event_counts(client: TestClient, db_session: Session) -> None:
    _seed_event(db_session)
    response = client.get("/api/aois")
    assert response.status_code == 200
    body = response.json()
    assert body == [{"id": body[0]["id"], "name": "Seeded AOI", "event_count": 1}]


def test_aoi_events_returns_geojson_feature_collection(
    client: TestClient, db_session: Session
) -> None:
    aoi = _seed_event(db_session)
    response = client.get(f"/api/aois/{aoi.id}/events")
    assert response.status_code == 200
    collection = response.json()
    assert collection["type"] == "FeatureCollection"
    assert len(collection["features"]) == 1

    feature = collection["features"][0]
    assert feature["geometry"]["type"] == "MultiPolygon"
    props = feature["properties"]
    assert props["status"] == "ongoing"
    assert props["observation_count"] == 2
    assert props["latest_area_m2"] == 15_000.0
    # The cumulative unioned footprint (geodesic m²) is exposed alongside it.
    event = db_session.execute(select(DisturbanceEvent)).scalar_one()
    assert props["footprint_area_m2"] == pytest.approx(
        footprint_area_m2(db_session, event.geometry)
    )


def test_event_detail_has_timeline_and_evidence(client: TestClient, db_session: Session) -> None:
    _seed_event(db_session)
    event_id = db_session.execute(select(DisturbanceEvent.id)).scalar_one()

    response = client.get(f"/api/events/{event_id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["status"] == "ongoing"
    assert detail["geometry"]["type"] == "MultiPolygon"

    timeline = detail["timeline"]
    assert [m["area_m2"] for m in timeline] == [10_000.0, 15_000.0]
    assert timeline[0]["growth_m2"] is None
    # growth_m2 is footprint expansion (geodesic m²), not a detection-area delta.
    assert timeline[1]["growth_m2"] > 0
    assert detail["footprint_area_m2"] > 0

    # Supporting evidence: the two source ΔNBR change rasters.
    assert len(detail["evidence"]) == 2
    assert all(item["change_type"] == "delta_nbr" for item in detail["evidence"])
    assert {item["cog_path"] for item in detail["evidence"]} == {"/cogs/1.tif", "/cogs/8.tif"}


def _seed_run(
    session: Session,
    aoi: Aoi,
    *,
    started_at: datetime,
    status: str = "running",
    events: int = 0,
    methodology_version_id: int | None = None,
) -> PipelineRun:
    run = PipelineRun(
        aoi_id=aoi.id,
        methodology_version_id=methodology_version_id,
        started_at=started_at,
        status=status,
        since=date(2026, 6, 16),
        until=date(2026, 7, 16),
    )
    session.add(run)
    session.flush()
    for index in range(events):
        session.add(
            PipelineRunEvent(
                run_id=run.id,
                stage="index",
                batch_index=index + 1,
                batch_total=events,
                exports=4,
                outcome="submitted",
            )
        )
    session.flush()
    return run


def test_aoi_runs_lists_newest_first_with_last_event(
    client: TestClient, db_session: Session
) -> None:
    aoi = make_aoi(db_session, name="Seeded AOI")
    methodology = make_methodology(db_session, parameters={"delta_nbr_threshold": -0.25})
    older = _seed_run(
        db_session, aoi, started_at=datetime(2026, 7, 15, 3, 0, tzinfo=UTC), status="succeeded"
    )
    newer = _seed_run(
        db_session,
        aoi,
        started_at=datetime(2026, 7, 16, 3, 0, tzinfo=UTC),
        events=2,
        methodology_version_id=methodology.id,
    )
    db_session.flush()

    response = client.get(f"/api/aois/{aoi.id}/runs")
    assert response.status_code == 200
    runs = response.json()
    assert [run["id"] for run in runs] == [newer.id, older.id]
    assert runs[0]["status"] == "running"
    assert runs[0]["since"] == "2026-06-16"
    assert runs[0]["last_event_at"] is not None
    assert runs[0]["methodology"] == {
        "id": methodology.id,
        "name": "optical-change",
        "version": "1.0.0",
    }
    assert runs[1]["status"] == "succeeded"
    assert runs[1]["last_event_at"] is None  # no progress events seeded
    assert runs[1]["methodology"] is None  # rows predating the provenance column


def test_run_detail_returns_events_in_order(client: TestClient, db_session: Session) -> None:
    aoi = make_aoi(db_session, name="Seeded AOI")
    methodology = make_methodology(db_session, parameters={"delta_nbr_threshold": -0.25})
    run = _seed_run(
        db_session,
        aoi,
        started_at=datetime(2026, 7, 16, 3, 0, tzinfo=UTC),
        events=3,
        methodology_version_id=methodology.id,
    )

    response = client.get(f"/api/runs/{run.id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["id"] == run.id
    assert detail["status"] == "running"
    # The run's non-data inputs are inspectable from the dashboard.
    assert detail["methodology"]["version"] == "1.0.0"
    assert detail["methodology"]["parameters"] == {"delta_nbr_threshold": -0.25}
    events = detail["events"]
    assert [event["batch_index"] for event in events] == [1, 2, 3]
    assert all(event["stage"] == "index" for event in events)
    assert all(event["outcome"] == "submitted" for event in events)
    assert all(event["exports"] == 4 for event in events)
    assert all(event["batch_total"] == 3 for event in events)


def test_unknown_aoi_runs_is_404(client: TestClient) -> None:
    assert client.get("/api/aois/999999/runs").status_code == 404


def test_unknown_run_detail_is_404(client: TestClient) -> None:
    assert client.get("/api/runs/999999").status_code == 404


def test_unknown_aoi_events_is_404(client: TestClient) -> None:
    assert client.get("/api/aois/999999/events").status_code == 404


def test_unknown_event_detail_is_404(client: TestClient) -> None:
    assert client.get("/api/events/999999").status_code == 404


def test_index_serves_the_map_page(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Open Forest Sentinel" in response.text
    # The page wires up the API endpoints it consumes.
    assert "/api/aois" in response.text
    assert "leaflet" in response.text.lower()
