"""Context layers (E17, #125): operator-loaded GeoJSON joined to events later.

A context layer is a plain GeoJSON ``FeatureCollection`` (or single ``Feature``)
of concessions, protected areas, roads, rivers, settlements, mills, or ports —
reference data that turns a detection into reviewable intelligence. Layers are
**operator-uploaded files**, mirroring the AOI pattern: load one explicitly with
``forest-sentinel context load``, or drop files into ``config/context/`` named
``<kind>--<name>.geojson`` and every pipeline run harvests the directory.

Layers are reference data, not provenance: re-loading a name **replaces** its
features wholesale (the current file is the truth), unlike the append-only
detection tables. ``event_context`` computation (the next bead) re-derives
relations from whatever layers exist at run time.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geoalchemy2.shape import from_shape
from shapely.errors import ShapelyError
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from forest_sentinel.models import AOI_SRID, CONTEXT_KINDS, ContextFeature, ContextLayer

logger = logging.getLogger(__name__)

# Directory of context GeoJSONs, harvested at pipeline start like config/aois.
CONTEXT_DIR_ENV_VAR = "FOREST_SENTINEL_CONTEXT_DIR"
DEFAULT_CONTEXT_DIR = "config/context"

# Harvested filenames encode the kind: <kind>--<name>.geojson.
_KIND_SEPARATOR = "--"

# GeoJSON is WGS 84 by definition; an explicit CRS member must agree (same
# contract as aoi.py).
_ALLOWED_CRS_NAMES = {
    "urn:ogc:def:crs:ogc:1.3:crs84",
    "urn:ogc:def:crs:epsg::4326",
    "epsg:4326",
    "crs84",
}


class ContextConfigError(ValueError):
    """Raised when a context-layer file is missing or invalid."""


def _reject_non_wgs84_crs(document: dict[str, Any], source: object) -> None:
    crs = document.get("crs")
    if crs is None:
        return
    name = ""
    if isinstance(crs, dict):
        properties = crs.get("properties")
        if isinstance(properties, dict):
            name = str(properties.get("name", ""))
    if name.lower() not in _ALLOWED_CRS_NAMES:
        raise ContextConfigError(
            f"context geometry must be in WGS 84 (EPSG:4326); got CRS {name!r}: {source}"
        )


@dataclass(frozen=True)
class ContextDocument:
    """A validated context layer: parallel geometry and property lists."""

    geometries: list[BaseGeometry]
    properties: list[dict[str, Any]]


@dataclass(frozen=True)
class HarvestResult:
    """What a `config/context/` sweep did."""

    layers: int = 0
    features: int = 0
    skipped: int = 0


def load_context_file(path: Path) -> ContextDocument:
    """Load and validate a context GeoJSON file.

    Raises ``ContextConfigError`` with a human-readable message for any problem.
    """
    if not path.is_file():
        raise ContextConfigError(f"context layer file not found: {path}")
    try:
        document: Any = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContextConfigError(f"context layer is not valid JSON: {path} ({exc})") from exc
    return load_context_document(document, source=path)


def load_context_document(document: Any, *, source: object = "<upload>") -> ContextDocument:
    """Validate an already-parsed context GeoJSON document (e.g. an upload)."""
    if not isinstance(document, dict):
        raise ContextConfigError(f"context layer must be a GeoJSON object: {source}")
    _reject_non_wgs84_crs(document, source)

    document_type = document.get("type")
    if document_type == "FeatureCollection":
        features = document.get("features")
        if not isinstance(features, list) or not features:
            raise ContextConfigError(
                f"context FeatureCollection must contain at least one feature: {source}"
            )
    elif document_type == "Feature":
        features = [document]
    else:
        raise ContextConfigError(
            f"context layer must be a GeoJSON Feature or FeatureCollection: {source}"
        )

    geometries: list[BaseGeometry] = []
    properties: list[dict[str, Any]] = []
    for index, feature in enumerate(features):
        if not isinstance(feature, dict):
            raise ContextConfigError(f"context feature {index} is not a GeoJSON object: {source}")
        raw_geometry = feature.get("geometry")
        if not isinstance(raw_geometry, dict):
            raise ContextConfigError(f"context feature {index} is missing a geometry: {source}")
        try:
            geometry = shape(raw_geometry)
        except (ShapelyError, ValueError, KeyError, TypeError) as exc:
            raise ContextConfigError(
                f"context feature {index} has invalid geometry: {source} ({exc})"
            ) from exc
        if geometry.is_empty:
            raise ContextConfigError(f"context feature {index} has an empty geometry: {source}")
        if not geometry.is_valid:
            raise ContextConfigError(
                f"context feature {index} has an invalid geometry "
                f"(e.g. self-intersecting): {source}"
            )
        raw_properties = feature.get("properties")
        geometries.append(geometry)
        properties.append(raw_properties if isinstance(raw_properties, dict) else {})
    return ContextDocument(geometries=geometries, properties=properties)


def replace_layer(
    session: Session,
    *,
    name: str,
    kind: str,
    document: ContextDocument,
    source_file: str | None = None,
) -> ContextLayer:
    """Create or replace the named layer with the document's features.

    Idempotent by design: the layer row is reused (kind/source updated) and its
    features are replaced wholesale, so re-loading the same file is safe and
    loading a revised file leaves no stale features behind. The caller owns the
    transaction.
    """
    if kind not in CONTEXT_KINDS:
        raise ContextConfigError(f"unknown context kind {kind!r}; expected one of {CONTEXT_KINDS}")
    layer = session.execute(
        select(ContextLayer).where(ContextLayer.name == name)
    ).scalar_one_or_none()
    if layer is None:
        layer = ContextLayer(name=name, kind=kind, source_file=source_file)
        session.add(layer)
        session.flush()
    else:
        layer.kind = kind
        layer.source_file = source_file
        session.execute(delete(ContextFeature).where(ContextFeature.context_layer_id == layer.id))
    for geometry, props in zip(document.geometries, document.properties, strict=True):
        session.add(
            ContextFeature(
                context_layer_id=layer.id,
                geometry=from_shape(geometry, srid=AOI_SRID),
                properties=props,
            )
        )
    session.flush()
    return layer


def parse_harvest_filename(path: Path) -> tuple[str, str] | None:
    """``<kind>--<name>.geojson`` -> (kind, name); None when unparseable."""
    stem = path.stem
    if _KIND_SEPARATOR not in stem:
        return None
    kind, _, name = stem.partition(_KIND_SEPARATOR)
    if kind not in CONTEXT_KINDS or not name:
        return None
    return kind, name


def harvest_context_dir(session: Session, directory: Path) -> HarvestResult:
    """Load every ``<kind>--<name>.geojson`` in ``directory`` (non-recursive).

    Files that don't follow the naming convention or fail validation are
    skipped with a warning rather than failing the pipeline run — a bad
    context file must never block detection.
    """
    if not directory.is_dir():
        return HarvestResult()
    layers = 0
    features = 0
    skipped = 0
    for path in sorted(directory.glob("*.geojson")):
        parsed = parse_harvest_filename(path)
        if parsed is None:
            logger.warning(
                "skipping context file %s: name must be <kind>--<name>.geojson with kind one of %s",
                path,
                ", ".join(CONTEXT_KINDS),
            )
            skipped += 1
            continue
        kind, name = parsed
        try:
            document = load_context_file(path)
        except ContextConfigError as exc:
            logger.warning("skipping context file %s: %s", path, exc)
            skipped += 1
            continue
        replace_layer(session, name=name, kind=kind, document=document, source_file=str(path))
        layers += 1
        features += len(document.geometries)
    return HarvestResult(layers=layers, features=features, skipped=skipped)
