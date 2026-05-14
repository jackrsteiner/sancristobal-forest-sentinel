"""Load, validate, and persist a configured Area of Interest (AOI).

An AOI is supplied as a GeoJSON file containing a single ``Feature`` (or a
``FeatureCollection`` holding exactly one feature). The feature's geometry is
the area the pipeline runs against and ``properties.name`` is its identifier.
GeoJSON coordinates are WGS 84 (EPSG:4326) per RFC 7946; an explicit CRS that
says otherwise is rejected.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geoalchemy2.shape import from_shape
from shapely.errors import ShapelyError
from shapely.geometry import MultiPolygon, Polygon, shape
from sqlalchemy.orm import Session

from forest_sentinel.models import AOI_SRID, Aoi

# GeoJSON is WGS 84 by definition; an explicit CRS member must agree.
_ALLOWED_CRS_NAMES = {
    "urn:ogc:def:crs:ogc:1.3:crs84",
    "urn:ogc:def:crs:epsg::4326",
    "epsg:4326",
    "crs84",
}


class AoiConfigError(ValueError):
    """Raised when an AOI configuration file is missing or invalid."""


@dataclass(frozen=True)
class AoiConfig:
    """A validated AOI configuration: a name and a WGS 84 multipolygon."""

    name: str
    geometry: MultiPolygon


def load_aoi_config(path: Path) -> AoiConfig:
    """Load and validate an AOI GeoJSON file.

    Raises ``AoiConfigError`` with a human-readable message for any problem.
    """
    if not path.is_file():
        raise AoiConfigError(f"AOI config file not found: {path}")

    try:
        document: Any = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AoiConfigError(f"AOI config is not valid JSON: {path} ({exc})") from exc

    if not isinstance(document, dict):
        raise AoiConfigError(f"AOI config must be a GeoJSON object: {path}")

    _reject_non_wgs84_crs(document, path)
    feature = _single_feature(document, path)
    name = _validated_name(feature, path)
    geometry = _validated_geometry(feature, path)
    return AoiConfig(name=name, geometry=geometry)


def persist_aoi(session: Session, config: AoiConfig) -> Aoi:
    """Persist a validated AOI configuration to the ``aoi`` table.

    The row is added and flushed; the caller owns the transaction.
    """
    aoi = Aoi(name=config.name, geometry=from_shape(config.geometry, srid=AOI_SRID))
    session.add(aoi)
    session.flush()
    return aoi


def _reject_non_wgs84_crs(document: dict[str, Any], path: Path) -> None:
    crs = document.get("crs")
    if crs is None:
        return
    name = ""
    if isinstance(crs, dict):
        properties = crs.get("properties")
        if isinstance(properties, dict):
            name = str(properties.get("name", ""))
    if name.lower() not in _ALLOWED_CRS_NAMES:
        raise AoiConfigError(
            f"AOI geometry must be in WGS 84 (EPSG:4326); got CRS {name!r}: {path}"
        )


def _single_feature(document: dict[str, Any], path: Path) -> dict[str, Any]:
    document_type = document.get("type")
    if document_type == "FeatureCollection":
        features = document.get("features")
        if not isinstance(features, list) or len(features) != 1:
            raise AoiConfigError(f"AOI FeatureCollection must contain exactly one feature: {path}")
        feature = features[0]
    elif document_type == "Feature":
        feature = document
    else:
        raise AoiConfigError(f"AOI config must be a GeoJSON Feature or FeatureCollection: {path}")

    if not isinstance(feature, dict):
        raise AoiConfigError(f"AOI feature must be a GeoJSON object: {path}")
    return feature


def _validated_name(feature: dict[str, Any], path: Path) -> str:
    properties = feature.get("properties")
    name = properties.get("name") if isinstance(properties, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise AoiConfigError(f"AOI feature is missing a non-empty 'properties.name': {path}")
    return name.strip()


def _validated_geometry(feature: dict[str, Any], path: Path) -> MultiPolygon:
    raw_geometry = feature.get("geometry")
    if not isinstance(raw_geometry, dict):
        raise AoiConfigError(f"AOI feature is missing a geometry: {path}")

    try:
        geometry = shape(raw_geometry)
    except (ShapelyError, ValueError, KeyError, TypeError) as exc:
        raise AoiConfigError(f"AOI geometry is not valid GeoJSON geometry: {path} ({exc})") from exc

    if isinstance(geometry, Polygon):
        geometry = MultiPolygon([geometry])
    if not isinstance(geometry, MultiPolygon):
        raise AoiConfigError(
            f"AOI geometry must be a Polygon or MultiPolygon, got {geometry.geom_type}: {path}"
        )
    if geometry.is_empty:
        raise AoiConfigError(f"AOI geometry is empty: {path}")
    if not geometry.is_valid:
        raise AoiConfigError(f"AOI geometry is not valid (e.g. self-intersecting): {path}")
    return geometry
