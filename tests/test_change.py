from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel import indices
from forest_sentinel.change import (
    CHANGE_TYPES,
    compute_change_products_for_observation,
)
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    ChangeRasterSource,
    MethodologyVersion,
    Observation,
)
from tests.fakes import (
    FakeEarthEngine,
    FakeStorage,
    make_aoi,
    make_methodology,
    make_observation,
)


def _build_history(
    session: Session, days: list[int], fake_ee: FakeEarthEngine, tmp_path: Path
) -> tuple[Aoi, list[Observation], MethodologyVersion]:
    """Create observations on the given days and compute their index rasters."""
    aoi = make_aoi(session)
    methodology = make_methodology(session, parameters={"baseline_window": 5})
    observations = [make_observation(session, aoi, day=day) for day in days]
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
        # The path carries the change type and the (sanitized) source scene id.
        assert row.cog_path.endswith(f"{row.change_type}-scene-6.tif")

    # Provenance: current + 5 baselines = 6 contributing index rasters per change type.
    sources = db_session.execute(select(ChangeRasterSource)).scalars().all()
    assert len(sources) == 12


def test_baseline_excludes_observations_without_index_rasters(
    db_session: Session, tmp_path: Path
) -> None:
    """A prior observation with no index raster (failed export, or pre-methodology)
    must be excluded from the baseline median, not silently omitted from the recorded
    provenance while its imagery is still used (re-audit round 3, finding 1)."""
    fake_ee = FakeEarthEngine()
    aoi, observations, methodology = _build_history(db_session, [1, 3], fake_ee, tmp_path)
    # Day-2 observation exists but its index exports failed: no index rasters.
    make_observation(db_session, aoi, day=2)

    compute_change_products_for_observation(
        db_session,
        aoi=aoi,
        observation=observations[-1],  # day 3
        methodology=methodology,
        storage=FakeStorage(tmp_path / "change"),
        ee_module=fake_ee,
    )
    db_session.commit()

    # The median reduced only day-1's imagery (not day-2's) for each change type...
    assert fake_ee.median_sizes == [1, 1]
    # ...and the recorded provenance matches exactly: current + day-1, per type.
    sources = db_session.execute(select(ChangeRasterSource)).scalars().all()
    assert len(sources) == 4  # 2 change types x (current + 1 baseline)


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


def test_frozen_change_rasters_are_not_recomputed(db_session: Session, tmp_path: Path) -> None:
    """Once a raster's candidates are tracked into events, a re-run must not
    re-export its COG or rewrite its recorded sources (re-audit R2) — even when a
    late-arriving observation would change the baseline."""
    from datetime import UTC, datetime

    from geoalchemy2.shape import from_shape
    from shapely.geometry import Polygon

    from forest_sentinel.events import track_events_for_aoi
    from forest_sentinel.models import AOI_SRID, DisturbanceCandidate

    fake_ee = FakeEarthEngine()
    aoi, observations, methodology = _build_history(db_session, [1, 4], fake_ee, tmp_path)
    current = observations[-1]
    products = compute_change_products_for_observation(
        db_session,
        aoi=aoi,
        observation=current,
        methodology=methodology,
        storage=FakeStorage(tmp_path / "change"),
        ee_module=fake_ee,
    )
    db_session.commit()
    delta_nbr = next(p.change_raster for p in products if p.change_type == "delta_nbr")
    sources_before = {
        (s.change_raster_id, s.index_raster_id)
        for s in db_session.execute(select(ChangeRasterSource)).scalars()
        if s.change_raster_id == delta_nbr.id
    }

    # Track a candidate from the ΔNBR raster into an event: the raster is now frozen.
    ring = Polygon([(0.1, 0.1), (0.2, 0.1), (0.2, 0.2), (0.1, 0.2), (0.1, 0.1)])
    db_session.add(
        DisturbanceCandidate(
            change_raster_id=delta_nbr.id,
            methodology_version_id=methodology.id,
            geometry=from_shape(ring, srid=AOI_SRID),
            detected_at=datetime(2026, 1, 4, tzinfo=UTC),
            area_m2=10_000.0,
        )
    )
    db_session.flush()
    track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()

    # A late-arriving observation lands inside the window (would change the baseline).
    make_observation(db_session, aoi, day=3)
    # The ΔNDVI COG goes missing (e.g. pruned): its row alone no longer satisfies the
    # reuse check (#77), so it is re-exported; the frozen ΔNBR raster is skipped
    # regardless of its file.
    delta_ndvi = next(p.change_raster for p in products if p.change_type == "delta_ndvi")
    Path(delta_ndvi.cog_path).unlink()
    rerun_storage = FakeStorage(tmp_path / "rerun")
    rerun = compute_change_products_for_observation(
        db_session,
        aoi=aoi,
        observation=current,
        methodology=methodology,
        storage=rerun_storage,
        ee_module=fake_ee,
    )
    db_session.commit()

    frozen = next(p for p in rerun if p.change_type == "delta_nbr")
    assert frozen.change_raster.id == delta_nbr.id
    assert frozen.delta_image is None  # nothing recomputed
    # Only the unfrozen ΔNDVI product was re-exported.
    assert [key.product for _, key, _ in rerun_storage.exports] == ["delta_ndvi"]
    # The frozen raster's provenance is untouched.
    sources_after = {
        (s.change_raster_id, s.index_raster_id)
        for s in db_session.execute(select(ChangeRasterSource)).scalars()
        if s.change_raster_id == delta_nbr.id
    }
    assert sources_after == sources_before


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


def test_persisted_change_rasters_are_reused_without_export(
    db_session: Session, tmp_path: Path
) -> None:
    """A non-frozen change raster whose row and COG both exist is reused (#77):
    no export is submitted and its recorded baseline stands."""
    fake_ee = FakeEarthEngine()
    aoi, observations, methodology = _build_history(db_session, [1, 2, 3], fake_ee, tmp_path)
    current = observations[-1]
    storage = FakeStorage(tmp_path / "change")
    compute_change_products_for_observation(
        db_session,
        aoi=aoi,
        observation=current,
        methodology=methodology,
        storage=storage,
        ee_module=fake_ee,
    )
    db_session.commit()
    first_exports = len(storage.exports)
    assert first_exports == 2

    rerun_storage = FakeStorage(tmp_path / "rerun")
    rerun = compute_change_products_for_observation(
        db_session,
        aoi=aoi,
        observation=current,
        methodology=methodology,
        storage=rerun_storage,
        ee_module=fake_ee,
    )
    assert rerun_storage.exports == []
    assert all(product.reused for product in rerun)
    assert all(product.delta_image is None for product in rerun)
