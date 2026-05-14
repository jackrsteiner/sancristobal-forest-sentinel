"""Command-line entrypoint for Open Forest Sentinel.

``forest-sentinel run --aoi <config>`` loads a configured AOI, persists it, and
prints a summary. It is the Slice 0 walking skeleton: a single thread through
configuration, the database, and reporting.
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from forest_sentinel.aoi import AoiConfigError, load_aoi_config, persist_aoi
from forest_sentinel.db import get_engine
from forest_sentinel.models import Aoi


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    # ``run`` is the only subcommand today; argparse enforces that it is present.
    return _run(args.aoi)


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
    return parser


def _run(aoi_path: Path) -> int:
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
