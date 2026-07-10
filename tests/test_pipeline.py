"""Hallway test: the Slice 1 pipeline produces candidate polygons in PostGIS.

Earth Engine and storage are fully stubbed (no live calls / no GCP creds), but the run
exercises the real orchestration and persists real rows, so a candidate can be dumped to
valid WGS 84 GeoJSON — the slice's hallway test, mock-backed.
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.models import (
    ChangeRaster,
    DisturbanceCandidate,
    DisturbanceEvent,
    IndexRaster,
    Observation,
)
from forest_sentinel.pipeline import run_pipeline
from forest_sentinel.storage import CogKey, StorageError
from tests.fakes import FakeEarthEngine, FakeStorage, make_aoi, make_methodology

# A small candidate polygon inside the AOI bbox, returned by the stubbed vectorizer.
_CANDIDATE_RING = [[0.2, 0.2], [0.25, 0.2], [0.25, 0.25], [0.2, 0.25], [0.2, 0.2]]
_CANDIDATE_FEATURE: dict[str, Any] = {
    "type": "Feature",
    "geometry": {"type": "Polygon", "coordinates": [_CANDIDATE_RING]},
    "properties": {"area_m2": 50_000.0},
}


def _scene(day: int) -> dict[str, Any]:
    ms = int(datetime(2026, 1, day, tzinfo=UTC).timestamp() * 1000)
    return {
        "id": f"NASA/HLS/HLSL30/v002/scene-{day}",
        "properties": {"system:index": f"scene-{day}", "system:time_start": ms},
    }


def _fake_ee(days: tuple[int, ...]) -> FakeEarthEngine:
    """All synthetic scenes belong to the Landsat collection."""
    return FakeEarthEngine(
        scenes={"NASA/HLS/HLSL30/v002": [_scene(day) for day in days]},
        features=[_CANDIDATE_FEATURE],
        valid_fraction=0.95,
    )


def test_run_full_pipeline_produces_candidates(db_session: Session, tmp_path: Path) -> None:
    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)
    fake_ee = _fake_ee((1, 2, 3, 4, 5, 6))

    summary = run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        methodology=methodology,
        storage=FakeStorage(tmp_path),
        baseline_window=5,
        ee_module=fake_ee,
    )
    db_session.commit()

    # 6 observations -> 12 index rasters; first has no baseline so 5 obs x 2 = 10 change rasters;
    # candidates come from the 5 delta_nbr rasters, one polygon each.
    assert summary.observations_discovered == 6
    assert summary.observations_recorded == 6
    assert summary.index_rasters == 12
    assert summary.change_rasters == 10
    assert summary.candidates == 5
    # All 5 candidates share the stubbed geometry, so they overlap into one tracked event.
    assert summary.events_created == 1
    assert summary.event_observations == 5

    assert len(db_session.execute(select(Observation)).scalars().all()) == 6
    assert len(db_session.execute(select(IndexRaster)).scalars().all()) == 12
    assert len(db_session.execute(select(ChangeRaster)).scalars().all()) == 10

    # Candidates are tracked into a single disturbance event with a valid footprint.
    event = db_session.execute(select(DisturbanceEvent)).scalar_one()
    assert event.status == "ongoing"
    assert to_shape(event.geometry).is_valid

    candidate = db_session.execute(select(DisturbanceCandidate)).scalars().first()
    assert candidate is not None
    geometry = to_shape(candidate.geometry)
    assert geometry.geom_type == "Polygon"
    assert geometry.is_valid
    # The candidate dumps cleanly to GeoJSON for eyeballing on a map.
    geojson = json.dumps(mapping(geometry))
    assert json.loads(geojson)["type"] == "Polygon"


def test_rerunning_full_pipeline_is_idempotent(db_session: Session, tmp_path: Path) -> None:
    """A second run over the same window must succeed and add nothing (audit BUG-2):
    tracked candidates are event history and survive candidate re-extraction."""
    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)
    fake_ee = _fake_ee((1, 2, 3, 4, 5, 6))

    def run() -> Any:
        return run_pipeline(
            db_session,
            aoi=aoi,
            since=date(2026, 1, 1),
            until=date(2026, 2, 1),
            methodology=methodology,
            storage=FakeStorage(tmp_path),
            baseline_window=5,
            ee_module=fake_ee,
        )

    run()
    db_session.commit()
    second = run()
    db_session.commit()

    # Candidates are frozen once tracked; events and measurements are unchanged.
    assert second.candidates == 5  # the existing (frozen) candidate set is reported
    assert second.events_created == 0
    assert second.event_observations == 0
    assert len(db_session.execute(select(DisturbanceCandidate)).scalars().all()) == 5
    assert len(db_session.execute(select(DisturbanceEvent)).scalars().all()) == 1


def test_one_failing_export_does_not_starve_the_run(db_session: Session, tmp_path: Path) -> None:
    """A persistently failing export must be skipped and counted, not abort the whole
    run and roll everything back (re-audit R4)."""

    class FlakyStorage(FakeStorage):
        def export_image(
            self, image: Any, key: CogKey, *, scale: int | None = None, region: Any = None
        ) -> Path:
            if key.date == "2026-01-02":  # scene-2's exports always fail
                raise StorageError("Earth Engine export ended in state FAILED")
            return super().export_image(image, key, scale=scale, region=region)

    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)
    fake_ee = _fake_ee((1, 2, 3))

    summary = run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        methodology=methodology,
        storage=FlakyStorage(tmp_path),
        ee_module=fake_ee,
    )
    db_session.commit()

    # scene-2 was skipped once (index stage); the other observations completed and
    # event tracking still ran.
    assert summary.export_failures == 1
    assert summary.observations_recorded == 3
    assert summary.index_rasters == 4  # scenes 1 and 3 only
    assert summary.events_created == 1


def test_concurrent_runs_are_serialized_per_aoi(db_session: Session, tmp_path: Path) -> None:
    """run_pipeline takes a per-AOI advisory lock so a manual run alongside the
    systemd timer waits instead of racing the read-then-write upserts (re-audit
    round 2, finding 2)."""
    from sqlalchemy import func
    from sqlalchemy import select as sa_select

    from forest_sentinel.pipeline import AOI_RUN_LOCK_CLASS

    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)
    run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        methodology=methodology,
        storage=FakeStorage(tmp_path),
        ee_module=_fake_ee((1,)),
    )
    # The lock is transaction-scoped and still held (no commit yet): a concurrent
    # session cannot take it...
    engine = db_session.get_bind()
    with Session(engine) as other:
        assert (
            other.execute(
                sa_select(func.pg_try_advisory_xact_lock(AOI_RUN_LOCK_CLASS, aoi.id))
            ).scalar_one()
            is False
        )
    db_session.commit()
    # ...and can once the first run's transaction ends.
    with Session(engine) as other:
        assert (
            other.execute(
                sa_select(func.pg_try_advisory_xact_lock(AOI_RUN_LOCK_CLASS, aoi.id))
            ).scalar_one()
            is True
        )


def test_ee_failure_in_candidate_stage_is_isolated(db_session: Session, tmp_path: Path) -> None:
    """A raw Earth Engine failure during candidate extraction must be skipped and
    counted like a storage failure, not abort the run (re-audit round 2, finding 1)."""
    from forest_sentinel.earthengine import EarthEngineError

    class FailingVectorizeEE(FakeEarthEngine):
        def threshold_and_vectorize(
            self,
            delta_image: Any,
            *,
            threshold: float,
            scale: int,
            region: Any,
            min_area_m2: float,
        ) -> list[dict[str, Any]]:
            raise EarthEngineError("candidate vectorization failed: quota exceeded")

    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)
    fake_ee = FailingVectorizeEE(
        scenes={"NASA/HLS/HLSL30/v002": [_scene(day) for day in (1, 2)]},
        features=[_CANDIDATE_FEATURE],
        valid_fraction=0.95,
    )

    summary = run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        methodology=methodology,
        storage=FakeStorage(tmp_path),
        ee_module=fake_ee,
    )
    db_session.commit()

    # Scene 2's candidate stage failed (scene 1 has no baseline); indices completed.
    assert summary.export_failures == 1
    assert summary.index_rasters == 4
    assert summary.candidates == 0
    assert summary.events_created == 0


def test_pipeline_only_processes_observations_in_the_window(
    db_session: Session, tmp_path: Path
) -> None:
    """Observations outside --since/--until must not be reprocessed (audit BUG-5):
    a later run over a new window leaves the history alone."""
    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)
    fake_ee = _fake_ee((1, 2, 3))

    first = run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        methodology=methodology,
        storage=FakeStorage(tmp_path),
        ee_module=fake_ee,
    )
    db_session.commit()
    assert first.index_rasters == 6

    # A February window: the January observations are re-discovered (and skipped) but
    # must not be re-exported or re-processed.
    second = run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 2, 1),
        until=date(2026, 3, 1),
        methodology=methodology,
        storage=FakeStorage(tmp_path),
        ee_module=fake_ee,
    )
    db_session.commit()

    assert second.observations_recorded == 0
    assert second.index_rasters == 0
    assert second.change_rasters == 0
    assert second.candidates == 0
