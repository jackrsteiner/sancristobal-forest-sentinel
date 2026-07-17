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
from forest_sentinel.storage import StorageError
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
        def export_images(self, requests: Any) -> list[Path | StorageError]:
            results = super().export_images(requests)
            return [
                StorageError("Earth Engine export ended in state FAILED")
                if request.key.date == "2026-01-02"  # scene-2's exports always fail
                else result
                for request, result in zip(requests, results, strict=True)
            ]

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
    round 2, finding 2). The lock is session-scoped (#77: it must survive the
    run's checkpoint commits) and released when the run finishes."""
    from sqlalchemy import Engine, func
    from sqlalchemy import select as sa_select

    from forest_sentinel.models import Aoi, MethodologyVersion
    from forest_sentinel.pipeline import (
        AOI_RUN_LOCK_CLASS,
        _acquire_aoi_run_lock,
        _release_aoi_run_lock,
    )

    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)
    db_session.commit()
    aoi_id, methodology_id = aoi.id, methodology.id
    engine = db_session.get_bind()
    assert isinstance(engine, Engine)

    def other_can_lock() -> bool:
        with Session(engine) as other:
            taken = other.execute(
                sa_select(func.pg_try_advisory_lock(AOI_RUN_LOCK_CLASS, aoi_id))
            ).scalar_one()
            if taken:
                other.execute(sa_select(func.pg_advisory_unlock(AOI_RUN_LOCK_CLASS, aoi_id)))
            return bool(taken)

    # The run session is bound to one pinned connection, as the CLI binds it: the
    # session-scoped lock lives on that connection across checkpoint commits.
    with engine.connect() as pinned, Session(bind=pinned) as run_session:
        run_aoi = run_session.get(Aoi, aoi_id)
        run_methodology = run_session.get(MethodologyVersion, methodology_id)
        assert run_aoi is not None and run_methodology is not None

        # Held: a concurrent session cannot take it, and a checkpoint commit does
        # not release it (the whole point of a session-scoped lock)...
        _acquire_aoi_run_lock(run_session, aoi_id)
        assert other_can_lock() is False
        run_session.commit()
        assert other_can_lock() is False
        # ...until it is explicitly released.
        _release_aoi_run_lock(run_session, aoi_id)
        assert other_can_lock() is True

        # A completed run leaves the lock free for the next scheduled run.
        run_pipeline(
            run_session,
            aoi=run_aoi,
            since=date(2026, 1, 1),
            until=date(2026, 2, 1),
            methodology=run_methodology,
            storage=FakeStorage(tmp_path),
            ee_module=_fake_ee((1,)),
        )
        run_session.commit()
    assert other_can_lock() is True


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


def test_second_run_reuses_persisted_artifacts(db_session: Session, tmp_path: Path) -> None:
    """A re-run over an already-processed window submits zero Earth Engine exports
    (#77): every artifact is reused from the catalog + COG store."""
    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)
    fake_ee = _fake_ee((1, 2, 3, 4, 5, 6))

    def run(storage: FakeStorage) -> Any:
        return run_pipeline(
            db_session,
            aoi=aoi,
            since=date(2026, 1, 1),
            until=date(2026, 2, 1),
            methodology=methodology,
            storage=storage,
            baseline_window=5,
            ee_module=fake_ee,
        )

    run(FakeStorage(tmp_path))
    db_session.commit()

    second_storage = FakeStorage(tmp_path)
    second = run(second_storage)
    db_session.commit()

    assert second_storage.exports == []
    assert second.index_rasters == 0
    assert second.index_rasters_reused == 12
    assert second.change_rasters_reused > 0
    assert second.export_failures == 0


def test_missing_cog_triggers_exactly_one_reexport(db_session: Session, tmp_path: Path) -> None:
    """A pruned/lost COG self-heals (#77): only that artifact is re-exported."""
    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)
    fake_ee = _fake_ee((1, 2, 3))

    first_storage = FakeStorage(tmp_path)
    run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        methodology=methodology,
        storage=first_storage,
        ee_module=fake_ee,
    )
    db_session.commit()

    row = (
        db_session.execute(select(IndexRaster).where(IndexRaster.index_type == "NBR").limit(1))
        .scalars()
        .one()
    )
    Path(row.cog_path).unlink()

    second_storage = FakeStorage(tmp_path)
    second = run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        methodology=methodology,
        storage=second_storage,
        ee_module=fake_ee,
    )
    db_session.commit()

    assert len(second_storage.exports) == 1
    assert second.index_rasters == 1
    assert second_storage.exports[0][1].product == "NBR"


def test_interrupted_run_keeps_checkpointed_progress(db_session: Session, tmp_path: Path) -> None:
    """Progress commits per chunk (#77): a run killed partway (systemd timeout,
    SIGTERM) leaves the completed observations' artifacts in the catalog."""

    class DyingStorage(FakeStorage):
        def export_images(self, requests: Any) -> list[Path | StorageError]:
            if any(request.key.date == "2026-01-03" for request in requests):
                raise RuntimeError("simulated SIGTERM")  # not a per-item failure: the run dies
            return super().export_images(requests)

    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)
    fake_ee = _fake_ee((1, 2, 3))

    import pytest

    with pytest.raises(RuntimeError, match="simulated SIGTERM"):
        run_pipeline(
            db_session,
            aoi=aoi,
            since=date(2026, 1, 1),
            until=date(2026, 2, 1),
            methodology=methodology,
            storage=DyingStorage(tmp_path),
            ee_module=fake_ee,
        )
    # The process dies: whatever was not checkpointed is lost.
    db_session.rollback()

    persisted = db_session.execute(select(IndexRaster)).scalars().all()
    assert len(persisted) == 4  # scenes 1 and 2 were committed before the kill

    # The next scheduled run completes the window, re-exporting only the remainder.
    second_storage = FakeStorage(tmp_path)
    second = run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        methodology=methodology,
        storage=second_storage,
        ee_module=fake_ee,
    )
    db_session.commit()
    assert second.index_rasters == 2  # scene-3 only
    assert second.index_rasters_reused == 4


def test_exports_are_batched_up_to_the_concurrency_limit(
    db_session: Session, tmp_path: Path
) -> None:
    """With max_concurrent_exports=4, index exports are submitted four at a time
    (two observations x NBR+NDVI per batch), not one by one (#79)."""
    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)
    storage = FakeStorage(tmp_path)

    run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        methodology=methodology,
        storage=storage,
        max_concurrent_exports=4,
        ee_module=_fake_ee((1, 2, 3, 4, 5, 6)),
    )
    db_session.commit()

    # Index stage: 6 observations in chunks of 2 -> three batches of 4 exports.
    assert storage.batch_sizes[:3] == [4, 4, 4]


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


def _run_events(db_session: Session, run_id: int) -> list[Any]:
    from forest_sentinel.models import PipelineRunEvent

    return list(
        db_session.execute(
            select(PipelineRunEvent)
            .where(PipelineRunEvent.run_id == run_id)
            .order_by(PipelineRunEvent.id)
        )
        .scalars()
        .all()
    )


def test_run_is_recorded_with_labelled_batch_events(
    db_session: Session, tmp_path: Path, caplog: Any
) -> None:
    """Every EE batch submit/success is recorded as a committed pipeline_run_event
    and mirrored to the log with a `run <started> · <stage> batch i/N` label."""
    import logging as logging_module

    from forest_sentinel.models import PipelineRun

    caplog.set_level(logging_module.INFO, logger="forest_sentinel.runlog")
    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)

    summary = run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        methodology=methodology,
        storage=FakeStorage(tmp_path),
        max_concurrent_exports=4,
        ee_module=_fake_ee((1, 2, 3)),
    )
    db_session.commit()

    run = db_session.execute(select(PipelineRun)).scalar_one()
    assert run.aoi_id == aoi.id
    assert run.status == "succeeded"
    assert run.finished_at is not None
    assert run.since == date(2026, 1, 1)
    assert run.until == date(2026, 2, 1)
    assert run.summary is not None
    assert run.summary["index_rasters"] == summary.index_rasters == 6

    events = _run_events(db_session, run.id)
    # Index stage: 3 observations in chunks of 2 -> batch 1/2 (4 exports) and 2/2 (2).
    submitted = [
        (event.batch_index, event.batch_total, event.exports)
        for event in events
        if event.stage == "index" and event.outcome == "submitted"
    ]
    assert submitted == [(1, 2, 4), (2, 2, 2)]
    succeeded = [
        (event.batch_index, event.batch_total)
        for event in events
        if event.stage == "index" and event.outcome == "succeeded"
    ]
    assert succeeded == [(1, 2), (2, 2)]
    # Change stage: one batch per observation; scene-1 has no baseline (no submit).
    change_submitted = [
        (event.batch_index, event.batch_total, event.exports)
        for event in events
        if event.stage == "change" and event.outcome == "submitted"
    ]
    assert change_submitted == [(2, 3, 2), (3, 3, 2)]
    # Stage transitions are recorded too.
    assert {event.stage for event in events if event.outcome == "info"} == {
        "discovery",
        "index",
        "change",
        "events",
    }

    # The journald-facing mirror carries the run-start datetime and batch position.
    import re

    assert any(
        re.search(
            r"run \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z · index batch 1/2: "
            r"submitted 4 exports to Earth Engine",
            record.getMessage(),
        )
        for record in caplog.records
    )


def test_failed_change_batch_records_failure_and_partial_status(
    db_session: Session, tmp_path: Path
) -> None:
    """A failed observation leaves a `failed` batch event and the run ends `partial`."""
    from forest_sentinel.earthengine import EarthEngineError
    from forest_sentinel.models import PipelineRun

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

    assert summary.export_failures == 1
    run = db_session.execute(select(PipelineRun)).scalar_one()
    assert run.status == "partial"
    failed = [event for event in _run_events(db_session, run.id) if event.outcome == "failed"]
    assert len(failed) == 1
    assert failed[0].stage == "change"
    assert "quota exceeded" in (failed[0].message or "")


def test_dying_run_is_stamped_failed_and_superseded_as_interrupted(
    db_session: Session, tmp_path: Path
) -> None:
    """An exception escaping the run stamps `failed`; a row left `running` by a
    SIGKILLed process is flipped to `interrupted` by the next run for the AOI."""
    import pytest

    from forest_sentinel.models import PipelineRun

    class DyingStorage(FakeStorage):
        def export_images(self, requests: Any) -> list[Path | StorageError]:
            raise RuntimeError("simulated crash")

    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)
    fake_ee = _fake_ee((1,))

    with pytest.raises(RuntimeError, match="simulated crash"):
        run_pipeline(
            db_session,
            aoi=aoi,
            since=date(2026, 1, 1),
            until=date(2026, 2, 1),
            methodology=methodology,
            storage=DyingStorage(tmp_path),
            ee_module=fake_ee,
        )
    failed_run = db_session.execute(select(PipelineRun)).scalar_one()
    assert failed_run.status == "failed"
    assert failed_run.finished_at is not None

    # Simulate a run killed without cleanup (SIGKILL / systemd timeout): the row
    # stays "running" until the next run, holding the lock, supersedes it.
    stale = PipelineRun(
        aoi_id=aoi.id,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="running",
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
    )
    db_session.add(stale)
    db_session.commit()

    run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        methodology=methodology,
        storage=FakeStorage(tmp_path),
        ee_module=fake_ee,
    )
    db_session.commit()
    db_session.refresh(stale)
    assert stale.status == "interrupted"
    statuses = db_session.execute(select(PipelineRun.status)).scalars().all()
    assert sorted(statuses) == ["failed", "interrupted", "succeeded"]


def test_methodology_change_records_a_warning_event(db_session: Session, tmp_path: Path) -> None:
    """A run under a different methodology than the AOI's previous run must record a
    prominent warning: the new lineage reuses nothing and re-exports the window."""
    from forest_sentinel.models import PipelineRun, PipelineRunEvent
    from tests.fakes import make_methodology

    aoi = make_aoi(db_session, name="Hallway AOI")
    first_methodology = make_methodology(db_session)
    fake_ee = _fake_ee((1,))

    def run(methodology: Any) -> Any:
        return run_pipeline(
            db_session,
            aoi=aoi,
            since=date(2026, 1, 1),
            until=date(2026, 2, 1),
            methodology=methodology,
            storage=FakeStorage(tmp_path),
            ee_module=fake_ee,
        )

    run(first_methodology)
    db_session.commit()

    second_methodology = make_methodology(
        db_session, version="auto-abc123", parameters={"delta_nbr_threshold": -0.15}
    )
    run(second_methodology)
    db_session.commit()

    runs = db_session.execute(select(PipelineRun).order_by(PipelineRun.id)).scalars().all()
    assert [r.methodology_version_id for r in runs] == [
        first_methodology.id,
        second_methodology.id,
    ]
    warnings = (
        db_session.execute(select(PipelineRunEvent).where(PipelineRunEvent.outcome == "warning"))
        .scalars()
        .all()
    )
    assert len(warnings) == 1
    assert warnings[0].run_id == runs[1].id
    assert "methodology changed" in (warnings[0].message or "")
    assert "delta_nbr_threshold" in (warnings[0].message or "")

    # Same methodology again: no new warning.
    run(second_methodology)
    db_session.commit()
    warning_count = len(
        db_session.execute(select(PipelineRunEvent).where(PipelineRunEvent.outcome == "warning"))
        .scalars()
        .all()
    )
    assert warning_count == 1


def test_candidate_extraction_vectorizes_over_the_clipped_region(
    db_session: Session, tmp_path: Path
) -> None:
    """End to end (#78): with a scene footprint available, reduceToVectors runs
    over scene ∩ AOI rather than the whole AOI."""
    from geoalchemy2.shape import to_shape
    from shapely.geometry import shape

    footprint = {
        "type": "Polygon",
        "coordinates": [[[0.5, 0.5], [2.0, 0.5], [2.0, 2.0], [0.5, 2.0], [0.5, 0.5]]],
    }
    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)
    fake_ee = FakeEarthEngine(
        scenes={"NASA/HLS/HLSL30/v002": [_scene(day) for day in (1, 2)]},
        features=[_CANDIDATE_FEATURE],
        valid_fraction=0.95,
        footprint=footprint,
    )

    run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        methodology=methodology,
        storage=FakeStorage(tmp_path),
        ee_module=fake_ee,
    )
    db_session.commit()

    expected = to_shape(aoi.geometry).intersection(shape(footprint))
    assert len(fake_ee.calls) == 1  # scene-1 has no baseline; scene-2 vectorizes
    assert shape(fake_ee.calls[0]["region"]).equals(expected)


def test_candidate_extraction_falls_back_to_the_aoi_region(
    db_session: Session, tmp_path: Path
) -> None:
    from geoalchemy2.shape import to_shape
    from shapely.geometry import shape

    aoi = make_aoi(db_session, name="Hallway AOI")
    methodology = make_methodology(db_session)
    fake_ee = _fake_ee((1, 2))  # no footprint -> whole-AOI fallback everywhere

    run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        methodology=methodology,
        storage=FakeStorage(tmp_path),
        ee_module=fake_ee,
    )
    db_session.commit()

    assert len(fake_ee.calls) == 1
    assert shape(fake_ee.calls[0]["region"]).equals(to_shape(aoi.geometry))
