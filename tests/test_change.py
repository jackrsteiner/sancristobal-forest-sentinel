from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel import indices
from forest_sentinel.change import (
    CHANGE_TYPES,
    compute_change_products_for_observation,
)
from forest_sentinel.methodology import get_or_create_methodology_version
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    ChangeRasterSource,
    MethodologyVersion,
    Observation,
)
from forest_sentinel.storage import CogKey


class FakeEarthEngine:
    def __init__(self) -> None:
        self.median_sizes: list[int] = []

    def image_by_id(self, image_id: str) -> dict[str, Any]:
        return {"id": image_id}

    def apply_fmask_mask(self, image: Any) -> dict[str, Any]:
        return {"masked": image}

    def valid_pixel_fraction(self, image: Any, band: str, region: Any, scale: int) -> float:
        return 0.9

    def normalized_difference(self, image: Any, bands: list[str]) -> dict[str, Any]:
        return {"nd": tuple(bands), "image": image}

    def median_of(self, images: list[Any]) -> dict[str, Any]:
        self.median_sizes.append(len(images))
        return {"median": len(images)}

    def subtract(self, image: Any, other: Any) -> dict[str, Any]:
        return {"delta": (image, other)}


class FakeStorage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.exports: list[CogKey] = []

    def path_for(self, key: CogKey) -> Path:
        return self.root / key.relative_path()

    def export_image(
        self, image: Any, key: CogKey, *, scale: int | None = None, region: Any = None
    ) -> Path:
        self.exports.append(key)
        return self.path_for(key)


def _aoi(session: Session) -> Aoi:
    square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
    aoi = Aoi(name="Test AOI", geometry=from_shape(MultiPolygon([square]), srid=4326))
    session.add(aoi)
    session.flush()
    return aoi


def _observation(session: Session, aoi: Aoi, day: int) -> Observation:
    obs = Observation(
        aoi_id=aoi.id,
        sensor="HLSL30",
        acquired_at=datetime(2026, 1, day, tzinfo=UTC),
        source_scene_id=f"scene-{day}",
    )
    session.add(obs)
    session.flush()
    return obs


def _methodology(session: Session) -> MethodologyVersion:
    return get_or_create_methodology_version(
        session, name="optical-change", version="1.0.0", parameters={"baseline_window": 5}
    )


def _build_history(
    session: Session, days: list[int], fake_ee: FakeEarthEngine, tmp_path: Path
) -> tuple[Aoi, list[Observation], MethodologyVersion]:
    """Create observations on the given days and compute their index rasters."""
    aoi = _aoi(session)
    methodology = _methodology(session)
    observations = [_observation(session, aoi, day) for day in days]
    storage = FakeStorage(tmp_path / "indices")
    for obs in observations:
        indices.compute_indices_for_observation(
            session,
            aoi=aoi,
            observation=obs,
            methodology=methodology,
            storage=storage,
            ee_module=fake_ee,
        )
    session.flush()
    return aoi, observations, methodology


def test_change_types_map_to_indices() -> None:
    assert CHANGE_TYPES == {"delta_nbr": "NBR", "delta_ndvi": "NDVI"}


def test_computes_delta_against_trailing_median(db_session: Session, tmp_path: Path) -> None:
    fake_ee = FakeEarthEngine()
    aoi, observations, methodology = _build_history(
        db_session, [1, 2, 3, 4, 5, 6], fake_ee, tmp_path
    )
    current = observations[-1]

    results = compute_change_products_for_observation(
        db_session,
        aoi=aoi,
        observation=current,
        methodology=methodology,
        storage=FakeStorage(tmp_path / "change"),
        baseline_window=5,
        ee_module=fake_ee,
    )
    db_session.commit()

    assert {r.change_type for r in results} == {"delta_nbr", "delta_ndvi"}
    # Each change product reduced exactly 5 baseline index images.
    assert fake_ee.median_sizes == [5, 5]

    rows = db_session.execute(select(ChangeRaster)).scalars().all()
    assert len(rows) == 2
    for row in rows:
        assert row.observation_id == current.id
        assert row.methodology_version_id == methodology.id
        assert row.baseline_window == 5
        assert row.valid_pixel_fraction == 0.9
        assert row.cog_path.endswith(f"{row.change_type}.tif")

    # Provenance: current + 5 baselines = 6 contributing index rasters per change type.
    sources = db_session.execute(select(ChangeRasterSource)).scalars().all()
    assert len(sources) == 12


def test_no_baseline_is_skipped(db_session: Session, tmp_path: Path) -> None:
    fake_ee = FakeEarthEngine()
    aoi, observations, methodology = _build_history(db_session, [1], fake_ee, tmp_path)

    results = compute_change_products_for_observation(
        db_session,
        aoi=aoi,
        observation=observations[0],
        methodology=methodology,
        storage=FakeStorage(tmp_path / "change"),
        ee_module=fake_ee,
    )
    assert results == []
    assert db_session.execute(select(ChangeRaster)).scalars().all() == []


def test_baseline_window_limits_history(db_session: Session, tmp_path: Path) -> None:
    fake_ee = FakeEarthEngine()
    aoi, observations, methodology = _build_history(
        db_session, [1, 2, 3, 4, 5, 6, 7], fake_ee, tmp_path
    )
    compute_change_products_for_observation(
        db_session,
        aoi=aoi,
        observation=observations[-1],
        methodology=methodology,
        storage=FakeStorage(tmp_path / "change"),
        baseline_window=3,
        ee_module=fake_ee,
    )
    assert fake_ee.median_sizes == [3, 3]


def test_rerun_replaces_sources(db_session: Session, tmp_path: Path) -> None:
    fake_ee = FakeEarthEngine()
    aoi, observations, methodology = _build_history(db_session, [1, 2, 3], fake_ee, tmp_path)
    current = observations[-1]
    for _ in range(2):
        compute_change_products_for_observation(
            db_session,
            aoi=aoi,
            observation=current,
            methodology=methodology,
            storage=FakeStorage(tmp_path / "change"),
            baseline_window=5,
            ee_module=fake_ee,
        )
        db_session.commit()

    assert len(db_session.execute(select(ChangeRaster)).scalars().all()) == 2
    # current + 2 baselines = 3 sources per change type, not doubled on re-run.
    assert len(db_session.execute(select(ChangeRasterSource)).scalars().all()) == 6
