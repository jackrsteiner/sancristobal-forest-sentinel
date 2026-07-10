from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.fakes import (
    FakeEarthEngine,
    FakeStorage,
    make_aoi,
    make_methodology,
    make_observation,
)

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
