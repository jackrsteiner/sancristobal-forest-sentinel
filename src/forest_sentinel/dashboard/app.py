"""FastAPI dashboard backend.

Read-only JSON/GeoJSON endpoints over the PostGIS catalog that let a user answer the six
core README "Product Deliverable" questions for a disturbance event: where (geometry), when
first detected, size (footprint area), expansion rate (footprint growth over the timeline),
status, and supporting evidence (the source change rasters); the README's remaining
deliverable questions arrive with later slices (see ``docs/architecture.md`` §2). The map UI
(a static Leaflet page) consumes these endpoints.

The database session is provided by the ``get_session`` dependency so tests can override it
with a transactional test session; no Earth Engine or storage access happens here.
"""

import json
import os
import subprocess
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from geoalchemy2 import Geography
from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import Engine, cast, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forest_sentinel.aoi import (
    AOIS_DIR_ENV_VAR,
    DEFAULT_AOIS_DIR,
    AoiConfigError,
    load_aoi_config_document,
    persist_aoi,
)
from forest_sentinel.db import get_engine
from forest_sentinel.events import footprint_area_m2
from forest_sentinel.models import (
    REVIEW_OPINIONS,
    Aoi,
    ChangeRaster,
    DisturbanceCandidate,
    DisturbanceEvent,
    EventObservation,
    ManualReview,
    MethodologyVersion,
    PipelineRun,
    PipelineRunEvent,
)
from forest_sentinel.runlog import STATUS_INTERRUPTED, STATUS_RUNNING
from forest_sentinel.storage import StorageError, sanitize_path_component

# How many runs / progress events the runs endpoints return.
RUNS_LIMIT = 20
RUN_EVENTS_LIMIT = 50

# Set to "0" to disable uploads (vm_setup.sh does this when the dashboard port
# is opened to the world: a public dashboard must stay read-only).
AOI_UPLOADS_ENV_VAR = "FOREST_SENTINEL_AOI_UPLOADS"

# Set to "0" to disable the run-now trigger (vm_setup.sh does this when the
# dashboard port is opened to the world, same as uploads).
PIPELINE_TRIGGER_ENV_VAR = "FOREST_SENTINEL_PIPELINE_TRIGGER"

# Set to "0" to disable recording manual reviews (vm_setup.sh does this when
# the dashboard port is opened to the world — tunnel-as-auth means whoever can
# reach the dashboard can review, which must not include the open internet).
REVIEWS_ENV_VAR = "FOREST_SENTINEL_REVIEWS"
# The same systemd unit the daily timer fires; --no-block returns immediately
# and systemd merges a start into an already-running job, so repeated clicks
# are harmless (the per-AOI advisory lock backstops everything else). The `ofs`
# service user has passwordless sudo on the VM (vm_startup.sh).
PIPELINE_START_COMMAND = (
    "sudo",
    "systemctl",
    "start",
    "forest-sentinel-pipeline.service",
    "--no-block",
)
# Stop is synchronous (no --no-block): SIGTERM is fast, and a blocking result
# lets the endpoint report failures instead of guessing. The pipeline is
# checkpoint-committed and resume-safe, so a deliberate stop is exactly the
# kill it already tolerates (timeouts, SIGKILL) — committed work is kept and
# the next run (daily timer or Run now) resumes from the last checkpoint.
PIPELINE_STOP_COMMAND = (
    "sudo",
    "systemctl",
    "stop",
    "forest-sentinel-pipeline.service",
)


@lru_cache(maxsize=1)
def _engine() -> Engine:
    return get_engine()


def get_session() -> Iterator[Session]:
    """Yield a database session for one request (overridable in tests)."""
    with Session(_engine()) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]

_INDEX_HTML = Path(__file__).parent / "static" / "index.html"


def create_app() -> FastAPI:
    app = FastAPI(title="Open Forest Sentinel", description="Forest disturbance dashboard.")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        """The Leaflet map page that consumes the JSON/GeoJSON API."""
        return _INDEX_HTML.read_text()

    @app.get("/api/capabilities")
    def capabilities() -> dict[str, bool]:
        """Which write features this instance has enabled (all off = world-open)."""
        return {
            "aoi_uploads": os.environ.get(AOI_UPLOADS_ENV_VAR, "1") != "0",
            "pipeline_trigger": os.environ.get(PIPELINE_TRIGGER_ENV_VAR, "1") != "0",
            "reviews": os.environ.get(REVIEWS_ENV_VAR, "1") != "0",
        }

    @app.get("/api/aois")
    def list_aois(session: SessionDep) -> list[dict[str, Any]]:
        """AOIs with summary metrics (event counts)."""
        rows = session.execute(
            select(Aoi.id, Aoi.name, Aoi.geometry, func.count(DisturbanceEvent.id))
            .outerjoin(DisturbanceEvent, DisturbanceEvent.aoi_id == Aoi.id)
            .group_by(Aoi.id)
            .order_by(Aoi.name)
        ).all()
        return [
            {
                "id": aoi_id,
                "name": name,
                "event_count": event_count,
                # [min_lon, min_lat, max_lon, max_lat] — enough for the map to
                # zoom to the AOI without shipping its full boundary.
                "bbox": list(to_shape(geometry).bounds),
            }
            for aoi_id, name, geometry, event_count in rows
        ]

    @app.post("/api/aois", status_code=201)
    def upload_aoi(session: SessionDep, document: Annotated[Any, Body()]) -> dict[str, Any]:
        """Register a new AOI from an uploaded GeoJSON document.

        The document is the same single-feature GeoJSON accepted as a file; it is
        validated, written into the AOIs directory (so the scheduled run picks it
        up exactly like a committed file), and persisted as an ``aoi`` row (visible
        in the dropdown immediately; processed on the next pipeline run). Uploads
        are instance-local — commit the written file to the instance repo to
        survive a teardown/redeploy. A name that already exists is rejected (409):
        stored geometry is pinned to the name, so a new footprint needs a new name.

        The body must be JSON (not multipart): a cross-origin browser POST with
        ``Content-Type: application/json`` requires a CORS preflight, and no CORS
        middleware is configured — which structurally blocks CSRF while the
        dashboard is reachable through the SSH tunnel.
        """
        if os.environ.get(AOI_UPLOADS_ENV_VAR, "1") == "0":
            raise HTTPException(
                status_code=403, detail="AOI uploads are disabled on this dashboard"
            )
        try:
            config = load_aoi_config_document(document)
            filename = f"{sanitize_path_component(config.name)}.geojson"
        except (AoiConfigError, StorageError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        aois_dir = Path(os.environ.get(AOIS_DIR_ENV_VAR, DEFAULT_AOIS_DIR))
        target = aois_dir / filename
        if target.exists():
            raise HTTPException(
                status_code=409, detail=f"an AOI file named {filename!r} already exists"
            )
        aois_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, indent=2) + "\n")
        try:
            aoi = persist_aoi(session, config)
            session.commit()
        except IntegrityError:
            session.rollback()
            target.unlink(missing_ok=True)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"an AOI named {config.name!r} already exists; stored geometry is "
                    "pinned to the name — upload under a new name to monitor a new footprint"
                ),
            ) from None
        return {"id": aoi.id, "name": aoi.name, "file": str(target)}

    @app.post("/api/pipeline/run", status_code=202)
    def trigger_pipeline_run(_payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Start a pipeline run now — the same systemd unit the daily timer fires.

        Requires a JSON body (send ``{}``): like the AOI upload, the mandatory
        ``Content-Type: application/json`` makes a cross-origin browser POST
        need a CORS preflight that no configured middleware permits. The start
        is asynchronous; progress appears in the runs panel as the run commits
        its events. Repeated triggers are safe — systemd merges a start into a
        running job and the per-AOI advisory lock serializes runs.
        """
        if os.environ.get(PIPELINE_TRIGGER_ENV_VAR, "1") == "0":
            raise HTTPException(
                status_code=403, detail="pipeline triggering is disabled on this dashboard"
            )
        try:
            result = subprocess.run(  # noqa: S603 - fixed command, no user input
                PIPELINE_START_COMMAND, capture_output=True, text=True, check=False, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(
                status_code=502, detail=f"could not start the pipeline service: {exc}"
            ) from exc
        if result.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail=(
                    "could not start the pipeline service: "
                    f"{result.stderr.strip() or f'exit code {result.returncode}'}"
                ),
            )
        return {"detail": "pipeline run requested; progress appears in the runs panel"}

    @app.post("/api/pipeline/stop", status_code=202)
    def stop_pipeline_run(
        session: SessionDep, _payload: Annotated[dict[str, Any], Body()]
    ) -> dict[str, Any]:
        """Stop the executing pipeline run (all AOIs run inside one systemd unit).

        Guarded by the same knob as the run trigger — a world-open dashboard must
        expose neither. This is a stop, not a pause: committed checkpoints are
        kept and reused, and the daily timer still fires the next run. After a
        successful stop, rows still marked ``running`` are stamped
        ``interrupted`` so the runs panel tells the truth immediately instead of
        waiting for the next run start (or the stale-heartbeat threshold).
        """
        if os.environ.get(PIPELINE_TRIGGER_ENV_VAR, "1") == "0":
            raise HTTPException(
                status_code=403, detail="pipeline triggering is disabled on this dashboard"
            )
        try:
            result = subprocess.run(  # noqa: S603 - fixed command, no user input
                PIPELINE_STOP_COMMAND, capture_output=True, text=True, check=False, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(
                status_code=502, detail=f"could not stop the pipeline service: {exc}"
            ) from exc
        if result.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail=(
                    "could not stop the pipeline service: "
                    f"{result.stderr.strip() or f'exit code {result.returncode}'}"
                ),
            )
        stopped = session.execute(
            update(PipelineRun)
            .where(PipelineRun.status == STATUS_RUNNING)
            .values(status=STATUS_INTERRUPTED)
            .returning(PipelineRun.id)
        ).all()
        session.commit()
        return {"stopped_runs": len(stopped)}

    @app.get("/api/aois/{aoi_id}/events")
    def aoi_events(aoi_id: int, session: SessionDep) -> dict[str, Any]:
        """Events for an AOI as a GeoJSON FeatureCollection.

        One aggregate query: per-event observation count, latest detection area, and
        footprint area come back with the events instead of a timeline fetch per event.
        """
        if session.get(Aoi, aoi_id) is None:
            raise HTTPException(status_code=404, detail=f"AOI {aoi_id} not found")

        measurements = (
            select(
                EventObservation.event_id,
                EventObservation.area_m2,
                func.count().over(partition_by=EventObservation.event_id).label("count"),
                func.row_number()
                .over(
                    partition_by=EventObservation.event_id,
                    order_by=(EventObservation.observed_at.desc(), EventObservation.id.desc()),
                )
                .label("recency"),
            )
        ).subquery()
        latest = (
            select(measurements.c.event_id, measurements.c.area_m2, measurements.c.count)
            .where(measurements.c.recency == 1)
            .subquery()
        )
        rows = session.execute(
            select(
                DisturbanceEvent,
                func.ST_Area(cast(DisturbanceEvent.geometry, Geography)),
                func.coalesce(latest.c.count, 0),
                latest.c.area_m2,
                _latest_opinion_subquery(),
            )
            .outerjoin(latest, latest.c.event_id == DisturbanceEvent.id)
            .where(DisturbanceEvent.aoi_id == aoi_id)
            .order_by(DisturbanceEvent.first_detected_at)
        ).all()
        return {
            "type": "FeatureCollection",
            "features": [
                _event_feature(event, footprint_area, count, latest_area, latest_opinion)
                for event, footprint_area, count, latest_area, latest_opinion in rows
            ],
        }

    @app.get("/api/aois/{aoi_id}/runs")
    def aoi_runs(aoi_id: int, session: SessionDep) -> list[dict[str, Any]]:
        """Recent pipeline runs for an AOI, newest first.

        Runs commit their progress events live, so polling this endpoint shows
        an executing run advancing batch by batch; ``last_event_at`` lets the
        UI flag a ``running`` row whose run actually died (stale heartbeat).
        """
        if session.get(Aoi, aoi_id) is None:
            raise HTTPException(status_code=404, detail=f"AOI {aoi_id} not found")
        rows = session.execute(
            select(PipelineRun, MethodologyVersion, func.max(PipelineRunEvent.occurred_at))
            .outerjoin(PipelineRunEvent, PipelineRunEvent.run_id == PipelineRun.id)
            .outerjoin(
                MethodologyVersion,
                MethodologyVersion.id == PipelineRun.methodology_version_id,
            )
            .where(PipelineRun.aoi_id == aoi_id)
            .group_by(PipelineRun.id, MethodologyVersion.id)
            .order_by(PipelineRun.started_at.desc())
            .limit(RUNS_LIMIT)
        ).all()
        return [
            _run_summary(run, last_event_at, methodology)
            for run, methodology, last_event_at in rows
        ]

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: int, session: SessionDep) -> dict[str, Any]:
        """One run with the tail of its progress events (oldest first).

        Includes the full methodology ``parameters`` — the run's non-data inputs
        (threshold, min area, baseline window, EE script version, ...) — so the
        provenance behind the results is inspectable from the dashboard.
        """
        run = session.get(PipelineRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        methodology = (
            session.get(MethodologyVersion, run.methodology_version_id)
            if run.methodology_version_id is not None
            else None
        )
        last_event_at = session.execute(
            select(func.max(PipelineRunEvent.occurred_at)).where(PipelineRunEvent.run_id == run_id)
        ).scalar_one()
        detail = {**_run_summary(run, last_event_at, methodology)}
        if methodology is not None:
            detail["methodology"]["parameters"] = methodology.parameters
        detail["progress"] = _run_progress(session, run_id)
        detail["events"] = _run_events(session, run_id)
        return detail

    @app.get("/api/methodologies")
    def list_methodologies(session: SessionDep) -> list[dict[str, Any]]:
        """Every methodology version with its full inputs, newest first.

        The review surface for provenance: each row is one content-addressed
        parameter set (the ``version`` hash is the identity, ``display_version``
        the at-a-glance label) plus how many runs used it and when it last ran.
        """
        rows = session.execute(
            select(
                MethodologyVersion,
                func.count(PipelineRun.id),
                func.max(PipelineRun.started_at),
            )
            .outerjoin(PipelineRun, PipelineRun.methodology_version_id == MethodologyVersion.id)
            .group_by(MethodologyVersion.id)
            .order_by(MethodologyVersion.id.desc())
        ).all()
        return [
            {
                "id": methodology.id,
                "name": methodology.name,
                "version": methodology.version,
                "display_version": methodology.display_version,
                "parameters": methodology.parameters,
                "created_at": methodology.created_at,
                "run_count": run_count,
                "last_run_at": last_run_at,
            }
            for methodology, run_count, last_run_at in rows
        ]

    @app.get("/api/events/{event_id}")
    def event_detail(event_id: int, session: SessionDep) -> dict[str, Any]:
        """One event with its measurement timeline, supporting evidence, and reviews."""
        event = session.get(DisturbanceEvent, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail=f"event {event_id} not found")
        timeline = _timeline(session, event_id)
        reviews = _reviews(session, event_id)
        return {
            "id": event.id,
            "aoi_id": event.aoi_id,
            "status": event.status,
            "first_detected_at": event.first_detected_at,
            "last_detected_at": event.last_detected_at,
            "geometry": mapping(to_shape(event.geometry)),
            "footprint_area_m2": footprint_area_m2(session, event.geometry),
            "timeline": timeline,
            "evidence": _evidence(session, event_id),
            # Newest first: the head is the current opinion (or None when unreviewed).
            "reviews": reviews,
            "latest_opinion": reviews[0]["opinion"] if reviews else None,
        }

    @app.post("/api/events/{event_id}/reviews", status_code=201)
    def record_review(
        event_id: int, session: SessionDep, payload: Annotated[dict[str, Any], Body()]
    ) -> dict[str, Any]:
        """Record a manual-review opinion for an event (append-only).

        Opinions are a human judgment recorded ALONGSIDE the automatic status —
        this endpoint never mutates ``disturbance_event.status``. Requires a JSON
        body (CORS-preflight defense, like the other POSTs) and is disabled on a
        world-open dashboard: tunnel-as-auth means whoever can reach the
        dashboard can review, which must not include the open internet.
        """
        if os.environ.get(REVIEWS_ENV_VAR, "1") == "0":
            raise HTTPException(
                status_code=403, detail="manual review is disabled on this dashboard"
            )
        event = session.get(DisturbanceEvent, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail=f"event {event_id} not found")
        opinion = payload.get("opinion")
        if opinion not in REVIEW_OPINIONS:
            raise HTTPException(
                status_code=422,
                detail=f"opinion must be one of {', '.join(REVIEW_OPINIONS)}",
            )
        review = ManualReview(
            event_id=event.id,
            opinion=opinion,
            notes=_optional_str(payload.get("notes")),
            reviewer=_optional_str(payload.get("reviewer")),
        )
        session.add(review)
        session.commit()
        return {
            "id": review.id,
            "event_id": review.event_id,
            "opinion": review.opinion,
            "notes": review.notes,
            "reviewer": review.reviewer,
            "created_at": review.created_at,
        }

    return app


def _latest_opinion_subquery() -> Any:
    """Correlated scalar subquery: the newest review's opinion per event, or NULL."""
    return (
        select(ManualReview.opinion)
        .where(ManualReview.event_id == DisturbanceEvent.id)
        .order_by(ManualReview.id.desc())
        .limit(1)
        .scalar_subquery()
    )


def _event_feature(
    event: DisturbanceEvent,
    footprint_area: float,
    observation_count: int,
    latest_area: float | None,
    latest_opinion: str | None,
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": mapping(to_shape(event.geometry)),
        "properties": {
            "id": event.id,
            "status": event.status,
            # The newest manual-review opinion — a human judgment held alongside
            # (never mutating) the automatic status; null when unreviewed.
            "latest_opinion": latest_opinion,
            "first_detected_at": event.first_detected_at,
            "last_detected_at": event.last_detected_at,
            "observation_count": observation_count,
            # Cumulative unioned footprint vs the latest single-scene detection.
            "footprint_area_m2": footprint_area,
            "latest_area_m2": latest_area,
        },
    }


def _reviews(session: Session, event_id: int) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            select(ManualReview)
            .where(ManualReview.event_id == event_id)
            .order_by(ManualReview.id.desc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": review.id,
            "opinion": review.opinion,
            "notes": review.notes,
            "reviewer": review.reviewer,
            "created_at": review.created_at,
        }
        for review in rows
    ]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _timeline(session: Session, event_id: int) -> list[dict[str, Any]]:
    observations = (
        session.execute(
            select(EventObservation)
            .where(EventObservation.event_id == event_id)
            .order_by(EventObservation.observed_at)
        )
        .scalars()
        .all()
    )
    return [
        {
            "observed_at": obs.observed_at,
            "area_m2": obs.area_m2,
            "growth_m2": obs.growth_m2,
            "candidate_id": obs.disturbance_candidate_id,
        }
        for obs in observations
    ]


def _run_summary(
    run: PipelineRun, last_event_at: Any, methodology: MethodologyVersion | None
) -> dict[str, Any]:
    return {
        "id": run.id,
        "aoi_id": run.aoi_id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": run.status,
        "since": run.since,
        "until": run.until,
        "summary": run.summary,
        "last_event_at": last_event_at,
        "methodology": (
            {
                "id": methodology.id,
                "name": methodology.name,
                "version": methodology.version,
                "display_version": methodology.display_version,
            }
            if methodology is not None
            else None
        ),
    }


def _run_progress(session: Session, run_id: int) -> dict[str, Any] | None:
    """The run's current batch position plus whole-run counters.

    The events tail is capped at ``RUN_EVENTS_LIMIT``, so for a long run the
    client cannot reconstruct totals by summing what it sees; these counters
    aggregate over *all* of the run's events. ``None`` until the run records
    its first batch event (discovery / stage-preamble events carry no batch).
    """
    latest_batch = session.execute(
        select(PipelineRunEvent)
        .where(PipelineRunEvent.run_id == run_id)
        .where(PipelineRunEvent.batch_index.is_not(None))
        .order_by(PipelineRunEvent.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_batch is None:
        return None
    exports_completed, batches_failed = session.execute(
        select(
            func.coalesce(
                func.sum(PipelineRunEvent.exports).filter(PipelineRunEvent.outcome == "succeeded"),
                0,
            ),
            func.count().filter(PipelineRunEvent.outcome == "failed"),
        ).where(PipelineRunEvent.run_id == run_id)
    ).one()
    return {
        "stage": latest_batch.stage,
        "batch_index": latest_batch.batch_index,
        "batch_total": latest_batch.batch_total,
        "exports_completed": exports_completed,
        "batches_failed": batches_failed,
    }


def _run_events(session: Session, run_id: int) -> list[dict[str, Any]]:
    # The most recent events, returned oldest-first for chronological display.
    tail = (
        session.execute(
            select(PipelineRunEvent)
            .where(PipelineRunEvent.run_id == run_id)
            .order_by(PipelineRunEvent.id.desc())
            .limit(RUN_EVENTS_LIMIT)
        )
        .scalars()
        .all()
    )
    return [
        {
            "occurred_at": event.occurred_at,
            "stage": event.stage,
            "batch_index": event.batch_index,
            "batch_total": event.batch_total,
            "exports": event.exports,
            "outcome": event.outcome,
            "message": event.message,
        }
        for event in reversed(tail)
    ]


def _evidence(session: Session, event_id: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(ChangeRaster.change_type, ChangeRaster.cog_path, DisturbanceCandidate.id)
        .join(
            DisturbanceCandidate,
            DisturbanceCandidate.change_raster_id == ChangeRaster.id,
        )
        .join(
            EventObservation,
            EventObservation.disturbance_candidate_id == DisturbanceCandidate.id,
        )
        .where(EventObservation.event_id == event_id)
        .order_by(DisturbanceCandidate.id)
    ).all()
    return [
        {"change_type": change_type, "cog_path": cog_path, "candidate_id": candidate_id}
        for change_type, cog_path, candidate_id in rows
    ]


app = create_app()
