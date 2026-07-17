import json
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

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
    assert body == [
        {
            "id": body[0]["id"],
            "name": "Seeded AOI",
            "event_count": 1,
            # The unit-square fixture AOI, as [min_lon, min_lat, max_lon, max_lat].
            "bbox": [0.0, 0.0, 1.0, 1.0],
        }
    ]


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


def test_run_detail_reports_whole_run_progress(client: TestClient, db_session: Session) -> None:
    aoi = make_aoi(db_session, name="Seeded AOI")
    run = _seed_run(db_session, aoi, started_at=datetime(2026, 7, 16, 3, 0, tzinfo=UTC))
    # A long run: far more events than the tail returns, so the counters must be
    # aggregated server-side rather than summed from the visible tail.
    for index in range(1, 61):
        for outcome, message in (
            ("submitted", None),
            ("succeeded", "2 exported, 0 reused, 0 candidates"),
        ):
            db_session.add(
                PipelineRunEvent(
                    run_id=run.id,
                    stage="change",
                    batch_index=index,
                    batch_total=301,
                    exports=2,
                    outcome=outcome,
                    message=message,
                )
            )
    db_session.add(
        PipelineRunEvent(
            run_id=run.id,
            stage="change",
            batch_index=61,
            batch_total=301,
            outcome="failed",
            message="boom",
        )
    )
    db_session.flush()

    response = client.get(f"/api/runs/{run.id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["progress"] == {
        "stage": "change",
        "batch_index": 61,
        "batch_total": 301,
        "exports_completed": 120,
        "batches_failed": 1,
    }
    assert len(detail["events"]) == 50  # the tail stays capped


def test_run_detail_progress_is_null_before_first_batch(
    client: TestClient, db_session: Session
) -> None:
    aoi = make_aoi(db_session, name="Seeded AOI")
    run = _seed_run(db_session, aoi, started_at=datetime(2026, 7, 16, 3, 0, tzinfo=UTC))
    # Discovery / stage-preamble events carry no batch position.
    db_session.add(
        PipelineRunEvent(run_id=run.id, stage="discovery", outcome="info", message="3 discovered")
    )
    db_session.flush()

    detail = client.get(f"/api/runs/{run.id}").json()
    assert detail["progress"] is None


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


# --- POST /api/aois: the dashboard AOI upload ---

_UPLOAD_SQUARE = {
    "type": "Feature",
    "properties": {"name": "Uploaded AOI"},
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]],
    },
}


def test_upload_aoi_creates_row_and_file(
    client: TestClient, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOREST_SENTINEL_AOIS_DIR", str(tmp_path / "aois"))

    response = client.post("/api/aois", json=_UPLOAD_SQUARE)
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Uploaded AOI"

    # The row is visible to the read API immediately...
    names = {aoi["name"] for aoi in client.get("/api/aois").json()}
    assert "Uploaded AOI" in names
    # ...and the file joins the aois/ directory the scheduled run loops over.
    written = tmp_path / "aois" / "uploaded-aoi.geojson"
    assert str(written) == body["file"]
    assert json.loads(written.read_text())["properties"]["name"] == "Uploaded AOI"


def test_upload_aoi_duplicate_name_is_409(
    client: TestClient, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOREST_SENTINEL_AOIS_DIR", str(tmp_path / "aois"))
    make_aoi(db_session, name="Uploaded AOI")
    db_session.commit()

    response = client.post("/api/aois", json=_UPLOAD_SQUARE)
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
    # The rejected upload must not leave a file behind for the run loop.
    assert not (tmp_path / "aois" / "uploaded-aoi.geojson").exists()


def test_upload_aoi_duplicate_file_is_409(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aois_dir = tmp_path / "aois"
    aois_dir.mkdir()
    (aois_dir / "uploaded-aoi.geojson").write_text("{}")
    monkeypatch.setenv("FOREST_SENTINEL_AOIS_DIR", str(aois_dir))

    response = client.post("/api/aois", json=_UPLOAD_SQUARE)
    assert response.status_code == 409
    assert "file" in response.json()["detail"]


def test_upload_aoi_invalid_document_is_400(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOREST_SENTINEL_AOIS_DIR", str(tmp_path / "aois"))
    invalid = {**_UPLOAD_SQUARE, "properties": {}}
    response = client.post("/api/aois", json=invalid)
    assert response.status_code == 400
    assert "properties.name" in response.json()["detail"]
    assert not (tmp_path / "aois").exists()  # nothing written for a rejected document


def test_upload_aoi_can_be_disabled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOREST_SENTINEL_AOI_UPLOADS", "0")
    response = client.post("/api/aois", json=_UPLOAD_SQUARE)
    assert response.status_code == 403
    assert "disabled" in response.json()["detail"]


# --- POST /api/pipeline/run: the dashboard run-now trigger ---


def test_trigger_pipeline_run_starts_the_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dashboard package re-exports `app` (the FastAPI instance), shadowing
    # the submodule on attribute access — go through sys.modules instead.
    app_module = sys.modules["forest_sentinel.dashboard.app"]

    recorded: list[tuple[str, ...]] = []

    class _Result:
        returncode = 0
        stderr = ""

    def fake_run(command: tuple[str, ...], **kwargs: object) -> _Result:
        recorded.append(tuple(command))
        return _Result()

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    response = client.post("/api/pipeline/run", json={})
    assert response.status_code == 202
    assert "runs panel" in response.json()["detail"]
    assert recorded == [app_module.PIPELINE_START_COMMAND]


def test_trigger_pipeline_run_reports_a_failing_start(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dashboard package re-exports `app` (the FastAPI instance), shadowing
    # the submodule on attribute access — go through sys.modules instead.
    app_module = sys.modules["forest_sentinel.dashboard.app"]

    class _Result:
        returncode = 1
        stderr = "Failed to connect to bus"

    monkeypatch.setattr(app_module.subprocess, "run", lambda *a, **k: _Result())
    response = client.post("/api/pipeline/run", json={})
    assert response.status_code == 502
    assert "Failed to connect to bus" in response.json()["detail"]


def test_trigger_pipeline_run_can_be_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOREST_SENTINEL_PIPELINE_TRIGGER", "0")
    response = client.post("/api/pipeline/run", json={})
    assert response.status_code == 403
    assert "disabled" in response.json()["detail"]


def test_trigger_pipeline_run_requires_a_json_body(client: TestClient) -> None:
    # A cross-origin "simple request" (form/text body, no preflight) must not
    # reach the handler: FastAPI rejects a non-JSON body before it runs.
    response = client.post(
        "/api/pipeline/run",
        content="x=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422
