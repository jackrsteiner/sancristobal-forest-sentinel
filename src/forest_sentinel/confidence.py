"""Transparent confidence scoring for disturbance events (E15).

The rule is a **fully explained weighted average** over normalized factors —
no learned weights, no hidden state. Rule ``fused-v3`` (superseding
``fused-v2`` and ``optical-v1``, whose rows remain valid history) scores six
factors, whose inputs are persisted at extraction time or computed from
retained local rasters (docs/architecture.md §7):

- **magnitude** — the deepest candidate ΔNBR drop across the event
  (``delta_min``, #95): a −0.5 drop is unambiguous clearing, −0.1 is noise-adjacent.
- **persistence** — how many dated observations confirm the event.
- **coverage** — mean valid-pixel fraction of the event's candidates: how much
  of each detection was actually observable.
- **currency** — days since the last detection: fresh evidence beats stale.
- **agreement** (E16, #118) — cross-lineage confirmation. Event lineages stay
  methodology-scoped (fusion decision, PR #100); "radar-confirmed" is computed
  here instead: an event whose footprint intersects candidates of the *other*
  sensor kind (via the ``sensor_source`` registry) within ±30 days of its
  detection span scores 1 and is classified ``both``; other-kind coverage with
  no overlap scores 0 (``optical-only``/``radar-only``); no other-kind
  observations in the window at all leaves the factor missing — absence of
  looking is not disagreement.
- **stability** (#168) — the post-detection NBR trajectory inside the event
  footprint (``trajectory.py``, local COG reads, zero Earth Engine). A
  ``transient`` trajectory — the signal bounced back to the pre-event
  reference — is the strongest single disconfirming evidence the system
  measures (the unmasked-cloud-shadow profile) and scores 0; ``persistent``
  scores 1; ``recovering`` sits between. ``insufficient-data`` (cloudy
  aftermath, or index COGs pruned past retention) leaves the factor missing
  and the weights renormalize, like ``agreement`` without radar.

Every factor value, subscore, weight, and the rule version are recorded in the
``confidence_assessment.inputs`` JSONB, so a level is fully explainable from the
row alone. The rule version is content-addressed like ``methodology_version``:
``RULE_VERSION`` is the rule family name plus a hash of every weight, cutoff,
and normalization constant, so editing a tunable mints a new version instead of
silently relabeling old semantics (config-inventory Finding 6). Factors whose
inputs are unavailable (statistics predating #95, no radar coverage) are
recorded as ``null`` and the weights renormalize over what is available —
degraded, never fabricated.

Assessments are **append-only**, but only appended when the outcome moved:
a new row is written when the event has no assessment under the current rule
version or when the (level, rounded score) changed — so history captures every
change of conclusion without a row per daily run. Context proximity (E17)
joins the same structure under a bumped rule version.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forest_sentinel import trajectory
from forest_sentinel.methodology import parameter_hash
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    ConfidenceAssessment,
    DisturbanceCandidate,
    DisturbanceEvent,
    EventObservation,
    Observation,
    SensorSource,
)

RULE_NAME = "fused-v3"

# Factor weights (renormalized over available factors when inputs are null).
WEIGHTS = {
    "magnitude": 0.25,
    "persistence": 0.20,
    "coverage": 0.10,
    "currency": 0.10,
    "agreement": 0.15,
    "stability": 0.20,
}

# Normalization constants, recorded implicitly by the rule version.
MAGNITUDE_FLOOR = 0.1  # |ΔNBR| at or below this scores 0 (noise-adjacent)
MAGNITUDE_CEIL = 0.5  # |ΔNBR| at or above this scores 1 (unambiguous clearing)
PERSISTENCE_CEIL = 5  # observations at or above this score 1
CURRENCY_HORIZON_DAYS = 180  # this many days since last detection scores 0
AGREEMENT_WINDOW_DAYS = 30  # other-lineage evidence within ± this window counts
# Trajectory state -> stability subscore (#168): transient = disconfirming.
STABILITY_SUBSCORES = {"persistent": 1.0, "recovering": 0.5, "transient": 0.0}

# score >= HIGH_CUTOFF -> high; >= MEDIUM_CUTOFF -> medium; else low.
MEDIUM_CUTOFF = 0.4
HIGH_CUTOFF = 0.65

# The rule version is content-addressed over every tunable above, mirroring
# methodology_version: editing a weight or cutoff cannot silently reuse the old
# label. The human-readable RULE_NAME still identifies the rule family; the
# suffix pins the exact numbers an assessment was scored with.
_TUNABLES = {
    "weights": WEIGHTS,
    "magnitude_floor": MAGNITUDE_FLOOR,
    "magnitude_ceil": MAGNITUDE_CEIL,
    "persistence_ceil": PERSISTENCE_CEIL,
    "currency_horizon_days": CURRENCY_HORIZON_DAYS,
    "agreement_window_days": AGREEMENT_WINDOW_DAYS,
    "stability_subscores": STABILITY_SUBSCORES,
    "medium_cutoff": MEDIUM_CUTOFF,
    "high_cutoff": HIGH_CUTOFF,
}
RULE_VERSION = f"{RULE_NAME}+{parameter_hash(_TUNABLES, length=8)}"

# Appending threshold: a score move smaller than this (with an unchanged level)
# is not a changed conclusion worth a history row.
_SCORE_PRECISION = 2


@dataclass(frozen=True)
class Assessment:
    """A computed (not yet persisted) confidence outcome."""

    level: str
    score: float
    inputs: dict[str, Any]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def normalize_magnitude(delta_min: float | None) -> float | None:
    """Deepest ΔNBR drop → 0..1 (deeper drop = stronger evidence)."""
    if delta_min is None:
        return None
    return _clamp((abs(delta_min) - MAGNITUDE_FLOOR) / (MAGNITUDE_CEIL - MAGNITUDE_FLOOR))


def normalize_persistence(observation_count: int) -> float:
    """Confirming observations → 0..1 (a single look scores 0)."""
    return _clamp((observation_count - 1) / (PERSISTENCE_CEIL - 1))


def normalize_coverage(mean_fraction: float | None) -> float | None:
    """Mean valid-pixel fraction is already 0..1."""
    return None if mean_fraction is None else _clamp(mean_fraction)


def normalize_currency(days_since_last: float) -> float:
    """Days since the last detection → 0..1 (today = 1, horizon+ = 0)."""
    return _clamp(1.0 - days_since_last / CURRENCY_HORIZON_DAYS)


def score_to_level(score: float) -> str:
    if score >= HIGH_CUTOFF:
        return "high"
    if score >= MEDIUM_CUTOFF:
        return "medium"
    return "low"


def compute_assessment(
    *,
    delta_min: float | None,
    delta_mean: float | None,
    mean_valid_fraction: float | None,
    observation_count: int,
    days_since_last: float,
    agreement: float | None,
    agreement_details: dict[str, Any],
    stability: float | None,
    stability_details: dict[str, Any],
) -> Assessment:
    """Apply the rule to raw factor inputs; pure and fully deterministic.

    ``agreement`` and ``stability`` are already 0..1 (or ``None`` when their
    evidence is unavailable); their details dicts are recorded verbatim as the
    factors' explainable inputs.
    """
    subscores: dict[str, float | None] = {
        "magnitude": normalize_magnitude(delta_min),
        "persistence": normalize_persistence(observation_count),
        "coverage": normalize_coverage(mean_valid_fraction),
        "currency": normalize_currency(days_since_last),
        "agreement": None if agreement is None else _clamp(agreement),
        "stability": None if stability is None else _clamp(stability),
    }
    available = {name: value for name, value in subscores.items() if value is not None}
    total_weight = sum(WEIGHTS[name] for name in available)
    score = (
        sum(WEIGHTS[name] * value for name, value in available.items()) / total_weight
        if total_weight
        else 0.0
    )
    score = round(score, 4)
    return Assessment(
        level=score_to_level(score),
        score=score,
        inputs={
            "rule_version": RULE_VERSION,
            "weights": WEIGHTS,
            "factors": {
                "magnitude": {"delta_min": delta_min, "delta_mean": delta_mean},
                "persistence": {"observation_count": observation_count},
                "coverage": {"mean_valid_fraction": mean_valid_fraction},
                "currency": {"days_since_last": round(days_since_last, 2)},
                "agreement": agreement_details,
                "stability": stability_details,
            },
            "subscores": subscores,
            "missing": sorted(set(subscores) - set(available)),
        },
    )


def assess_events_for_aoi(
    session: Session,
    *,
    aoi: Aoi,
    pipeline_run_id: int | None = None,
    now: datetime | None = None,
) -> int:
    """Assess every event of the AOI; append rows where the conclusion moved.

    All events are evaluated (currency decays even without new detections), but
    a row is appended only when the event has no assessment under the current
    rule version or the (level, rounded score) changed — append-only history
    without a row per daily run. Returns how many rows were appended.
    """
    moment = now or datetime.now(UTC)
    events = (
        session.execute(select(DisturbanceEvent).where(DisturbanceEvent.aoi_id == aoi.id))
        .scalars()
        .all()
    )
    # Batch the trajectories up front (#170): one pass over the AOI's COGs for
    # ALL events, instead of an events x rasters open storm per event.
    trajectories = trajectory.trajectories_for_events(session, events=events)
    appended = 0
    for event in events:
        assessment = _assess_event(session, event, moment, trajectories[event.id])
        if not _conclusion_moved(session, event.id, assessment):
            continue
        session.add(
            ConfidenceAssessment(
                event_id=event.id,
                pipeline_run_id=pipeline_run_id,
                level=assessment.level,
                score=assessment.score,
                inputs=assessment.inputs,
                rule_version=RULE_VERSION,
            )
        )
        appended += 1
    session.flush()
    return appended


def _assess_event(
    session: Session,
    event: DisturbanceEvent,
    now: datetime,
    event_trajectory: trajectory.Trajectory,
) -> Assessment:
    delta_min, delta_mean, mean_fraction, observation_count = session.execute(
        select(
            func.min(DisturbanceCandidate.delta_min),
            func.avg(DisturbanceCandidate.delta_mean),
            func.avg(DisturbanceCandidate.valid_pixel_fraction),
            func.count(EventObservation.id),
        )
        .join(
            DisturbanceCandidate,
            DisturbanceCandidate.id == EventObservation.disturbance_candidate_id,
        )
        .where(EventObservation.event_id == event.id)
    ).one()
    days_since_last = max(0.0, (now - event.last_detected_at).total_seconds() / 86_400)
    agreement, agreement_details = _assess_agreement(session, event)
    stability, stability_details = _stability_from_trajectory(event_trajectory)
    return compute_assessment(
        delta_min=delta_min,
        # Rounded: these are recorded verbatim in the explainable inputs, where
        # float-average noise (-0.32999999999999996) would just be ugly.
        delta_mean=round(float(delta_mean), 4) if delta_mean is not None else None,
        mean_valid_fraction=round(float(mean_fraction), 4) if mean_fraction is not None else None,
        observation_count=observation_count,
        days_since_last=days_since_last,
        agreement=agreement,
        agreement_details=agreement_details,
        stability=stability,
        stability_details=stability_details,
    )


def _stability_from_trajectory(
    result: trajectory.Trajectory,
) -> tuple[float | None, dict[str, Any]]:
    """Stability factor from a precomputed trajectory: (subscore, details).

    Local COG reads only (batched in ``trajectories_for_events``, #170) —
    never Earth Engine. ``insufficient-data`` (cloudy aftermath, pruned COGs)
    leaves the factor missing so the weights renormalize; a bounced-back
    ``transient`` trajectory scores 0 — the strongest disconfirming evidence
    available.
    """
    latest = result.points[-1].mean_nbr if result.points else None
    details: dict[str, Any] = {
        "state": result.state,
        "reference_nbr": _rounded(result.reference_nbr),
        "detection_nbr": _rounded(result.detection_nbr),
        "latest_mean_nbr": _rounded(latest),
        "usable_dates": len(result.points),
    }
    return STABILITY_SUBSCORES.get(result.state), details


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _assess_agreement(
    session: Session, event: DisturbanceEvent
) -> tuple[float | None, dict[str, Any]]:
    """Cross-lineage agreement for one event: (subscore, explainable details).

    The event's own sensor kind comes from its candidates' observations through
    the ``sensor_source`` registry — data-driven, so radar lineages need no
    special-casing here. Scoring: other-kind candidates intersecting the event
    footprint within ±``AGREEMENT_WINDOW_DAYS`` of the detection span → 1.0
    (basis ``both``); other-kind *observations* in the window but no overlapping
    candidate → 0.0 (the other sensor looked and saw nothing); no other-kind
    observations at all → ``None`` (absence of looking is not disagreement, the
    factor is missing and the weights renormalize).
    """
    details: dict[str, Any] = {"window_days": AGREEMENT_WINDOW_DAYS}
    own_kinds = set(
        session.execute(
            select(SensorSource.kind)
            .distinct()
            .select_from(EventObservation)
            .join(
                DisturbanceCandidate,
                DisturbanceCandidate.id == EventObservation.disturbance_candidate_id,
            )
            .join(ChangeRaster, ChangeRaster.id == DisturbanceCandidate.change_raster_id)
            .join(Observation, Observation.id == ChangeRaster.observation_id)
            .join(SensorSource, SensorSource.name == Observation.sensor)
            .where(EventObservation.event_id == event.id)
        )
        .scalars()
        .all()
    )
    if len(own_kinds) != 1:
        # Sensor absent from the registry (or, impossibly, a mixed lineage):
        # unattributable, so the factor degrades like any other missing input.
        details.update({"own_kind": None, "basis": None})
        return None, details
    own_kind = own_kinds.pop()
    other_kind = "radar" if own_kind == "optical" else "optical"
    window = timedelta(days=AGREEMENT_WINDOW_DAYS)
    start = event.first_detected_at - window
    end = event.last_detected_at + window
    other_observations = session.execute(
        select(func.count(Observation.id))
        .join(SensorSource, SensorSource.name == Observation.sensor)
        .where(Observation.aoi_id == event.aoi_id)
        .where(SensorSource.kind == other_kind)
        .where(Observation.acquired_at >= start)
        .where(Observation.acquired_at <= end)
    ).scalar_one()
    matching_candidate_ids = list(
        session.execute(
            select(DisturbanceCandidate.id)
            .join(ChangeRaster, ChangeRaster.id == DisturbanceCandidate.change_raster_id)
            .join(Observation, Observation.id == ChangeRaster.observation_id)
            .join(SensorSource, SensorSource.name == Observation.sensor)
            .where(Observation.aoi_id == event.aoi_id)
            .where(SensorSource.kind == other_kind)
            .where(DisturbanceCandidate.detected_at >= start)
            .where(DisturbanceCandidate.detected_at <= end)
            .where(func.ST_Intersects(DisturbanceCandidate.geometry, event.geometry))
            .order_by(DisturbanceCandidate.id)
        )
        .scalars()
        .all()
    )
    if matching_candidate_ids:
        agreement: float | None = 1.0
        basis = "both"
    else:
        agreement = 0.0 if other_observations else None
        basis = f"{own_kind}-only"
    details.update(
        {
            "own_kind": own_kind,
            "other_kind": other_kind,
            "other_kind_observations": other_observations,
            "matching_candidate_ids": matching_candidate_ids,
            "basis": basis,
        }
    )
    return agreement, details


def _conclusion_moved(session: Session, event_id: int, assessment: Assessment) -> bool:
    latest = session.execute(
        select(ConfidenceAssessment)
        .where(ConfidenceAssessment.event_id == event_id)
        .where(ConfidenceAssessment.rule_version == RULE_VERSION)
        .order_by(ConfidenceAssessment.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        return True
    return latest.level != assessment.level or round(latest.score, _SCORE_PRECISION) != round(
        assessment.score, _SCORE_PRECISION
    )
