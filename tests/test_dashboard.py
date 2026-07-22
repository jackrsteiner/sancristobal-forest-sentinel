import json
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.dashboard.app import app, get_session
from forest_sentinel.events import footprint_area_m2, track_events_for_aoi
from forest_sentinel.models import Aoi, DisturbanceEvent, PipelineRun, PipelineRunEvent
from tests.fakes import make_aoi, make_candidate, make_methodology, make_radar_methodology

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
            # No file in the AOIs dir → runs normally, but not toggleable (#149).
            "enabled": True,
            "can_disable": False,
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
        "display_version": "1.0.0",
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


def test_methodologies_lists_versions_with_inputs_newest_first(
    client: TestClient, db_session: Session
) -> None:
    aoi = make_aoi(db_session, name="Seeded AOI")
    old = make_methodology(db_session, parameters={"ee_script_version": "s1"})
    new = make_methodology(db_session, version="auto-bbb", parameters={"ee_script_version": "s2"})
    _seed_run(
        db_session,
        aoi,
        started_at=datetime(2026, 7, 15, 3, 0, tzinfo=UTC),
        methodology_version_id=old.id,
    )
    _seed_run(
        db_session,
        aoi,
        started_at=datetime(2026, 7, 16, 3, 0, tzinfo=UTC),
        methodology_version_id=new.id,
    )
    db_session.flush()

    response = client.get("/api/methodologies")
    assert response.status_code == 200
    body = response.json()
    assert [(m["version"], m["display_version"]) for m in body] == [
        ("auto-bbb", "1.1.0"),  # a changed EE script bumped the minor version
        ("1.0.0", "1.0.0"),
    ]
    # The review surface carries the full inputs and usage at a glance.
    assert body[0]["parameters"] == {"ee_script_version": "s2"}
    assert body[0]["run_count"] == 1
    assert body[0]["last_run_at"] is not None
    assert body[1]["run_count"] == 1


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


# --- GET /api/events/{id}/trajectory: persistence sparkline data (#165) ---


def test_event_trajectory_endpoint_shape_and_404(client: TestClient, db_session: Session) -> None:
    aoi = _seed_event(db_session)
    db_session.commit()
    event_id = db_session.execute(select(DisturbanceEvent.id)).scalars().first()
    payload = client.get(f"/api/events/{event_id}/trajectory").json()
    # No index COGs on disk in this fixture: honest insufficiency, right shape.
    assert payload["state"] == "insufficient-data"
    assert payload["points"] == []
    assert set(payload) == {"state", "reference_nbr", "detection_nbr", "points"}
    assert client.get("/api/events/999999/trajectory").status_code == 404
    del aoi


# --- GET /api/aois/{id}/observation-quality: slider clear-day ticks (#160) ---


def test_observation_quality_reports_best_fraction_per_day(
    client: TestClient, db_session: Session
) -> None:
    from datetime import timedelta

    from forest_sentinel.qa import record_quality_mask
    from tests.fakes import make_observation

    aoi = make_aoi(db_session, name="Quality AOI")
    now = datetime.now(UTC)
    # Two observations on the same recent day (best-of wins), one clearer day
    # earlier, and one outside the 30-day window (must be absent).
    for scene, days_ago, fraction in (
        ("q-a", 3, 0.42),
        ("q-b", 3, 0.81),
        ("q-c", 10, 0.95),
        ("q-old", 45, 0.99),
    ):
        obs = make_observation(
            db_session, aoi, source_scene_id=scene, acquired_at=now - timedelta(days=days_ago)
        )
        record_quality_mask(db_session, observation_id=obs.id, valid_pixel_fraction=fraction)
    db_session.commit()

    payload = client.get(f"/api/aois/{aoi.id}/observation-quality?days=30").json()
    by_date = {entry["date"]: entry for entry in payload["days"]}
    assert len(by_date) == 2
    same_day = by_date[(now - timedelta(days=3)).date().isoformat()]
    assert (same_day["valid_fraction"], same_day["observations"]) == (0.81, 2)
    clear_day = by_date[(now - timedelta(days=10)).date().isoformat()]
    assert (clear_day["valid_fraction"], clear_day["observations"]) == (0.95, 1)


def test_observation_quality_empty_aoi(client: TestClient, db_session: Session) -> None:
    aoi = make_aoi(db_session, name="Empty AOI")
    db_session.commit()
    assert client.get(f"/api/aois/{aoi.id}/observation-quality").json() == {"days": []}


# --- POST /api/aois/{id}/enabled: disable/re-enable for future runs (#149) ---


def _aoi_row(client: TestClient, name: str) -> dict[str, Any]:
    return next(aoi for aoi in client.get("/api/aois").json() if aoi["name"] == name)


def test_aoi_disable_writes_marker_and_reenable_removes_it(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOREST_SENTINEL_AOIS_DIR", str(tmp_path / "aois"))
    aoi_id = client.post("/api/aois", json=_UPLOAD_SQUARE).json()["id"]
    marker = tmp_path / "aois" / "uploaded-aoi.geojson.disabled"

    row = _aoi_row(client, "Uploaded AOI")
    assert (row["enabled"], row["can_disable"]) == (True, True)

    response = client.post(f"/api/aois/{aoi_id}/enabled", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert marker.exists()
    assert _aoi_row(client, "Uploaded AOI")["enabled"] is False

    response = client.post(f"/api/aois/{aoi_id}/enabled", json={"enabled": True})
    assert response.status_code == 200
    assert not marker.exists()
    assert _aoi_row(client, "Uploaded AOI")["enabled"] is True


def test_aoi_toggle_matches_by_config_name_not_filename(
    client: TestClient, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Committed seed files can be named anything; the config name is the key."""
    aois_dir = tmp_path / "aois"
    aois_dir.mkdir()
    document = {**_UPLOAD_SQUARE, "properties": {"name": "Seeded AOI"}}
    (aois_dir / "some-committed-file.geojson").write_text(json.dumps(document))
    monkeypatch.setenv("FOREST_SENTINEL_AOIS_DIR", str(aois_dir))
    aoi = _seed_event(db_session)
    db_session.commit()

    response = client.post(f"/api/aois/{aoi.id}/enabled", json={"enabled": False})
    assert response.status_code == 200
    assert (aois_dir / "some-committed-file.geojson.disabled").exists()
    # Events of a disabled AOI remain browsable — only future runs skip it.
    assert client.get(f"/api/aois/{aoi.id}/events").json()["features"]


def test_aoi_toggle_without_file_is_409(
    client: TestClient, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOREST_SENTINEL_AOIS_DIR", str(tmp_path / "aois"))
    aoi = make_aoi(db_session, name="No File AOI")
    db_session.commit()
    row = _aoi_row(client, "No File AOI")
    assert row["can_disable"] is False
    response = client.post(f"/api/aois/{aoi.id}/enabled", json={"enabled": False})
    assert response.status_code == 409
    assert "cannot be toggled" in response.json()["detail"]


def test_aoi_toggle_guard_and_validation(
    client: TestClient, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOREST_SENTINEL_AOIS_DIR", str(tmp_path / "aois"))
    aoi_id = client.post("/api/aois", json=_UPLOAD_SQUARE).json()["id"]

    assert client.post(f"/api/aois/{aoi_id}/enabled", json={}).status_code == 422
    assert client.post("/api/aois/999999/enabled", json={"enabled": False}).status_code == 404

    monkeypatch.setenv("FOREST_SENTINEL_AOI_UPLOADS", "0")
    response = client.post(f"/api/aois/{aoi_id}/enabled", json={"enabled": False})
    assert response.status_code == 403


def test_run_pipeline_and_sync_honor_disabled_markers() -> None:
    """Contract: the run loop skips marked AOIs and the sync harvests markers."""
    root = Path(__file__).resolve().parents[1]
    runner = (root / "scripts" / "run_pipeline.sh").read_text()
    assert '[ -e "${file}.disabled" ]' in runner

    workflow = (root / ".github" / "workflows" / "update-instance.yml").read_text()
    assert "config/aois/*.geojson*" in workflow  # markers ride the harvest
    assert "*.geojson.disabled" in workflow  # ...and removals are reconciled
    assert "git add -A config/aois" in workflow


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


def test_trigger_assess_starts_the_oneshot_unit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#168: the Re-assess button's endpoint starts the assess service."""
    app_module = sys.modules["forest_sentinel.dashboard.app"]
    recorded: list[tuple[str, ...]] = []

    class _Result:
        returncode = 0
        stderr = ""

    def fake_run(command: tuple[str, ...], **kwargs: object) -> _Result:
        recorded.append(tuple(command))
        return _Result()

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    response = client.post("/api/pipeline/assess", json={})
    assert response.status_code == 202
    assert "re-assessment started" in response.json()["detail"].lower()
    assert recorded == [app_module.PIPELINE_ASSESS_COMMAND]


def test_trigger_assess_shares_the_pipeline_trigger_guard(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOREST_SENTINEL_PIPELINE_TRIGGER", "0")
    response = client.post("/api/pipeline/assess", json={})
    assert response.status_code == 403
    assert "disabled" in response.json()["detail"]


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


def test_stop_pipeline_run_stops_the_service_and_stamps_interrupted(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dashboard package re-exports `app` (the FastAPI instance), shadowing
    # the submodule on attribute access — go through sys.modules instead.
    app_module = sys.modules["forest_sentinel.dashboard.app"]
    aoi = make_aoi(db_session, name="Seeded AOI")
    running = _seed_run(db_session, aoi, started_at=datetime(2026, 7, 16, 3, 0, tzinfo=UTC))
    finished = _seed_run(
        db_session, aoi, started_at=datetime(2026, 7, 15, 3, 0, tzinfo=UTC), status="succeeded"
    )
    db_session.flush()

    recorded: list[tuple[str, ...]] = []

    class _Result:
        returncode = 0
        stderr = ""

    def fake_run(command: tuple[str, ...], **kwargs: object) -> _Result:
        recorded.append(tuple(command))
        return _Result()

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    response = client.post("/api/pipeline/stop", json={})
    assert response.status_code == 202
    assert response.json() == {"stopped_runs": 1}
    assert recorded == [app_module.PIPELINE_STOP_COMMAND]
    # The runs panel tells the truth immediately: running -> interrupted; the
    # already-finished run keeps its terminal status.
    db_session.expire_all()
    statuses = {run.id: run.status for run in db_session.execute(select(PipelineRun)).scalars()}
    assert statuses[running.id] == "interrupted"
    assert statuses[finished.id] == "succeeded"


def test_stop_pipeline_run_failure_leaves_runs_untouched(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_module = sys.modules["forest_sentinel.dashboard.app"]
    aoi = make_aoi(db_session, name="Seeded AOI")
    running = _seed_run(db_session, aoi, started_at=datetime(2026, 7, 16, 3, 0, tzinfo=UTC))
    db_session.flush()

    class _Result:
        returncode = 1
        stderr = "Failed to connect to bus"

    monkeypatch.setattr(app_module.subprocess, "run", lambda *a, **k: _Result())
    response = client.post("/api/pipeline/stop", json={})
    assert response.status_code == 502
    assert "Failed to connect to bus" in response.json()["detail"]
    # The stop did not happen, so the row must still read running.
    db_session.expire_all()
    row = db_session.execute(select(PipelineRun).where(PipelineRun.id == running.id)).scalar_one()
    assert row.status == "running"


def test_stop_pipeline_run_can_be_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOREST_SENTINEL_PIPELINE_TRIGGER", "0")
    response = client.post("/api/pipeline/stop", json={})
    assert response.status_code == 403
    assert "disabled" in response.json()["detail"]


def test_stop_pipeline_run_requires_a_json_body(client: TestClient) -> None:
    response = client.post(
        "/api/pipeline/stop",
        content="x=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422


def test_record_review_appends_and_reads_back(client: TestClient, db_session: Session) -> None:
    _seed_event(db_session)
    event_id = db_session.execute(select(DisturbanceEvent.id)).scalar_one()

    first = client.post(
        f"/api/events/{event_id}/reviews",
        json={"opinion": "uncertain", "notes": "cloud shadow?", "reviewer": "jack"},
    )
    assert first.status_code == 201
    body = first.json()
    assert body["opinion"] == "uncertain"
    assert body["reviewer"] == "jack"

    second = client.post(f"/api/events/{event_id}/reviews", json={"opinion": "confirmed"})
    assert second.status_code == 201

    detail = client.get(f"/api/events/{event_id}").json()
    # Newest first; the head is the current opinion.
    assert [review["opinion"] for review in detail["reviews"]] == ["confirmed", "uncertain"]
    assert detail["latest_opinion"] == "confirmed"
    # The automatic status is untouched by review.
    assert detail["status"] == "ongoing"

    aoi_id = detail["aoi_id"]
    features = client.get(f"/api/aois/{aoi_id}/events").json()["features"]
    assert features[0]["properties"]["latest_opinion"] == "confirmed"


def test_unreviewed_events_read_null_opinion(client: TestClient, db_session: Session) -> None:
    aoi = _seed_event(db_session)
    features = client.get(f"/api/aois/{aoi.id}/events").json()["features"]
    assert features[0]["properties"]["latest_opinion"] is None
    event_id = features[0]["properties"]["id"]
    detail = client.get(f"/api/events/{event_id}").json()
    assert detail["reviews"] == []
    assert detail["latest_opinion"] is None


def test_record_review_rejects_unknown_opinion(client: TestClient, db_session: Session) -> None:
    _seed_event(db_session)
    event_id = db_session.execute(select(DisturbanceEvent.id)).scalar_one()
    response = client.post(f"/api/events/{event_id}/reviews", json={"opinion": "looks-bad"})
    assert response.status_code == 422
    assert "opinion must be one of" in response.json()["detail"]


def test_record_review_unknown_event_is_404(client: TestClient) -> None:
    assert (
        client.post("/api/events/999999/reviews", json={"opinion": "confirmed"}).status_code == 404
    )


def test_record_review_can_be_disabled(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_event(db_session)
    event_id = db_session.execute(select(DisturbanceEvent.id)).scalar_one()
    monkeypatch.setenv("FOREST_SENTINEL_REVIEWS", "0")
    response = client.post(f"/api/events/{event_id}/reviews", json={"opinion": "confirmed"})
    assert response.status_code == 403
    assert "disabled" in response.json()["detail"]


def test_record_review_requires_a_json_body(client: TestClient, db_session: Session) -> None:
    _seed_event(db_session)
    event_id = db_session.execute(select(DisturbanceEvent.id)).scalar_one()
    response = client.post(
        f"/api/events/{event_id}/reviews",
        content="opinion=confirmed",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422


def test_vm_setup_disables_reviews_on_a_world_open_dashboard() -> None:
    setup = (Path(__file__).resolve().parents[1] / "scripts" / "vm_setup.sh").read_text()
    assert 'echo "FOREST_SENTINEL_REVIEWS=0"' in setup
    assert 'echo "FOREST_SENTINEL_CONTEXT_UPLOADS=0"' in setup
    assert 'echo "FOREST_SENTINEL_SETTINGS_EDIT=0"' in setup


def test_vm_setup_appends_overrides_before_the_forced_off_guards() -> None:
    """Bead 7.2 (#135): overrides win over instance.env (last assignment) but the
    world-open guard lines must still win over the overrides file."""
    setup = (Path(__file__).resolve().parents[1] / "scripts" / "vm_setup.sh").read_text()
    instance_at = setup.index("# --- from config/instance.env ---")
    overrides_at = setup.index("config/overrides.env (dashboard settings edits)")
    computed_at = setup.index("# --- computed by vm_setup.sh")
    guard_at = setup.index('echo "FOREST_SENTINEL_SETTINGS_EDIT=0"')
    assert instance_at < overrides_at < computed_at < guard_at


def test_capabilities_reflect_env_guards(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.get("/api/capabilities").json() == {
        "aoi_uploads": True,
        "pipeline_trigger": True,
        "reviews": True,
        "context_uploads": True,
        "settings_edit": True,
    }
    monkeypatch.setenv("FOREST_SENTINEL_REVIEWS", "0")
    monkeypatch.setenv("FOREST_SENTINEL_AOI_UPLOADS", "0")
    monkeypatch.setenv("FOREST_SENTINEL_CONTEXT_UPLOADS", "0")
    monkeypatch.setenv("FOREST_SENTINEL_SETTINGS_EDIT", "0")
    assert client.get("/api/capabilities").json() == {
        "aoi_uploads": False,
        "pipeline_trigger": True,
        "reviews": False,
        "context_uploads": False,
        "settings_edit": False,
    }


def test_timeline_and_evidence_carry_quality_metadata(
    client: TestClient, db_session: Session
) -> None:
    """E14 residual (#105): the persisted ΔNBR statistics and coverage reach the API."""
    aoi = make_aoi(db_session, name="Seeded AOI")
    methodology = make_methodology(db_session)
    make_candidate(
        db_session,
        aoi,
        methodology,
        day=1,
        ring=_PATCH,
        area_m2=10_000.0,
        delta_mean=-0.31,
        delta_min=-0.52,
        valid_pixel_fraction=0.82,
    )
    # A pre-statistics candidate: everything must degrade to null, not crash.
    make_candidate(db_session, aoi, methodology, day=8, ring=_PATCH_GROWN, area_m2=15_000.0)
    track_events_for_aoi(db_session, aoi=aoi)
    db_session.flush()

    event_id = db_session.execute(select(DisturbanceEvent.id)).scalar_one()
    detail = client.get(f"/api/events/{event_id}").json()
    with_stats, without_stats = detail["timeline"]
    assert with_stats["delta_mean"] == -0.31
    assert with_stats["delta_min"] == -0.52
    assert with_stats["valid_pixel_fraction"] == 0.82
    assert without_stats["delta_mean"] is None
    assert without_stats["valid_pixel_fraction"] is None

    # Evidence carries the source raster's scene coverage (written by change.py;
    # the seeded rasters have none, so it reads null rather than erroring).
    assert all("valid_pixel_fraction" in item for item in detail["evidence"])

    # Event features expose the LATEST detection's coverage (day 8 -> null here).
    features = client.get(f"/api/aois/{aoi.id}/events").json()["features"]
    assert features[0]["properties"]["latest_valid_fraction"] is None


def test_event_features_and_detail_carry_confidence(
    client: TestClient, db_session: Session
) -> None:
    """E15 surface (#107): latest level/score on features; explained history in detail."""
    from forest_sentinel import confidence
    from forest_sentinel.confidence import assess_events_for_aoi

    aoi = make_aoi(db_session, name="Seeded AOI")
    methodology = make_methodology(db_session)
    make_candidate(
        db_session,
        aoi,
        methodology,
        day=1,
        ring=_PATCH,
        area_m2=10_000.0,
        delta_min=-0.5,
        delta_mean=-0.3,
        valid_pixel_fraction=0.9,
    )
    track_events_for_aoi(db_session, aoi=aoi)
    assess_events_for_aoi(db_session, aoi=aoi, now=datetime(2026, 1, 10, tzinfo=UTC))
    db_session.flush()

    features = client.get(f"/api/aois/{aoi.id}/events").json()["features"]
    props = features[0]["properties"]
    assert props["confidence_level"] in ("low", "medium", "high")
    assert 0.0 <= props["confidence_score"] <= 1.0

    detail = client.get(f"/api/events/{props['id']}").json()
    (assessment,) = detail["confidence"]
    assert assessment["level"] == props["confidence_level"]
    assert assessment["rule_version"] == confidence.RULE_VERSION
    # The recorded inputs make the level explainable without recomputation.
    assert assessment["inputs"]["factors"]["magnitude"]["delta_min"] == -0.5
    assert "weights" in assessment["inputs"]

    # Detection basis (#118) from the agreement factor: no radar lineage was
    # seeded, so this optical event reads optical-only on features and detail.
    assert props["basis"] == "optical-only"
    assert detail["basis"] == "optical-only"

    # Method attribution (#119): the lineage's methodology IS the "which
    # method produced this detection" answer.
    assert detail["methodology"]["name"] == "optical-change"
    assert detail["methodology"]["display_version"] == "1.0.0"


def test_cross_lineage_events_read_both_basis_and_their_method(
    client: TestClient, db_session: Session
) -> None:
    """#119: agreeing lineages read "both", each attributed to its own method."""
    from forest_sentinel.confidence import assess_events_for_aoi

    aoi = make_aoi(db_session, name="Seeded AOI")
    optical = make_methodology(db_session)
    radar = make_radar_methodology(db_session)
    make_candidate(db_session, aoi, optical, day=1, ring=_PATCH, area_m2=10_000.0, delta_min=-0.5)
    make_candidate(db_session, aoi, radar, day=3, ring=_PATCH, area_m2=10_000.0, sensor="S1GRD")
    track_events_for_aoi(db_session, aoi=aoi)
    assess_events_for_aoi(db_session, aoi=aoi, now=datetime(2026, 1, 10, tzinfo=UTC))
    db_session.flush()

    features = client.get(f"/api/aois/{aoi.id}/events").json()["features"]
    assert len(features) == 2
    assert {f["properties"]["basis"] for f in features} == {"both"}

    details = [client.get(f"/api/events/{f['properties']['id']}").json() for f in features]
    assert {d["methodology"]["name"] for d in details} == {"optical-change", "radar-change"}
    for detail in details:
        assert detail["basis"] == "both"
        agreement = detail["confidence"][0]["inputs"]["factors"]["agreement"]
        # The agreeing other-lineage evidence is enumerable from the detail.
        assert agreement["matching_candidate_ids"]


def test_unassessed_events_read_null_confidence(client: TestClient, db_session: Session) -> None:
    aoi = _seed_event(db_session)
    props = client.get(f"/api/aois/{aoi.id}/events").json()["features"][0]["properties"]
    assert props["confidence_level"] is None
    assert props["confidence_score"] is None
    assert props["basis"] is None
    detail = client.get(f"/api/events/{props['id']}").json()
    assert detail["confidence"] == []
    assert detail["basis"] is None


_CONTEXT_POLY = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [0.3, 0.0], [0.3, 0.3], [0.0, 0.3], [0.0, 0.0]]],
}


def _context_document(*geometries: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": g, "properties": {"name": f"feature-{i}"}}
            for i, g in enumerate(geometries)
        ],
    }


def test_context_layer_upload_list_and_features_round_trip(
    client: TestClient,
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#127: upload writes the harvest-convention file and replaces the DB layer."""
    monkeypatch.setenv("FOREST_SENTINEL_CONTEXT_DIR", str(tmp_path / "context"))

    response = client.post(
        "/api/context/layers",
        json={
            "name": "Acme Palm",
            "kind": "concession",
            "document": _context_document(_CONTEXT_POLY),
        },
    )
    assert response.status_code == 201
    body = response.json()
    # The sanitized name is both the stored name and the harvest filename, so
    # the next pipeline harvest replaces this same layer.
    assert body["name"] == "acme-palm"
    assert body["feature_count"] == 1
    assert body["file"].endswith("concession--acme-palm.geojson")
    assert Path(body["file"]).is_file()

    layers = client.get("/api/context/layers").json()
    assert layers == [
        {"id": body["id"], "name": "acme-palm", "kind": "concession", "feature_count": 1}
    ]
    collection = client.get(f"/api/context/layers/{body['id']}/features").json()
    assert collection["type"] == "FeatureCollection"
    (feature,) = collection["features"]
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["properties"]["kind"] == "concession"
    assert feature["properties"]["name"] == "feature-0"

    # Re-uploading the same name replaces the features (no 409: layers are
    # reference data, not provenance).
    line = {"type": "LineString", "coordinates": [[0.0, 0.0], [0.1, 0.1]]}
    again = client.post(
        "/api/context/layers",
        json={
            "name": "Acme Palm",
            "kind": "concession",
            "document": _context_document(_CONTEXT_POLY, line),
        },
    )
    assert again.status_code == 201
    assert again.json()["id"] == body["id"]
    assert client.get("/api/context/layers").json()[0]["feature_count"] == 2


def test_context_layer_upload_rejects_bad_payloads(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOREST_SENTINEL_CONTEXT_DIR", str(tmp_path / "context"))
    document = _context_document(_CONTEXT_POLY)
    assert (
        client.post(
            "/api/context/layers", json={"name": "x", "kind": "volcano", "document": document}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/context/layers", json={"name": "  ", "kind": "road", "document": document}
        ).status_code
        == 422
    )
    bad = client.post(
        "/api/context/layers", json={"name": "x", "kind": "road", "document": {"type": "nope"}}
    )
    assert bad.status_code == 400
    assert not (tmp_path / "context").exists() or not list((tmp_path / "context").iterdir())


def test_context_layer_upload_disabled_by_env_guard(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOREST_SENTINEL_CONTEXT_UPLOADS", "0")
    response = client.post(
        "/api/context/layers",
        json={"name": "x", "kind": "road", "document": _context_document(_CONTEXT_POLY)},
    )
    assert response.status_code == 403


def test_unknown_context_layer_features_return_404(client: TestClient, db_session: Session) -> None:
    assert client.get("/api/context/layers/999/features").status_code == 404


def test_event_detail_lists_context_relations(client: TestClient, db_session: Session) -> None:
    """The Slice 6 hallway test's API half: relations readable per event."""
    from forest_sentinel.context import (
        compute_event_context,
        load_context_document,
        replace_layer,
    )

    aoi = _seed_event(db_session)
    replace_layer(
        db_session,
        name="acme",
        kind="concession",
        document=load_context_document(_context_document(_CONTEXT_POLY)),
    )
    river = {"type": "LineString", "coordinates": [[0.32, 0.0], [0.32, 0.3]]}
    replace_layer(
        db_session,
        name="big-river",
        kind="river",
        document=load_context_document(_context_document(river)),
    )
    compute_event_context(db_session, aoi=aoi)
    db_session.flush()

    event_id = client.get(f"/api/aois/{aoi.id}/events").json()["features"][0]["properties"]["id"]
    detail = client.get(f"/api/events/{event_id}").json()
    relations = {(rel["relation"], rel["kind"]) for rel in detail["context"]}
    assert relations == {("contains", "concession"), ("nearby", "river")}
    by_relation = {rel["relation"]: rel for rel in detail["context"]}
    assert by_relation["contains"]["layer"] == "acme"
    assert by_relation["contains"]["name"] == "feature-0"
    assert by_relation["contains"]["distance_m"] is None
    assert by_relation["nearby"]["distance_m"] > 0


def test_settings_endpoint_serves_the_catalogue(
    client: TestClient, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 7 bead 7.1 (#134): the live catalogue over the inventory categories."""
    from forest_sentinel.settings import OVERRIDES_PATH_ENV_VAR

    monkeypatch.setenv(OVERRIDES_PATH_ENV_VAR, str(tmp_path / "overrides.env"))
    from tests.fakes import make_methodology

    make_methodology(db_session, version="auto-s", parameters={"min_area_m2": 9000.0})

    response = client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["categories"] == ["instance", "pipeline-tuning", "methodology", "lifecycle"]
    by_key = {entry["key"]: entry for entry in body["settings"]}
    assert by_key["THRESHOLD"]["editability"] == "guarded"
    assert by_key["WINDOW_DAYS"]["editability"] == "editable"
    assert by_key["FOREST_SENTINEL_COG_ROOT"]["editability"] == "display-only"
    assert by_key["MIN_AREA"]["recorded"] == 9000.0
    # Secrets never reach the response.
    assert "secret" not in json.dumps(body)


def test_settings_write_persists_override_and_audit_row(
    client: TestClient, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bead 7.2 (#135): an allowlisted edit lands in overrides.env + settings_change."""
    from forest_sentinel.models import SettingsChange
    from forest_sentinel.settings import OVERRIDES_PATH_ENV_VAR

    overrides = tmp_path / "overrides.env"
    monkeypatch.setenv(OVERRIDES_PATH_ENV_VAR, str(overrides))

    response = client.post("/api/settings", json={"key": "RESOLVED_AFTER_DAYS", "value": "120"})
    assert response.status_code == 200
    body = response.json()
    assert (body["old"], body["new"]) == ("90", "120")
    assert "RESOLVED_AFTER_DAYS=120" in overrides.read_text()

    row = db_session.execute(select(SettingsChange)).scalar_one()
    assert (row.key, row.old_value, row.new_value, row.category) == (
        "RESOLVED_AFTER_DAYS",
        "90",
        "120",
        "lifecycle",
    )
    # The catalogue immediately resolves the new value from the overrides layer.
    catalogue = client.get("/api/settings").json()
    entry = next(s for s in catalogue["settings"] if s["key"] == "RESOLVED_AFTER_DAYS")
    assert (entry["resolved"], entry["source"]) == ("120", "override")


def test_settings_write_guard_and_allowlist(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forest_sentinel.settings import OVERRIDES_PATH_ENV_VAR

    monkeypatch.setenv(OVERRIDES_PATH_ENV_VAR, str(tmp_path / "overrides.env"))

    # Footguns and unknown keys are indistinguishable: both are unknown to the
    # write path.
    for key in ("FOREST_SENTINEL_DATABASE_URL", "FOREST_SENTINEL_COG_ROOT", "NO_SUCH_KEY"):
        response = client.post("/api/settings", json={"key": key, "value": "x"})
        assert response.status_code == 422
        assert "unknown or non-editable" in response.json()["detail"]

    # Validation failures write nothing.
    response = client.post("/api/settings", json={"key": "WINDOW_DAYS", "value": "zero"})
    assert response.status_code == 422
    assert not (tmp_path / "overrides.env").exists()

    # The guard turns the whole surface off.
    monkeypatch.setenv("FOREST_SENTINEL_SETTINGS_EDIT", "0")
    response = client.post("/api/settings", json={"key": "WINDOW_DAYS", "value": "45"})
    assert response.status_code == 403


def test_methodology_settings_require_confirmation(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guarded keys reject without the flag, carrying the consequence copy."""
    from forest_sentinel.settings import OVERRIDES_PATH_ENV_VAR

    overrides = tmp_path / "overrides.env"
    monkeypatch.setenv(OVERRIDES_PATH_ENV_VAR, str(overrides))

    refused = client.post("/api/settings", json={"key": "THRESHOLD", "value": "-0.3"})
    assert refused.status_code == 422
    assert "mints a new content-addressed methodology version" in refused.json()["detail"]
    assert not overrides.exists()

    confirmed = client.post(
        "/api/settings",
        json={"key": "THRESHOLD", "value": "-0.3", "confirm_methodology_change": True},
    )
    assert confirmed.status_code == 200
    assert "THRESHOLD=-0.3" in overrides.read_text()


def test_retention_floor_cross_rule(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forest_sentinel.settings import OVERRIDES_PATH_ENV_VAR

    monkeypatch.setenv(OVERRIDES_PATH_ENV_VAR, str(tmp_path / "overrides.env"))
    monkeypatch.setenv("WINDOW_DAYS", "30")

    below_floor = client.post("/api/settings", json={"key": "COG_RETENTION_DAYS", "value": "20"})
    assert below_floor.status_code == 422
    assert "WINDOW_DAYS + 14 floor" in below_floor.json()["detail"]
    # 0 (keep forever) and at-floor values pass.
    keep_forever = client.post("/api/settings", json={"key": "COG_RETENTION_DAYS", "value": "0"})
    at_floor = client.post("/api/settings", json={"key": "COG_RETENTION_DAYS", "value": "44"})
    assert keep_forever.status_code == 200
    assert at_floor.status_code == 200


def test_writes_fire_the_sync_dispatch(
    client: TestClient, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bead 7.3 (#136): settings edits and uploads request a repo sync — after
    the local write, best-effort, never blocking the response."""
    from forest_sentinel import dispatch
    from forest_sentinel.settings import OVERRIDES_PATH_ENV_VAR

    monkeypatch.setenv(OVERRIDES_PATH_ENV_VAR, str(tmp_path / "overrides.env"))
    reasons: list[str] = []

    def fake_sync(*, reason: str, **_kw: Any) -> bool:
        reasons.append(reason)
        return True

    monkeypatch.setattr(dispatch, "request_sync", fake_sync)

    saved = client.post("/api/settings", json={"key": "WINDOW_DAYS", "value": "45"})
    assert saved.status_code == 200
    assert saved.json()["sync_requested"] is True

    monkeypatch.setenv("FOREST_SENTINEL_AOIS_DIR", str(tmp_path / "aois"))
    uploaded = client.post("/api/aois", json=_UPLOAD_SQUARE)
    assert uploaded.status_code == 201
    assert uploaded.json()["sync_requested"] is True

    assert reasons == ["settings-edit", "aoi-upload"]


def test_sync_dispatch_failure_never_blocks_the_write(
    client: TestClient, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forest_sentinel import dispatch
    from forest_sentinel.settings import OVERRIDES_PATH_ENV_VAR

    overrides = tmp_path / "overrides.env"
    monkeypatch.setenv(OVERRIDES_PATH_ENV_VAR, str(overrides))
    monkeypatch.setattr(dispatch, "request_sync", lambda *, reason, **kw: False)

    saved = client.post("/api/settings", json={"key": "WINDOW_DAYS", "value": "45"})
    assert saved.status_code == 200
    assert saved.json()["sync_requested"] is False
    assert "WINDOW_DAYS=45" in overrides.read_text()


def test_unit_rendered_settings_request_a_vm_rollout(
    client: TestClient, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bead 7.5 (#139): schedule edits escalate the sync to update_vm; ordinary
    knobs request a plain sync."""
    from forest_sentinel import dispatch
    from forest_sentinel.settings import OVERRIDES_PATH_ENV_VAR

    monkeypatch.setenv(OVERRIDES_PATH_ENV_VAR, str(tmp_path / "overrides.env"))
    requests: list[tuple[str, bool]] = []

    def fake_sync(*, reason: str, update_vm: bool = False, **_kw: Any) -> bool:
        requests.append((reason, update_vm))
        return True

    monkeypatch.setattr(dispatch, "request_sync", fake_sync)

    schedule = client.post("/api/settings", json={"key": "PIPELINE_SCHEDULE", "value": "04:30"})
    assert schedule.status_code == 200
    assert "Update-instance" in schedule.json()["detail"]

    plain = client.post("/api/settings", json={"key": "WINDOW_DAYS", "value": "45"})
    assert plain.status_code == 200
    assert "next pipeline run" in plain.json()["detail"]

    assert requests == [("settings-edit", True), ("settings-edit", False)]
