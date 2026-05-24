"""Disturbance candidate extraction — the visible output of Slice 1.

Threshold a ΔNBR change image (disturbance = an NBR drop beyond a configurable threshold,
``delta < threshold``), polygonize the mask with ``reduceToVectors``, filter by a minimum
area, and persist each surviving polygon as a ``disturbance_candidate`` with provenance to
the source change raster and the methodology version. Threshold and minimum area are
configurable, default-documented, and captured in the ``methodology_version`` (``docs/
architecture.md`` §4a / §5.7).
"""

from typing import Any

from geoalchemy2.shape import from_shape
from shapely.geometry import shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel import earthengine, indices
from forest_sentinel.models import (
    AOI_SRID,
    ChangeRaster,
    DisturbanceCandidate,
    MethodologyVersion,
    Observation,
)

# Documented defaults; overridable via methodology parameters or explicit kwargs.
DEFAULT_DELTA_NBR_THRESHOLD = -0.25  # an NBR drop of 0.25 or more is a candidate
DEFAULT_MIN_AREA_M2 = 4_500.0  # ≈ 0.45 ha; smaller patches are dropped as noise

_THRESHOLD_PARAM = "delta_nbr_threshold"
_MIN_AREA_PARAM = "min_area_m2"


def resolve_threshold(methodology: MethodologyVersion, override: float | None) -> float:
    """Threshold from the explicit override, else methodology parameters, else default."""
    if override is not None:
        return override
    return float(methodology.parameters.get(_THRESHOLD_PARAM, DEFAULT_DELTA_NBR_THRESHOLD))


def resolve_min_area(methodology: MethodologyVersion, override: float | None) -> float:
    """Minimum area from the explicit override, else methodology parameters, else default."""
    if override is not None:
        return override
    return float(methodology.parameters.get(_MIN_AREA_PARAM, DEFAULT_MIN_AREA_M2))


def extract_candidates_for_change_raster(
    session: Session,
    *,
    change_raster: ChangeRaster,
    delta_image: Any,
    region: Any,
    scale: int = indices.DEFAULT_SCALE_METERS,
    threshold: float | None = None,
    min_area_m2: float | None = None,
    ee_module: Any = earthengine,
) -> list[DisturbanceCandidate]:
    """Extract and persist candidate polygons from one change raster's ΔNBR image.

    Re-runs replace the candidate set for this change raster so the rows reflect the
    latest parameters.
    """
    methodology = session.get(MethodologyVersion, change_raster.methodology_version_id)
    if methodology is None:
        raise ValueError(f"change_raster {change_raster.id} has no methodology version")
    resolved_threshold = resolve_threshold(methodology, threshold)
    resolved_min_area = resolve_min_area(methodology, min_area_m2)

    observation = session.get(Observation, change_raster.observation_id)
    if observation is None:
        raise ValueError(f"change_raster {change_raster.id} has no source observation")
    detected_at = observation.acquired_at

    features = ee_module.threshold_and_vectorize(
        delta_image,
        threshold=resolved_threshold,
        scale=scale,
        region=region,
        min_area_m2=resolved_min_area,
    )

    _delete_existing(session, change_raster.id)

    candidates: list[DisturbanceCandidate] = []
    for feature in features:
        area_m2 = float(feature.get("properties", {}).get("area_m2", 0.0))
        if area_m2 < resolved_min_area:
            continue  # belt-and-braces: also guard client-side against sub-threshold polygons
        geometry = shape(feature["geometry"])
        candidate = DisturbanceCandidate(
            change_raster_id=change_raster.id,
            methodology_version_id=change_raster.methodology_version_id,
            geometry=from_shape(geometry, srid=AOI_SRID),
            detected_at=detected_at,
            area_m2=area_m2,
        )
        session.add(candidate)
        candidates.append(candidate)

    session.flush()
    return candidates


def _delete_existing(session: Session, change_raster_id: int) -> None:
    for candidate in session.execute(
        select(DisturbanceCandidate).where(
            DisturbanceCandidate.change_raster_id == change_raster_id
        )
    ).scalars():
        session.delete(candidate)
    session.flush()
