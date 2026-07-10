"""Command-line entrypoint for Open Forest Sentinel.

``forest-sentinel run --aoi <config>`` loads a configured AOI, persists it, and prints a
summary (the Slice 0 walking skeleton). Adding ``--since`` and ``--until`` runs the full
Slice 1 optical-change pipeline for that AOI over the window: discover HLS observations →
NBR/NDVI indices → ΔNBR/ΔNDVI change products → candidate disturbance polygons, all through
Earth Engine, persisting results to PostGIS.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from forest_sentinel import earthengine, pipeline, storage
from forest_sentinel.aoi import AoiConfigError, get_or_create_aoi, load_aoi_config, persist_aoi
from forest_sentinel.candidates import DEFAULT_DELTA_NBR_THRESHOLD, DEFAULT_MIN_AREA_M2
from forest_sentinel.change import DEFAULT_BASELINE_WINDOW
from forest_sentinel.db import get_engine
from forest_sentinel.hls import HLS_COLLECTIONS
from forest_sentinel.methodology import get_or_create_methodology_version
from forest_sentinel.models import Aoi
from forest_sentinel.storage import StorageError

# Pins what Google ran for this build, recorded in the methodology version for reproducibility.
EE_SCRIPT_VERSION = "slice1-optical-change-v1"


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


def _run_slice0(aoi_path: Path) -> int:
    try:
        config = load_aoi_config(aoi_path)
    except AoiConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    engine = get_engine()
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
    finally:
        engine.dispose()

    minx, miny, maxx, maxy = config.geometry.bounds
    print(f"Loaded AOI {config.name!r} from {aoi_path}")
    print(f"Persisted as aoi id={aoi_id}")
    print(f"Bounding box (minx, miny, maxx, maxy): ({minx}, {miny}, {maxx}, {maxy})")
    print(f"Total AOIs in database: {total}")
    return 0


def _run_pipeline(args: argparse.Namespace) -> int:
    try:
        config = load_aoi_config(args.aoi)
    except AoiConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
    }

    engine = get_engine()
    try:
        earthengine.initialize(args.gee_project)
        cog_storage = storage.local_disk_storage_from_env()
        with Session(engine) as session:
            aoi = get_or_create_aoi(session, config)
            methodology = get_or_create_methodology_version(
                session,
                name=args.methodology_name,
                version=args.methodology_version,
                parameters=parameters,
            )
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
            )
            session.commit()
    except StorageError as exc:
        print(f"error: storage is not configured ({exc})", file=sys.stderr)
        return 1
    except OperationalError as exc:
        print(f"error: could not connect to the database ({exc})", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    print(f"Ran Slice 1 pipeline for AOI {config.name!r} ({args.since} → {args.until})")
    print(
        "Observations: "
        f"{summary.observations_discovered} discovered, "
        f"{summary.observations_recorded} recorded, "
        f"{summary.observations_skipped} skipped"
    )
    print(f"Index rasters: {summary.index_rasters}")
    print(f"Change rasters: {summary.change_rasters}")
    print(f"Disturbance candidates: {summary.candidates}")
    print(
        f"Disturbance events: {summary.events_created} created, "
        f"{summary.event_observations} observations tracked"
    )
    return 0
