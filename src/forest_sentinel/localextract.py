"""Zero-EE candidate extraction from a stored change COG (Finding 2).

The exported change COGs are the *raw signed* ΔNBR / ΔVV-dB — thresholds and
the forest mask are applied at extraction time, never baked into the file — so
re-extracting candidates for a new detection layer does not need Earth Engine
at all: threshold + polygonize the local COG. The pipeline prefers this path
when the raster's COG is on disk and falls back to rebuilding the delta graph
in EE when it is not (retention interplay: a pruned COG still has its recorded
provenance).

Memory discipline (config-inventory Finding 2's cost analysis): the float delta
is streamed in windows and reduced to one uint8 mask array — never more than
one window of float pixels plus ~1 byte/pixel resident — so a full HLS tile
stays well inside the reference VM's budget. Polygonization runs once over the
assembled mask so shapes never split at window boundaries.

The forest mask is applied from a locally exported mask COG (one static export
per AOI and mask config, reused forever), warped onto the delta's exact grid
with a nearest-neighbour ``WarpedVRT`` — grid alignment is handled by GDAL, not
assumed.

Output features carry the same shape ``earthengine.threshold_and_vectorize``
returns — WGS 84 geometry plus ``area_m2`` / ``delta_mean`` / ``delta_min`` /
``valid_pixels`` properties — so persistence downstream cannot tell the paths
apart.
"""

import hashlib
import json
import logging
from typing import Any

import numpy as np
import pyproj
import rasterio
import rasterio.features
import rasterio.mask
import rasterio.vrt
import rasterio.warp
from shapely.geometry import mapping, shape

logger = logging.getLogger(__name__)

_WGS84 = "EPSG:4326"
_GEOD = pyproj.Geod(ellps="WGS84")


class LocalExtractError(RuntimeError):
    """Raised when a stored COG cannot be extracted locally (caller falls back)."""


def extract_features_from_cog(
    cog_path: str,
    *,
    threshold: float,
    min_area_m2: float,
    mask_cog_path: str | None = None,
    window_size: int = 512,
) -> list[dict[str, Any]]:
    """Threshold + polygonize a stored delta COG into candidate features.

    Pixels with ``delta < threshold`` (and, when a mask COG is given, mask == 1)
    form the candidate mask; connected regions (4-connectivity, matching
    ``reduceToVectors``' ``eightConnected=False``) polygonize into features.
    Per-polygon statistics are computed from the same file, so a feature is
    fully self-describing before the COG is ever pruned.
    """
    try:
        with rasterio.open(cog_path) as delta:
            candidate_mask, valid = _build_mask(
                delta, threshold=threshold, mask_cog_path=mask_cog_path, window_size=window_size
            )
            return _polygonize(delta, candidate_mask, valid, min_area_m2=min_area_m2)
    except (rasterio.errors.RasterioError, rasterio.errors.RasterioIOError, ValueError) as exc:
        raise LocalExtractError(f"cannot extract locally from {cog_path}: {exc}") from exc


def _build_mask(
    delta: rasterio.DatasetReader,
    *,
    threshold: float,
    mask_cog_path: str | None,
    window_size: int,
) -> tuple[np.ndarray, np.ndarray]:
    """One streamed pass: (candidate mask, valid-data mask) as uint8/bool arrays."""
    candidate = np.zeros((delta.height, delta.width), dtype=np.uint8)
    valid = np.zeros((delta.height, delta.width), dtype=bool)
    forest_vrt = None
    forest_dataset = None
    try:
        if mask_cog_path is not None:
            forest_dataset = rasterio.open(mask_cog_path)
            # Warp the static AOI-wide mask onto the delta's exact grid; nearest
            # neighbour keeps it binary.
            forest_vrt = rasterio.vrt.WarpedVRT(
                forest_dataset,
                crs=delta.crs,
                transform=delta.transform,
                width=delta.width,
                height=delta.height,
                resampling=rasterio.enums.Resampling.nearest,
            )
        for _, window in _windows(delta, window_size):
            # Earth Engine exports masked pixels as NaN with NO nodata tag, so
            # the masked read alone does not exclude them — mask non-finite
            # values or they count as valid data (same fix as trajectory.py).
            block = np.ma.masked_invalid(delta.read(1, window=window, masked=True))
            rows, cols = window.toslices()
            valid[rows, cols] = ~np.ma.getmaskarray(block)
            hits = np.ma.filled(block < threshold, False)
            if forest_vrt is not None:
                forest_block = forest_vrt.read(1, window=window, masked=True)
                hits &= np.ma.filled(forest_block, 0).astype(bool)
            candidate[rows, cols] = hits
    finally:
        if forest_vrt is not None:
            forest_vrt.close()
        if forest_dataset is not None:
            forest_dataset.close()
    return candidate, valid


def _windows(delta: rasterio.DatasetReader, size: int) -> list[tuple[Any, rasterio.windows.Window]]:
    return [
        (
            (row, col),
            rasterio.windows.Window(
                col, row, min(size, delta.width - col), min(size, delta.height - row)
            ),
        )
        for row in range(0, delta.height, size)
        for col in range(0, delta.width, size)
    ]


def _polygonize(
    delta: rasterio.DatasetReader,
    candidate_mask: np.ndarray,
    valid: np.ndarray,
    *,
    min_area_m2: float,
) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for geom, value in rasterio.features.shapes(
        candidate_mask, mask=candidate_mask.astype(bool), transform=delta.transform
    ):
        if not value:  # background shapes are excluded by mask=, belt and braces
            continue
        geometry_wgs84 = (
            geom
            if delta.crs is None or delta.crs.to_string() == _WGS84
            else rasterio.warp.transform_geom(delta.crs, _WGS84, geom)
        )
        area_m2 = abs(_GEOD.geometry_area_perimeter(shape(geometry_wgs84))[0])
        if area_m2 < min_area_m2:
            continue
        stats = _polygon_statistics(delta, geom, valid)
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(shape(geometry_wgs84)),
                "properties": {"area_m2": area_m2, **stats},
            }
        )
    return features


def _polygon_statistics(
    delta: rasterio.DatasetReader, geometry: dict[str, Any], valid: np.ndarray
) -> dict[str, Any]:
    """delta_mean / delta_min / valid_pixels over the polygon, from its bbox window.

    Mirrors the EE extraction's per-polygon ``reduceRegions`` statistics (#95):
    persisted at extraction time because the source COG is prunable.
    """
    window = rasterio.features.geometry_window(delta, [geometry])
    # NaN-as-masked (no nodata tag) is the EE export convention: unmasked NaNs
    # here would inflate valid_pixels and turn delta_mean/delta_min into NaN —
    # which downstream confidence factors would ingest as recorded evidence.
    block = np.ma.masked_invalid(delta.read(1, window=window, masked=True))
    rows, cols = window.toslices()
    polygon_mask = rasterio.features.geometry_mask(
        [geometry],
        out_shape=(int(window.height), int(window.width)),
        transform=delta.window_transform(window),
        invert=True,
    )
    inside = np.ma.masked_array(block, mask=np.ma.getmaskarray(block) | ~polygon_mask)
    valid_pixels = int((~np.ma.getmaskarray(inside)).sum())
    if valid_pixels == 0:
        return {"delta_mean": None, "delta_min": None, "valid_pixels": 0}
    return {
        "delta_mean": float(inside.mean()),
        "delta_min": float(inside.min()),
        "valid_pixels": valid_pixels,
    }


def mask_cog_key_filename(mask_config: dict[str, Any]) -> str:
    """Deterministic mask-COG filename for a forest-mask config."""
    canonical = json.dumps(mask_config, sort_keys=True, separators=(",", ":"))
    return f"forest-mask-{hashlib.sha256(canonical.encode()).hexdigest()[:10]}.tif"
