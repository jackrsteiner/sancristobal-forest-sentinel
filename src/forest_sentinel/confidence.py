"""Transparent confidence scoring for disturbance events (E15).

The rule is a **fully explained weighted average** over normalized factors —
no learned weights, no hidden state. Rule ``optical-v1`` uses the optical-only
inputs that are all persisted at extraction time (docs/architecture.md §7):

- **magnitude** — the deepest candidate ΔNBR drop across the event
  (``delta_min``, #95): a −0.5 drop is unambiguous clearing, −0.1 is noise-adjacent.
- **persistence** — how many dated observations confirm the event.
- **coverage** — mean valid-pixel fraction of the event's candidates: how much
  of each detection was actually observable.
- **currency** — days since the last detection: fresh evidence beats stale.

Every factor value, subscore, weight, and the rule version are recorded in the
``confidence_assessment.inputs`` JSONB, so a level is fully explainable from the
row alone. Factors whose inputs predate #95 are recorded as ``null`` and the
weights renormalize over what is available — degraded, never fabricated.

Assessments are **append-only**, but only appended when the outcome moved:
a new row is written when the event has no assessment under the current rule
version or when the (level, rounded score) changed — so history captures every
change of conclusion without a row per daily run. Radar agreement (E16) and
context proximity (E17) join the same structure under a bumped rule version.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forest_sentinel.models import (
    Aoi,
    ConfidenceAssessment,
    DisturbanceCandidate,
    DisturbanceEvent,
    EventObservation,
)

RULE_VERSION = "optical-v1"

# Factor weights (renormalized over available factors when statistics are null).
WEIGHTS = {
    "magnitude": 0.35,
    "persistence": 0.30,
    "coverage": 0.20,
    "currency": 0.15,
}

# Normalization constants, recorded implicitly by the rule version.
MAGNITUDE_FLOOR = 0.1  # |ΔNBR| at or below this scores 0 (noise-adjacent)
MAGNITUDE_CEIL = 0.5  # |ΔNBR| at or above this scores 1 (unambiguous clearing)
PERSISTENCE_CEIL = 5  # observations at or above this score 1
CURRENCY_HORIZON_DAYS = 180  # this many days since last detection scores 0

# score >= HIGH_CUTOFF -> high; >= MEDIUM_CUTOFF -> medium; else low.
MEDIUM_CUTOFF = 0.4
HIGH_CUTOFF = 0.65

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
) -> Assessment:
    """Apply the rule to raw factor inputs; pure and fully deterministic."""
    subscores: dict[str, float | None] = {
        "magnitude": normalize_magnitude(delta_min),
        "persistence": normalize_persistence(observation_count),
        "coverage": normalize_coverage(mean_valid_fraction),
        "currency": normalize_currency(days_since_last),
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
    appended = 0
    for event in events:
        assessment = _assess_event(session, event, moment)
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


def _assess_event(session: Session, event: DisturbanceEvent, now: datetime) -> Assessment:
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
    return compute_assessment(
        delta_min=delta_min,
        # Rounded: these are recorded verbatim in the explainable inputs, where
        # float-average noise (-0.32999999999999996) would just be ugly.
        delta_mean=round(float(delta_mean), 4) if delta_mean is not None else None,
        mean_valid_fraction=round(float(mean_fraction), 4) if mean_fraction is not None else None,
        observation_count=observation_count,
        days_since_last=days_since_last,
    )


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
