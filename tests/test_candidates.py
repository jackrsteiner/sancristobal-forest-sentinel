from typing import Any

import pytest
from geoalchemy2.shape import to_shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.candidates import (
    DEFAULT_DELTA_NBR_THRESHOLD,
    DEFAULT_MIN_AREA_M2,
    extract_candidates_for_change_raster,
    resolve_min_area,
    resolve_threshold,
)
from forest_sentinel.events import track_events_for_aoi
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    DisturbanceCandidate,
    MethodologyVersion,
    Observation,
)
from tests.fakes import (
    FakeEarthEngine,
    make_aoi,
    make_change_raster,
    make_methodology,
    make_observation,
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


def _setup(
    session: Session, *, parameters: dict[str, Any] | None = None
) -> tuple[ChangeRaster, MethodologyVersion, Observation]:
    aoi = make_aoi(session)
    obs = make_observation(session, aoi, day=6)
    methodology = make_methodology(session, parameters=parameters)
    change = make_change_raster(
        session, obs, methodology, cog_path="/data/cogs/aoi/delta_nbr/2026-01-06/delta_nbr.tif"
    )
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


def test_resolvers_treat_stored_null_as_absent() -> None:
    # A methodology recorded with explicit nulls (e.g. by an older CLI run) must fall
    # back to the defaults instead of crashing on float(None).
    m = MethodologyVersion(
        name="m", version="1", parameters={"delta_nbr_threshold": None, "min_area_m2": None}
    )
    assert resolve_threshold(m, None) == DEFAULT_DELTA_NBR_THRESHOLD
    assert resolve_min_area(m, None) == DEFAULT_MIN_AREA_M2


def test_extracts_candidates_with_provenance(db_session: Session) -> None:
    change, methodology, obs = _setup(db_session)
    fake = FakeEarthEngine(features=[_poly_feature(0.1, 10_000), _poly_feature(0.3, 20_000)])

    candidates = extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        methodology=methodology,
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
    # The EE call used the documented defaults, over the caller's region, on the
    # unmasked delta (no forest mask in this methodology).
    assert fake.calls == [
        {
            "threshold": DEFAULT_DELTA_NBR_THRESHOLD,
            "scale": 30,
            "min_area_m2": DEFAULT_MIN_AREA_M2,
            "region": _REGION,
            "delta_image": "delta-ee-image",
        }
    ]


def test_persists_per_candidate_delta_statistics(db_session: Session) -> None:
    """ΔNBR statistics from the extraction call (#95) land on the candidate row."""
    with_stats = _poly_feature(0.1, 9_000)
    with_stats["properties"].update({"delta_mean": -0.31, "delta_min": -0.52, "valid_pixels": 8})
    overshoot = _poly_feature(0.3, 9_000)
    # Pixel counting at scale is approximate: an overshooting count must clamp to 1.0.
    overshoot["properties"].update({"delta_mean": -0.4, "delta_min": -0.6, "valid_pixels": 11})
    fake = FakeEarthEngine(features=[with_stats, overshoot])

    change, methodology, _ = _setup(db_session)
    extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        methodology=methodology,
        delta_image="img",
        region=_REGION,
        ee_module=fake,
    )

    rows = (
        db_session.execute(select(DisturbanceCandidate).order_by(DisturbanceCandidate.id))
        .scalars()
        .all()
    )
    assert [row.delta_mean for row in rows] == [-0.31, -0.4]
    assert [row.delta_min for row in rows] == [-0.52, -0.6]
    # 8 valid pixels × 30 m² pixels = 7 200 m² of 9 000 m² → 0.8; 11 pixels clamps.
    assert rows[0].valid_pixel_fraction == pytest.approx(0.8)
    assert rows[1].valid_pixel_fraction == 1.0


def test_features_without_statistics_read_null(db_session: Session) -> None:
    # Features from an older extraction shape (no #95 properties) must not crash —
    # the row simply records no statistics, matching pre-migration rows.
    fake = FakeEarthEngine(features=[_poly_feature(0.1, 10_000)])

    change, methodology, _ = _setup(db_session)
    extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        methodology=methodology,
        delta_image="img",
        region=_REGION,
        ee_module=fake,
    )

    row = db_session.execute(select(DisturbanceCandidate)).scalar_one()
    assert row.delta_mean is None
    assert row.delta_min is None
    assert row.valid_pixel_fraction is None


def test_no_features_yields_no_candidates(db_session: Session) -> None:
    change, methodology, _ = _setup(db_session)
    candidates = extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        methodology=methodology,
        delta_image="img",
        region=_REGION,
        ee_module=FakeEarthEngine(features=[]),
    )
    assert candidates == []
    assert db_session.execute(select(DisturbanceCandidate)).scalars().all() == []


def test_sub_minimum_area_polygons_are_dropped(db_session: Session) -> None:
    change, methodology, _ = _setup(db_session)
    fake = FakeEarthEngine(features=[_poly_feature(0.1, 100.0), _poly_feature(0.3, 50_000.0)])
    candidates = extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        methodology=methodology,
        delta_image="img",
        region=_REGION,
        ee_module=fake,
    )
    assert len(candidates) == 1
    assert candidates[0].area_m2 == 50_000.0


def test_methodology_parameters_drive_extraction(db_session: Session) -> None:
    change, methodology, _ = _setup(
        db_session, parameters={"delta_nbr_threshold": -0.4, "min_area_m2": 9_000}
    )
    fake = FakeEarthEngine(features=[_poly_feature(0.1, 10_000)])
    extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        methodology=methodology,
        delta_image="img",
        region=_REGION,
        ee_module=fake,
    )
    assert fake.calls[0]["threshold"] == -0.4
    assert fake.calls[0]["min_area_m2"] == 9_000


def test_explicit_overrides_win(db_session: Session) -> None:
    change, methodology, _ = _setup(
        db_session, parameters={"delta_nbr_threshold": -0.4, "min_area_m2": 9_000}
    )
    fake = FakeEarthEngine(features=[_poly_feature(0.1, 10_000)])
    extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        methodology=methodology,
        delta_image="img",
        region=_REGION,
        threshold=-0.6,
        min_area_m2=1_000,
        ee_module=fake,
    )
    assert fake.calls[0]["threshold"] == -0.6
    assert fake.calls[0]["min_area_m2"] == 1_000


def test_rerun_replaces_candidates(db_session: Session) -> None:
    change, methodology, _ = _setup(db_session)
    for _ in range(2):
        extract_candidates_for_change_raster(
            db_session,
            change_raster=change,
            methodology=methodology,
            delta_image="img",
            region=_REGION,
            ee_module=FakeEarthEngine(features=[_poly_feature(0.1, 10_000)]),
        )
        db_session.commit()
    assert len(db_session.execute(select(DisturbanceCandidate)).scalars().all()) == 1


def test_tracked_candidates_are_frozen_on_rerun(db_session: Session) -> None:
    """Once a candidate is tracked into an event, re-extraction must not touch the
    set (audit BUG-2): no FK violation, no duplicates, no Earth Engine call."""
    change, methodology, _ = _setup(db_session)
    first = extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        methodology=methodology,
        delta_image="img",
        region=_REGION,
        ee_module=FakeEarthEngine(features=[_poly_feature(0.1, 10_000)]),
    )
    db_session.commit()
    aoi = db_session.execute(select(Aoi)).scalar_one()
    track_events_for_aoi(db_session, aoi=aoi)
    db_session.commit()

    fake = FakeEarthEngine(features=[_poly_feature(0.3, 20_000)])
    rerun = extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        methodology=methodology,
        delta_image="img",
        region=_REGION,
        ee_module=fake,
    )
    db_session.commit()

    assert fake.calls == []  # frozen set: extraction short-circuits before EE
    assert [c.id for c in rerun] == [first[0].id]
    assert len(db_session.execute(select(DisturbanceCandidate)).scalars().all()) == 1


def test_methodology_forest_mask_is_applied_to_the_delta(db_session: Session) -> None:
    """A methodology with a forest mask (#82) thresholds the MASKED delta: only
    forested pixels can produce candidates."""
    mask_config = {
        "source": "hansen",
        "asset": "UMD/hansen/global_forest_change_2023_v1_11",
        "canopy_threshold_pct": 30.0,
    }
    change, methodology, _ = _setup(db_session, parameters={"forest_mask": mask_config})
    fake = FakeEarthEngine(features=[_poly_feature(0.1, 10_000)])

    candidates = extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        methodology=methodology,
        delta_image="delta-ee-image",
        region=_REGION,
        ee_module=fake,
    )

    assert len(candidates) == 1
    # The mask was built from the recorded methodology parameters...
    assert fake.forest_mask_calls == [mask_config]
    # ...applied to the delta...
    assert fake.update_mask_calls == [("delta-ee-image", {"forest_mask": mask_config})]
    # ...and the masked image is what was thresholded/vectorized.
    assert fake.calls[0]["delta_image"] == {
        "masked": "delta-ee-image",
        "mask": {"forest_mask": mask_config},
    }


def test_worldcover_forest_mask_is_applied(db_session: Session) -> None:
    mask_config = {"source": "worldcover", "asset": "ESA/WorldCover/v200", "tree_class": 10}
    change, methodology, _ = _setup(db_session, parameters={"forest_mask": mask_config})
    fake = FakeEarthEngine(features=[])

    extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        methodology=methodology,
        delta_image="img",
        region=_REGION,
        ee_module=fake,
    )

    assert fake.forest_mask_calls == [mask_config]
    assert len(fake.update_mask_calls) == 1


def test_no_forest_mask_key_means_unmasked_extraction(db_session: Session) -> None:
    """Methodologies minted before #82 (no forest_mask key) must extract exactly as
    before: no mask built, the raw delta thresholded."""
    change, methodology, _ = _setup(db_session)  # default parameters: no forest_mask key
    fake = FakeEarthEngine(features=[_poly_feature(0.1, 10_000)])

    extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        methodology=methodology,
        delta_image="raw-delta",
        region=_REGION,
        ee_module=fake,
    )

    assert fake.forest_mask_calls == []
    assert fake.update_mask_calls == []
    assert fake.calls[0]["delta_image"] == "raw-delta"


def test_explicit_forest_mask_override_wins(db_session: Session) -> None:
    """An explicit override replaces the methodology's mask, mirroring the
    threshold/min-area override semantics."""
    stored = {"source": "worldcover", "asset": "ESA/WorldCover/v200", "tree_class": 10}
    override = {"source": "hansen", "asset": "custom/asset", "canopy_threshold_pct": 50.0}
    change, methodology, _ = _setup(db_session, parameters={"forest_mask": stored})
    fake = FakeEarthEngine(features=[])

    extract_candidates_for_change_raster(
        db_session,
        change_raster=change,
        methodology=methodology,
        delta_image="img",
        region=_REGION,
        forest_mask=override,
        ee_module=fake,
    )

    assert fake.forest_mask_calls == [override]
