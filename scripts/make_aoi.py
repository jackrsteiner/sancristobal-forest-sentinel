#!/usr/bin/env python
"""Generate a validated AOI GeoJSON for Open Forest Sentinel.

Builds a single-``Feature`` GeoJSON (WGS 84) from a bounding box and validates it
with the same loader the pipeline uses (:func:`forest_sentinel.aoi.load_aoi_config`),
so a file produced here is guaranteed to be accepted by ``forest-sentinel run``.

Example:
    uv run python scripts/make_aoi.py \\
        --bbox 159.0 -9.6 159.3 -9.3 \\
        --name "Guadalcanal North Coast" \\
        --out config/aois/guadalcanal.geojson
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from forest_sentinel.aoi import AoiConfigError, load_aoi_config


def _bbox_feature(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float, name: str
) -> dict:
    if not (min_lon < max_lon and min_lat < max_lat):
        raise SystemExit("error: --bbox must be 'min_lon min_lat max_lon max_lat' with min < max")
    if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
        raise SystemExit("error: longitudes must be within [-180, 180]")
    if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
        raise SystemExit("error: latitudes must be within [-90, 90]")
    ring = [
        [min_lon, min_lat],
        [max_lon, min_lat],
        [max_lon, max_lat],
        [min_lon, max_lat],
        [min_lon, min_lat],
    ]
    return {
        "type": "Feature",
        "properties": {"name": name},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make_aoi.py",
        description="Generate a validated AOI GeoJSON from a bounding box.",
    )
    parser.add_argument(
        "--bbox",
        required=True,
        nargs=4,
        type=float,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        help="Bounding box in WGS 84 decimal degrees.",
    )
    parser.add_argument("--name", required=True, help="AOI name (stored as properties.name).")
    parser.add_argument(
        "--out",
        type=Path,
        metavar="PATH",
        help="Output path. Defaults to stdout when omitted.",
    )
    args = parser.parse_args(argv)

    feature = _bbox_feature(*args.bbox, name=args.name.strip())
    document = json.dumps(feature, indent=2) + "\n"

    # Validate with the real loader so the file is guaranteed pipeline-compatible.
    with tempfile.NamedTemporaryFile("w", suffix=".geojson", delete=False) as handle:
        handle.write(document)
        tmp_path = Path(handle.name)
    try:
        config = load_aoi_config(tmp_path)
    except AoiConfigError as exc:
        print(f"error: generated AOI failed validation: {exc}", file=sys.stderr)
        return 1
    finally:
        tmp_path.unlink(missing_ok=True)

    if args.out is None:
        sys.stdout.write(document)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(document)
        minx, miny, maxx, maxy = config.geometry.bounds
        print(f"Wrote AOI {config.name!r} to {args.out}")
        print(f"Bounding box (minx, miny, maxx, maxy): ({minx}, {miny}, {maxx}, {maxy})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
