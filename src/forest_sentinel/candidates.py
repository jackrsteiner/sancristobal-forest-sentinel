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

from forest_sentinel import earthengine, forestmask, indices
from forest_sentinel.models import (
    AOI_SRID,
    CandidateExtraction,
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
    methodology: MethodologyVersion,
    delta_image: Any,
    region: Any,
    scale: int = indices.DEFAULT_SCALE_METERS,
    threshold: float | None = None,
    min_area_m2: float | None = None,
    forest_mask: dict[str, Any] | None = None,
    ee_module: Any = earthengine,
) -> list[DisturbanceCandidate]:
    """Extract and persist candidate polygons from one change raster's ΔNBR image.

    Change rasters are keyed on the raster lineage; the candidate set is the
    *detection layer* — per ``(change_raster, methodology)``, so several
    methodologies sharing one raster each keep their own set (Finding 1). The
    methodology's forest mask (#82) is applied to the delta before
    thresholding, so only forested pixels can produce candidates; the exported
    rasters themselves stay unmasked. Re-runs replace this methodology's
    candidate set so the rows reflect the latest parameters — but only while
    none of it has been tracked into events. Once a candidate is referenced by
    an ``event_observation`` it is part of event history: the set is frozen and
    returned as-is (deleting it would violate the ``event_observation`` FK and
    silently invalidate event footprints).
    """
    # Frozen for THIS methodology: its candidates are already event history.
    if candidates_are_frozen(session, change_raster.id, methodology.id):
        return _existing_candidates(session, change_raster.id, methodology.id)

    resolved_threshold = resolve_threshold(methodology, threshold)
    resolved_min_area = resolve_min_area(methodology, min_area_m2)

    observation = session.get(Observation, change_raster.observation_id)
    if observation is None:
        raise ValueError(f"change_raster {change_raster.id} has no source observation")
    detected_at = observation.acquired_at

    # Detection-time forest masking (#82): non-forest pixels cannot cross the
    # threshold, so crop/grassland change never reaches the candidate table.
    mask = forestmask.build_mask(
        forestmask.resolve_config(methodology, forest_mask), ee_module=ee_module
    )
    if mask is not None:
        delta_image = ee_module.update_mask(delta_image, mask)

    features = ee_module.threshold_and_vectorize(
        delta_image,
        threshold=resolved_threshold,
        scale=scale,
        region=region,
        min_area_m2=resolved_min_area,
    )

    return _persist_features(
        session,
        change_raster=change_raster,
        methodology=methodology,
        features=features,
        detected_at=detected_at,
        scale=scale,
        min_area_m2=resolved_min_area,
    )


def persist_candidate_features(
    session: Session,
    *,
    change_raster: ChangeRaster,
    methodology: MethodologyVersion,
    features: list[dict[str, Any]],
    scale: int = indices.DEFAULT_SCALE_METERS,
    min_area_m2: float | None = None,
) -> list[DisturbanceCandidate]:
    """Persist pre-computed candidate features (the local-extraction path, Finding 2).

    Features must carry the same shape ``threshold_and_vectorize`` returns; the
    freeze/replace semantics are identical to the EE path.
    """
    if candidates_are_frozen(session, change_raster.id, methodology.id):
        return _existing_candidates(session, change_raster.id, methodology.id)
    observation = session.get(Observation, change_raster.observation_id)
    if observation is None:
        raise ValueError(f"change_raster {change_raster.id} has no source observation")
    return _persist_features(
        session,
        change_raster=change_raster,
        methodology=methodology,
        features=features,
        detected_at=observation.acquired_at,
        scale=scale,
        min_area_m2=resolve_min_area(methodology, min_area_m2),
    )


def _persist_features(
    session: Session,
    *,
    change_raster: ChangeRaster,
    methodology: MethodologyVersion,
    features: list[dict[str, Any]],
    detected_at: Any,
    scale: int,
    min_area_m2: float,
) -> list[DisturbanceCandidate]:
    _delete_existing(session, change_raster.id, methodology.id)

    candidates: list[DisturbanceCandidate] = []
    for feature in features:
        properties = feature.get("properties", {})
        area_m2 = float(properties.get("area_m2", 0.0))
        if area_m2 < min_area_m2:
            continue  # belt-and-braces: also guard client-side against sub-threshold polygons
        geometry = shape(feature["geometry"])
        candidate = DisturbanceCandidate(
            change_raster_id=change_raster.id,
            methodology_version_id=methodology.id,
            geometry=from_shape(geometry, srid=AOI_SRID),
            detected_at=detected_at,
            area_m2=area_m2,
            # ΔNBR statistics reduced per polygon at extraction time (#95);
            # null when the feature carries none (statistics are never backfilled).
            delta_mean=_optional_float(properties.get("delta_mean")),
            delta_min=_optional_float(properties.get("delta_min")),
            valid_pixel_fraction=_valid_pixel_fraction(
                properties.get("valid_pixels"), scale=scale, area_m2=area_m2
            ),
        )
        session.add(candidate)
        candidates.append(candidate)

    _mark_extracted(session, change_raster.id, methodology.id)
    session.flush()
    return candidates


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _valid_pixel_fraction(valid_pixels: Any, *, scale: int, area_m2: float) -> float | None:
    """Unmasked-delta coverage of the polygon: ``valid_pixels·scale² / area``.

    Capped at 1.0 — pixel counting at ``scale`` against a geodesic polygon area is
    approximate, and a fully valid polygon must read as exactly full coverage.
    """
    if valid_pixels is None or area_m2 <= 0:
        return None
    return min(1.0, float(valid_pixels) * scale * scale / area_m2)


def count_candidates_for_change_raster(
    session: Session, change_raster_id: int, methodology_version_id: int
) -> int:
    """How many candidates this methodology already has on a raster (#77)."""
    return session.execute(
        select(func.count())
        .select_from(DisturbanceCandidate)
        .where(DisturbanceCandidate.change_raster_id == change_raster_id)
        .where(DisturbanceCandidate.methodology_version_id == methodology_version_id)
    ).scalar_one()


def _existing_candidates(
    session: Session, change_raster_id: int, methodology_version_id: int
) -> list[DisturbanceCandidate]:
    return list(
        session.execute(
            select(DisturbanceCandidate)
            .where(DisturbanceCandidate.change_raster_id == change_raster_id)
            .where(DisturbanceCandidate.methodology_version_id == methodology_version_id)
            .order_by(DisturbanceCandidate.id)
        ).scalars()
    )


def change_raster_is_frozen(session: Session, change_raster_id: int) -> bool:
    """True once ANY methodology's candidates on the raster are event history.

    A frozen raster is event evidence: its COG/provenance must not be recomputed
    (``change.py``/``radar.py`` check this before re-export). Whether one
    methodology's candidate *set* may be replaced is the narrower
    ``candidates_are_frozen`` — other methodologies' sets never block it.
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


def candidates_are_frozen(
    session: Session, change_raster_id: int, methodology_version_id: int
) -> bool:
    """True once this methodology's candidates on the raster are event history."""
    return (
        session.execute(
            select(DisturbanceCandidate.id)
            .join(
                EventObservation,
                EventObservation.disturbance_candidate_id == DisturbanceCandidate.id,
            )
            .where(DisturbanceCandidate.change_raster_id == change_raster_id)
            .where(DisturbanceCandidate.methodology_version_id == methodology_version_id)
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def has_extraction(session: Session, change_raster_id: int, methodology_version_id: int) -> bool:
    """True when this methodology has already extracted from this raster.

    Candidate counts cannot answer this (an extraction can yield zero rows);
    the marker row can. Absent marker + reused raster = a new detection layer
    on a shared raster lineage — the pipeline rebuilds the delta and extracts.
    """
    return (
        session.execute(
            select(CandidateExtraction.id)
            .where(CandidateExtraction.change_raster_id == change_raster_id)
            .where(CandidateExtraction.methodology_version_id == methodology_version_id)
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _mark_extracted(session: Session, change_raster_id: int, methodology_version_id: int) -> None:
    if not has_extraction(session, change_raster_id, methodology_version_id):
        session.add(
            CandidateExtraction(
                change_raster_id=change_raster_id,
                methodology_version_id=methodology_version_id,
            )
        )


def _delete_existing(session: Session, change_raster_id: int, methodology_version_id: int) -> None:
    session.execute(
        delete(DisturbanceCandidate)
        .where(DisturbanceCandidate.change_raster_id == change_raster_id)
        .where(DisturbanceCandidate.methodology_version_id == methodology_version_id)
    )
    session.flush()
