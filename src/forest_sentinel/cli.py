"""Command-line entrypoint for Open Forest Sentinel.

``forest-sentinel run --aoi <config>`` loads a configured AOI, persists it, and prints a
summary (the Slice 0 walking skeleton). Adding ``--since`` and ``--until`` runs the full
optical-change pipeline for that AOI over the window: discover HLS observations →
NBR/NDVI indices → ΔNBR/ΔNDVI change products → candidate disturbance polygons → tracked
disturbance events, all through Earth Engine, persisting results to PostGIS.
"""

import argparse
import logging
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from forest_sentinel import (
    confidence,
    context,
    earthengine,
    events,
    forestmask,
    indices,
    pipeline,
    qa,
    radar,
    reproduce,
    retention,
    sentinel1,
    storage,
)
from forest_sentinel.aoi import (
    AOIS_DIR_ENV_VAR,
    DEFAULT_AOIS_DIR,
    AoiConfig,
    AoiConfigError,
    get_or_create_aoi,
    load_aoi_config,
    persist_aoi,
)
from forest_sentinel.aoi_admin import delete_aoi, inventory_aoi, remove_cog_directory
from forest_sentinel.candidates import DEFAULT_DELTA_NBR_THRESHOLD, DEFAULT_MIN_AREA_M2
from forest_sentinel.change import DEFAULT_BASELINE_WINDOW
from forest_sentinel.db import get_engine
from forest_sentinel.earthengine import EarthEngineError
from forest_sentinel.hls import HLS_COLLECTIONS
from forest_sentinel.methodology import (
    MethodologyVersionMismatch,
    resolve_methodology_version,
)
from forest_sentinel.models import (
    CONTEXT_KINDS,
    Aoi,
    ChangeRaster,
    DisturbanceEvent,
    IndexRaster,
    Observation,
    PipelineRun,
)
from forest_sentinel.storage import StorageConfigurationError, StorageError

# Pins what Google ran for this build, recorded in the methodology version for reproducibility.
# Split per stage (config-inventory Finding 4): the RASTER pin invalidates raster
# lineages (index/change COGs re-export when it bumps); the detection pin below
# invalidates only the methodology (candidates re-extract, every COG reused).
# The raster pins keep the pre-split values so lineages backfilled by migration
# 0020 content-match the ones new runs derive — no re-export on upgrade.
EE_SCRIPT_VERSION = "slice1-optical-change-v1"
RASTER_SCRIPT_VERSION = "slice1-optical-change-v1"

# The radar stage's own pins (Slice 5): radar runs under its own content-addressed
# methodology (radar-change), so its code versions are pinned independently.
RADAR_SCRIPT_VERSION = "slice5-radar-change-v1"
RADAR_RASTER_SCRIPT_VERSION = "slice5-radar-change-v1"
RADAR_ENV_VAR = "FOREST_SENTINEL_RADAR"
RADAR_THRESHOLD_ENV_VAR = "FOREST_SENTINEL_RADAR_THRESHOLD"

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
    # Without a configured handler only WARNING+ reaches stderr (Python's
    # last-resort handler), which would drop the run-progress INFO lines
    # (runlog.py) that journald is meant to capture. journald stamps times.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "cogs":
        return _run_cogs_command(args)
    if args.command == "aoi":
        return _run_aoi_command(args)
    if args.command == "context":
        return _run_context_command(args)
    if args.command == "assess":
        return _run_assess(args)
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
    run_parser.add_argument(
        "--methodology-version",
        default=None,
        help=(
            "Pin an explicit methodology version (strict: re-using a version with "
            "different parameters is an error). Default: content-addressed — the "
            "parameter set selects or mints the version automatically."
        ),
    )
    run_parser.add_argument("--baseline-window", type=int, default=DEFAULT_BASELINE_WINDOW)
    run_parser.add_argument("--threshold", type=float, default=None)
    run_parser.add_argument("--min-area", type=float, default=None, metavar="M2")
    run_parser.add_argument("--gee-project", default=None)

    aoi_parser = subparsers.add_parser("aoi", help="Inspect or remove configured AOIs.")
    aoi_subparsers = aoi_parser.add_subparsers(dest="aoi_command", required=True)
    aoi_subparsers.add_parser("list", help="List AOIs with row counts and last run.")
    delete_parser = aoi_subparsers.add_parser(
        "delete", help="Delete an AOI and all of its derived data."
    )
    delete_parser.add_argument("name", help="AOI name (see `forest-sentinel aoi list`).")
    delete_parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete. Without it, print what would be deleted and exit 1.",
    )

    context_parser = subparsers.add_parser(
        "context", help="Manage contextual GeoJSON layers (concessions, roads, rivers, ...)."
    )
    context_subparsers = context_parser.add_subparsers(dest="context_command", required=True)
    context_load_parser = context_subparsers.add_parser(
        "load",
        help=(
            "Load (or replace) one context layer from a GeoJSON file. Re-loading "
            "a name replaces its features wholesale."
        ),
    )
    context_load_parser.add_argument("file", type=Path, help="GeoJSON Feature/FeatureCollection.")
    context_load_parser.add_argument(
        "--kind", required=True, choices=list(CONTEXT_KINDS), help="What the layer represents."
    )
    context_load_parser.add_argument(
        "--name",
        default=None,
        help=(
            "Layer name (default: derived from the filename — the part after "
            "'--' for <kind>--<name>.geojson, else the whole stem)."
        ),
    )

    cogs_parser = subparsers.add_parser("cogs", help="Manage the local COG store.")
    cogs_subparsers = cogs_parser.add_subparsers(dest="cogs_command", required=True)
    prune_parser = cogs_subparsers.add_parser(
        "prune",
        help=(
            "Delete COGs older than the retention policy (COG_RETENTION_DAYS; "
            "blank/0 keeps everything). Database rows are never touched."
        ),
    )
    prune_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be pruned without deleting anything.",
    )
    reproduce_parser = cogs_subparsers.add_parser(
        "reproduce",
        help=(
            "Re-export one raster from its recorded provenance (e.g. after the "
            "retention policy pruned its COG). Database rows are never modified."
        ),
    )
    reproduce_parser.add_argument(
        "kind", choices=["index", "change"], help="Which catalog table the id refers to."
    )
    reproduce_parser.add_argument("raster_id", type=int, help="The raster row's id.")
    reproduce_parser.add_argument(
        "--force-version",
        action="store_true",
        help=(
            "Reproduce even when the recorded ee_script_version does not match this "
            "build's pin (logged as a loud warning; the output may differ from the "
            "recorded provenance)."
        ),
    )
    reproduce_parser.add_argument("--gee-project", default=None)

    assess_parser = subparsers.add_parser(
        "assess",
        help=(
            "Re-score event confidence offline (#168): database + retained local "
            "COGs only, no Earth Engine."
        ),
    )
    assess_parser.add_argument(
        "--aoi-name", default=None, help="Assess only this AOI (default: every AOI)."
    )
    return parser


def _positive_int_env(name: str, default: int) -> int:
    """A positive integer from the environment; blank/malformed/<=0 -> ``default``."""
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _run_cogs_command(args: argparse.Namespace) -> int:
    """Dispatch `forest-sentinel cogs prune|reproduce` (#80/#94)."""
    if args.cogs_command == "reproduce":
        return _run_cogs_reproduce(args)
    retention_days = _positive_int_env(retention.RETENTION_DAYS_ENV_VAR, 0)
    if retention_days <= 0:
        print(
            f"COG retention is disabled ({retention.RETENTION_DAYS_ENV_VAR} is unset, "
            "blank, or 0); nothing to prune."
        )
        return 0
    window_days = _positive_int_env(retention.WINDOW_DAYS_ENV_VAR, retention.DEFAULT_WINDOW_DAYS)
    root = Path(os.environ.get(storage.COG_ROOT_ENV_VAR, storage.DEFAULT_COG_ROOT))

    report = retention.prune_cogs(
        root,
        retention_days=retention_days,
        window_days=window_days,
        today=date.today(),
        dry_run=args.dry_run,
    )
    if report.floor_applied:
        print(
            f"warning: {retention.RETENTION_DAYS_ENV_VAR}={retention_days} is below the "
            f"safe floor (WINDOW_DAYS={window_days} + {retention.FLOOR_MARGIN_DAYS}-day "
            f"margin); using {report.effective_retention_days} days — pruning inside the "
            "active window would re-spend Earth Engine quota and rewrite change-raster "
            "provenance (docs/architecture.md §7)",
            file=sys.stderr,
        )
    verb = "Would prune" if args.dry_run else "Pruned"
    unrecognized = (
        f"; left {report.unrecognized} unrecognized file(s) alone" if report.unrecognized else ""
    )
    print(
        f"{verb} {len(report.pruned)} COG(s) ({report.pruned_bytes / 1_000_000:.1f} MB) "
        f"acquired before {report.cutoff.isoformat()} from {root} "
        f"(retention: {report.effective_retention_days} days; kept {report.kept}{unrecognized})"
    )
    for path in report.pruned:
        print(f"  {path}")
    return 0


def _run_cogs_reproduce(args: argparse.Namespace) -> int:
    """Dispatch `forest-sentinel cogs reproduce {index|change} <id>` (#94)."""
    with _disposing_engine() as engine:
        try:
            earthengine.initialize(args.gee_project)
            cog_storage = storage.local_disk_storage_from_env()
            with Session(engine) as session:
                if args.kind == "index":
                    index_raster = session.get(IndexRaster, args.raster_id)
                    if index_raster is None:
                        print(f"error: index_raster {args.raster_id} not found", file=sys.stderr)
                        return 1
                    path = reproduce.reproduce_index_raster(
                        session,
                        raster=index_raster,
                        storage=cog_storage,
                        current_script_version=RASTER_SCRIPT_VERSION,
                        force_version=args.force_version,
                    )
                else:
                    change_raster = session.get(ChangeRaster, args.raster_id)
                    if change_raster is None:
                        print(f"error: change_raster {args.raster_id} not found", file=sys.stderr)
                        return 1
                    path = reproduce.reproduce_change_raster(
                        session,
                        raster=change_raster,
                        storage=cog_storage,
                        current_script_version=RASTER_SCRIPT_VERSION,
                        force_version=args.force_version,
                    )
        except StorageConfigurationError as exc:
            print(f"error: storage is not configured ({exc})", file=sys.stderr)
            return 1
        except (StorageError, EarthEngineError, reproduce.ReproduceError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except OperationalError as exc:
            print(f"error: could not connect to the database ({exc})", file=sys.stderr)
            return 1
    print(f"Reproduced {args.kind}_raster {args.raster_id} -> {path}")
    return 0


def _run_assess(args: argparse.Namespace) -> int:
    """Re-score event confidence for AOIs — offline (#168).

    Only the confidence stage runs: no Earth Engine initialization, no
    discovery, no exports — the stability factor reads retained index COGs
    from local disk. Each AOI is scored under its per-AOI advisory run lock
    (try-lock: an AOI with a live pipeline run is skipped, not waited on;
    that run's own confidence stage will assess it) and committed
    independently. Re-running with unchanged inputs appends nothing.
    """
    with (
        _disposing_engine() as engine,
        engine.connect() as connection,
        Session(bind=connection) as session,
    ):
        query = select(Aoi).order_by(Aoi.name)
        if args.aoi_name is not None:
            query = query.where(Aoi.name == args.aoi_name)
        aois = session.execute(query).scalars().all()
        if not aois:
            target = f"AOI {args.aoi_name!r}" if args.aoi_name else "any AOI"
            print(f"error: no {target} found", file=sys.stderr)
            return 1
        exit_code = 0
        for aoi in aois:
            locked = session.execute(
                select(func.pg_try_advisory_lock(pipeline.AOI_RUN_LOCK_CLASS, aoi.id))
            ).scalar_one()
            if not locked:
                print(f"{aoi.name}: skipped — a pipeline run holds the AOI lock")
                exit_code = 1
                continue
            try:
                appended = confidence.assess_events_for_aoi(session, aoi=aoi)
                session.commit()
                print(
                    f"{aoi.name}: {appended} assessment(s) appended "
                    f"(rule {confidence.RULE_VERSION}; unchanged conclusions skipped)"
                )
            finally:
                session.execute(
                    select(func.pg_advisory_unlock(pipeline.AOI_RUN_LOCK_CLASS, aoi.id))
                )
        return exit_code


def _run_context_command(args: argparse.Namespace) -> int:
    """Dispatch `forest-sentinel context load` (E17, #125)."""
    try:
        document = context.load_context_file(args.file)
    except context.ContextConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parsed = context.parse_harvest_filename(args.file)
    name = args.name or (parsed[1] if parsed else args.file.stem)
    with _disposing_engine() as engine:
        try:
            with Session(engine) as session:
                context.replace_layer(
                    session,
                    name=name,
                    kind=args.kind,
                    document=document,
                    source_file=str(args.file),
                )
                session.commit()
        except OperationalError as exc:
            print(f"error: could not connect to the database ({exc})", file=sys.stderr)
            return 1
    print(f"Loaded context layer {name!r} ({args.kind}): {len(document.geometries)} feature(s)")
    return 0


def _run_aoi_command(args: argparse.Namespace) -> int:
    """Dispatch `forest-sentinel aoi list|delete` (#83)."""
    with _disposing_engine() as engine:
        try:
            with Session(engine) as session:
                if args.aoi_command == "list":
                    return _aoi_list(session)
                return _aoi_delete(session, name=args.name, confirmed=args.yes)
        except OperationalError as exc:
            print(f"error: could not connect to the database ({exc})", file=sys.stderr)
            return 1


def _aoi_list(session: Session) -> int:
    rows = session.execute(
        select(
            Aoi.id,
            Aoi.name,
            select(func.count())
            .where(Observation.aoi_id == Aoi.id)
            .correlate(Aoi)
            .scalar_subquery(),
            select(func.count())
            .where(DisturbanceEvent.aoi_id == Aoi.id)
            .correlate(Aoi)
            .scalar_subquery(),
        ).order_by(Aoi.id)
    ).all()
    if not rows:
        print("No AOIs configured.")
        return 0
    print(f"{'id':>4}  {'name':<32} {'observations':>12} {'events':>7}  last run")
    for aoi_id, name, observations, event_count in rows:
        last_run = session.execute(
            select(PipelineRun.status, PipelineRun.started_at)
            .where(PipelineRun.aoi_id == aoi_id)
            .order_by(PipelineRun.started_at.desc())
            .limit(1)
        ).first()
        run_label = f"{last_run.status} @ {last_run.started_at:%Y-%m-%d %H:%M}" if last_run else "—"
        print(f"{aoi_id:>4}  {name:<32} {observations:>12} {event_count:>7}  {run_label}")
    return 0


def _aoi_delete(session: Session, *, name: str, confirmed: bool) -> int:
    aoi = session.execute(select(Aoi).where(Aoi.name == name)).scalar_one_or_none()
    if aoi is None:
        print(f"error: no AOI named {name!r} (see `forest-sentinel aoi list`)", file=sys.stderr)
        return 1

    inventory = inventory_aoi(session, aoi)
    print(f"AOI {aoi.id} {aoi.name!r} — deleting removes:")
    print(
        f"  observations:      {inventory.observations} (+{inventory.quality_masks} quality masks)"
    )
    print(f"  index rasters:     {inventory.index_rasters}")
    print(f"  change rasters:    {inventory.change_rasters} (+{inventory.candidates} candidates)")
    print(f"  events:            {inventory.events} (+{inventory.event_observations} measurements)")
    print(f"  pipeline runs:     {inventory.runs} (+{inventory.run_events} progress events)")
    print(f"  COG directory:     {inventory.cog_directory}")
    if not confirmed:
        print("\nNothing deleted. Re-run with --yes to delete.", file=sys.stderr)
        return 1

    delete_aoi(session, aoi)
    session.commit()
    if remove_cog_directory(inventory.cog_directory):
        print(f"Removed {inventory.cog_directory}")
    seed = Path(os.environ.get(AOIS_DIR_ENV_VAR, DEFAULT_AOIS_DIR)) / (
        f"{storage.sanitize_path_component(name)}.geojson"
    )
    if seed.is_file():
        seed.unlink()
        print(f"Removed {seed}")
    print(f"Deleted AOI {name!r}.")
    print(
        "note: if a GeoJSON for this AOI is still committed to the repo "
        "(aois/*.geojson or config/aoi.geojson), the next pipeline run will "
        "re-create it — remove the file from the repo too."
    )
    return 0


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
    try:
        forest_mask_config = forestmask.config_from_env()
    except forestmask.ForestMaskConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parameters = {
        "ee_script_version": EE_SCRIPT_VERSION,
        "raster_script_version": RASTER_SCRIPT_VERSION,
        "collections": sorted(HLS_COLLECTIONS),
        "baseline_window": args.baseline_window,
        "delta_nbr_threshold": threshold,
        "min_area_m2": min_area,
        # Everything that shapes the output belongs in the provenance record: the
        # export / reduceToVectors scale, the Fmask categories masked out, and the
        # forest mask candidates were restricted to (#82).
        "scale_m": indices.DEFAULT_SCALE_METERS,
        "masked_categories": list(qa.MASK_CATEGORIES),
        **forestmask.parameters_entry(forest_mask_config),
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
                methodology = resolve_methodology_version(
                    session,
                    name=args.methodology_name,
                    parameters=parameters,
                    version=args.methodology_version,
                )
                # Radar augmentation (Slice 5) is opt-in and runs under its own
                # content-addressed methodology — separate lineage, separate pin.
                radar_methodology = None
                if os.environ.get(RADAR_ENV_VAR, "0") == "1":
                    radar_threshold_raw = os.environ.get(RADAR_THRESHOLD_ENV_VAR, "")
                    try:
                        radar_threshold = float(radar_threshold_raw)
                    except ValueError:
                        radar_threshold = radar.DEFAULT_DELTA_VV_DB_THRESHOLD
                    radar_methodology = resolve_methodology_version(
                        session,
                        name="radar-change",
                        parameters={
                            "ee_script_version": RADAR_SCRIPT_VERSION,
                            "raster_script_version": RADAR_RASTER_SCRIPT_VERSION,
                            "collection": sentinel1.S1_COLLECTION,
                            "metric": "vv_db",
                            "delta_vv_db_threshold": radar_threshold,
                            "baseline_window": args.baseline_window,
                            "orbit_policy": "same_direction",
                            "scale_m": indices.DEFAULT_SCALE_METERS,
                            "min_area_m2": min_area,
                            **forestmask.parameters_entry(forest_mask_config),
                        },
                    )
                # Harvest config/context/*.geojson (E17): idempotent replace, so
                # running per AOI file is safe; a bad file warns, never blocks.
                harvest = context.harvest_context_dir(
                    session,
                    Path(os.environ.get(context.CONTEXT_DIR_ENV_VAR, context.DEFAULT_CONTEXT_DIR)),
                )
                if harvest.layers or harvest.skipped:
                    print(
                        f"Context layers: {harvest.layers} loaded "
                        f"({harvest.features} features), {harvest.skipped} skipped"
                    )
                # Commit the AOI/methodology rows before the (hours-long) pipeline
                # body so the dashboard lists the AOI as soon as a run starts,
                # rather than only after the first full run commits — and even if
                # that run fails partway.
                session.commit()
                methodology_label = (
                    f"v{methodology.display_version} ({methodology.version})"
                    if methodology.display_version
                    else methodology.version
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
                    max_concurrent_exports=_max_concurrent_exports(),
                    # Lifecycle config, not a methodology input: changing it never
                    # mints a new methodology version.
                    resolved_after_days=_positive_int_env(
                        "RESOLVED_AFTER_DAYS", events.DEFAULT_RESOLVED_AFTER_DAYS
                    ),
                    radar_methodology=radar_methodology,
                    context_buffer_m=float(
                        _positive_int_env(
                            context.CONTEXT_BUFFER_ENV_VAR,
                            int(context.DEFAULT_CONTEXT_BUFFER_M),
                        )
                    ),
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
    print(f"Methodology: {args.methodology_name} {methodology_label}")
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
        f"{summary.event_observations} observations tracked, "
        f"{summary.events_resolved} resolved"
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
