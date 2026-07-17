"""Cover the Earth Engine seam by stubbing the ``ee`` module with a MagicMock.

These tests pin the exact EE interactions (collection ids, band name, export options,
bit math) without a live Earth Engine session.
"""

from unittest.mock import MagicMock

import pytest

from forest_sentinel import earthengine


@pytest.fixture
def fake_ee(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock(name="ee")
    monkeypatch.setattr(earthengine, "ee", fake)
    return fake


def test_initialize_uses_explicit_project(fake_ee: MagicMock) -> None:
    earthengine.initialize("my-project")
    fake_ee.Initialize.assert_called_once_with(project="my-project")


def test_initialize_falls_back_to_env(fake_ee: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(earthengine.GEE_PROJECT_ENV_VAR, "env-project")
    earthengine.initialize()
    fake_ee.Initialize.assert_called_once_with(project="env-project")


def test_initialize_wraps_ee_failures(fake_ee: MagicMock) -> None:
    class FakeEEException(Exception):
        pass

    fake_ee.EEException = FakeEEException
    fake_ee.Initialize.side_effect = FakeEEException("no credentials")
    with pytest.raises(earthengine.EarthEngineError, match="initialization failed"):
        earthengine.initialize("my-project")


def test_start_image_export_submits_cog_task(fake_ee: MagicMock) -> None:
    task = fake_ee.batch.Export.image.toCloudStorage.return_value
    region = {"type": "MultiPolygon", "coordinates": []}
    returned = earthengine.start_image_export_to_gcs(
        "image", bucket="b", file_name_prefix="a/b/c", scale=30, region=region
    )
    assert returned is task
    task.start.assert_called_once_with()
    _, kwargs = fake_ee.batch.Export.image.toCloudStorage.call_args
    assert kwargs["bucket"] == "b"
    assert kwargs["fileNamePrefix"] == "a/b/c"
    assert kwargs["fileFormat"] == "GeoTIFF"
    assert kwargs["formatOptions"] == {"cloudOptimized": True}
    # Bounded headroom over EE's 1e8 default; regions are scene-clipped (#78).
    assert kwargs["maxPixels"] == 1_000_000_000
    # Export.image rejects a raw GeoJSON dict — the region must be an ee.Geometry.
    fake_ee.Geometry.assert_called_once_with(region)
    assert kwargs["region"] is fake_ee.Geometry.return_value


def test_start_image_export_leaves_missing_region_unset(fake_ee: MagicMock) -> None:
    earthengine.start_image_export_to_gcs("image", bucket="b", file_name_prefix="a/b/c")
    fake_ee.Geometry.assert_not_called()
    _, kwargs = fake_ee.batch.Export.image.toCloudStorage.call_args
    assert kwargs["region"] is None


def test_export_task_state_reads_status() -> None:
    task = MagicMock()
    task.status.return_value = {"state": "RUNNING"}
    assert earthengine.export_task_state(task) == "RUNNING"


def test_export_task_state_wraps_ee_failures(fake_ee: MagicMock) -> None:
    class FakeEEException(Exception):
        pass

    fake_ee.EEException = FakeEEException
    task = MagicMock()
    task.status.side_effect = FakeEEException("transient 500")
    with pytest.raises(earthengine.EarthEngineError, match="task state"):
        earthengine.export_task_state(task)


@pytest.mark.parametrize(
    ("state", "expected"),
    [("FAILED", True), ("CANCELLED", True), ("RUNNING", False), ("COMPLETED", False)],
)
def test_is_terminal_failure(state: str, expected: bool) -> None:
    assert earthengine.is_terminal_failure(state) is expected


def test_list_image_properties_maps_features(fake_ee: MagicMock) -> None:
    chain = fake_ee.ImageCollection.return_value.filterBounds.return_value.filterDate.return_value
    chain.getInfo.return_value = {
        "features": [{"id": "img-1", "properties": {"system:index": "scene-1"}}]
    }
    result = earthengine.list_image_properties("C", {"type": "Polygon"}, "2026-01-01", "2026-01-31")
    assert result == [{"id": "img-1", "properties": {"system:index": "scene-1"}}]
    fake_ee.ImageCollection.assert_called_once_with("C")


def test_list_image_properties_wraps_ee_failures(fake_ee: MagicMock) -> None:
    class FakeEEException(Exception):
        pass

    fake_ee.EEException = FakeEEException
    chain = fake_ee.ImageCollection.return_value.filterBounds.return_value.filterDate.return_value
    chain.getInfo.side_effect = FakeEEException("quota exceeded")
    with pytest.raises(earthengine.EarthEngineError, match="listing"):
        earthengine.list_image_properties("C", {}, "2026-01-01", "2026-01-31")


def test_list_image_properties_handles_empty(fake_ee: MagicMock) -> None:
    chain = fake_ee.ImageCollection.return_value.filterBounds.return_value.filterDate.return_value
    chain.getInfo.return_value = None
    assert earthengine.list_image_properties("C", {}, "2026-01-01", "2026-01-31") == []


def test_apply_fmask_mask_selects_band_and_updates_mask(fake_ee: MagicMock) -> None:
    image = MagicMock(name="image")
    result = earthengine.apply_fmask_mask(image)
    image.select.assert_called_once_with("Fmask")
    image.updateMask.assert_called_once()
    assert result is image.updateMask.return_value


def test_valid_pixel_fraction_reduces_mask(fake_ee: MagicMock) -> None:
    image = MagicMock(name="image")
    reduced = image.select.return_value.mask.return_value.reduceRegion.return_value
    reduced.get.return_value.getInfo.return_value = 0.75
    assert earthengine.valid_pixel_fraction(image, "NBR", {"type": "Polygon"}, 30) == 0.75


def test_valid_pixel_fraction_none_is_zero(fake_ee: MagicMock) -> None:
    image = MagicMock(name="image")
    reduced = image.select.return_value.mask.return_value.reduceRegion.return_value
    reduced.get.return_value.getInfo.return_value = None
    assert earthengine.valid_pixel_fraction(image, "NBR", {}, 30) == 0.0


def test_apply_fmask_mask_accepts_custom_band(fake_ee: MagicMock) -> None:
    image = MagicMock(name="image")
    earthengine.apply_fmask_mask(image, fmask_band="QA")
    image.select.assert_called_once_with("QA")


def test_normalized_difference_passes_bands() -> None:
    image = MagicMock(name="image")
    result = earthengine.normalized_difference(image, ["B5", "B7"])
    image.normalizedDifference.assert_called_once_with(["B5", "B7"])
    assert result is image.normalizedDifference.return_value


def test_median_of_builds_collection(fake_ee: MagicMock) -> None:
    result = earthengine.median_of(["a", "b"])
    fake_ee.ImageCollection.assert_called_once_with(["a", "b"])
    assert result is fake_ee.ImageCollection.return_value.median.return_value


def test_subtract_delegates() -> None:
    image = MagicMock(name="image")
    result = earthengine.subtract(image, "baseline")
    image.subtract.assert_called_once_with("baseline")
    assert result is image.subtract.return_value


def test_feature_with_area_sets_area_property() -> None:
    feature = MagicMock(name="feature")
    result = earthengine._feature_with_area(feature)
    feature.area.assert_called_once_with(maxError=1)
    feature.set.assert_called_once_with("area_m2", feature.area.return_value)
    assert result is feature.set.return_value


def _vectorize_result(delta: MagicMock) -> MagicMock:
    """The MagicMock node returned by the threshold/vectorize chain's ``.filter(...)``."""
    vectors = delta.lt.return_value.selfMask.return_value.reduceToVectors.return_value
    filtered: MagicMock = vectors.map.return_value.filter.return_value
    return filtered


def test_threshold_and_vectorize_returns_features(fake_ee: MagicMock) -> None:
    delta = MagicMock(name="delta")
    _vectorize_result(delta).getInfo.return_value = {
        "features": [{"geometry": {}, "properties": {"area_m2": 5}}]
    }

    features = earthengine.threshold_and_vectorize(
        delta, threshold=-0.25, scale=30, region={"type": "Polygon"}, min_area_m2=4500
    )
    assert features == [{"geometry": {}, "properties": {"area_m2": 5}}]
    delta.lt.assert_called_once_with(-0.25)


def test_threshold_and_vectorize_handles_empty(fake_ee: MagicMock) -> None:
    delta = MagicMock(name="delta")
    _vectorize_result(delta).getInfo.return_value = None
    result = earthengine.threshold_and_vectorize(
        delta, threshold=-0.25, scale=30, region={}, min_area_m2=4500
    )
    assert result == []


def test_scene_footprint_returns_geometry_geojson(fake_ee: MagicMock) -> None:
    image = MagicMock()
    image.geometry.return_value.getInfo.return_value = {"type": "Polygon", "coordinates": []}
    footprint = earthengine.scene_footprint(image)
    image.geometry.assert_called_once_with(30.0)
    assert footprint == {"type": "Polygon", "coordinates": []}


def test_scene_footprint_wraps_ee_failures(fake_ee: MagicMock) -> None:
    class FakeEEException(Exception):
        pass

    fake_ee.EEException = FakeEEException
    image = MagicMock()
    image.geometry.return_value.getInfo.side_effect = FakeEEException("quota")
    with pytest.raises(earthengine.EarthEngineError, match="scene footprint"):
        earthengine.scene_footprint(image)


def test_hansen_forest_mask_thresholds_canopy_and_excludes_loss(fake_ee: MagicMock) -> None:
    """Forest = treecover2000 >= threshold AND no recorded loss year (#82)."""
    treecover, lossyear = MagicMock(name="treecover2000"), MagicMock(name="lossyear")
    gfc = fake_ee.Image.return_value
    gfc.select.side_effect = lambda band: {"treecover2000": treecover, "lossyear": lossyear}[band]

    mask = earthengine.hansen_forest_mask("UMD/hansen/gfc", canopy_threshold_pct=30.0)

    fake_ee.Image.assert_called_once_with("UMD/hansen/gfc")
    treecover.gte.assert_called_once_with(30.0)
    # Already-lost pixels (lossyear != 0) are excluded; masked lossyear reads as 0.
    lossyear.unmask.assert_called_once_with(0)
    lossyear.unmask.return_value.eq.assert_called_once_with(0)
    treecover.gte.return_value.And.assert_called_once_with(
        lossyear.unmask.return_value.eq.return_value
    )
    assert mask is treecover.gte.return_value.And.return_value


def test_worldcover_forest_mask_selects_the_tree_class(fake_ee: MagicMock) -> None:
    first = fake_ee.ImageCollection.return_value.first.return_value
    mask = earthengine.worldcover_forest_mask("ESA/WorldCover/v200", tree_class=10)

    fake_ee.ImageCollection.assert_called_once_with("ESA/WorldCover/v200")
    first.select.assert_called_once_with("Map")
    first.select.return_value.eq.assert_called_once_with(10)
    assert mask is first.select.return_value.eq.return_value


def test_update_mask_applies_the_mask() -> None:
    image, mask = MagicMock(), MagicMock()
    assert earthengine.update_mask(image, mask) is image.updateMask.return_value
    image.updateMask.assert_called_once_with(mask)
