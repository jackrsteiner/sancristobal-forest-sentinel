"""Hallway test: the Slice 1 pipeline produces candidate polygons in PostGIS.

Earth Engine and storage are fully stubbed (no live calls / no GCP creds), but the run
exercises the real orchestration and persists real rows, so a candidate can be dumped to
valid WGS 84 GeoJSON — the slice's hallway test, mock-backed.
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon, Polygon, mapping
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.methodology import get_or_create_methodology_version
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    DisturbanceCandidate,
    DisturbanceEvent,
    IndexRaster,
    Observation,
)
from forest_sentinel.pipeline import run_pipeline
from forest_sentinel.storage import CogKey

# A small candidate polygon inside the AOI bbox, returned by the stubbed vectorizer.
_CANDIDATE_RING = [[0.2, 0.2], [0.25, 0.2], [0.25, 0.25], [0.2, 0.25], [0.2, 0.2]]


class FakeEarthEngine:
    """Stubs every EE operation the pipeline touches; returns plain Python."""

    def __init__(self, scenes: list[dict[str, Any]]) -> None:
        self._scenes = scenes

    def list_image_properties(
        self, collection_id: str, region: Any, since: str, until: str
    ) -> list[dict[str, Any]]:
        # All synthetic scenes belong to the Landsat collection.
        if collection_id.endswith("HLSL30/v002"):
            return self._scenes
        return []

    def image_by_id(self, image_id: str) -> dict[str, Any]:
        return {"id": image_id}

    def apply_fmask_mask(self, image: Any) -> dict[str, Any]:
        return {"masked": image}

    def valid_pixel_fraction(self, image: Any, band: str, region: Any, scale: int) -> float:
        return 0.95

    def normalized_difference(self, image: Any, bands: list[str]) -> dict[str, Any]:
        return {"nd": tuple(bands)}

    def median_of(self, images: list[Any]) -> dict[str, Any]:
        return {"median": len(images)}

    def subtract(self, image: Any, other: Any) -> dict[str, Any]:
        return {"delta": True}

    def threshold_and_vectorize(
        self, delta_image: Any, *, threshold: float, scale: int, region: Any, min_area_m2: float
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [_CANDIDATE_RING]},
                "properties": {"area_m2": 50_000.0},
            }
        ]


class FakeStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, key: CogKey) -> Path:
        return self.root / key.relative_path()

    def export_image(
        self, image: Any, key: CogKey, *, scale: int | None = None, region: Any = None
    ) -> Path:
        return self.path_for(key)


def _scene(day: int) -> dict[str, Any]:
    ms = int(datetime(2026, 1, day, tzinfo=UTC).timestamp() * 1000)
    return {
        "id": f"NASA/HLS/HLSL30/v002/scene-{day}",
        "properties": {"system:index": f"scene-{day}", "system:time_start": ms},
    }


def _make_aoi(session: Session) -> Aoi:
    square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
    aoi = Aoi(name="Hallway AOI", geometry=from_shape(MultiPolygon([square]), srid=4326))
    session.add(aoi)
    session.flush()
    return aoi


def test_run_full_pipeline_produces_candidates(db_session: Session, tmp_path: Path) -> None:
    aoi = _make_aoi(db_session)
    methodology = get_or_create_methodology_version(
        db_session, name="optical-change", version="1.0.0", parameters={}
    )
    fake_ee = FakeEarthEngine([_scene(day) for day in (1, 2, 3, 4, 5, 6)])

    summary = run_pipeline(
        db_session,
        aoi=aoi,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        methodology=methodology,
        storage=FakeStorage(tmp_path),
        baseline_window=5,
        ee_module=fake_ee,
    )
    db_session.commit()

    # 6 observations -> 12 index rasters; first has no baseline so 5 obs x 2 = 10 change rasters;
    # candidates come from the 5 delta_nbr rasters, one polygon each.
    assert summary.observations_discovered == 6
    assert summary.observations_recorded == 6
    assert summary.index_rasters == 12
    assert summary.change_rasters == 10
    assert summary.candidates == 5
    # All 5 candidates share the stubbed geometry, so they overlap into one tracked event.
    assert summary.events_created == 1
    assert summary.event_observations == 5

    assert len(db_session.execute(select(Observation)).scalars().all()) == 6
    assert len(db_session.execute(select(IndexRaster)).scalars().all()) == 12
    assert len(db_session.execute(select(ChangeRaster)).scalars().all()) == 10

    # Candidates are tracked into a single disturbance event with a valid footprint.
    event = db_session.execute(select(DisturbanceEvent)).scalar_one()
    assert event.status == "ongoing"
    assert to_shape(event.geometry).is_valid

    candidate = db_session.execute(select(DisturbanceCandidate)).scalars().first()
    assert candidate is not None
    geometry = to_shape(candidate.geometry)
    assert geometry.geom_type == "Polygon"
    assert geometry.is_valid
    # The candidate dumps cleanly to GeoJSON for eyeballing on a map.
    geojson = json.dumps(mapping(geometry))
    assert json.loads(geojson)["type"] == "Polygon"


def test_rerunning_full_pipeline_is_idempotent(db_session: Session, tmp_path: Path) -> None:
    """A second run over the same window must succeed and add nothing (audit BUG-2):
    tracked candidates are event history and survive candidate re-extraction."""
    aoi = _make_aoi(db_session)
    methodology = get_or_create_methodology_version(
        db_session, name="optical-change", version="1.0.0", parameters={}
    )
    fake_ee = FakeEarthEngine([_scene(day) for day in (1, 2, 3, 4, 5, 6)])

    def run() -> Any:
        return run_pipeline(
            db_session,
            aoi=aoi,
            since=date(2026, 1, 1),
            until=date(2026, 2, 1),
            methodology=methodology,
            storage=FakeStorage(tmp_path),
            baseline_window=5,
            ee_module=fake_ee,
        )

    run()
    db_session.commit()
    second = run()
    db_session.commit()

    # Candidates are frozen once tracked; events and measurements are unchanged.
    assert second.candidates == 5  # the existing (frozen) candidate set is reported
    assert second.events_created == 0
    assert second.event_observations == 0
    assert len(db_session.execute(select(DisturbanceCandidate)).scalars().all()) == 5
    assert len(db_session.execute(select(DisturbanceEvent)).scalars().all()) == 1
