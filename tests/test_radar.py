"""Radar backscatter change (E16, #116): VV dB delta vs same-orbit trailing median."""

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.events import track_events_for_aoi
from forest_sentinel.models import Aoi, ChangeRaster, DisturbanceCandidate, Observation
from forest_sentinel.radar import RADAR_CHANGE_TYPE, compute_radar_change_for_observation
from forest_sentinel.sentinel1 import S1_COLLECTION, S1_SENSOR
from tests.fakes import FakeEarthEngine, FakeStorage, make_aoi, make_methodology

_PATCH = [(0.1, 0.1), (0.2, 0.1), (0.2, 0.2), (0.1, 0.2), (0.1, 0.1)]


def _s1_observation(
    session: Session, aoi: Aoi, *, day: int, orbit: str = "DESCENDING"
) -> Observation:
    obs = Observation(
        aoi_id=aoi.id,
        sensor=S1_SENSOR,
        acquired_at=datetime(2026, 1, day, tzinfo=UTC),
        source_scene_id=f"S1A_{orbit[:3]}_{day}",
        orbit_direction=orbit,
        relative_orbit=18,
    )
    session.add(obs)
    session.flush()
    return obs


def test_delta_lineage_uses_same_orbit_trailing_median(db_session: Session, tmp_path: Path) -> None:
    aoi = make_aoi(db_session)
    methodology = make_methodology(db_session, version="radar-1", parameters={"metric": "vv_db"})
    for day in (1, 3, 5):
        _s1_observation(db_session, aoi, day=day)
    # A cross-orbit prior: viewing geometry differs — must NOT enter the baseline.
    _s1_observation(db_session, aoi, day=4, orbit="ASCENDING")
    current = _s1_observation(db_session, aoi, day=8)
    fake = FakeEarthEngine()
    storage = FakeStorage(tmp_path)

    products = compute_radar_change_for_observation(
        db_session,
        aoi=aoi,
        observation=current,
        methodology=methodology,
        storage=storage,
        ee_module=fake,
    )

    (product,) = products
    assert product.change_type == RADAR_CHANGE_TYPE
    # Exactly the three same-orbit priors were reduced into the median.
    assert fake.median_sizes == [3]
    image, key, scale = storage.exports[0]
    current_vv = {"band": ("VV", {"id": f"{S1_COLLECTION}/{current.source_scene_id}"})}
    assert image == {"delta": (current_vv, {"median": 3})}
    assert key.product == RADAR_CHANGE_TYPE
    assert scale == 30

    row = db_session.execute(select(ChangeRaster)).scalar_one()
    assert row.change_type == RADAR_CHANGE_TYPE
    # Baseline provenance: the ordered scene-id recipe (newest first).
    assert row.baseline_source_scene_ids == ["S1A_DES_5", "S1A_DES_3", "S1A_DES_1"]
    assert row.baseline_window == 5


def test_no_same_orbit_priors_skips_the_observation(db_session: Session, tmp_path: Path) -> None:
    aoi = make_aoi(db_session)
    methodology = make_methodology(db_session, version="radar-1", parameters={"metric": "vv_db"})
    _s1_observation(db_session, aoi, day=4, orbit="ASCENDING")
    current = _s1_observation(db_session, aoi, day=8)  # DESCENDING, no same-orbit prior
    storage = FakeStorage(tmp_path)

    products = compute_radar_change_for_observation(
        db_session,
        aoi=aoi,
        observation=current,
        methodology=methodology,
        storage=storage,
        ee_module=FakeEarthEngine(),
    )

    assert products == []
    assert storage.exports == []


def test_existing_row_and_cog_is_reused_without_export(db_session: Session, tmp_path: Path) -> None:
    aoi = make_aoi(db_session)
    methodology = make_methodology(db_session, version="radar-1", parameters={"metric": "vv_db"})
    _s1_observation(db_session, aoi, day=1)
    current = _s1_observation(db_session, aoi, day=8)
    fake = FakeEarthEngine()
    storage = FakeStorage(tmp_path)

    first = compute_radar_change_for_observation(
        db_session,
        aoi=aoi,
        observation=current,
        methodology=methodology,
        storage=storage,
        ee_module=fake,
    )
    assert first[0].reused is False
    again = compute_radar_change_for_observation(
        db_session,
        aoi=aoi,
        observation=current,
        methodology=methodology,
        storage=storage,
        ee_module=fake,
    )

    assert again[0].reused is True
    assert again[0].delta_image is None
    assert len(storage.exports) == 1  # no second export


def test_frozen_raster_is_never_recomputed(db_session: Session, tmp_path: Path) -> None:
    from geoalchemy2.shape import from_shape
    from shapely.geometry import Polygon

    aoi = make_aoi(db_session)
    methodology = make_methodology(db_session, version="radar-1", parameters={"metric": "vv_db"})
    _s1_observation(db_session, aoi, day=1)
    current = _s1_observation(db_session, aoi, day=8)
    storage = FakeStorage(tmp_path)
    (product,) = compute_radar_change_for_observation(
        db_session,
        aoi=aoi,
        observation=current,
        methodology=methodology,
        storage=storage,
        ee_module=FakeEarthEngine(),
    )
    # Track a candidate from this raster into an event -> frozen.
    db_session.add(
        DisturbanceCandidate(
            change_raster_id=product.change_raster.id,
            methodology_version_id=methodology.id,
            geometry=from_shape(Polygon(_PATCH), srid=4326),
            detected_at=current.acquired_at,
            area_m2=9_000.0,
        )
    )
    db_session.flush()
    track_events_for_aoi(db_session, aoi=aoi)
    # Remove the COG: a frozen raster must STILL not be recomputed.
    Path(product.change_raster.cog_path).unlink()

    (frozen,) = compute_radar_change_for_observation(
        db_session,
        aoi=aoi,
        observation=current,
        methodology=methodology,
        storage=storage,
        ee_module=FakeEarthEngine(),
    )

    assert frozen.delta_image is None
    assert frozen.reused is False
    assert len(storage.exports) == 1
