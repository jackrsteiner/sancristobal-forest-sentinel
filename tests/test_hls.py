from datetime import UTC, date, datetime
from typing import Any

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.hls import (
    HLS_COLLECTIONS,
    discover_observations,
    parse_granule,
)
from forest_sentinel.models import Aoi, Observation

HLSL30 = "NASA/HLS/HLSL30/v002"
HLSS30 = "NASA/HLS/HLSS30/v002"


def _time_ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


def _feature(index: str, time_ms: int, cloud: str | None = "10") -> dict[str, Any]:
    properties: dict[str, Any] = {"system:index": index, "system:time_start": time_ms}
    if cloud is not None:
        properties["CLOUD_COVERAGE"] = cloud
    return {"id": f"{HLSL30}/{index}", "properties": properties}


class FakeEarthEngine:
    def __init__(self, per_collection: dict[str, list[dict[str, Any]]]) -> None:
        self._per_collection = per_collection
        self.calls: list[tuple[str, Any, str, str]] = []

    def list_image_properties(
        self, collection_id: str, region: Any, since: str, until: str
    ) -> list[dict[str, Any]]:
        self.calls.append((collection_id, region, since, until))
        return self._per_collection.get(collection_id, [])


def _make_aoi(session: Session) -> Aoi:
    square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
    aoi = Aoi(name="Test AOI", geometry=from_shape(MultiPolygon([square]), srid=4326))
    session.add(aoi)
    session.flush()
    return aoi


def test_parse_granule_reads_fields() -> None:
    granule = parse_granule(_feature("HLS.L30.T55LBC.X", _time_ms(2026, 1, 2)), "HLSL30")
    assert granule.sensor == "HLSL30"
    assert granule.source_scene_id == "HLS.L30.T55LBC.X"
    assert granule.acquired_at == datetime(2026, 1, 2, tzinfo=UTC)
    assert granule.cloud_cover_percent == 10.0


def test_parse_granule_cloud_cover_optional() -> None:
    granule = parse_granule(_feature("scene", _time_ms(2026, 1, 2), cloud=None), "HLSS30")
    assert granule.cloud_cover_percent is None


def test_parse_granule_falls_back_to_feature_id() -> None:
    feature = {"id": "full/id/path", "properties": {"system:time_start": _time_ms(2026, 1, 2)}}
    assert parse_granule(feature, "HLSL30").source_scene_id == "full/id/path"


def test_parse_granule_requires_time_start() -> None:
    with pytest.raises(ValueError, match="system:time_start"):
        parse_granule({"id": "x", "properties": {"system:index": "x"}}, "HLSL30")


def test_discovers_across_both_collections(db_session: Session) -> None:
    aoi = _make_aoi(db_session)
    fake = FakeEarthEngine(
        {
            HLSL30: [_feature("L30-a", _time_ms(2026, 1, 2))],
            HLSS30: [_feature("S30-b", _time_ms(2026, 1, 3))],
        }
    )

    result = discover_observations(
        db_session, aoi, since=date(2026, 1, 1), until=date(2026, 1, 31), ee_module=fake
    )

    assert (result.discovered, result.recorded, result.skipped) == (2, 2, 0)
    rows = db_session.execute(select(Observation).order_by(Observation.source_scene_id)).scalars()
    by_scene = {row.source_scene_id: row.sensor for row in rows}
    assert by_scene == {"L30-a": "HLSL30", "S30-b": "HLSS30"}
    # Both collections queried with the AOI region and the window as ISO dates.
    queried = {(collection, since, until) for collection, _, since, until in fake.calls}
    assert queried == {
        (HLSL30, "2026-01-01", "2026-01-31"),
        (HLSS30, "2026-01-01", "2026-01-31"),
    }


def test_rerun_is_idempotent(db_session: Session) -> None:
    aoi = _make_aoi(db_session)
    fake = FakeEarthEngine({HLSL30: [_feature("L30-a", _time_ms(2026, 1, 2))]})
    since, until = date(2026, 1, 1), date(2026, 1, 31)

    first = discover_observations(db_session, aoi, since=since, until=until, ee_module=fake)
    db_session.commit()
    second = discover_observations(db_session, aoi, since=since, until=until, ee_module=fake)

    assert (first.recorded, first.skipped) == (1, 0)
    assert (second.recorded, second.skipped) == (0, 1)
    assert len(db_session.execute(select(Observation)).scalars().all()) == 1


def test_empty_window_records_nothing(db_session: Session) -> None:
    aoi = _make_aoi(db_session)
    fake = FakeEarthEngine({})
    result = discover_observations(
        db_session, aoi, since=date(2026, 1, 1), until=date(2026, 1, 2), ee_module=fake
    )
    assert (result.discovered, result.recorded, result.skipped) == (0, 0, 0)
    assert db_session.execute(select(Observation)).scalars().all() == []


def test_known_collections_cover_both_sensors() -> None:
    assert set(HLS_COLLECTIONS.values()) == {"HLSL30", "HLSS30"}
