"""Local COG-based candidate extraction (config-inventory Finding 2).

These tests write real (tiny) GeoTIFFs — the whole point of the local path is
reading actual raster bytes instead of holding an EE handle.
"""

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from forest_sentinel.localextract import (
    LocalExtractError,
    extract_features_from_cog,
    mask_cog_key_filename,
)

_NODATA = -9999.0
# ~33 m pixels in degrees, inside the unit-square test AOI.
_PIXEL = 0.0003


def _write_delta(
    path: Path,
    data: np.ndarray,
    *,
    crs: str = "EPSG:4326",
    # None = no nodata tag at all — the Earth Engine export convention.
    nodata: float | None = _NODATA,
) -> None:
    height, width = data.shape
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs=crs,
        transform=from_origin(0.1, 0.9, _PIXEL, _PIXEL),
        nodata=nodata,
    ) as dst:
        dst.write(data.astype("float32"), 1)


def _grid(fill: float = 0.0, size: int = 20) -> np.ndarray:
    return np.full((size, size), fill, dtype="float32")


def test_extracts_thresholded_patch_with_statistics(tmp_path: Path) -> None:
    data = _grid()
    data[5:10, 5:10] = -0.5  # 25 pixels below a -0.25 threshold
    cog = tmp_path / "delta.tif"
    _write_delta(cog, data)

    features = extract_features_from_cog(str(cog), threshold=-0.25, min_area_m2=1_000.0)

    assert len(features) == 1
    properties = features[0]["properties"]
    # 25 pixels of ~33 m: ~27,000 m2 geodesically; assert the right magnitude.
    assert properties["area_m2"] == pytest.approx(27_500, rel=0.2)
    assert properties["delta_min"] == pytest.approx(-0.5)
    assert properties["delta_mean"] == pytest.approx(-0.5)
    assert properties["valid_pixels"] == 25
    assert features[0]["geometry"]["type"] == "Polygon"


def test_min_area_filters_small_patches(tmp_path: Path) -> None:
    data = _grid()
    data[2, 2] = -0.5  # a single pixel: ~1,100 m2
    data[10:16, 10:16] = -0.5  # 36 pixels: ~40,000 m2
    cog = tmp_path / "delta.tif"
    _write_delta(cog, data)

    features = extract_features_from_cog(str(cog), threshold=-0.25, min_area_m2=4_500.0)

    assert len(features) == 1
    assert features[0]["properties"]["valid_pixels"] == 36


def test_nodata_pixels_are_neither_candidates_nor_valid(tmp_path: Path) -> None:
    data = _grid(-0.5)  # everything is a hit...
    data[:, 10:] = _NODATA  # ...but the right half was never observed
    cog = tmp_path / "delta.tif"
    _write_delta(cog, data)

    features = extract_features_from_cog(str(cog), threshold=-0.25, min_area_m2=1_000.0)

    assert len(features) == 1
    assert features[0]["properties"]["valid_pixels"] == 20 * 10


def test_ee_style_nan_pixels_count_as_invalid_not_valid(tmp_path: Path) -> None:
    """Regression: EE exports masked pixels as NaN with NO nodata tag. Unmasked,
    they inflated valid_pixels and could turn delta_mean/delta_min into NaN —
    values that get persisted and fed to confidence factors."""
    import numpy as np

    data = _grid()
    data[5:10, 5:10] = -0.5  # the candidate patch
    data[5:10, 5:7] = np.nan  # 10 of its 25 pixels are EE-masked (NaN)
    cog = tmp_path / "delta.tif"
    # EE convention: no nodata tag at all.
    _write_delta(cog, data, nodata=None)

    features = extract_features_from_cog(str(cog), threshold=-0.25, min_area_m2=1_000.0)

    assert len(features) == 1
    properties = features[0]["properties"]
    assert properties["valid_pixels"] == 15  # NaN pixels are invalid, not valid
    assert properties["delta_min"] == pytest.approx(-0.5)  # finite, never NaN
    assert properties["delta_mean"] == pytest.approx(-0.5)


def test_forest_mask_cog_clips_candidates(tmp_path: Path) -> None:
    data = _grid()
    data[0:10, 0:20] = -0.5
    cog = tmp_path / "delta.tif"
    _write_delta(cog, data)
    # Forest only on the left half; the mask COG shares the delta's grid here,
    # but is applied through a WarpedVRT so a different grid would also align.
    mask = np.zeros((20, 20), dtype="float32")
    mask[:, 0:10] = 1.0
    mask_cog = tmp_path / "mask.tif"
    _write_delta(mask_cog, mask, nodata=0.0)

    features = extract_features_from_cog(
        str(cog), threshold=-0.25, min_area_m2=1_000.0, mask_cog_path=str(mask_cog)
    )

    assert len(features) == 1
    assert features[0]["properties"]["valid_pixels"] == 10 * 10


def test_four_connectivity_matches_reduce_to_vectors(tmp_path: Path) -> None:
    data = _grid()
    data[2:5, 2:5] = -0.5
    data[5:8, 5:8] = -0.5  # touches the first patch only diagonally
    cog = tmp_path / "delta.tif"
    _write_delta(cog, data)

    features = extract_features_from_cog(str(cog), threshold=-0.25, min_area_m2=1_000.0)

    assert len(features) == 2  # eightConnected=False in EE; 4-connectivity here


def test_unreadable_cog_raises_local_extract_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty.tif"
    empty.touch()  # what FakeStorage writes: zero bytes, not a raster
    with pytest.raises(LocalExtractError):
        extract_features_from_cog(str(empty), threshold=-0.25, min_area_m2=1_000.0)


def test_projected_cogs_reproject_to_wgs84(tmp_path: Path) -> None:
    data = _grid()
    data[5:10, 5:10] = -0.5
    cog = tmp_path / "delta_utm.tif"
    height, width = data.shape
    with rasterio.open(
        cog,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs="EPSG:32601",  # UTM 1N: the test transform's coordinates are valid here
        transform=from_origin(500_000, 5_000_000, 30, 30),
        nodata=_NODATA,
    ) as dst:
        dst.write(data.astype("float32"), 1)

    features = extract_features_from_cog(str(cog), threshold=-0.25, min_area_m2=1_000.0)

    assert len(features) == 1
    longitude = features[0]["geometry"]["coordinates"][0][0][0]
    assert -180.0 <= longitude <= 180.0  # geometry left the projected CRS
    # 25 UTM pixels of exactly 30 m: 22,500 m2 (geodesic ≈ planar in UTM).
    assert features[0]["properties"]["area_m2"] == pytest.approx(22_500, rel=0.05)


def test_mask_filename_is_content_addressed() -> None:
    hansen = {"source": "hansen", "canopy_threshold_pct": 30.0}
    assert mask_cog_key_filename(hansen) == mask_cog_key_filename(dict(hansen))
    assert mask_cog_key_filename(hansen) != mask_cog_key_filename(
        {"source": "hansen", "canopy_threshold_pct": 50.0}
    )
