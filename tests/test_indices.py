from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel import indices
from forest_sentinel.indices import compute_indices_for_observation, index_bands
from forest_sentinel.models import (
    Aoi,
    IndexRaster,
    MethodologyVersion,
    Observation,
    QualityMask,
)
from forest_sentinel.storage import CogKey
from tests.fakes import (
    FakeEarthEngine,
    FakeStorage,
    make_aoi,
    make_methodology,
    make_observation,
)


def _setup(session: Session, sensor: str) -> tuple[Aoi, Observation, MethodologyVersion]:
    aoi = make_aoi(session)
    obs = make_observation(session, aoi, day=2, sensor=sensor, source_scene_id="HLS.scene.X")
    methodology = make_methodology(session, parameters={"ee_script_version": "v1"})
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
        assert row.raster_lineage_id == methodology.raster_lineage_id
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
    obs_s30 = make_observation(
        db_session,
        aoi,
        sensor="HLSS30",
        source_scene_id="HLS.scene.Y",
        acquired_at=obs_l30.acquired_at,
    )

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
        aoi = make_aoi(db_session, name=name)
        obs2 = make_observation(
            db_session,
            aoi,
            sensor="HLSL30",
            source_scene_id="HLS.scene.X",
            acquired_at=obs.acquired_at,
        )
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


# --- Region clipping (#78): exports and QA reduce over scene footprint ∩ AOI ---

# Overlaps the unit-square AOI in its upper-right quarter.
_FOOTPRINT = {
    "type": "Polygon",
    "coordinates": [[[0.5, 0.5], [2.0, 0.5], [2.0, 2.0], [0.5, 2.0], [0.5, 0.5]]],
}


def test_exports_and_qa_are_clipped_to_scene_footprint(db_session: Session, tmp_path: Path) -> None:
    from geoalchemy2.shape import to_shape
    from shapely.geometry import shape

    aoi, obs, methodology = _setup(db_session, "HLSL30")
    fake_ee = FakeEarthEngine(footprint=_FOOTPRINT)
    storage = FakeStorage(tmp_path)

    compute_indices_for_observation(
        db_session,
        aoi=aoi,
        observation=obs,
        methodology=methodology,
        storage=storage,
        ee_module=fake_ee,
    )

    expected = to_shape(aoi.geometry).intersection(shape(_FOOTPRINT))
    assert fake_ee.footprint_calls == 1  # one clip per observation, shared by both exports
    assert len(storage.export_regions) == 2
    for region in storage.export_regions:
        assert shape(region).equals(expected)
    # The valid fraction reduces over the same clipped region — it now means
    # "valid within the scene's AOI coverage".
    assert shape(fake_ee.fraction_regions[0]).equals(expected)


def test_missing_footprint_falls_back_to_the_whole_aoi(db_session: Session, tmp_path: Path) -> None:
    from geoalchemy2.shape import to_shape
    from shapely.geometry import shape

    aoi, obs, methodology = _setup(db_session, "HLSL30")
    fake_ee = FakeEarthEngine()  # no footprint -> scene_footprint raises
    storage = FakeStorage(tmp_path)

    compute_indices_for_observation(
        db_session,
        aoi=aoi,
        observation=obs,
        methodology=methodology,
        storage=storage,
        ee_module=fake_ee,
    )

    for region in storage.export_regions:
        assert shape(region).equals(to_shape(aoi.geometry))
    assert shape(fake_ee.fraction_regions[0]).equals(to_shape(aoi.geometry))


def test_single_tile_aoi_clips_to_itself(db_session: Session, tmp_path: Path) -> None:
    """An AOI inside one scene footprint behaves identically to today (#78)."""
    from geoalchemy2.shape import to_shape
    from shapely.geometry import shape

    covering = {
        "type": "Polygon",
        "coordinates": [[[-1.0, -1.0], [2.0, -1.0], [2.0, 2.0], [-1.0, 2.0], [-1.0, -1.0]]],
    }
    aoi, obs, methodology = _setup(db_session, "HLSL30")
    storage = FakeStorage(tmp_path)

    compute_indices_for_observation(
        db_session,
        aoi=aoi,
        observation=obs,
        methodology=methodology,
        storage=storage,
        ee_module=FakeEarthEngine(footprint=covering),
    )

    for region in storage.export_regions:
        assert shape(region).equals(to_shape(aoi.geometry))
