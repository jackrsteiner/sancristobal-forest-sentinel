"""Shared test doubles and database seed helpers.

``FakeEarthEngine``/``FakeStorage`` cover the union of the Earth Engine and storage
seams the pipeline, indices, change, and candidates tests stub out; they return plain
Python values and record the interactions tests assert on. The ``make_*`` helpers seed
the common row shapes (unit-square AOI, observation, change raster, and the
Observation -> ChangeRaster -> DisturbanceCandidate chain).
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy.orm import Session

from forest_sentinel.methodology import get_or_create_methodology_version
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    DisturbanceCandidate,
    MethodologyVersion,
    Observation,
)
from forest_sentinel.storage import CogKey, ExportRequest, StorageError

UNIT_SQUARE = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])


class FakeEarthEngine:
    """Stubs every EE operation the code under test touches; returns plain Python.

    Configurable per test: ``scenes`` maps a collection id to the scene dicts returned
    by ``list_image_properties`` (other collections yield ``[]``); ``features`` is the
    GeoJSON feature list returned by ``threshold_and_vectorize``; ``valid_fraction`` is
    the reported valid-pixel fraction. Recorded interactions: ``image_ids``,
    ``nd_bands``, ``median_sizes``, and ``calls`` (threshold_and_vectorize kwargs).
    """

    def __init__(
        self,
        *,
        scenes: dict[str, list[dict[str, Any]]] | None = None,
        features: list[dict[str, Any]] | None = None,
        valid_fraction: float = 0.9,
    ) -> None:
        self._scenes = scenes or {}
        self._features = features or []
        self._valid_fraction = valid_fraction
        self.image_ids: list[str] = []
        self.nd_bands: list[list[str]] = []
        self.median_sizes: list[int] = []
        self.calls: list[dict[str, Any]] = []

    def list_image_properties(
        self, collection_id: str, region: Any, since: str, until: str
    ) -> list[dict[str, Any]]:
        return self._scenes.get(collection_id, [])

    def image_by_id(self, image_id: str) -> dict[str, Any]:
        self.image_ids.append(image_id)
        return {"id": image_id}

    def apply_fmask_mask(self, image: Any) -> dict[str, Any]:
        return {"masked": image}

    def valid_pixel_fraction(self, image: Any, band: str, region: Any, scale: int) -> float:
        return self._valid_fraction

    def normalized_difference(self, image: Any, bands: list[str]) -> dict[str, Any]:
        self.nd_bands.append(list(bands))
        return {"nd": tuple(bands), "image": image}

    def median_of(self, images: list[Any]) -> dict[str, Any]:
        self.median_sizes.append(len(images))
        return {"median": len(images)}

    def subtract(self, image: Any, other: Any) -> dict[str, Any]:
        return {"delta": (image, other)}

    def threshold_and_vectorize(
        self, delta_image: Any, *, threshold: float, scale: int, region: Any, min_area_m2: float
    ) -> list[dict[str, Any]]:
        self.calls.append({"threshold": threshold, "scale": scale, "min_area_m2": min_area_m2})
        return self._features


class FakeStorage:
    """Local-path storage double; records each export as ``(image, key, scale)``.

    ``export_images`` also records each batch's size in ``batch_sizes`` so tests can
    assert exports were submitted together, and ``fail_products`` lets a test turn
    specific products' exports into per-item ``StorageError`` results. Exported paths
    are written as real (empty) files so the skip-unchanged existence check sees them.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.exports: list[tuple[Any, CogKey, int | None]] = []
        self.batch_sizes: list[int] = []
        self.fail_products: set[str] = set()

    def path_for(self, key: CogKey) -> Path:
        return self.root / key.relative_path()

    def export_image(
        self, image: Any, key: CogKey, *, scale: int | None = None, region: Any = None
    ) -> Path:
        result = self.export_images([ExportRequest(image, key, scale=scale, region=region)])[0]
        if isinstance(result, StorageError):
            raise result
        return result

    def export_images(self, requests: Sequence[ExportRequest]) -> list[Path | StorageError]:
        self.batch_sizes.append(len(requests))
        results: list[Path | StorageError] = []
        for request in requests:
            if request.key.product in self.fail_products:
                results.append(StorageError(f"forced failure for {request.key.product}"))
                continue
            self.exports.append((request.image, request.key, request.scale))
            path = self.path_for(request.key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            results.append(path)
        return results


def make_aoi(session: Session, *, name: str = "Test AOI") -> Aoi:
    """Insert an AOI covering the unit square (0,0)-(1,1) in WGS 84."""
    aoi = Aoi(name=name, geometry=from_shape(MultiPolygon([UNIT_SQUARE]), srid=4326))
    session.add(aoi)
    session.flush()
    return aoi


def make_methodology(
    session: Session, *, version: str = "1.0.0", parameters: dict[str, Any] | None = None
) -> MethodologyVersion:
    """Get or create the standard optical-change methodology version."""
    return get_or_create_methodology_version(
        session, name="optical-change", version=version, parameters=parameters or {}
    )


def make_observation(
    session: Session,
    aoi: Aoi,
    *,
    day: int = 1,
    sensor: str = "HLSL30",
    source_scene_id: str | None = None,
    acquired_at: datetime | None = None,
) -> Observation:
    """Insert an Observation; defaults to 2026-01-<day> and scene id ``scene-<day>``."""
    obs = Observation(
        aoi_id=aoi.id,
        sensor=sensor,
        acquired_at=acquired_at or datetime(2026, 1, day, tzinfo=UTC),
        source_scene_id=source_scene_id or f"scene-{day}",
    )
    session.add(obs)
    session.flush()
    return obs


def make_change_raster(
    session: Session,
    observation: Observation,
    methodology: MethodologyVersion,
    *,
    cog_path: str,
    change_type: str = "delta_nbr",
    baseline_window: int = 5,
) -> ChangeRaster:
    """Insert a ChangeRaster row for the given observation."""
    change = ChangeRaster(
        observation_id=observation.id,
        methodology_version_id=methodology.id,
        change_type=change_type,
        cog_path=cog_path,
        baseline_window=baseline_window,
    )
    session.add(change)
    session.flush()
    return change


def make_candidate(
    session: Session,
    aoi: Aoi,
    methodology: MethodologyVersion,
    *,
    day: int,
    ring: list[tuple[float, float]],
    area_m2: float,
) -> DisturbanceCandidate:
    """Seed one detection: Observation -> ChangeRaster -> DisturbanceCandidate."""
    detected = datetime(2026, 1, day, tzinfo=UTC)
    obs = make_observation(session, aoi, day=day)
    change = make_change_raster(session, obs, methodology, cog_path=f"/cogs/{day}.tif")
    candidate = DisturbanceCandidate(
        change_raster_id=change.id,
        methodology_version_id=methodology.id,
        geometry=from_shape(Polygon(ring), srid=4326),
        detected_at=detected,
        area_m2=area_m2,
    )
    session.add(candidate)
    session.flush()
    return candidate
