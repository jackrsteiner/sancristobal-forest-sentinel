from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel import indices
from forest_sentinel.indices import compute_indices_for_observation, index_bands
from forest_sentinel.methodology import get_or_create_methodology_version
from forest_sentinel.models import (
    Aoi,
    IndexRaster,
    MethodologyVersion,
    Observation,
    QualityMask,
)
from forest_sentinel.storage import CogKey


class FakeEarthEngine:
    def __init__(self) -> None:
        self.image_ids: list[str] = []
        self.nd_bands: list[list[str]] = []

    def image_by_id(self, image_id: str) -> dict[str, Any]:
        self.image_ids.append(image_id)
        return {"id": image_id}

    def apply_fmask_mask(self, image: Any) -> dict[str, Any]:
        return {"masked": image}

    def valid_pixel_fraction(self, image: Any, band: str, region: Any, scale: int) -> float:
        return 0.9

    def normalized_difference(self, image: Any, bands: list[str]) -> dict[str, Any]:
        self.nd_bands.append(list(bands))
        return {"nd": tuple(bands)}


class FakeStorage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.exports: list[tuple[Any, CogKey, int | None]] = []

    def path_for(self, key: CogKey) -> Path:
        return self.root / key.relative_path()

    def export_image(
        self, image: Any, key: CogKey, *, scale: int | None = None, region: Any = None
    ) -> Path:
        self.exports.append((image, key, scale))
        return self.path_for(key)


def _setup(session: Session, sensor: str) -> tuple[Aoi, Observation, MethodologyVersion]:
    square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
    aoi = Aoi(name="Test AOI", geometry=from_shape(MultiPolygon([square]), srid=4326))
    session.add(aoi)
    session.flush()
    obs = Observation(
        aoi_id=aoi.id,
        sensor=sensor,
        acquired_at=datetime(2026, 1, 2, tzinfo=UTC),
        source_scene_id="HLS.scene.X",
    )
    session.add(obs)
    session.flush()
    methodology = get_or_create_methodology_version(
        session, name="optical-change", version="1.0.0", parameters={"ee_script_version": "v1"}
    )
    return aoi, obs, methodology


def test_index_bands_per_sensor() -> None:
    assert index_bands("HLSL30") == {"NBR": ["B5", "B7"], "NDVI": ["B5", "B4"]}
    assert index_bands("HLSS30") == {"NBR": ["B8A", "B12"], "NDVI": ["B8A", "B4"]}


def test_index_bands_unknown_sensor() -> None:
    with pytest.raises(ValueError, match="unsupported sensor"):
        index_bands("MODIS")


def test_computes_nbr_and_ndvi_for_landsat(db_session: Session, tmp_path: Path) -> None:
    aoi, obs, methodology = _setup(db_session, "HLSL30")
    fake_ee = FakeEarthEngine()
    storage = FakeStorage(tmp_path)

    results = compute_indices_for_observation(
        db_session,
        aoi=aoi,
        observation=obs,
        methodology=methodology,
        storage=storage,
        ee_module=fake_ee,
    )
    db_session.commit()

    assert {r.index_type for r in results} == {"NBR", "NDVI"}
    # Source image rebuilt from collection + scene id; NBR uses [NIR, SWIR2], NDVI uses [NIR, RED].
    assert fake_ee.image_ids == ["NASA/HLS/HLSL30/v002/HLS.scene.X"]
    assert fake_ee.nd_bands == [["B5", "B7"], ["B5", "B4"]]

    rows = db_session.execute(select(IndexRaster).order_by(IndexRaster.index_type)).scalars().all()
    assert [r.index_type for r in rows] == ["NBR", "NDVI"]
    for row in rows:
        assert row.observation_id == obs.id
        assert row.methodology_version_id == methodology.id
        assert row.valid_pixel_fraction == 0.9
        # The path carries the index type and the (sanitized) source scene id.
        assert row.cog_path.endswith(f"{row.index_type.lower()}-hls.scene.x.tif")


def test_uses_sentinel_band_mapping(db_session: Session, tmp_path: Path) -> None:
    aoi, obs, methodology = _setup(db_session, "HLSS30")
    fake_ee = FakeEarthEngine()
    compute_indices_for_observation(
        db_session,
        aoi=aoi,
        observation=obs,
        methodology=methodology,
        storage=FakeStorage(tmp_path),
        ee_module=fake_ee,
    )
    assert fake_ee.nd_bands == [["B8A", "B12"], ["B8A", "B4"]]


def test_export_keys_carry_product_and_date(db_session: Session, tmp_path: Path) -> None:
    aoi, obs, methodology = _setup(db_session, "HLSL30")
    storage = FakeStorage(tmp_path)
    compute_indices_for_observation(
        db_session,
        aoi=aoi,
        observation=obs,
        methodology=methodology,
        storage=storage,
        ee_module=FakeEarthEngine(),
    )
    keys = [key for _, key, _ in storage.exports]
    assert {k.product for k in keys} == {"NBR", "NDVI"}
    assert all(k.date == "2026-01-02" for k in keys)
    assert all(scale == indices.DEFAULT_SCALE_METERS for _, _, scale in storage.exports)


def test_same_day_observations_export_to_distinct_paths(
    db_session: Session, tmp_path: Path
) -> None:
    """Two observations on the same date (e.g. HLSL30 + HLSS30) must not share a COG
    path, or the second export silently overwrites the first (audit BUG-4)."""
    aoi, obs_l30, methodology = _setup(db_session, "HLSL30")
    obs_s30 = Observation(
        aoi_id=aoi.id,
        sensor="HLSS30",
        acquired_at=obs_l30.acquired_at,
        source_scene_id="HLS.scene.Y",
    )
    db_session.add(obs_s30)
    db_session.flush()

    storage = FakeStorage(tmp_path)
    for obs in (obs_l30, obs_s30):
        compute_indices_for_observation(
            db_session,
            aoi=aoi,
            observation=obs,
            methodology=methodology,
            storage=storage,
            ee_module=FakeEarthEngine(),
        )

    paths = [storage.path_for(key) for _, key, _ in storage.exports]
    assert len(paths) == 4  # 2 observations x (NBR, NDVI)
    assert len(set(paths)) == 4


def test_aois_with_colliding_sanitized_names_get_distinct_paths(
    db_session: Session, tmp_path: Path
) -> None:
    """'My AOI' and 'my-aoi' both sanitize to 'my-aoi'; the AOI id prefix must keep
    their COG trees separate (audit BUG-12)."""
    _, obs, methodology = _setup(db_session, "HLSL30")
    keys: list[CogKey] = []
    for name in ("My AOI", "my-aoi"):
        aoi = Aoi(name=name, geometry=db_session.execute(select(Aoi.geometry)).scalars().first())
        db_session.add(aoi)
        db_session.flush()
        obs2 = Observation(
            aoi_id=aoi.id,
            sensor="HLSL30",
            acquired_at=obs.acquired_at,
            source_scene_id="HLS.scene.X",
        )
        db_session.add(obs2)
        db_session.flush()
        storage = FakeStorage(tmp_path)
        compute_indices_for_observation(
            db_session,
            aoi=aoi,
            observation=obs2,
            methodology=methodology,
            storage=storage,
            ee_module=FakeEarthEngine(),
        )
        keys.extend(key for _, key, _ in storage.exports)

    paths = {str(key.relative_path()) for key in keys}
    assert len(paths) == 4  # 2 AOIs x (NBR, NDVI) — no shared tree
    assert all(path.split("/", 1)[0].split("-", 1)[0].isdigit() for path in paths)


def test_records_quality_mask_coverage(db_session: Session, tmp_path: Path) -> None:
    aoi, obs, methodology = _setup(db_session, "HLSL30")
    compute_indices_for_observation(
        db_session,
        aoi=aoi,
        observation=obs,
        methodology=methodology,
        storage=FakeStorage(tmp_path),
        ee_module=FakeEarthEngine(),
    )
    db_session.commit()
    mask = db_session.execute(select(QualityMask)).scalar_one()
    assert mask.observation_id == obs.id
    assert mask.valid_pixel_fraction == 0.9


def test_rerun_upserts_rather_than_duplicates(db_session: Session, tmp_path: Path) -> None:
    aoi, obs, methodology = _setup(db_session, "HLSL30")
    for _ in range(2):
        compute_indices_for_observation(
            db_session,
            aoi=aoi,
            observation=obs,
            methodology=methodology,
            storage=FakeStorage(tmp_path),
            ee_module=FakeEarthEngine(),
        )
        db_session.commit()

    rows = db_session.execute(select(IndexRaster)).scalars().all()
    assert len(rows) == 2  # still just NBR + NDVI


def test_unknown_sensor_is_rejected(db_session: Session, tmp_path: Path) -> None:
    aoi, obs, methodology = _setup(db_session, "HLSL30")
    obs.sensor = "MODIS"
    db_session.flush()
    with pytest.raises(ValueError, match="unsupported sensor"):
        compute_indices_for_observation(
            db_session,
            aoi=aoi,
            observation=obs,
            methodology=methodology,
            storage=FakeStorage(tmp_path),
            ee_module=FakeEarthEngine(),
        )
