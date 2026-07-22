"""Post-detection NBR trajectory inside an event footprint (#165).

The detector is an *onset* detector: ΔNBR compares each scene against a
trailing-median baseline, so once the baseline absorbs a cleared patch
(~``baseline_window`` scenes), re-detections stop whether or not the clearing
is still there. This module answers the question the event card cannot: is the
patch still bare, regenerating, or was the detection a one-look transient?

It reads the retained per-scene **NBR index COGs** directly with rasterio —
zero Earth Engine, same local-compute posture as ``localextract`` — computing
the footprint's mean NBR per post-detection date against a pre-event
reference. The state classification is a display-only interpretation for
reviewers, not a methodology parameter: it never touches detection, event
lifecycle, or confidence scoring.
"""

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import rasterio.features
import rasterio.warp
from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.models import DisturbanceEvent, IndexRaster, MethodologyVersion, Observation

logger = logging.getLogger(__name__)

_WGS84 = "EPSG:4326"
_INDEX_TYPE = "NBR"

# A scene must observe at least this fraction of the footprint's pixels to
# contribute a point — a sliver seen through a cloud gap is noise, not signal.
MIN_FOOTPRINT_COVERAGE = 0.3
# Recovery ratio cutoffs (display-only interpretation, #165): the recent mean's
# position between the detection-day low (0) and the pre-event reference (1).
RECOVERED_RATIO = 0.8
PERSISTENT_RATIO = 0.3
# Damp single-scene noise: "recent" is the mean of the last few usable dates.
_RECENT_DATES = 3

STATE_PERSISTENT = "persistent"
STATE_RECOVERING = "recovering"
STATE_TRANSIENT = "transient"
STATE_INSUFFICIENT = "insufficient-data"


@dataclass
class TrajectoryPoint:
    date: str
    mean_nbr: float
    valid_fraction: float


@dataclass
class Trajectory:
    state: str
    reference_nbr: float | None = None
    detection_nbr: float | None = None
    points: list[TrajectoryPoint] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reference_nbr": self.reference_nbr,
            "detection_nbr": self.detection_nbr,
            "points": [vars(point) for point in self.points],
        }


def event_trajectory(session: Session, *, event: DisturbanceEvent) -> Trajectory:
    """The event footprint's per-date mean NBR, before and after detection.

    Pre-detection dates provide the reference; the detection date anchors the
    low; later dates tell the story. Scenes whose COG is missing (pruned) or
    which observed under ``MIN_FOOTPRINT_COVERAGE`` of the footprint are
    skipped — absence degrades the answer to ``insufficient-data``, never to a
    wrong one.

    Single-event convenience over ``trajectories_for_events`` (#170) — batch
    callers (the confidence stage) must use the batch form, which opens each
    COG once for ALL events instead of once per (event, raster) pair.
    """
    return trajectories_for_events(session, events=[event])[event.id]


def trajectories_for_events(
    session: Session, *, events: Sequence[DisturbanceEvent]
) -> dict[int, Trajectory]:
    """Trajectories for many events with each COG opened exactly once (#170).

    The incident that motivated this: per-event computation over a
    whole-country AOI did opens ~ events × rasters (~270k header reads) and
    starved the e2-micro. Here events are grouped by (AOI, raster lineage),
    the raster list is fetched once per group, and the loop runs rasters
    OUTER, events INNER — one open per file, a cheap bounds pre-check per
    footprint, and a windowed read only on intersection.
    """
    results: dict[int, Trajectory] = {}
    groups: dict[tuple[int, int], list[DisturbanceEvent]] = {}
    for event in events:
        methodology = session.get(MethodologyVersion, event.methodology_version_id)
        if methodology is None:  # pragma: no cover - FK guarantees existence
            results[event.id] = Trajectory(state=STATE_INSUFFICIENT)
            continue
        groups.setdefault((event.aoi_id, methodology.raster_lineage_id), []).append(event)

    for (aoi_id, lineage_id), group in groups.items():
        rows = session.execute(
            select(Observation.acquired_at, IndexRaster.cog_path)
            .join(IndexRaster, IndexRaster.observation_id == Observation.id)
            .where(Observation.aoi_id == aoi_id)
            .where(IndexRaster.index_type == _INDEX_TYPE)
            .where(IndexRaster.raster_lineage_id == lineage_id)
            .order_by(Observation.acquired_at)
        ).all()
        footprints = {event.id: mapping(to_shape(event.geometry)) for event in group}
        # Pixel-weighted per-date aggregation per event: adjacent same-day
        # granules (tile boundaries) merge into one honest look, not two.
        by_date: dict[int, dict[str, list[tuple[float, int, int]]]] = {
            event.id: {} for event in group
        }
        for acquired_at, cog_path in rows:
            date = _utc_date(acquired_at)
            for event_id, stats in _footprint_statistics_many(cog_path, footprints).items():
                if stats is None:
                    continue
                by_date[event_id].setdefault(date, []).append(stats)
        for event in group:
            results[event.id] = _assemble(event, by_date[event.id])

    return results


def _assemble(
    event: DisturbanceEvent, by_date: dict[str, list[tuple[float, int, int]]]
) -> Trajectory:
    detected_date = _utc_date(event.first_detected_at)
    reference_values: list[float] = []
    points: list[TrajectoryPoint] = []
    for date in sorted(by_date):
        samples = by_date[date]
        valid = sum(v for _, v, _ in samples)
        total = sum(t for _, _, t in samples)
        if total == 0 or valid / total < MIN_FOOTPRINT_COVERAGE:
            continue
        mean = sum(m * v for m, v, _ in samples) / valid
        if not math.isfinite(mean):  # belt-and-braces: never emit NaN/null
            continue
        if date < detected_date:
            reference_values.append(mean)
            continue
        points.append(TrajectoryPoint(date=date, mean_nbr=mean, valid_fraction=valid / total))

    reference = sum(reference_values) / len(reference_values) if reference_values else None
    detection = next((p.mean_nbr for p in points if p.date == detected_date), None)
    return Trajectory(
        state=_classify(reference, detection, points, detected_date),
        reference_nbr=reference,
        detection_nbr=detection,
        points=points,
    )


def _classify(
    reference: float | None,
    detection: float | None,
    points: list[TrajectoryPoint],
    detected_date: str,
) -> str:
    after = [p for p in points if p.date > detected_date]
    if reference is None or detection is None or not after:
        return STATE_INSUFFICIENT
    span = reference - detection
    if span <= 0:
        # The "drop" is not visible in the index COGs (shouldn't happen for a
        # real ΔNBR detection) — refuse to over-interpret.
        return STATE_INSUFFICIENT
    recent = after[-_RECENT_DATES:]
    recent_mean = sum(p.mean_nbr for p in recent) / len(recent)
    ratio = (recent_mean - detection) / span
    if ratio >= RECOVERED_RATIO:
        return STATE_TRANSIENT
    if ratio <= PERSISTENT_RATIO:
        return STATE_PERSISTENT
    return STATE_RECOVERING


def _utc_date(moment: datetime) -> str:
    return moment.astimezone(UTC).date().isoformat()


def _footprint_statistics_many(
    cog_path: str, footprints: dict[int, dict[str, Any]]
) -> dict[int, tuple[float, int, int] | None]:
    """Per-footprint (mean NBR, valid pixels, total pixels) from ONE open (#170).

    ``None`` entries mean absence — a missing/unreadable file or a footprint
    the scene does not cover — never zeros. The file is opened exactly once
    for all footprints; non-intersecting footprints are rejected on bounds
    before any pixel is read.
    """
    results: dict[int, tuple[float, int, int] | None] = dict.fromkeys(footprints)
    if not Path(cog_path).exists():
        return results
    try:
        with rasterio.open(cog_path) as src:
            for event_id, footprint in footprints.items():
                results[event_id] = _stats_in_open_dataset(src, footprint)
    except (rasterio.errors.RasterioError, ValueError, OSError) as exc:
        logger.debug("trajectory read skipped for %s: %s", cog_path, exc)
    return results


def _stats_in_open_dataset(
    src: rasterio.DatasetReader, footprint: dict[str, Any]
) -> tuple[float, int, int] | None:
    try:
        geom = (
            footprint
            if src.crs is None or src.crs.to_string() == _WGS84
            else rasterio.warp.transform_geom(_WGS84, src.crs, footprint)
        )
        # Bounds pre-check: a tile that never sees this footprint costs a few
        # float comparisons, not a windowed read.
        left, bottom, right, top = rasterio.features.bounds(geom)
        if (
            right < src.bounds.left
            or left > src.bounds.right
            or top < src.bounds.bottom
            or bottom > src.bounds.top
        ):
            return None
        window = rasterio.features.geometry_window(src, [geom])
        # Earth Engine exports masked pixels as NaN with NO nodata tag, so
        # the masked read alone does not exclude them: unmasked NaNs would
        # poison the mean (NaN -> serialized null -> broken client) and
        # inflate the valid count so cloudy looks stop being skipped.
        data = np.ma.masked_invalid(src.read(1, window=window, masked=True))
        transform = src.window_transform(window)
    except (rasterio.errors.RasterioError, ValueError, OSError) as exc:
        logger.debug("trajectory read skipped for %s: %s", src.name, exc)
        return None
    inside = rasterio.features.geometry_mask(
        [geom], out_shape=data.shape, transform=transform, invert=True
    )
    total = int(inside.sum())
    if total == 0:
        return None
    values = np.ma.masked_array(data, mask=np.ma.getmaskarray(data) | ~inside)
    valid = int((~np.ma.getmaskarray(values) & inside).sum())
    if valid == 0:
        return (0.0, 0, total)
    return (float(values.mean()), valid, total)
