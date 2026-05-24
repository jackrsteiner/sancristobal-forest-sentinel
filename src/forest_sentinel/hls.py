"""HLS scene discovery into ``observation`` rows, via Google Earth Engine.

Slice 1 starts from NASA HLS analysis-ready imagery accessed through Earth Engine
(``docs/architecture.md`` §4a). For a configured AOI and time window this module
enumerates intersecting images from the two HLS v2.0 collections and records each as
an ``observation``. Discovery is idempotent: the ``observation`` unique constraint on
``(aoi_id, source_scene_id)`` means a re-run over the same window adds nothing.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel import earthengine
from forest_sentinel.models import Aoi, Observation

# HLS v2.0 collections in the Earth Engine catalog, mapped to the sensor we record.
HLS_COLLECTIONS: dict[str, str] = {
    "NASA/HLS/HLSL30/v002": "HLSL30",  # Landsat 8/9
    "NASA/HLS/HLSS30/v002": "HLSS30",  # Sentinel-2
}


@dataclass(frozen=True)
class DiscoveryResult:
    """Counts from one discovery pass."""

    discovered: int  # images enumerated from Earth Engine
    recorded: int  # new observation rows inserted
    skipped: int  # images already recorded for this AOI


@dataclass(frozen=True)
class HlsGranule:
    """A parsed HLS image: the fields an ``observation`` needs."""

    sensor: str
    source_scene_id: str
    acquired_at: datetime
    cloud_cover_percent: float | None


def parse_granule(feature: Mapping[str, Any], sensor: str) -> HlsGranule:
    """Turn an Earth Engine image feature into an ``HlsGranule``."""
    properties: Mapping[str, Any] = feature.get("properties", {})
    scene_id = properties.get("system:index") or feature.get("id")
    if not scene_id:
        raise ValueError(f"HLS image is missing an identifier: {feature!r}")

    time_start = properties.get("system:time_start")
    if time_start is None:
        raise ValueError(f"HLS image {scene_id!r} is missing system:time_start")
    acquired_at = datetime.fromtimestamp(int(time_start) / 1000, tz=UTC)

    return HlsGranule(
        sensor=sensor,
        source_scene_id=str(scene_id),
        acquired_at=acquired_at,
        cloud_cover_percent=_parse_cloud_cover(properties.get("CLOUD_COVERAGE")),
    )


def discover_observations(
    session: Session,
    aoi: Aoi,
    *,
    since: date,
    until: date,
    ee_module: Any = earthengine,
) -> DiscoveryResult:
    """Discover HLS images for ``aoi`` over [since, until) and record new observations.

    An empty window or an AOI with no available scenes yields zero observations without
    error. New rows are flushed; the caller owns the transaction.
    """
    region = mapping(to_shape(aoi.geometry))
    existing: set[str] = set(
        session.execute(select(Observation.source_scene_id).where(Observation.aoi_id == aoi.id))
        .scalars()
        .all()
    )

    discovered = 0
    recorded = 0
    skipped = 0
    seen_this_run: set[str] = set()
    for collection_id, sensor in HLS_COLLECTIONS.items():
        features = ee_module.list_image_properties(
            collection_id, region, since.isoformat(), until.isoformat()
        )
        for feature in features:
            discovered += 1
            granule = parse_granule(feature, sensor)
            if granule.source_scene_id in existing or granule.source_scene_id in seen_this_run:
                skipped += 1
                continue
            seen_this_run.add(granule.source_scene_id)
            session.add(
                Observation(
                    aoi_id=aoi.id,
                    sensor=granule.sensor,
                    acquired_at=granule.acquired_at,
                    source_scene_id=granule.source_scene_id,
                    cloud_cover_percent=granule.cloud_cover_percent,
                )
            )
            recorded += 1

    session.flush()
    return DiscoveryResult(discovered=discovered, recorded=recorded, skipped=skipped)


def _parse_cloud_cover(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
