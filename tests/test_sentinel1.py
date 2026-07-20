"""Sentinel-1 GRD discovery (E16, #115), mirroring the HLS discovery tests."""

from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.models import Observation
from forest_sentinel.sentinel1 import (
    S1_COLLECTION,
    S1_SENSOR,
    discover_radar_observations,
    parse_scene,
)
from tests.fakes import FakeEarthEngine, make_aoi


def _scene(
    index: str,
    *,
    mode: str = "IW",
    polarisations: list[str] | None = None,
    orbit: str = "DESCENDING",
    relative_orbit: int | None = 18,
) -> dict[str, "Any"]:
    return {
        "id": f"{S1_COLLECTION}/{index}",
        "properties": {
            "system:index": index,
            "system:time_start": int(datetime(2026, 1, 5, tzinfo=UTC).timestamp() * 1000),
            "instrumentMode": mode,
            "transmitterReceiverPolarisation": (
                polarisations if polarisations is not None else ["VV", "VH"]
            ),
            "orbitProperties_pass": orbit,
            "relativeOrbitNumber_start": relative_orbit,
        },
    }


def test_discovery_records_iw_vv_scenes_with_orbit_fields(db_session: Session) -> None:
    aoi = make_aoi(db_session)
    fake = FakeEarthEngine(scenes={S1_COLLECTION: [_scene("S1A_IW_1"), _scene("S1A_IW_2")]})

    result = discover_radar_observations(
        db_session, aoi, since=date(2026, 1, 1), until=date(2026, 2, 1), ee_module=fake
    )
    db_session.commit()

    assert (result.discovered, result.recorded, result.skipped) == (2, 2, 0)
    rows = db_session.execute(select(Observation).order_by(Observation.id)).scalars().all()
    assert [row.sensor for row in rows] == [S1_SENSOR, S1_SENSOR]
    assert all(row.orbit_direction == "DESCENDING" for row in rows)
    assert all(row.relative_orbit == 18 for row in rows)
    assert all(row.cloud_cover_percent is None for row in rows)  # radar sees through


def test_ineligible_modes_and_polarisations_are_skipped(db_session: Session) -> None:
    aoi = make_aoi(db_session)
    fake = FakeEarthEngine(
        scenes={
            S1_COLLECTION: [
                _scene("S1A_EW", mode="EW"),
                _scene("S1A_HH", polarisations=["HH"]),
                _scene("S1A_OK"),
            ]
        }
    )

    result = discover_radar_observations(
        db_session, aoi, since=date(2026, 1, 1), until=date(2026, 2, 1), ee_module=fake
    )

    assert (result.discovered, result.recorded, result.skipped) == (3, 1, 2)
    row = db_session.execute(select(Observation)).scalar_one()
    assert row.source_scene_id == "S1A_OK"


def test_discovery_is_idempotent(db_session: Session) -> None:
    aoi = make_aoi(db_session)
    fake = FakeEarthEngine(scenes={S1_COLLECTION: [_scene("S1A_IW_1")]})

    first = discover_radar_observations(
        db_session, aoi, since=date(2026, 1, 1), until=date(2026, 2, 1), ee_module=fake
    )
    db_session.commit()
    second = discover_radar_observations(
        db_session, aoi, since=date(2026, 1, 1), until=date(2026, 2, 1), ee_module=fake
    )

    assert first.recorded == 1
    assert (second.recorded, second.skipped) == (0, 1)
    assert len(db_session.execute(select(Observation)).scalars().all()) == 1


def test_parse_scene_rejects_broken_eligible_scenes() -> None:
    missing_time = _scene("S1A_IW_1")
    del missing_time["properties"]["system:time_start"]
    with pytest.raises(ValueError, match="system:time_start"):
        parse_scene(missing_time)

    no_orbit = _scene("S1A_IW_2")
    no_orbit["properties"]["orbitProperties_pass"] = None
    with pytest.raises(ValueError, match="orbit direction"):
        parse_scene(no_orbit)


def test_parse_scene_falls_back_to_asset_id() -> None:
    scene = _scene("S1A_IW_1")
    del scene["properties"]["system:index"]
    parsed = parse_scene(scene)
    assert parsed is not None
    assert parsed.source_scene_id == "S1A_IW_1"
