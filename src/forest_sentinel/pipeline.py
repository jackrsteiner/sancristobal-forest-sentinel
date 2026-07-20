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
from pathlib import Path
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forest_sentinel import (
    candidates,
    change,
    confidence,
    context,
    earthengine,
    events,
    forestmask,
    indices,
    localextract,
    radar,
    reproduce,
    runlog,
    sentinel1,
)
from forest_sentinel.earthengine import EarthEngineError
from forest_sentinel.hls import discover_observations
from forest_sentinel.models import Aoi, ChangeRaster, IndexRaster, MethodologyVersion, Observation
from forest_sentinel.storage import CogKey, Storage, StorageError

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

# A majority of the window's cataloged COGs missing on disk points at a moved
# FOREST_SENTINEL_COG_ROOT or a repointed database — not routine churn — and the
# run is about to silently re-export all of them (config-inventory Finding 5).
_MISSING_COG_WARNING_FRACTION = 0.5


def _chunked(items: list[Observation], size: int) -> list[list[Observation]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _missing_cog_counts(
    session: Session,
    *,
    observations: list[Observation],
    raster_lineage_ids: list[int],
) -> tuple[int, int]:
    """(missing, cataloged) counts of the window's on-disk raster files.

    Only rows the run could actually reuse are counted: this window's
    observations under the run's raster lineages. Missing files are
    re-exported per-row anyway (indices/change check per artifact); this
    preflight exists to make a *wholesale* miss loud before EE quota is spent.
    """
    observation_ids = [observation.id for observation in observations]
    if not observation_ids or not raster_lineage_ids:
        return (0, 0)
    paths: list[str] = []
    for model in (IndexRaster, ChangeRaster):
        paths.extend(
            session.execute(
                select(model.cog_path)
                .where(model.observation_id.in_(observation_ids))
                .where(model.raster_lineage_id.in_(raster_lineage_ids))
            )
            .scalars()
            .all()
        )
    missing = sum(1 for path in paths if not Path(path).exists())
    return (missing, len(paths))


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
    events_resolved: int = 0  # ongoing events auto-resolved this run (quiet + clear look)
    confidence_assessments: int = 0  # appended this run (unchanged conclusions skipped)
    radar_change_rasters: int = 0  # VV dB deltas produced (radar stage, when enabled)
    radar_change_rasters_reused: int = 0
    radar_candidates: int = 0
    context_relations: int = 0  # event-context rows recorded (replaced per run)


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
    resolved_after_days: int = events.DEFAULT_RESOLVED_AFTER_DAYS,
    radar_methodology: MethodologyVersion | None = None,
    context_buffer_m: float = context.DEFAULT_CONTEXT_BUFFER_M,
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
                resolved_after_days=resolved_after_days,
                radar_methodology=radar_methodology,
                context_buffer_m=context_buffer_m,
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


def _candidates_for_product(
    session: Session,
    *,
    product: change.ChangeProduct,
    methodology: MethodologyVersion,
    aoi: Aoi,
    storage: Storage,
    fallback_region: Any,
    scale: int,
    threshold: float | None,
    min_area_m2: float | None,
    ee_module: Any,
) -> int:
    """Candidate count for one change product under the run's methodology.

    A product with a live delta extracts directly. A frozen/reused product
    (``delta_image is None``) usually already has this methodology's candidates
    — count them. When it does not, the raster was minted under a different
    detection layer of the same raster lineage (Finding 1) and is re-extracted
    with **no new export**: preferably zero-EE from the stored COG (Finding 2),
    else from a delta graph rebuilt out of recorded provenance.
    """
    raster = product.change_raster
    delta_image = product.delta_image
    region = product.region if product.region is not None else fallback_region
    if delta_image is None:
        if candidates.has_extraction(session, raster.id, methodology.id):
            return candidates.count_candidates_for_change_raster(session, raster.id, methodology.id)
        local = _extract_locally(
            session,
            raster=raster,
            methodology=methodology,
            aoi=aoi,
            storage=storage,
            scale=scale,
            threshold=threshold,
            min_area_m2=min_area_m2,
            ee_module=ee_module,
        )
        if local is not None:
            return local
        delta_image, region, _, _ = reproduce.rebuild_change_delta(
            session, raster=raster, ee_module=ee_module
        )
    return len(
        candidates.extract_candidates_for_change_raster(
            session,
            change_raster=raster,
            methodology=methodology,
            delta_image=delta_image,
            region=region,
            scale=scale,
            threshold=threshold,
            min_area_m2=min_area_m2,
            ee_module=ee_module,
        )
    )


def _extract_locally(
    session: Session,
    *,
    raster: ChangeRaster,
    methodology: MethodologyVersion,
    aoi: Aoi,
    storage: Storage,
    scale: int,
    threshold: float | None,
    min_area_m2: float | None,
    ee_module: Any,
) -> int | None:
    """Finding 2: zero-EE re-extraction from the stored COG; ``None`` = fall back.

    Requires the COG on disk and — when the methodology has a forest mask — a
    local mask COG (exported once per AOI + mask config, then reused forever).
    Any local failure logs and falls back to the EE rebuild path rather than
    failing the observation.
    """
    if not Path(raster.cog_path).exists():
        return None
    if raster.change_type == CANDIDATE_CHANGE_TYPE:
        resolved_threshold = candidates.resolve_threshold(methodology, threshold)
    elif threshold is not None:
        resolved_threshold = threshold
    else:  # radar callers always resolve; refuse to guess a dB cutoff here
        return None
    mask_cog_path: str | None = None
    mask_config = forestmask.resolve_config(methodology, None)
    if mask_config.get("source") != forestmask.SOURCE_NONE:
        mask_cog_path = _ensure_mask_cog(
            aoi, mask_config, storage=storage, scale=scale, ee_module=ee_module
        )
        if mask_cog_path is None:
            return None
    try:
        features = localextract.extract_features_from_cog(
            raster.cog_path,
            threshold=resolved_threshold,
            min_area_m2=candidates.resolve_min_area(methodology, min_area_m2),
            mask_cog_path=mask_cog_path,
        )
    except localextract.LocalExtractError as exc:
        logger.warning("local extraction fell back to Earth Engine: %s", exc)
        return None
    return len(
        candidates.persist_candidate_features(
            session,
            change_raster=raster,
            methodology=methodology,
            features=features,
            scale=scale,
            min_area_m2=min_area_m2,
        )
    )


def _ensure_mask_cog(
    aoi: Aoi,
    mask_config: dict[str, Any],
    *,
    storage: Storage,
    scale: int,
    ee_module: Any,
) -> str | None:
    """The local forest-mask COG for (AOI, mask config), exporting it on first use.

    Static reference data, so the CogKey date component is ``static`` — the
    retention pruner only deletes files under parseable ISO date directories,
    so the mask survives pruning and is exported exactly once.
    """
    key = CogKey(
        aoi=f"{aoi.id}-{aoi.name}",
        product="forest_mask",
        date="static",
        filename=localextract.mask_cog_key_filename(mask_config),
    )
    path = storage.path_for(key)
    if path.exists():
        return str(path)
    try:
        mask_image = forestmask.build_mask(mask_config, ee_module=ee_module)
        if mask_image is None:
            return None
        region = mapping(to_shape(aoi.geometry))
        return str(storage.export_image(mask_image, key, scale=scale, region=region))
    except (StorageError, EarthEngineError) as exc:
        logger.warning("forest-mask COG export failed; falling back to EE extraction: %s", exc)
        return None


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
    resolved_after_days: int,
    radar_methodology: MethodologyVersion | None,
    context_buffer_m: float,
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

    lineage_ids = [methodology.raster_lineage_id] + (
        [radar_methodology.raster_lineage_id] if radar_methodology is not None else []
    )
    missing_cogs, cataloged_cogs = _missing_cog_counts(
        session, observations=observations, raster_lineage_ids=lineage_ids
    )
    if cataloged_cogs and missing_cogs / cataloged_cogs >= _MISSING_COG_WARNING_FRACTION:
        recorder.record(
            "run",
            "warning",
            message=(
                f"{missing_cogs} of {cataloged_cogs} cataloged COGs for this window are "
                "missing on disk (moved FOREST_SENTINEL_COG_ROOT? database repointed? "
                "over-aggressive prune?) — reuse will miss and they will be re-exported "
                "from Earth Engine"
            ),
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
                # Vectorize over the same scene ∩ AOI region the delta was
                # exported with (#78); whole-AOI is the defensive fallback.
                observation_candidates += _candidates_for_product(
                    session,
                    product=product,
                    methodology=methodology,
                    aoi=aoi,
                    storage=storage,
                    fallback_region=region,
                    scale=scale,
                    threshold=threshold,
                    min_area_m2=min_area_m2,
                    ee_module=ee_module,
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

    radar_count = radar_reused = radar_candidates = 0
    if radar_methodology is not None:
        radar_count, radar_reused, radar_candidates, radar_failures = _run_radar_stage(
            session,
            aoi=aoi,
            since=since,
            until=until,
            methodology=radar_methodology,
            storage=storage,
            baseline_window=baseline_window,
            scale=scale,
            ee_module=ee_module,
            recorder=recorder,
        )
        export_failures += radar_failures

    tracking = events.track_events_for_aoi(session, aoi=aoi)
    # Lifecycle after tracking: extension has already reopened any re-detected
    # events, so what remains quiet-past-window (with a clear later look) resolves.
    events_resolved = events.apply_resolved_lifecycle(
        session, aoi=aoi, resolved_after_days=resolved_after_days
    )
    recorder.record(
        "events",
        "info",
        message=(
            f"{tracking.events_created} events created, "
            f"{tracking.observations_added} observations tracked, "
            f"{events_resolved} resolved"
        ),
    )

    # Confidence after lifecycle: assessments see final statuses and dates.
    assessments = confidence.assess_events_for_aoi(
        session, aoi=aoi, pipeline_run_id=recorder.run.id
    )
    recorder.record(
        "confidence",
        "info",
        message=(
            f"{assessments} assessment(s) appended "
            f"(rule {confidence.RULE_VERSION}; unchanged conclusions skipped)"
        ),
    )

    # Context relations last: a derived view over final event footprints,
    # replaced wholesale each run from whatever layers exist right now.
    context_relations = context.compute_event_context(session, aoi=aoi, buffer_m=context_buffer_m)
    recorder.record(
        "context",
        "info",
        message=f"{context_relations} context relation(s) recorded (replaced per run)",
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
        events_resolved=events_resolved,
        confidence_assessments=assessments,
        radar_change_rasters=radar_count,
        radar_change_rasters_reused=radar_reused,
        radar_candidates=radar_candidates,
        context_relations=context_relations,
    )


def _run_radar_stage(
    session: Session,
    *,
    aoi: Aoi,
    since: date,
    until: date,
    methodology: MethodologyVersion,
    storage: Storage,
    baseline_window: int,
    scale: int,
    ee_module: Any,
    recorder: runlog.RunRecorder,
) -> tuple[int, int, int, int]:
    """Discovery → VV dB deltas → radar candidates, under the radar methodology.

    Mirrors the optical change stage's shape: one batch per observation,
    per-observation failure isolation with a rollback before the failure event
    (so a committed change raster always implies extracted candidates), and
    frozen/reused deltas counting their existing candidates. Radar candidates
    land in the same ``disturbance_candidate`` table; event tracking downstream
    is methodology-scoped, so they form their own lineages.
    """
    discovery = sentinel1.discover_radar_observations(
        session, aoi, since=since, until=until, ee_module=ee_module
    )
    recorder.record(
        "radar",
        "info",
        message=(
            f"{discovery.discovered} scenes discovered, {discovery.recorded} recorded, "
            f"{discovery.skipped} skipped"
        ),
    )
    observations = list(
        session.execute(
            select(Observation)
            .where(Observation.aoi_id == aoi.id)
            .where(Observation.sensor == sentinel1.S1_SENSOR)
            .where(Observation.acquired_at >= datetime.combine(since, time.min, tzinfo=UTC))
            .where(Observation.acquired_at < datetime.combine(until, time.min, tzinfo=UTC))
            .order_by(Observation.acquired_at)
        )
        .scalars()
        .all()
    )

    threshold = radar.resolve_db_threshold(methodology)
    min_area = candidates.resolve_min_area(methodology, None)
    delta_count = reused_count = candidate_count = failures = 0
    batches = len(observations)
    recorder.record("radar", "info", message=f"{batches} batches (one per scene)")
    for batch_index, observation in enumerate(observations, start=1):
        try:
            products = radar.compute_radar_change_for_observation(
                session,
                aoi=aoi,
                observation=observation,
                methodology=methodology,
                storage=storage,
                baseline_window=baseline_window,
                scale=scale,
                ee_module=ee_module,
                on_export_submit=_submit_event(recorder, "radar", batch_index, batches),
            )
            observation_candidates = 0
            for product in products:
                observation_candidates += _candidates_for_product(
                    session,
                    product=product,
                    methodology=methodology,
                    aoi=aoi,
                    storage=storage,
                    fallback_region=mapping(to_shape(aoi.geometry)),
                    scale=scale,
                    threshold=threshold,
                    min_area_m2=min_area,
                    ee_module=ee_module,
                )
            delta_count += len(products)
            reused_count += sum(1 for product in products if product.reused)
            candidate_count += observation_candidates
            exported = sum(1 for product in products if product.delta_image is not None)
            recorder.record(
                "radar",
                "succeeded",
                batch_index=batch_index,
                batch_total=batches,
                exports=exported,
                message=(
                    f"{exported} exported, "
                    f"{sum(1 for p in products if p.reused)} reused, "
                    f"{observation_candidates} candidates"
                ),
            )
        except (StorageError, EarthEngineError) as exc:
            failures += 1
            logger.warning(
                "skipping radar scene %s: change/candidate stage failed (%s)",
                observation.source_scene_id,
                exc,
            )
            session.rollback()
            recorder.record(
                "radar",
                "failed",
                batch_index=batch_index,
                batch_total=batches,
                message=str(exc),
            )
    return delta_count, reused_count, candidate_count, failures
