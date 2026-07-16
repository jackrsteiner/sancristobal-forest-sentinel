"""Command-line entrypoint for Open Forest Sentinel.

``forest-sentinel run --aoi <config>`` loads a configured AOI, persists it, and prints a
summary (the Slice 0 walking skeleton). Adding ``--since`` and ``--until`` runs the full
optical-change pipeline for that AOI over the window: discover HLS observations →
NBR/NDVI indices → ΔNBR/ΔNDVI change products → candidate disturbance polygons → tracked
disturbance events, all through Earth Engine, persisting results to PostGIS.
"""

import argparse
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from forest_sentinel import earthengine, indices, pipeline, qa, storage
from forest_sentinel.aoi import (
    AoiConfig,
    AoiConfigError,
    get_or_create_aoi,
    load_aoi_config,
    persist_aoi,
)
from forest_sentinel.candidates import DEFAULT_DELTA_NBR_THRESHOLD, DEFAULT_MIN_AREA_M2
from forest_sentinel.change import DEFAULT_BASELINE_WINDOW
from forest_sentinel.db import get_engine
from forest_sentinel.earthengine import EarthEngineError
from forest_sentinel.hls import HLS_COLLECTIONS
from forest_sentinel.methodology import (
    MethodologyVersionMismatch,
    get_or_create_methodology_version,
)
from forest_sentinel.models import Aoi
from forest_sentinel.storage import StorageConfigurationError, StorageError

# Pins what Google ran for this build, recorded in the methodology version for reproducibility.
EE_SCRIPT_VERSION = "slice1-optical-change-v1"

# How many Earth Engine batch exports the pipeline keeps in flight at once.
MAX_CONCURRENT_EXPORTS_ENV_VAR = "FOREST_SENTINEL_MAX_CONCURRENT_EXPORTS"
DEFAULT_MAX_CONCURRENT_EXPORTS = 4


def _max_concurrent_exports() -> int:
    """The configured export concurrency; malformed or non-positive values fall back."""
    raw = os.environ.get(MAX_CONCURRENT_EXPORTS_ENV_VAR, "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_CONCURRENT_EXPORTS
    return value if value > 0 else DEFAULT_MAX_CONCURRENT_EXPORTS


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if (args.since is None) != (args.until is None):
        # A lone window flag used to fall through to the Slice 0 load silently.
        parser.error("--since and --until must be provided together")
    if args.since is not None and args.until is not None:
        return _run_pipeline(args)
    return _run_slice0(args.aoi)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forest-sentinel",
        description="Forest disturbance monitoring for a configurable Area of Interest.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Run the pipeline for a configured AOI.")
    run_parser.add_argument(
        "--aoi",
        required=True,
        type=Path,
        metavar="PATH",
        help="Path to the AOI GeoJSON configuration file.",
    )
    run_parser.add_argument(
        "--since",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Start of the observation window (inclusive). Enables the full pipeline.",
    )
    run_parser.add_argument(
        "--until",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="End of the observation window (exclusive). Enables the full pipeline.",
    )
    run_parser.add_argument("--methodology-name", default="optical-change")
    run_parser.add_argument("--methodology-version", default="1.0.0")
    run_parser.add_argument("--baseline-window", type=int, default=DEFAULT_BASELINE_WINDOW)
    run_parser.add_argument("--threshold", type=float, default=None)
    run_parser.add_argument("--min-area", type=float, default=None, metavar="M2")
    run_parser.add_argument("--gee-project", default=None)
    return parser


def _load_aoi_or_report(aoi_path: Path) -> AoiConfig | None:
    """Load the AOI config, printing the standard error message on failure."""
    try:
        return load_aoi_config(aoi_path)
    except AoiConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


@contextmanager
def _disposing_engine() -> Iterator[Engine]:
    """An engine for one CLI invocation, disposed on the way out."""
    engine = get_engine()
    try:
        yield engine
    finally:
        engine.dispose()


def _run_slice0(aoi_path: Path) -> int:
    config = _load_aoi_or_report(aoi_path)
    if config is None:
        return 1

    with _disposing_engine() as engine:
        try:
            with Session(engine) as session:
                try:
                    aoi = persist_aoi(session, config)
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    print(f"error: an AOI named {config.name!r} already exists", file=sys.stderr)
                    return 1
                aoi_id = aoi.id
                total = session.execute(select(func.count()).select_from(Aoi)).scalar_one()
        except OperationalError as exc:
            print(f"error: could not connect to the database ({exc})", file=sys.stderr)
            return 1

    minx, miny, maxx, maxy = config.geometry.bounds
    print(f"Loaded AOI {config.name!r} from {aoi_path}")
    print(f"Persisted as aoi id={aoi_id}")
    print(f"Bounding box (minx, miny, maxx, maxy): ({minx}, {miny}, {maxx}, {maxy})")
    print(f"Total AOIs in database: {total}")
    return 0


def _run_pipeline(args: argparse.Namespace) -> int:
    config = _load_aoi_or_report(args.aoi)
    if config is None:
        return 1

    # Record the *resolved* values so provenance reflects what the run actually used,
    # even when the CLI flags are omitted.
    threshold = args.threshold if args.threshold is not None else DEFAULT_DELTA_NBR_THRESHOLD
    min_area = args.min_area if args.min_area is not None else DEFAULT_MIN_AREA_M2
    parameters = {
        "ee_script_version": EE_SCRIPT_VERSION,
        "collections": sorted(HLS_COLLECTIONS),
        "baseline_window": args.baseline_window,
        "delta_nbr_threshold": threshold,
        "min_area_m2": min_area,
        # Everything that shapes the output belongs in the provenance record: the
        # export / reduceToVectors scale and the Fmask categories masked out.
        "scale_m": indices.DEFAULT_SCALE_METERS,
        "masked_categories": list(qa.MASK_CATEGORIES),
    }

    with _disposing_engine() as engine:
        try:
            earthengine.initialize(args.gee_project)
            cog_storage = storage.local_disk_storage_from_env()
            # The session is bound to one pinned connection: the pipeline holds a
            # session-scoped per-AOI advisory lock across its checkpoint commits,
            # and the lock lives on the connection (see pipeline._acquire_aoi_run_lock).
            with engine.connect() as connection, Session(bind=connection) as session:
                aoi = get_or_create_aoi(session, config)
                methodology = get_or_create_methodology_version(
                    session,
                    name=args.methodology_name,
                    version=args.methodology_version,
                    parameters=parameters,
                )
                # Commit the AOI/methodology rows before the (hours-long) pipeline
                # body so the dashboard lists the AOI as soon as a run starts,
                # rather than only after the first full run commits — and even if
                # that run fails partway.
                session.commit()
                summary = pipeline.run_pipeline(
                    session,
                    aoi=aoi,
                    since=args.since,
                    until=args.until,
                    methodology=methodology,
                    storage=cog_storage,
                    baseline_window=args.baseline_window,
                    threshold=threshold,
                    min_area_m2=min_area,
                    max_concurrent_exports=_max_concurrent_exports(),
                )
                session.commit()
        except StorageConfigurationError as exc:
            print(f"error: storage is not configured ({exc})", file=sys.stderr)
            return 1
        except (StorageError, EarthEngineError, MethodologyVersionMismatch) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except IntegrityError:
            # First-ever run for an AOI racing another run: the per-AOI advisory lock
            # can only be taken once the AOI row exists, so the get-or-creates above
            # can collide. Nothing was committed; a re-run reuses the winner's rows.
            print(
                "error: a concurrent run created this AOI or methodology at the same "
                "time; re-run to reuse it",
                file=sys.stderr,
            )
            return 1
        except OperationalError as exc:
            print(f"error: could not connect to the database ({exc})", file=sys.stderr)
            return 1

    print(f"Ran Slice 1 pipeline for AOI {config.name!r} ({args.since} → {args.until})")
    print(
        "Observations: "
        f"{summary.observations_discovered} discovered, "
        f"{summary.observations_recorded} recorded, "
        f"{summary.observations_skipped} skipped"
    )
    print(f"Index rasters: {summary.index_rasters} ({summary.index_rasters_reused} reused)")
    print(f"Change rasters: {summary.change_rasters} ({summary.change_rasters_reused} reused)")
    print(f"Disturbance candidates: {summary.candidates}")
    print(
        f"Disturbance events: {summary.events_created} created, "
        f"{summary.event_observations} observations tracked"
    )
    if summary.export_failures:
        # Partial results are committed; a nonzero exit alerts the scheduler.
        print(
            f"error: {summary.export_failures} observation(s) skipped due to failed "
            "Earth Engine exports (see logs); partial results were persisted",
            file=sys.stderr,
        )
        return 1
    return 0
