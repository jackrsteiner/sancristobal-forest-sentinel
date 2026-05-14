import json
from pathlib import Path
from typing import Any

import pytest
from shapely.geometry import MultiPolygon
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.aoi import AoiConfigError, load_aoi_config, persist_aoi
from forest_sentinel.models import Aoi

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

# A valid CCW unit square, reused across the validation cases.
_SQUARE = [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]]


def _write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "aoi.geojson"
    path.write_text(json.dumps(document))
    return path


def _feature(geometry: dict[str, Any], properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"name": "Test AOI"} if properties is None else properties,
        "geometry": geometry,
    }


def test_loads_valid_sample_aoi() -> None:
    config = load_aoi_config(EXAMPLES / "aoi-sample.geojson")
    assert config.name == "Example AOI"
    assert isinstance(config.geometry, MultiPolygon)
    assert config.geometry.is_valid


def test_polygon_is_normalized_to_multipolygon(tmp_path: Path) -> None:
    path = _write(tmp_path, _feature({"type": "Polygon", "coordinates": _SQUARE}))
    config = load_aoi_config(path)
    assert isinstance(config.geometry, MultiPolygon)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AoiConfigError, match="not found"):
        load_aoi_config(tmp_path / "does-not-exist.geojson")


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "aoi.geojson"
    path.write_text("{ not json")
    with pytest.raises(AoiConfigError, match="not valid JSON"):
        load_aoi_config(path)


def test_non_feature_document_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, {"type": "Polygon", "coordinates": _SQUARE})
    with pytest.raises(AoiConfigError, match="Feature or FeatureCollection"):
        load_aoi_config(path)


def test_missing_name_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, _feature({"type": "Polygon", "coordinates": _SQUARE}, properties={}))
    with pytest.raises(AoiConfigError, match="properties.name"):
        load_aoi_config(path)


def test_wrong_geometry_type_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, _feature({"type": "Point", "coordinates": [0.0, 0.0]}))
    with pytest.raises(AoiConfigError, match="Polygon or MultiPolygon"):
        load_aoi_config(path)


def test_invalid_geometry_is_rejected(tmp_path: Path) -> None:
    bowtie = [[[0.0, 0.0], [1.0, 1.0], [1.0, 0.0], [0.0, 1.0], [0.0, 0.0]]]
    path = _write(tmp_path, _feature({"type": "Polygon", "coordinates": bowtie}))
    with pytest.raises(AoiConfigError, match="not valid"):
        load_aoi_config(path)


def test_feature_collection_with_multiple_features_is_rejected(tmp_path: Path) -> None:
    feature = _feature({"type": "Polygon", "coordinates": _SQUARE})
    path = _write(tmp_path, {"type": "FeatureCollection", "features": [feature, feature]})
    with pytest.raises(AoiConfigError, match="exactly one feature"):
        load_aoi_config(path)


def test_non_wgs84_crs_is_rejected(tmp_path: Path) -> None:
    document = _feature({"type": "Polygon", "coordinates": _SQUARE})
    document["crs"] = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3857"}}
    path = _write(tmp_path, document)
    with pytest.raises(AoiConfigError, match="WGS 84"):
        load_aoi_config(path)


def test_persist_aoi_writes_a_row(db_session: Session) -> None:
    config = load_aoi_config(EXAMPLES / "aoi-sample.geojson")
    persisted = persist_aoi(db_session, config)
    db_session.commit()

    rows = db_session.execute(select(Aoi).where(Aoi.name == "Example AOI")).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == persisted.id
    assert rows[0].name == "Example AOI"
