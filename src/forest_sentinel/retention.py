"""Automated COG retention (#80): prune old rasters from the local COG store.

The prune is catalog-aware through the store's deterministic layout
(``{aoi}/{product}/{date}/{file}.tif``, see ``storage.CogKey``): a file's age is
its observation **acquisition date** — the path's date component — not its
mtime, so re-downloading an old raster never resets its retention clock, and
files that don't match the catalog layout are left alone. Database rows are
never touched: they are the reproduction recipe (``docs/architecture.md`` §7),
and a pruned COG that is needed again is re-exported by the pipeline's
missing-file path (#77).

Files inside the scheduler's active window must never be pruned — the reuse
check treats a missing in-window COG as "re-export", silently re-spending Earth
Engine quota, and a re-exported non-frozen change raster recomputes its
baseline provenance. The effective retention is therefore floored at
``WINDOW_DAYS`` plus a safety margin, regardless of the configured value.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# Read from the instance config (config/instance.env -> .env). Blank/0/unset
# disables pruning (keep forever).
RETENTION_DAYS_ENV_VAR = "COG_RETENTION_DAYS"
WINDOW_DAYS_ENV_VAR = "WINDOW_DAYS"
# Keep in sync with run_pipeline.sh's WINDOW_DAYS default.
DEFAULT_WINDOW_DAYS = 30
# Safety margin over the active window: belt-and-braces headroom for the
# trailing baseline (whose imagery is rebuilt in EE from rows, not read from
# COGs) and for clock/timer skew between the prune and pipeline jobs.
FLOOR_MARGIN_DAYS = 14


def effective_retention_days(retention_days: int, window_days: int) -> tuple[int, bool]:
    """The retention actually applied, floored at the active window + margin.

    Returns ``(days, floor_applied)`` — ``floor_applied`` is True when the
    configured value was raised to the floor.
    """
    floor = window_days + FLOOR_MARGIN_DAYS
    if retention_days < floor:
        return floor, True
    return retention_days, False


@dataclass
class PruneReport:
    """What one prune pass did (or would do, under ``dry_run``)."""

    effective_retention_days: int
    floor_applied: bool
    cutoff: date
    pruned: list[Path] = field(default_factory=list)
    pruned_bytes: int = 0
    kept: int = 0
    unrecognized: int = 0  # files not matching the catalog layout; never touched


def prune_cogs(
    root: Path,
    *,
    retention_days: int,
    window_days: int,
    today: date,
    dry_run: bool = False,
) -> PruneReport:
    """Prune catalog COGs older than the effective retention; return a report.

    Only ``*.tif`` files at the catalog depth (``aoi/product/date/file.tif``)
    with a parseable ISO date component are candidates; everything else is
    counted as ``unrecognized`` and kept. Directories emptied by the prune are
    removed too, so the store doesn't accumulate dead date/product trees.
    """
    days, floor_applied = effective_retention_days(retention_days, window_days)
    cutoff = today - timedelta(days=days)
    report = PruneReport(effective_retention_days=days, floor_applied=floor_applied, cutoff=cutoff)
    if not root.is_dir():
        return report

    for path in sorted(root.glob("*/*/*/*.tif")):
        try:
            file_date = date.fromisoformat(path.parent.name)
        except ValueError:
            report.unrecognized += 1
            continue
        if file_date >= cutoff:
            report.kept += 1
            continue
        report.pruned.append(path)
        report.pruned_bytes += path.stat().st_size
        if not dry_run:
            path.unlink()

    if not dry_run:
        _remove_empty_dirs(root)
    return report


def _remove_empty_dirs(root: Path) -> None:
    """Remove now-empty date/product/aoi directories (never ``root`` itself)."""
    # Deepest-first so a date dir emptied by the prune lets its product dir
    # (and then its aoi dir) collapse in the same pass.
    for directory in sorted(
        (p for p in root.glob("**/") if p != root), key=lambda p: len(p.parts), reverse=True
    ):
        try:
            directory.rmdir()  # only succeeds when empty
        except OSError:
            continue
