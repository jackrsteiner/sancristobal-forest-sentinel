"""Sentinel-1 GRD scene discovery into ``observation`` rows (E16, #115).

Mirrors ``hls.py`` against ``COPERNICUS/S1_GRD``: for a configured AOI and time
window, enumerate intersecting scenes and record each as an ``observation`` with
``sensor="S1GRD"``. Only IW-mode scenes carrying a VV band participate — other
modes/polarisations are enumerated but skipped, so the counts stay honest.

Radar-specific fields: ``orbit_direction`` (ASCENDING/DESCENDING) and
``relative_orbit`` are recorded at discovery because backscatter baselines must
be built from same-orbit-direction scenes (viewing geometry changes backscatter
independently of the ground). ``cloud_cover_percent`` stays null — radar sees
through clouds, which is the whole point of the augmentation.

Discovery is idempotent under concurrency exactly like HLS discovery:
known scenes are skipped up front and inserts go through ON CONFLICT DO NOTHING
on the ``(aoi_id, source_scene_id)`` unique constraint.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from forest_sentinel import earthengine
from forest_sentinel.hls import DiscoveryResult
from forest_sentinel.models import Aoi, Observation

S1_COLLECTION = "COPERNICUS/S1_GRD"
S1_SENSOR = "S1GRD"

_REQUIRED_MODE = "IW"
_REQUIRED_BAND = "VV"


@dataclass(frozen=True)
class S1Scene:
    """A parsed, eligible Sentinel-1 GRD scene."""

    source_scene_id: str
    acquired_at: datetime
    orbit_direction: str
    relative_orbit: int | None


def parse_scene(feature: Mapping[str, Any]) -> S1Scene | None:
    """Parse an EE image feature; ``None`` for ineligible (non-IW / no-VV) scenes.

    Missing identifiers or timestamps on an *eligible* scene raise — silently
    dropping data the filters said we wanted would hide catalog problems.
    """
    properties: Mapping[str, Any] = feature.get("properties", {})
    mode = properties.get("instrumentMode")
    polarisations = properties.get("transmitterReceiverPolarisation") or []
    if mode != _REQUIRED_MODE or _REQUIRED_BAND not in polarisations:
        return None

    scene_id = properties.get("system:index")
    if not scene_id:
        fallback = feature.get("id")
        scene_id = str(fallback).rsplit("/", 1)[-1] if fallback else None
    if not scene_id:
        raise ValueError(f"Sentinel-1 image is missing an identifier: {feature!r}")

    time_start = properties.get("system:time_start")
    if time_start is None:
        raise ValueError(f"Sentinel-1 image {scene_id!r} is missing system:time_start")

    orbit_direction = properties.get("orbitProperties_pass")
    if orbit_direction not in ("ASCENDING", "DESCENDING"):
        raise ValueError(f"Sentinel-1 image {scene_id!r} has no orbit direction")

    relative_orbit = properties.get("relativeOrbitNumber_start")
    return S1Scene(
        source_scene_id=str(scene_id),
        acquired_at=datetime.fromtimestamp(int(time_start) / 1000, tz=UTC),
        orbit_direction=str(orbit_direction),
        relative_orbit=int(relative_orbit) if relative_orbit is not None else None,
    )


def discover_radar_observations(
    session: Session,
    aoi: Aoi,
    *,
    since: date,
    until: date,
    ee_module: Any = earthengine,
) -> DiscoveryResult:
    """Discover S1 GRD scenes for ``aoi`` over [since, until); record new observations."""
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
    features = ee_module.list_image_properties(
        S1_COLLECTION, region, since.isoformat(), until.isoformat()
    )
    for feature in features:
        discovered += 1
        scene = parse_scene(feature)
        if scene is None:  # ineligible mode/polarisation
            skipped += 1
            continue
        if scene.source_scene_id in existing or scene.source_scene_id in seen_this_run:
            skipped += 1
            continue
        seen_this_run.add(scene.source_scene_id)
        inserted_id = session.execute(
            pg_insert(Observation)
            .values(
                aoi_id=aoi.id,
                sensor=S1_SENSOR,
                acquired_at=scene.acquired_at,
                source_scene_id=scene.source_scene_id,
                cloud_cover_percent=None,
                orbit_direction=scene.orbit_direction,
                relative_orbit=scene.relative_orbit,
            )
            .on_conflict_do_nothing(constraint="uq_observation_aoi_id_source_scene_id")
            .returning(Observation.id)
        ).scalar_one_or_none()
        if inserted_id is not None:
            recorded += 1
        else:
            skipped += 1

    session.flush()
    return DiscoveryResult(discovered=discovered, recorded=recorded, skipped=skipped)
