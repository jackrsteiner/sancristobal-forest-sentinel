"""Run-progress recording: the DB-backed, journald-mirrored visibility layer.

A :class:`RunRecorder` owns one ``pipeline_run`` row and appends
``pipeline_run_event`` rows as the run progresses, committing each event so the
dashboard (a separate session under READ COMMITTED) sees progress live — most
importantly *while* the run is blocked waiting on Earth Engine's batch queue.
Every event is mirrored to the log with a stable label carrying the run's start
datetime and the batch position::

    run 2026-07-16T09:58:48Z · index batch 3/6: submitted 4 exports to Earth Engine

Commit discipline: ``record`` commits the session. Callers only invoke it at
points where everything pending is safe to persist (the pipeline's checkpoint
boundaries, or — for submit events — before the stage writes any rasters), so
these commits are always valid checkpoints themselves.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

from geoalchemy2.shape import to_shape
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from forest_sentinel.models import Aoi, MethodologyVersion, PipelineRun, PipelineRunEvent

logger = logging.getLogger(__name__)

# Terminal statuses for PipelineRun.status; "running" is the only live one.
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_PARTIAL = "partial"  # finished, but some observations failed to export
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"  # killed without cleanup (SIGKILL, timeout)


def start_run(
    session: Session,
    *,
    aoi: Aoi,
    since: date,
    until: date,
    methodology: MethodologyVersion | None = None,
) -> "RunRecorder":
    """Create the ``running`` row for a new run and return its recorder.

    Any earlier row for this AOI still marked ``running`` is flipped to
    ``interrupted`` first: the caller holds the per-AOI advisory lock, so a
    lingering ``running`` row can only belong to a run that died without
    cleanup (SIGKILL, systemd timeout past ``TimeoutStopSec``).

    If the run's methodology differs from the AOI's previous run's (parameters
    changed, or a template update bumped the EE script version — the methodology
    is content-addressed), a warning event is recorded: the new lineage starts
    cold, so nothing from the previous methodology is reusable and the run will
    re-export the whole window.

    The AOI's geometry hash is stamped on every run. AOI geometry is instance
    data, not a methodology input, so editing a footprint changes discovery
    scope silently — the stamp plus a warning event when it differs from the
    previous run's makes the edit visible in run history (config-inventory
    Finding 8). History recorded under the old footprint is untouched.
    """
    previous = session.execute(
        select(PipelineRun)
        .where(PipelineRun.aoi_id == aoi.id)
        .order_by(PipelineRun.started_at.desc(), PipelineRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    session.execute(
        update(PipelineRun)
        .where(PipelineRun.aoi_id == aoi.id)
        .where(PipelineRun.status == STATUS_RUNNING)
        .values(status=STATUS_INTERRUPTED)
    )
    geometry_hash = aoi_geometry_hash(aoi)
    run = PipelineRun(
        aoi_id=aoi.id,
        methodology_version_id=methodology.id if methodology is not None else None,
        started_at=datetime.now(UTC),
        status=STATUS_RUNNING,
        since=since,
        until=until,
        aoi_geometry_hash=geometry_hash,
    )
    session.add(run)
    session.commit()
    recorder = RunRecorder(session=session, run=run)

    if (
        methodology is not None
        and previous is not None
        and previous.methodology_version_id is not None
        and previous.methodology_version_id != methodology.id
    ):
        recorder.record(
            "run",
            "warning",
            message=_methodology_change_message(session, previous, methodology),
        )
    if (
        previous is not None
        and previous.aoi_geometry_hash is not None
        and previous.aoi_geometry_hash != geometry_hash
    ):
        recorder.record(
            "run",
            "warning",
            message=(
                f"AOI geometry changed since the previous run "
                f"({previous.aoi_geometry_hash} → {geometry_hash}) — discovery scope and "
                "export regions change from this run onward; observations, candidates, "
                "and events recorded under the old footprint are unchanged"
            ),
        )
    return recorder


def aoi_geometry_hash(aoi: Aoi) -> str:
    """Deterministic short hash of the AOI footprint (WKB SHA-256)."""
    return hashlib.sha256(to_shape(aoi.geometry).wkb).hexdigest()[:12]


def _methodology_change_message(
    session: Session, previous: PipelineRun, methodology: MethodologyVersion
) -> str:
    """Human-readable diff of what changed between two methodology versions."""
    old = session.get(MethodologyVersion, previous.methodology_version_id)
    changes = "parameters unavailable"
    label = "?"
    if old is not None:
        label = f"{old.name} @ {old.version}"
        keys = sorted(set(old.parameters) | set(methodology.parameters))
        diffs = [
            f"{key}: {old.parameters.get(key)!r} → {methodology.parameters.get(key)!r}"
            for key in keys
            if old.parameters.get(key) != methodology.parameters.get(key)
        ]
        changes = "; ".join(diffs) or "no parameter diff"
    return (
        f"methodology changed since the previous run ({label} → "
        f"{methodology.name} @ {methodology.version}: {changes}) — artifacts from the "
        "previous methodology are not reusable; expect a full re-export of the window"
    )


@dataclass
class RunRecorder:
    """Appends progress events for one run and stamps its terminal status."""

    session: Session
    run: PipelineRun

    @property
    def label(self) -> str:
        """The run's log label: its start datetime, UTC, seconds precision."""
        return f"run {self.run.started_at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"

    def record(
        self,
        stage: str,
        outcome: str,
        *,
        batch_index: int | None = None,
        batch_total: int | None = None,
        exports: int | None = None,
        message: str | None = None,
    ) -> None:
        """Persist one progress event (committing it) and mirror it to the log."""
        self.session.add(
            PipelineRunEvent(
                run_id=self.run.id,
                stage=stage,
                batch_index=batch_index,
                batch_total=batch_total,
                exports=exports,
                outcome=outcome,
                message=message,
            )
        )
        self.session.commit()

        where = stage
        if batch_index is not None and batch_total is not None:
            where = f"{stage} batch {batch_index}/{batch_total}"
        if outcome == "submitted":
            what = f"submitted {exports} export{'s' if exports != 1 else ''} to Earth Engine"
        else:
            what = outcome if message is None else f"{outcome} — {message}"
        level = logging.WARNING if outcome in ("failed", "warning") else logging.INFO
        logger.log(level, "%s · %s: %s", self.label, where, what)

    def finish(self, status: str, summary: dict[str, object] | None = None) -> None:
        """Stamp the run's terminal status (best-effort: never masks the run's error)."""
        try:
            # Re-select rather than mutate: on the failure path the session was
            # rolled back and the identity-mapped instance may be stale/detached.
            run = self.session.execute(
                select(PipelineRun).where(PipelineRun.id == self.run.id)
            ).scalar_one()
            run.status = status
            run.finished_at = datetime.now(UTC)
            run.summary = summary
            self.session.commit()
            logger.info("%s · run: %s", self.label, status)
        except Exception:  # pragma: no cover - dead-connection safety net
            logger.warning("%s · run: could not record terminal status %r", self.label, status)
