"""`cogs reproduce` (#94): rebuild a pruned raster from recorded provenance."""

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from forest_sentinel.models import (
    Aoi,
    ChangeRasterSource,
    IndexRaster,
    MethodologyVersion,
    Observation,
)
from forest_sentinel.reproduce import (
    ReproduceError,
    reproduce_change_raster,
    reproduce_index_raster,
)
from forest_sentinel.storage import CogKey
from tests.fakes import (
    FakeEarthEngine,
    FakeStorage,
    make_aoi,
    make_change_raster,
    make_methodology,
    make_observation,
)

SCRIPT_VERSION = "slice1-optical-change-v1"


def _methodology(session: Session) -> MethodologyVersion:
    return make_methodology(
        session, parameters={"ee_script_version": SCRIPT_VERSION, "scale_m": 30}
    )


def _index_row(
    session: Session,
    storage: FakeStorage,
    aoi: Aoi,
    obs: Observation,
    methodology: MethodologyVersion,
    *,
    index_type: str = "NBR",
) -> IndexRaster:
    key = CogKey(
        aoi=f"{aoi.id}-{aoi.name}",
        product=index_type,
        date=obs.acquired_at.date().isoformat(),
        filename=f"{index_type.lower()}-{obs.source_scene_id}.tif",
    )
    row = IndexRaster(
        observation_id=obs.id,
        raster_lineage_id=methodology.raster_lineage_id,
        index_type=index_type,
        cog_path=str(storage.path_for(key)),
    )
    session.add(row)
    session.flush()
    return row


def test_reproduce_index_raster_reexports_to_recorded_path(
    db_session: Session, tmp_path: Path
) -> None:
    aoi = make_aoi(db_session)
    obs = make_observation(db_session, aoi, day=6)  # HLSL30, scene-6
    methodology = _methodology(db_session)
    storage = FakeStorage(tmp_path)
    row = _index_row(db_session, storage, aoi, obs, methodology)
    fake = FakeEarthEngine()

    path = reproduce_index_raster(
        db_session,
        raster=row,
        storage=storage,
        current_script_version=SCRIPT_VERSION,
        ee_module=fake,
    )

    assert path == Path(row.cog_path)
    assert path.exists()
    # The image lineage is the recorded recipe: the observation's scene, Fmask-masked,
    # NBR band math for its sensor, exported at the methodology's scale.
    assert fake.image_ids == ["NASA/HLS/HLSL30/v002/scene-6"]
    image, _key, scale = storage.exports[0]
    masked_scene = {"masked": {"id": "NASA/HLS/HLSL30/v002/scene-6"}}
    assert image == {"nd": ("B5", "B7"), "image": masked_scene}
    assert scale == 30


def test_reproduce_change_raster_uses_recorded_baseline(
    db_session: Session, tmp_path: Path
) -> None:
    aoi = make_aoi(db_session)
    methodology = _methodology(db_session)
    storage = FakeStorage(tmp_path)
    current = make_observation(db_session, aoi, day=6)
    priors = [make_observation(db_session, aoi, day=day) for day in (1, 2, 3)]
    # A newer prior that was indexed AFTER the change raster was recorded: it must
    # not leak into the reproduced baseline — provenance beats "the priors now".
    newer = make_observation(db_session, aoi, day=5)

    current_index = _index_row(db_session, storage, aoi, current, methodology)
    prior_indexes = [_index_row(db_session, storage, aoi, obs, methodology) for obs in priors]
    _index_row(db_session, storage, aoi, newer, methodology)

    change_key = CogKey(
        aoi=f"{aoi.id}-{aoi.name}",
        product="delta_nbr",
        date=current.acquired_at.date().isoformat(),
        filename=f"delta_nbr-{current.source_scene_id}.tif",
    )
    change = make_change_raster(
        db_session, current, methodology, cog_path=str(storage.path_for(change_key))
    )
    for index_row in (current_index, *prior_indexes):
        db_session.add(ChangeRasterSource(change_raster_id=change.id, index_raster_id=index_row.id))
    db_session.flush()
    fake = FakeEarthEngine()

    path = reproduce_change_raster(
        db_session,
        raster=change,
        storage=storage,
        current_script_version=SCRIPT_VERSION,
        ee_module=fake,
    )

    assert path == Path(change.cog_path)
    assert path.exists()
    # The baseline median reduced exactly the three recorded priors, not four.
    assert fake.median_sizes == [3]
    image, _key, scale = storage.exports[0]
    assert image == {
        "delta": (
            {"nd": ("B5", "B7"), "image": {"masked": {"id": "NASA/HLS/HLSL30/v002/scene-6"}}},
            {"median": 3},
        )
    }
    assert scale == 30


def test_change_raster_without_recorded_baseline_is_refused(
    db_session: Session, tmp_path: Path
) -> None:
    aoi = make_aoi(db_session)
    methodology = _methodology(db_session)
    storage = FakeStorage(tmp_path)
    current = make_observation(db_session, aoi, day=6)
    change = make_change_raster(db_session, current, methodology, cog_path="/data/x.tif")

    with pytest.raises(ReproduceError, match="no baseline sources"):
        reproduce_change_raster(
            db_session,
            raster=change,
            storage=storage,
            current_script_version=SCRIPT_VERSION,
            ee_module=FakeEarthEngine(),
        )
    assert storage.exports == []


def test_script_version_mismatch_is_refused_without_force(
    db_session: Session, tmp_path: Path
) -> None:
    aoi = make_aoi(db_session)
    obs = make_observation(db_session, aoi, day=6)
    methodology = _methodology(db_session)
    storage = FakeStorage(tmp_path)
    row = _index_row(db_session, storage, aoi, obs, methodology)

    with pytest.raises(ReproduceError, match="does not match the running code"):
        reproduce_index_raster(
            db_session,
            raster=row,
            storage=storage,
            current_script_version="slice9-different",
            ee_module=FakeEarthEngine(),
        )
    assert storage.exports == []


def test_script_version_mismatch_forced_warns_and_exports(
    db_session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    aoi = make_aoi(db_session)
    obs = make_observation(db_session, aoi, day=6)
    methodology = _methodology(db_session)
    storage = FakeStorage(tmp_path)
    row = _index_row(db_session, storage, aoi, obs, methodology)

    with caplog.at_level("WARNING", logger="forest_sentinel.reproduce"):
        path = reproduce_index_raster(
            db_session,
            raster=row,
            storage=storage,
            current_script_version="slice9-different",
            force_version=True,
            ee_module=FakeEarthEngine(),
        )

    assert path.exists()
    assert any("does not match" in record.message for record in caplog.records)


def test_destination_mismatch_is_refused(db_session: Session, tmp_path: Path) -> None:
    # The row's cog_path predates a store-layout (or AOI-name) change: exporting to
    # the newly derived path would strand a file the catalog doesn't reference.
    aoi = make_aoi(db_session)
    obs = make_observation(db_session, aoi, day=6)
    methodology = _methodology(db_session)
    storage = FakeStorage(tmp_path)
    row = IndexRaster(
        observation_id=obs.id,
        raster_lineage_id=methodology.raster_lineage_id,
        index_type="NBR",
        cog_path="/data/cogs/old-layout/nbr.tif",
    )
    db_session.add(row)
    db_session.flush()

    with pytest.raises(ReproduceError, match="does not match the recorded"):
        reproduce_index_raster(
            db_session,
            raster=row,
            storage=storage,
            current_script_version=SCRIPT_VERSION,
            ee_module=FakeEarthEngine(),
        )
    assert storage.exports == []
