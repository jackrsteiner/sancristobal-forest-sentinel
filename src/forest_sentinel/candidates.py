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
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from forest_sentinel import earthengine, indices
from forest_sentinel.models import (
    AOI_SRID,
    ChangeRaster,
    DisturbanceCandidate,
    EventObservation,
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
    # A stored null must fall back to the default, not reach float().
    value = methodology.parameters.get(_THRESHOLD_PARAM)
    return float(value) if value is not None else DEFAULT_DELTA_NBR_THRESHOLD


def resolve_min_area(methodology: MethodologyVersion, override: float | None) -> float:
    """Minimum area from the explicit override, else methodology parameters, else default."""
    if override is not None:
        return override
    value = methodology.parameters.get(_MIN_AREA_PARAM)
    return float(value) if value is not None else DEFAULT_MIN_AREA_M2


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
    latest parameters — but only while none of it has been tracked into events. Once a
    candidate is referenced by an ``event_observation`` it is part of event history:
    the set is frozen and returned as-is (deleting it would violate the
    ``event_observation`` FK and silently invalidate event footprints).
    """
    # Frozen: this raster's candidates are already event history; skip re-extraction.
    if change_raster_is_frozen(session, change_raster.id):
        return _existing_candidates(session, change_raster.id)

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


def count_candidates_for_change_raster(session: Session, change_raster_id: int) -> int:
    """How many candidates a change raster already has (frozen/reused rasters, #77)."""
    return session.execute(
        select(func.count())
        .select_from(DisturbanceCandidate)
        .where(DisturbanceCandidate.change_raster_id == change_raster_id)
    ).scalar_one()


def _existing_candidates(session: Session, change_raster_id: int) -> list[DisturbanceCandidate]:
    return list(
        session.execute(
            select(DisturbanceCandidate)
            .where(DisturbanceCandidate.change_raster_id == change_raster_id)
            .order_by(DisturbanceCandidate.id)
        ).scalars()
    )


def change_raster_is_frozen(session: Session, change_raster_id: int) -> bool:
    """True once any of the raster's candidates is tracked into an event.

    A frozen raster is event evidence: its candidate set must not be replaced (here)
    and its COG/provenance must not be recomputed (``change.py`` checks this too).
    """
    return (
        session.execute(
            select(DisturbanceCandidate.id)
            .join(
                EventObservation,
                EventObservation.disturbance_candidate_id == DisturbanceCandidate.id,
            )
            .where(DisturbanceCandidate.change_raster_id == change_raster_id)
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _delete_existing(session: Session, change_raster_id: int) -> None:
    session.execute(
        delete(DisturbanceCandidate).where(
            DisturbanceCandidate.change_raster_id == change_raster_id
        )
    )
    session.flush()
