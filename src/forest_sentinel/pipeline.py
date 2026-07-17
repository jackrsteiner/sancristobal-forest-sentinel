"""End-to-end orchestration: discover → indices → change → candidates → events.

Because compute runs in Earth Engine, each export is an asynchronous task; the storage
seam blocks and polls each export to completion before the dependent step, so a single
``run_pipeline`` call drives the whole thread synchronously (a submit-and-return mode is a
later bead if needed). Export failures are isolated per observation: a failing scene is
skipped (and counted in the summary) instead of starving the rest of the run, so one
persistently bad export cannot zero out a scheduled window. Event tracking (Slice 2)
runs as the final stage. This module is pure orchestration over the building blocks and
is fully injectable, so the hallway test runs it against stubbed EE/storage.
"""

import contextlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forest_sentinel import candidates, change, earthengine, events, indices, runlog
from forest_sentinel.earthengine import EarthEngineError
from forest_sentinel.hls import discover_observations
from forest_sentinel.models import Aoi, MethodologyVersion, Observation
from forest_sentinel.storage import Storage, StorageError

logger = logging.getLogger(__name__)

# Candidates are extracted from the ΔNBR product (NBR drop = disturbance).
CANDIDATE_CHANGE_TYPE = "delta_nbr"

# Namespace for the per-AOI advisory run lock (arbitrary app-wide constant).
AOI_RUN_LOCK_CLASS = 0x0F5


def _acquire_aoi_run_lock(session: Session, aoi_id: int) -> None:
    """Serialize pipeline runs per AOI for the duration of the run.

    Discovery is race-safe on its own (ON CONFLICT), but the later upserts
    (quality_mask, index/change rasters, candidate replacement) are read-then-write:
    a manual run alongside the systemd timer would hit duplicate-key errors or double
    candidate sets. The lock is **session-scoped** (not transaction-scoped): the run
    checkpoints its progress with commits after each observation chunk (#77), and a
    transaction-scoped lock would be released by the first of those commits. It is
    held by the session's database connection until `_release_aoi_run_lock` (or
    disconnect — a killed run releases it automatically). The CLI binds the session
    to a single pinned connection so commits cannot migrate it to another one.
    """
    session.execute(select(func.pg_advisory_lock(AOI_RUN_LOCK_CLASS, aoi_id)))


def _release_aoi_run_lock(session: Session, aoi_id: int) -> None:
    session.execute(select(func.pg_advisory_unlock(AOI_RUN_LOCK_CLASS, aoi_id)))


# NBR + NDVI per observation.
_EXPORTS_PER_OBSERVATION = 2


def _chunked(items: list[Observation], size: int) -> list[list[Observation]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _submit_event(
    recorder: runlog.RunRecorder, stage: str, batch_index: int, batch_total: int
) -> Callable[[int], None]:
    """The ``on_export_submit`` hook for one batch.

    Fires at actual Earth Engine submit time (a fully reused batch submits
    nothing); the committed event makes the pending batch visible in the
    dashboard while the run waits on the EE queue.
    """

    def on_submit(exports: int) -> None:
        recorder.record(
            stage, "submitted", batch_index=batch_index, batch_total=batch_total, exports=exports
        )

    return on_submit


@dataclass(frozen=True)
class PipelineSummary:
    """Per-stage counts from one pipeline run."""

    observations_discovered: int
    observations_recorded: int
    observations_skipped: int
    index_rasters: int
    change_rasters: int
    candidates: int
    events_created: int
    event_observations: int
    export_failures: int = 0  # observations skipped because an EE export failed
    index_rasters_reused: int = 0  # persisted by an earlier run; no export submitted
    change_rasters_reused: int = 0


def run_pipeline(
    session: Session,
    *,
    aoi: Aoi,
    since: date,
    until: date,
    methodology: MethodologyVersion,
    storage: Storage,
    baseline_window: int = change.DEFAULT_BASELINE_WINDOW,
    threshold: float | None = None,
    min_area_m2: float | None = None,
    scale: int = indices.DEFAULT_SCALE_METERS,
    max_concurrent_exports: int = 1,
    ee_module: Any = earthengine,
) -> PipelineSummary:
    """Run discover → indices → change → candidates → events for one AOI and window.

    Progress is **checkpointed**: the session is committed after each observation
    chunk in the index stage and after each observation's change/candidate stage,
    so a run killed partway (systemd timeout, SIGTERM) resumes on the next
    invocation — already-persisted artifacts are reused (#77) instead of
    re-exported. ``max_concurrent_exports`` bounds how many Earth Engine batch
    tasks are put in flight together (#79).

    Progress is also **recorded**: a ``pipeline_run`` row plus per-batch
    ``pipeline_run_event`` rows (submit/success/failure, labelled
    ``batch i/N``), committed as they happen so the dashboard shows the run
    live; the same events are mirrored to the log (see ``runlog.py``).
    """
    _acquire_aoi_run_lock(session, aoi.id)
    try:
        # The run row is created (and any stale "running" row for this AOI marked
        # interrupted) while holding the lock, so exactly one live run exists per AOI.
        recorder = runlog.start_run(
            session, aoi=aoi, since=since, until=until, methodology=methodology
        )
        try:
            summary = _run_pipeline_locked(
                session,
                aoi=aoi,
                since=since,
                until=until,
                methodology=methodology,
                storage=storage,
                baseline_window=baseline_window,
                threshold=threshold,
                min_area_m2=min_area_m2,
                scale=scale,
                max_concurrent_exports=max_concurrent_exports,
                ee_module=ee_module,
                recorder=recorder,
            )
        except Exception:
            # The escaping error may have aborted the transaction; discard any
            # partial state so the terminal status can still be stamped.
            with contextlib.suppress(Exception):
                session.rollback()
            recorder.finish(runlog.STATUS_FAILED)
            raise
        status = runlog.STATUS_PARTIAL if summary.export_failures else runlog.STATUS_SUCCEEDED
        recorder.finish(status, summary=asdict(summary))
        return summary
    finally:
        _release_aoi_run_lock(session, aoi.id)


def _run_pipeline_locked(
    session: Session,
    *,
    aoi: Aoi,
    since: date,
    until: date,
    methodology: MethodologyVersion,
    storage: Storage,
    baseline_window: int,
    threshold: float | None,
    min_area_m2: float | None,
    scale: int,
    max_concurrent_exports: int,
    ee_module: Any,
    recorder: runlog.RunRecorder,
) -> PipelineSummary:
    region = mapping(to_shape(aoi.geometry))

    discovery = discover_observations(session, aoi, since=since, until=until, ee_module=ee_module)
    session.commit()
    recorder.record(
        "discovery",
        "info",
        message=(
            f"{discovery.discovered} discovered, {discovery.recorded} recorded, "
            f"{discovery.skipped} skipped"
        ),
    )

    # Only the window's observations are (re)processed; without this filter every
    # scheduled run would re-export the AOI's entire history. The trailing baseline
    # still draws on all prior observations (change.py queries them itself).
    observations = list(
        session.execute(
            select(Observation)
            .where(Observation.aoi_id == aoi.id)
            .where(Observation.acquired_at >= datetime.combine(since, time.min, tzinfo=UTC))
            .where(Observation.acquired_at < datetime.combine(until, time.min, tzinfo=UTC))
            .order_by(Observation.acquired_at)
        )
        .scalars()
        .all()
    )

    # One bad export must not starve the run: failing observations are skipped and
    # counted; already-persisted rows for them are consistent (upserts) and the next
    # run retries.
    export_failures = 0
    failed_observation_ids: set[int] = set()

    index_count = 0
    index_reused = 0
    chunk_size = max(1, max_concurrent_exports // _EXPORTS_PER_OBSERVATION)
    chunks = _chunked(observations, chunk_size)
    index_batches = len(chunks)
    recorder.record(
        "index",
        "info",
        message=f"{index_batches} batches over {len(observations)} observations",
    )
    for batch_index, chunk in enumerate(chunks, start=1):
        outcome = indices.compute_indices_for_observations(
            session,
            aoi=aoi,
            observations=chunk,
            methodology=methodology,
            storage=storage,
            scale=scale,
            ee_module=ee_module,
            on_export_submit=_submit_event(recorder, "index", batch_index, index_batches),
        )
        index_count += outcome.exported
        index_reused += outcome.reused
        for observation in chunk:
            if observation.id in outcome.failures:
                export_failures += 1
                failed_observation_ids.add(observation.id)
                logger.warning(
                    "skipping observation %s: index export failed (%s)",
                    observation.source_scene_id,
                    outcome.failures[observation.id],
                )
        # Checkpoint: a later kill (timeout, SIGTERM) keeps this chunk's artifacts.
        # The batch outcome event rides the same commit via recorder.record.
        recorder.record(
            "index",
            "failed" if outcome.failures else "succeeded",
            batch_index=batch_index,
            batch_total=index_batches,
            exports=outcome.exported,
            message=(
                f"{outcome.exported} exported, {outcome.reused} reused, "
                f"{len(outcome.failures)} failed"
            ),
        )

    change_count = 0
    change_reused = 0
    candidate_count = 0
    surviving = [obs for obs in observations if obs.id not in failed_observation_ids]
    change_batches = len(surviving)
    recorder.record("change", "info", message=f"{change_batches} batches (one per observation)")
    for batch_index, observation in enumerate(surviving, start=1):
        try:
            products = change.compute_change_products_for_observation(
                session,
                aoi=aoi,
                observation=observation,
                methodology=methodology,
                storage=storage,
                baseline_window=baseline_window,
                scale=scale,
                ee_module=ee_module,
                on_export_submit=_submit_event(recorder, "change", batch_index, change_batches),
            )
            observation_candidates = 0
            for product in products:
                if product.change_type != CANDIDATE_CHANGE_TYPE:
                    continue
                if product.delta_image is None:
                    # Frozen or reused: the candidate set already exists (it commits
                    # in the same checkpoint as the raster) — count it, don't
                    # re-extract it.
                    observation_candidates += candidates.count_candidates_for_change_raster(
                        session, product.change_raster.id
                    )
                    continue
                observation_candidates += len(
                    candidates.extract_candidates_for_change_raster(
                        session,
                        change_raster=product.change_raster,
                        delta_image=product.delta_image,
                        region=region,
                        scale=scale,
                        threshold=threshold,
                        min_area_m2=min_area_m2,
                        ee_module=ee_module,
                    )
                )
            change_count += len(products)
            observation_reused = sum(1 for product in products if product.reused)
            change_reused += observation_reused
            candidate_count += observation_candidates
            # Checkpoint the rasters together with their candidates: reuse of a
            # change raster on a later run therefore implies its candidates exist.
            # The batch outcome event rides the same commit via recorder.record.
            exported = sum(1 for product in products if product.delta_image is not None)
            recorder.record(
                "change",
                "succeeded",
                batch_index=batch_index,
                batch_total=change_batches,
                exports=exported,
                message=(
                    f"{exported} exported, {observation_reused} reused, "
                    f"{observation_candidates} candidates"
                ),
            )
        except (StorageError, EarthEngineError) as exc:
            export_failures += 1
            logger.warning(
                "skipping observation %s: change/candidate stage failed (%s)",
                observation.source_scene_id,
                exc,
            )
            # Discard this observation's partial change-stage state so a committed
            # (non-frozen) change raster always implies extracted candidates; its
            # orphaned COGs are re-exported on retry (the exists-check needs the row).
            # Rollback happens BEFORE recording so the failure event survives it.
            session.rollback()
            recorder.record(
                "change",
                "failed",
                batch_index=batch_index,
                batch_total=change_batches,
                message=str(exc),
            )

    tracking = events.track_events_for_aoi(session, aoi=aoi)
    recorder.record(
        "events",
        "info",
        message=(
            f"{tracking.events_created} events created, "
            f"{tracking.observations_added} observations tracked"
        ),
    )

    return PipelineSummary(
        observations_discovered=discovery.discovered,
        observations_recorded=discovery.recorded,
        observations_skipped=discovery.skipped,
        index_rasters=index_count,
        change_rasters=change_count,
        candidates=candidate_count,
        events_created=tracking.events_created,
        event_observations=tracking.observations_added,
        export_failures=export_failures,
        index_rasters_reused=index_reused,
        change_rasters_reused=change_reused,
    )
