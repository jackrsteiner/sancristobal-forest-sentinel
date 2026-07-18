"""Context-layer loading (E17, #125): validation, idempotent replace, harvest."""

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from forest_sentinel.cli import main
from forest_sentinel.context import (
    ContextConfigError,
    HarvestResult,
    harvest_context_dir,
    load_context_document,
    load_context_file,
    parse_harvest_filename,
    replace_layer,
)
from forest_sentinel.models import ContextFeature, ContextLayer

_SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0.1, 0.1], [0.2, 0.1], [0.2, 0.2], [0.1, 0.2], [0.1, 0.1]]],
}
_LINE = {"type": "LineString", "coordinates": [[0.0, 0.0], [1.0, 1.0]]}
_POINT = {"type": "Point", "coordinates": [0.5, 0.5]}


def _collection(*features: tuple[dict[str, Any], dict[str, Any] | None]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": geometry, "properties": properties}
            for geometry, properties in features
        ],
    }


def test_document_accepts_mixed_geometry_types() -> None:
    document = load_context_document(
        _collection((_SQUARE, {"name": "Acme concession"}), (_LINE, None), (_POINT, {"n": 1}))
    )
    assert [g.geom_type for g in document.geometries] == ["Polygon", "LineString", "Point"]
    # Null properties become an empty dict, never None.
    assert document.properties == [{"name": "Acme concession"}, {}, {"n": 1}]


def test_document_accepts_a_single_feature() -> None:
    document = load_context_document(
        {"type": "Feature", "geometry": _SQUARE, "properties": {"name": "solo"}}
    )
    assert len(document.geometries) == 1


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("not a dict", "must be a GeoJSON object"),
        ({"type": "Polygon"}, "must be a GeoJSON Feature or FeatureCollection"),
        ({"type": "FeatureCollection", "features": []}, "at least one feature"),
        (
            {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}}]},
            "missing a geometry",
        ),
        (
            _collection(({"type": "Polygon", "coordinates": []}, None)),
            "empty geometry",
        ),
        (
            {
                **_collection((_SQUARE, None)),
                "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
            },
            "must be in WGS 84",
        ),
    ],
)
def test_document_validation_errors(document: Any, message: str) -> None:
    with pytest.raises(ContextConfigError, match=message):
        load_context_document(document)


def test_self_intersecting_geometry_is_rejected() -> None:
    bowtie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]],
    }
    with pytest.raises(ContextConfigError, match="invalid geometry"):
        load_context_document(_collection((bowtie, None)))


def test_load_context_file_reports_missing_and_malformed(tmp_path: Path) -> None:
    with pytest.raises(ContextConfigError, match="not found"):
        load_context_file(tmp_path / "missing.geojson")
    bad = tmp_path / "bad.geojson"
    bad.write_text("{not json")
    with pytest.raises(ContextConfigError, match="not valid JSON"):
        load_context_file(bad)


def test_replace_layer_is_idempotent_and_replaces_features(db_session: Session) -> None:
    document = load_context_document(_collection((_SQUARE, {"name": "Acme"}), (_POINT, None)))
    layer = replace_layer(
        db_session, name="acme", kind="concession", document=document, source_file="acme.geojson"
    )
    assert layer.kind == "concession"
    first_ids = set(db_session.execute(select(ContextFeature.id)).scalars())
    assert len(first_ids) == 2

    # Re-loading a revised file replaces the features wholesale: no stale rows.
    revised = load_context_document(_collection((_LINE, {"rev": 2})))
    same_layer = replace_layer(db_session, name="acme", kind="road", document=revised)
    assert same_layer.id == layer.id
    assert same_layer.kind == "road"
    rows = db_session.execute(select(ContextFeature)).scalars().all()
    assert len(rows) == 1
    assert rows[0].properties == {"rev": 2}
    assert rows[0].id not in first_ids
    assert db_session.execute(select(ContextLayer)).scalars().one().id == layer.id


def test_replace_layer_rejects_unknown_kind(db_session: Session) -> None:
    document = load_context_document(_collection((_SQUARE, None)))
    with pytest.raises(ContextConfigError, match="unknown context kind"):
        replace_layer(db_session, name="x", kind="volcano", document=document)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("concession--acme-palm.geojson", ("concession", "acme-palm")),
        ("road--main--north.geojson", ("road", "main--north")),
        ("volcano--x.geojson", None),
        ("no-separator.geojson", None),
        ("concession--.geojson", None),
    ],
)
def test_parse_harvest_filename(filename: str, expected: tuple[str, str] | None) -> None:
    assert parse_harvest_filename(Path(filename)) == expected


def test_harvest_loads_convention_files_and_skips_the_rest(
    db_session: Session, tmp_path: Path
) -> None:
    (tmp_path / "concession--acme.geojson").write_text(
        json.dumps(_collection((_SQUARE, {"name": "Acme"})))
    )
    (tmp_path / "road--main.geojson").write_text(json.dumps(_collection((_LINE, None))))
    (tmp_path / "unconventional.geojson").write_text(json.dumps(_collection((_POINT, None))))
    (tmp_path / "river--broken.geojson").write_text("{not json")

    result = harvest_context_dir(db_session, tmp_path)

    assert result == HarvestResult(layers=2, features=2, skipped=2)
    layers = {
        layer.name: layer for layer in db_session.execute(select(ContextLayer)).scalars().all()
    }
    assert set(layers) == {"acme", "main"}
    assert layers["acme"].kind == "concession"
    assert layers["main"].kind == "road"
    assert layers["acme"].source_file == str(tmp_path / "concession--acme.geojson")

    # A second sweep replaces rather than duplicates.
    assert harvest_context_dir(db_session, tmp_path) == result
    assert len(db_session.execute(select(ContextFeature)).scalars().all()) == 2


def test_harvest_of_a_missing_directory_is_a_noop(db_session: Session, tmp_path: Path) -> None:
    assert harvest_context_dir(db_session, tmp_path / "absent") == HarvestResult()


def test_cli_context_load_round_trips(
    migrated_database: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "concession--acme.geojson"
    path.write_text(json.dumps(_collection((_SQUARE, {"name": "Acme"}), (_POINT, None))))

    assert main(["context", "load", str(path), "--kind", "concession"]) == 0
    # The name defaults from the <kind>--<name> convention.
    assert "Loaded context layer 'acme' (concession): 2 feature(s)" in capsys.readouterr().out

    with Session(migrated_database) as session:
        layer = session.execute(select(ContextLayer)).scalars().one()
        assert (layer.name, layer.kind) == ("acme", "concession")
        assert len(session.execute(select(ContextFeature)).scalars().all()) == 2


def test_cli_context_load_reports_invalid_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "concession--bad.geojson"
    bad.write_text("{not json")
    assert main(["context", "load", str(bad), "--kind", "concession"]) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_pipeline_run_harvests_the_context_dir(
    migrated_database: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from forest_sentinel import earthengine, pipeline, storage
    from forest_sentinel.pipeline import PipelineSummary

    (tmp_path / "concession--acme.geojson").write_text(json.dumps(_collection((_SQUARE, None))))
    monkeypatch.setenv("FOREST_SENTINEL_CONTEXT_DIR", str(tmp_path))
    monkeypatch.setattr(earthengine, "initialize", lambda project=None: None)
    monkeypatch.setattr(storage, "local_disk_storage_from_env", lambda: object())
    monkeypatch.setattr(
        pipeline,
        "run_pipeline",
        lambda session, **kwargs: PipelineSummary(0, 0, 0, 0, 0, 0, 0, 0),
    )

    sample_aoi = Path(__file__).resolve().parents[1] / "examples" / "aoi-sample.geojson"
    exit_code = main(
        ["run", "--aoi", str(sample_aoi), "--since", "2026-01-01", "--until", "2026-02-01"]
    )
    assert exit_code == 0
    assert "Context layers: 1 loaded (1 features), 0 skipped" in capsys.readouterr().out
    with Session(migrated_database) as session:
        layer = session.execute(select(ContextLayer)).scalars().one()
        assert (layer.name, layer.kind) == ("acme", "concession")


def _seed_event(session: Session) -> Any:
    """One tracked event over the (0.1,0.1)-(0.2,0.2) patch; returns the AOI."""
    from forest_sentinel.events import track_events_for_aoi
    from tests.fakes import make_aoi, make_candidate, make_methodology

    aoi = make_aoi(session)
    methodology = make_methodology(session)
    make_candidate(
        session,
        aoi,
        methodology,
        day=1,
        ring=[(0.1, 0.1), (0.2, 0.1), (0.2, 0.2), (0.1, 0.2), (0.1, 0.1)],
        area_m2=10_000.0,
    )
    track_events_for_aoi(session, aoi=aoi)
    return aoi


def _layer(session: Session, name: str, kind: str, *geometries: dict[str, Any]) -> None:
    replace_layer(
        session,
        name=name,
        kind=kind,
        document=load_context_document(_collection(*((g, {"name": name}) for g in geometries))),
    )


def test_compute_event_context_records_expected_relations(db_session: Session) -> None:
    from forest_sentinel.context import compute_event_context
    from forest_sentinel.models import DisturbanceEvent, EventContext

    aoi = _seed_event(db_session)
    # A concession containing the event, a road crossing it, two rivers within
    # the 5 km buffer (only the nearest is recorded), and a mill far outside.
    _layer(
        db_session,
        "acme",
        "concession",
        {"type": "Polygon", "coordinates": [[[0, 0], [0.3, 0], [0.3, 0.3], [0, 0.3], [0, 0]]]},
    )
    _layer(
        db_session,
        "crossing-road",
        "road",
        {"type": "LineString", "coordinates": [[0.05, 0.15], [0.25, 0.15]]},
    )
    _layer(
        db_session,
        "rivers",
        "river",
        {"type": "LineString", "coordinates": [[0.22, 0.0], [0.22, 0.3]]},
        {"type": "LineString", "coordinates": [[0.24, 0.0], [0.24, 0.3]]},
    )
    _layer(db_session, "far-mill", "mill", {"type": "Point", "coordinates": [1.0, 1.0]})

    recorded = compute_event_context(db_session, aoi=aoi)

    assert recorded == 3
    event = db_session.execute(select(DisturbanceEvent)).scalar_one()
    rows = db_session.execute(select(EventContext)).scalars().all()
    assert all(row.event_id == event.id for row in rows)
    by_relation = {row.relation: row for row in rows}
    assert set(by_relation) == {"contains", "intersects", "nearby"}
    assert by_relation["contains"].distance_m is None
    # The nearest river (x=0.22, ~2.2 km from the patch edge at x=0.2) wins;
    # geodesic meters from PostGIS geography.
    nearby = by_relation["nearby"]
    assert nearby.distance_m is not None
    assert 2000 < nearby.distance_m < 2400
    river_feature = db_session.get(ContextFeature, nearby.context_feature_id)
    assert river_feature is not None
    from geoalchemy2.shape import to_shape

    assert to_shape(river_feature.geometry).bounds[0] == pytest.approx(0.22)


def test_compute_event_context_replaces_rows_per_run(db_session: Session) -> None:
    from forest_sentinel.context import compute_event_context
    from forest_sentinel.models import EventContext

    aoi = _seed_event(db_session)
    _layer(
        db_session,
        "rivers",
        "river",
        {"type": "LineString", "coordinates": [[0.22, 0.0], [0.22, 0.3]]},
    )
    assert compute_event_context(db_session, aoi=aoi) == 1
    first = db_session.execute(select(EventContext)).scalars().one()

    # The layer moved: recompute replaces the derived view, leaving no stale rows.
    _layer(
        db_session,
        "rivers",
        "river",
        {"type": "LineString", "coordinates": [[0.15, 0.0], [0.15, 0.3]]},
    )
    assert compute_event_context(db_session, aoi=aoi) == 1
    row = db_session.execute(select(EventContext)).scalars().one()
    assert row.id != first.id
    assert row.relation == "intersects"

    # An empty buffer records nothing at all.
    _layer(
        db_session,
        "rivers",
        "river",
        {"type": "LineString", "coordinates": [[0.9, 0.0], [0.9, 0.3]]},
    )
    assert compute_event_context(db_session, aoi=aoi) == 0
    assert db_session.execute(select(EventContext)).scalars().all() == []


def test_compute_event_context_buffer_is_configurable(db_session: Session) -> None:
    from forest_sentinel.context import compute_event_context
    from forest_sentinel.models import EventContext

    aoi = _seed_event(db_session)
    _layer(
        db_session,
        "rivers",
        "river",
        {"type": "LineString", "coordinates": [[0.22, 0.0], [0.22, 0.3]]},
    )
    # ~2.2 km away: outside a 1 km buffer, inside the 5 km default.
    assert compute_event_context(db_session, aoi=aoi, buffer_m=1_000.0) == 0
    assert compute_event_context(db_session, aoi=aoi) == 1
    assert db_session.execute(select(EventContext)).scalars().one().relation == "nearby"
