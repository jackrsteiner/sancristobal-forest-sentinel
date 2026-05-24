from datetime import UTC, datetime
from typing import Any

from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.candidates import (
    DEFAULT_DELTA_NBR_THRESHOLD,
    DEFAULT_MIN_AREA_M2,
    extract_candidates_for_change_raster,
    resolve_min_area,
    resolve_threshold,
)
from forest_sentinel.methodology import get_or_create_methodology_version
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    DisturbanceCandidate,
    MethodologyVersion,
    Observation,
)

_REGION = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}


def _poly_feature(offset: float, area_m2: float) -> dict[str, Any]:
    ring = [
        [offset, offset],
        [offset + 0.01, offset],
        [offset + 0.01, offset + 0.01],
        [offset, offset + 0.01],
        [offset, offset],
    ]
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {"area_m2": area_m2},
    }


class FakeEarthEngine:
    def __init__(self, features: list[dict[str, Any]]) -> None:
        self._features = features
        self.calls: list[dict[str, Any]] = []

    def threshold_and_vectorize(
        self,
        delta_image: Any,
        *,
        threshold: float,
        scale: int,
        region: Any,
        min_area_m2: float,
    ) -> list[dict[str, Any]]:
        self.calls.append({"threshold": threshold, "scale": scale, "min_area_m2": min_area_m2})
        return self._features


def _setup(
    session: Session, *, parameters: dict[str, Any] | None = None
) -> tuple[ChangeRaster, MethodologyVersion, Observation]:
    square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
    aoi = Aoi(name="Test AOI", geometry=from_shape(MultiPolygon([square]), srid=4326))
    session.add(aoi)
    session.flush()
    obs = Observation(
        aoi_id=aoi.id,
        sensor="HLSL30",
        acquired_at=datetime(2026, 1, 6, tzinfo=UTC),
        source_scene_id="scene-6",
    )
    session.add(obs)
    session.flush()
    methodology = get_or_create_methodology_version(
        session, name="optical-change", version="1.0.0", parameters=parameters or {}
    )
    change = ChangeRaster(
        observation_id=obs.id,
        methodology_version_id=methodology.id,
        change_type="delta_nbr",
        cog_path="/data/cogs/aoi/delta_nbr/2026-01-06/delta_nbr.tif",
        baseline_window=5,
    )
    session.add(change)
    session.flush()
    return change, methodology, obs


def test_resolve_threshold_precedence() -> None:
    m = MethodologyVersion(name="m", version="1", parameters={"delta_nbr_threshold": -0.3})
    assert resolve_threshold(m, None) == -0.3
    assert resolve_threshold(m, -0.5) == -0.5
    assert resolve_threshold(MethodologyVersion(name="m", version="1", parameters={}), None) == (
        DEFAULT_DELTA_NBR_THRESHOLD
    )


def test_resolve_min_area_precedence() -> None:
    m = MethodologyVersion(name="m", version="1", parameters={"min_area_m2": 9000})
    assert resolve_min_area(m, None) == 9000
    assert resolve_min_area(m, 1234) == 1234
    assert resolve_min_area(MethodologyVersion(name="m", version="1", parameters={}), None) == (
        DEFAULT_MIN_AREA_M2
    )


def test_extracts_candidates_with_provenance(db_session: Session) -> None:
    change, methodology, obs = _setup(db_session)
    fake = FakeEarthEngine([_poly_feature(0.1, 10_000), _poly_feature(0.3, 20_000)])

    candidates = extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        delta_image="delta-ee-image",
        region=_REGION,
        ee_module=fake,
    )
    db_session.commit()

    assert len(candidates) == 2
    rows = db_session.execute(select(DisturbanceCandidate)).scalars().all()
    assert len(rows) == 2
    for row in rows:
        assert row.change_raster_id == change.id
        assert row.methodology_version_id == methodology.id
        assert row.detected_at == obs.acquired_at
        geometry = to_shape(row.geometry)
        assert geometry.geom_type == "Polygon"
        assert geometry.is_valid
    # The EE call used the documented defaults.
    assert fake.calls == [
        {
            "threshold": DEFAULT_DELTA_NBR_THRESHOLD,
            "scale": 30,
            "min_area_m2": DEFAULT_MIN_AREA_M2,
        }
    ]


def test_no_features_yields_no_candidates(db_session: Session) -> None:
    change, _, _ = _setup(db_session)
    candidates = extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        delta_image="img",
        region=_REGION,
        ee_module=FakeEarthEngine([]),
    )
    assert candidates == []
    assert db_session.execute(select(DisturbanceCandidate)).scalars().all() == []


def test_sub_minimum_area_polygons_are_dropped(db_session: Session) -> None:
    change, _, _ = _setup(db_session)
    fake = FakeEarthEngine([_poly_feature(0.1, 100.0), _poly_feature(0.3, 50_000.0)])
    candidates = extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        delta_image="img",
        region=_REGION,
        ee_module=fake,
    )
    assert len(candidates) == 1
    assert candidates[0].area_m2 == 50_000.0


def test_methodology_parameters_drive_extraction(db_session: Session) -> None:
    change, _, _ = _setup(
        db_session, parameters={"delta_nbr_threshold": -0.4, "min_area_m2": 9_000}
    )
    fake = FakeEarthEngine([_poly_feature(0.1, 10_000)])
    extract_candidates_for_change_raster(
        db_session, change_raster=change, delta_image="img", region=_REGION, ee_module=fake
    )
    assert fake.calls[0]["threshold"] == -0.4
    assert fake.calls[0]["min_area_m2"] == 9_000


def test_explicit_overrides_win(db_session: Session) -> None:
    change, _, _ = _setup(
        db_session, parameters={"delta_nbr_threshold": -0.4, "min_area_m2": 9_000}
    )
    fake = FakeEarthEngine([_poly_feature(0.1, 10_000)])
    extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        delta_image="img",
        region=_REGION,
        threshold=-0.6,
        min_area_m2=1_000,
        ee_module=fake,
    )
    assert fake.calls[0]["threshold"] == -0.6
    assert fake.calls[0]["min_area_m2"] == 1_000


def test_rerun_replaces_candidates(db_session: Session) -> None:
    change, _, _ = _setup(db_session)
    for _ in range(2):
        extract_candidates_for_change_raster(
            db_session,
            change_raster=change,
            delta_image="img",
            region=_REGION,
            ee_module=FakeEarthEngine([_poly_feature(0.1, 10_000)]),
        )
        db_session.commit()
    assert len(db_session.execute(select(DisturbanceCandidate)).scalars().all()) == 1
