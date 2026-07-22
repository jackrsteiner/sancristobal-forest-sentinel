"""Post-detection footprint NBR trajectory + persistence state (#165).

Synthetic NBR index COGs (pattern from test_localextract.py): pre-event scenes
carry healthy NBR, the detection-day scene drops inside the footprint, and the
post-detection scenes decide the state — flat-low = persistent, ramp =
recovering, immediate bounce = transient. Zero Earth Engine anywhere.
"""

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
import rasterio
from geoalchemy2.shape import from_shape
from rasterio.transform import from_origin
from shapely.geometry import MultiPolygon, box
from sqlalchemy.orm import Session

from forest_sentinel import trajectory
from forest_sentinel.models import DisturbanceEvent, IndexRaster
from tests.fakes import make_aoi, make_methodology, make_observation

_NODATA = -9999.0
_PIXEL = 0.0003
# A footprint square covering a 5×5-pixel block inside the 20×20 test grid.
_FOOTPRINT = box(0.1015, 0.897, 0.103, 0.8985)


def _write_nbr(path: Path, *, footprint_value: float, background: float = 0.6) -> None:
    data = np.full((20, 20), background, dtype="float32")
    data[5:10, 5:10] = footprint_value
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=20,
        width=20,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(0.1, 0.9, _PIXEL, _PIXEL),
        nodata=_NODATA,
    ) as dst:
        dst.write(data, 1)


def _day(day: int) -> datetime:
    return datetime(2026, 6, day, tzinfo=UTC)


def _setup(
    db_session: Session, tmp_path: Path, *, post: dict[int, float | None]
) -> DisturbanceEvent:
    """Pre days 1 & 3 at healthy NBR, detection day 5 dropped, then ``post``
    (day -> footprint NBR, or None for a scene whose COG has been pruned)."""
    aoi = make_aoi(db_session)
    methodology = make_methodology(db_session)
    scenes: dict[int, float | None] = {1: 0.6, 3: 0.6, 5: 0.1, **post}
    for day, value in scenes.items():
        obs = make_observation(
            db_session, aoi, source_scene_id=f"traj-{day}", acquired_at=_day(day)
        )
        cog = tmp_path / f"nbr-{day}.tif"
        if value is not None:
            _write_nbr(cog, footprint_value=value)
        db_session.add(
            IndexRaster(
                observation_id=obs.id,
                raster_lineage_id=methodology.raster_lineage_id,
                index_type="NBR",
                cog_path=str(cog),
                valid_pixel_fraction=1.0,
            )
        )
    event = DisturbanceEvent(
        aoi_id=aoi.id,
        methodology_version_id=methodology.id,
        geometry=from_shape(MultiPolygon([_FOOTPRINT]), srid=4326),
        status="ongoing",
        first_detected_at=_day(5),
        last_detected_at=_day(5),
    )
    db_session.add(event)
    db_session.flush()
    return event


@pytest.mark.parametrize(
    ("post", "expected_state"),
    [
        ({10: 0.12, 15: 0.13}, trajectory.STATE_PERSISTENT),
        ({10: 0.3, 15: 0.35}, trajectory.STATE_RECOVERING),
        ({10: 0.55, 15: 0.58}, trajectory.STATE_TRANSIENT),
        ({}, trajectory.STATE_INSUFFICIENT),  # nothing after the detection
        ({10: None, 15: None}, trajectory.STATE_INSUFFICIENT),  # COGs pruned
    ],
)
def test_states_from_post_detection_shape(
    db_session: Session, tmp_path: Path, post: dict[int, float | None], expected_state: str
) -> None:
    event = _setup(db_session, tmp_path, post=post)
    result = trajectory.event_trajectory(db_session, event=event)
    assert result.state == expected_state


def test_reference_detection_and_points(db_session: Session, tmp_path: Path) -> None:
    event = _setup(db_session, tmp_path, post={10: 0.12})
    result = trajectory.event_trajectory(db_session, event=event)
    assert result.reference_nbr == pytest.approx(0.6, abs=1e-4)
    assert result.detection_nbr == pytest.approx(0.1, abs=1e-4)
    # Points cover detection day onward; pre-event days feed only the reference.
    assert [p.date for p in result.points] == ["2026-06-05", "2026-06-10"]
    assert all(p.valid_fraction == pytest.approx(1.0) for p in result.points)


def _add_nbr_raster(
    db_session: Session,
    event: DisturbanceEvent,
    *,
    aoi: object,
    day: int,
    scene: str,
    cog_path: Path,
) -> None:
    from forest_sentinel.models import MethodologyVersion

    methodology = db_session.get(MethodologyVersion, event.methodology_version_id)
    assert methodology is not None
    obs = make_observation(db_session, aoi, source_scene_id=scene, acquired_at=_day(day))  # type: ignore[arg-type]
    db_session.add(
        IndexRaster(
            observation_id=obs.id,
            raster_lineage_id=methodology.raster_lineage_id,
            index_type="NBR",
            cog_path=str(cog_path),
            valid_pixel_fraction=1.0,
        )
    )
    db_session.flush()


def test_cloudy_scene_is_skipped_not_misread(db_session: Session, tmp_path: Path) -> None:
    """A post scene fully masked over the footprint contributes no point."""
    from forest_sentinel.models import Aoi

    event = _setup(db_session, tmp_path, post={15: 0.12})
    aoi = db_session.get(Aoi, event.aoi_id)
    cloudy = tmp_path / "nbr-cloudy.tif"
    data = np.full((20, 20), _NODATA, dtype="float32")  # all nodata
    with rasterio.open(
        cloudy,
        "w",
        driver="GTiff",
        height=20,
        width=20,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(0.1, 0.9, _PIXEL, _PIXEL),
        nodata=_NODATA,
    ) as dst:
        dst.write(data, 1)
    _add_nbr_raster(db_session, event, aoi=aoi, day=10, scene="traj-10-cloudy", cog_path=cloudy)

    result = trajectory.event_trajectory(db_session, event=event)
    assert [p.date for p in result.points] == ["2026-06-05", "2026-06-15"]
    assert result.state == trajectory.STATE_PERSISTENT


def _write_nbr_ee_style(path: Path, data: np.ndarray) -> None:
    """Earth Engine convention: masked pixels are NaN and there is NO nodata tag."""
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=20,
        width=20,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(0.1, 0.9, _PIXEL, _PIXEL),
    ) as dst:
        dst.write(data.astype("float32"), 1)


def test_ee_style_nan_pixels_never_poison_the_trajectory(
    db_session: Session, tmp_path: Path
) -> None:
    """Regression (observed live): EE-exported COGs carry NaN for masked pixels
    with no nodata tag. Unmasked, they turned every mean into NaN — which the
    classifier fell through to "recovering" and pydantic serialized as null,
    crashing the sparkline. NaNs must count as invalid pixels instead."""
    from forest_sentinel.models import Aoi

    event = _setup(db_session, tmp_path, post={})
    aoi = db_session.get(Aoi, event.aoi_id)

    # Day 10: footprint half NaN (cloud), half still cleared -> a usable point
    # with valid_fraction ~= 0.5 and a finite low mean.
    half = np.full((20, 20), 0.6, dtype="float32")
    half[5:10, 5:10] = 0.12
    half[5:10, 5:7] = np.nan  # 2 of 5 footprint columns masked
    half_path = tmp_path / "nbr-ee-10.tif"
    _write_nbr_ee_style(half_path, half)
    _add_nbr_raster(db_session, event, aoi=aoi, day=10, scene="traj-ee-10", cog_path=half_path)

    # Day 15: footprint entirely NaN -> skipped, not a null/NaN point.
    blank = np.full((20, 20), 0.6, dtype="float32")
    blank[5:10, 5:10] = np.nan
    blank_path = tmp_path / "nbr-ee-15.tif"
    _write_nbr_ee_style(blank_path, blank)
    _add_nbr_raster(db_session, event, aoi=aoi, day=15, scene="traj-ee-15", cog_path=blank_path)

    result = trajectory.event_trajectory(db_session, event=event)
    assert [p.date for p in result.points] == ["2026-06-05", "2026-06-10"]
    day10 = result.points[-1]
    assert day10.mean_nbr == pytest.approx(0.12, abs=1e-3)
    assert day10.valid_fraction == pytest.approx(0.6, abs=0.05)  # 3 of 5 columns clear
    assert result.state == trajectory.STATE_PERSISTENT
    # Nothing non-finite may ever reach the payload.
    payload = result.as_dict()
    import math as _math

    assert all(_math.isfinite(p["mean_nbr"]) for p in payload["points"])


def test_same_day_granules_merge_pixel_weighted(db_session: Session, tmp_path: Path) -> None:
    from forest_sentinel.models import Aoi

    event = _setup(db_session, tmp_path, post={10: 0.2})
    aoi = db_session.get(Aoi, event.aoi_id)
    # A second granule on day 10 with a different footprint value: the point is
    # one pixel-weighted look, not two.
    second = tmp_path / "nbr-10b.tif"
    _write_nbr(second, footprint_value=0.4)
    _add_nbr_raster(db_session, event, aoi=aoi, day=10, scene="traj-10b", cog_path=second)

    result = trajectory.event_trajectory(db_session, event=event)
    day10 = next(p for p in result.points if p.date == "2026-06-10")
    assert day10.mean_nbr == pytest.approx(0.3, abs=1e-3)  # equal weights -> midpoint


def test_batch_matches_single_event_results(db_session: Session, tmp_path: Path) -> None:
    """#170: the inverted loop must be a pure optimization — identical output."""
    from forest_sentinel.models import Aoi

    event_a = _setup(db_session, tmp_path, post={10: 0.12, 15: 0.13})  # persistent
    aoi = db_session.get(Aoi, event_a.aoi_id)
    # A second event on the same AOI/lineage with a different footprint block:
    # offset by 8 pixels, sharing the same COGs.
    offset = box(0.1015 + 8 * _PIXEL, 0.897 - 8 * _PIXEL, 0.103 + 8 * _PIXEL, 0.8985 - 8 * _PIXEL)
    event_b = DisturbanceEvent(
        aoi_id=event_a.aoi_id,
        methodology_version_id=event_a.methodology_version_id,
        geometry=from_shape(MultiPolygon([offset]), srid=4326),
        status="ongoing",
        first_detected_at=_day(5),
        last_detected_at=_day(5),
    )
    db_session.add(event_b)
    db_session.flush()
    del aoi

    batch = trajectory.trajectories_for_events(db_session, events=[event_a, event_b])
    for event in (event_a, event_b):
        single = trajectory.event_trajectory(db_session, event=event)
        assert batch[event.id] == single


def test_batch_opens_each_cog_exactly_once(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#170: opens scale with rasters, not events x rasters."""
    event_a = _setup(db_session, tmp_path, post={10: 0.12, 15: 0.13})
    event_b = DisturbanceEvent(
        aoi_id=event_a.aoi_id,
        methodology_version_id=event_a.methodology_version_id,
        geometry=event_a.geometry,
        status="ongoing",
        first_detected_at=_day(5),
        last_detected_at=_day(5),
    )
    db_session.add(event_b)
    db_session.flush()

    opened: list[str] = []
    real_open = rasterio.open

    def counting_open(path: object, *args: object, **kwargs: object) -> object:
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(rasterio, "open", counting_open)
    trajectory.trajectories_for_events(db_session, events=[event_a, event_b])
    # 5 COGs in the fixture (days 1, 3, 5, 10, 15), 2 events: 5 opens, not 10.
    assert len(opened) == 5
    assert len(set(opened)) == 5
