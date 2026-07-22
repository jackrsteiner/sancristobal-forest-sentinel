"""The transparent confidence rule (E15 #106, fused agreement E16 #118)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel import confidence
from forest_sentinel.confidence import (
    HIGH_CUTOFF,
    MEDIUM_CUTOFF,
    RULE_VERSION,
    assess_events_for_aoi,
    compute_assessment,
    normalize_currency,
    normalize_magnitude,
    normalize_persistence,
    score_to_level,
)
from forest_sentinel.events import track_events_for_aoi
from forest_sentinel.methodology import parameter_hash
from forest_sentinel.models import ConfidenceAssessment, DisturbanceEvent
from tests.fakes import (
    make_aoi,
    make_candidate,
    make_methodology,
    make_observation,
    make_radar_methodology,
)

_PATCH = [(0.1, 0.1), (0.2, 0.1), (0.2, 0.2), (0.1, 0.2), (0.1, 0.1)]
_PATCH_GROWN = [(0.15, 0.1), (0.3, 0.1), (0.3, 0.2), (0.15, 0.2), (0.15, 0.1)]


@pytest.mark.parametrize(
    ("delta_min", "expected"),
    [(None, None), (-0.05, 0.0), (-0.1, 0.0), (-0.3, 0.5), (-0.5, 1.0), (-0.9, 1.0)],
)
def test_magnitude_normalization(delta_min: float | None, expected: float | None) -> None:
    result = normalize_magnitude(delta_min)
    assert result == (pytest.approx(expected) if expected is not None else None)


@pytest.mark.parametrize(("count", "expected"), [(1, 0.0), (3, 0.5), (5, 1.0), (9, 1.0)])
def test_persistence_normalization(count: int, expected: float) -> None:
    assert normalize_persistence(count) == pytest.approx(expected)


@pytest.mark.parametrize(("days", "expected"), [(0, 1.0), (90, 0.5), (180, 0.0), (400, 0.0)])
def test_currency_normalization(days: float, expected: float) -> None:
    assert normalize_currency(days) == pytest.approx(expected)


def test_level_cutoffs_are_pinned() -> None:
    assert score_to_level(HIGH_CUTOFF) == "high"
    assert score_to_level(HIGH_CUTOFF - 0.01) == "medium"
    assert score_to_level(MEDIUM_CUTOFF) == "medium"
    assert score_to_level(MEDIUM_CUTOFF - 0.01) == "low"


def test_rule_version_is_content_addressed() -> None:
    """Editing any tunable mints a new rule version (config-inventory Finding 6)."""
    assert RULE_VERSION.startswith(f"{confidence.RULE_NAME}+")
    tweaked = dict(confidence._TUNABLES, high_cutoff=confidence.HIGH_CUTOFF + 0.01)
    relabeled = f"{confidence.RULE_NAME}+{parameter_hash(tweaked, length=8)}"
    assert relabeled != RULE_VERSION
    # The label pins the exact numbers: recomputing over the live constants
    # reproduces the shipped version string.
    assert (
        f"{confidence.RULE_NAME}+{parameter_hash(confidence._TUNABLES, length=8)}" == RULE_VERSION
    )


def test_compute_assessment_records_every_input() -> None:
    assessment = compute_assessment(
        delta_min=-0.5,
        delta_mean=-0.3,
        mean_valid_fraction=0.8,
        observation_count=5,
        days_since_last=0,
        agreement=1.0,
        agreement_details={"basis": "both", "matching_candidate_ids": [7]},
        stability=1.0,
        stability_details={"state": "persistent"},
    )
    # All factors maxed except coverage (0.8):
    # 0.25 + 0.20 + 0.10*0.8 + 0.10 + 0.15 + 0.20 = 0.98.
    assert assessment.score == pytest.approx(0.98)
    assert assessment.level == "high"
    inputs = assessment.inputs
    assert inputs["rule_version"] == RULE_VERSION
    assert inputs["factors"]["magnitude"]["delta_min"] == -0.5
    assert inputs["factors"]["persistence"]["observation_count"] == 5
    assert inputs["factors"]["agreement"] == {"basis": "both", "matching_candidate_ids": [7]}
    assert inputs["factors"]["stability"] == {"state": "persistent"}
    assert inputs["subscores"]["coverage"] == pytest.approx(0.8)
    assert inputs["missing"] == []


def test_missing_statistics_degrade_with_renormalized_weights() -> None:
    # Pre-#95 candidates with no radar coverage and no retained COGs: no
    # magnitude, coverage, agreement, or stability. Weights renormalize over
    # persistence (0.20) + currency (0.10).
    assessment = compute_assessment(
        delta_min=None,
        delta_mean=None,
        mean_valid_fraction=None,
        observation_count=5,
        days_since_last=0,
        agreement=None,
        agreement_details={"basis": "optical-only"},
        stability=None,
        stability_details={"state": "insufficient-data"},
    )
    assert assessment.score == pytest.approx(1.0)
    assert sorted(assessment.inputs["missing"]) == [
        "agreement",
        "coverage",
        "magnitude",
        "stability",
    ]
    assert assessment.inputs["subscores"]["magnitude"] is None
    assert assessment.inputs["subscores"]["agreement"] is None


def test_transient_stability_pulls_a_high_score_down() -> None:
    """#168: the #254 profile — deep drop, clear look, same-day pair — scored
    high under fused-v2; a transient (bounced-back) trajectory must sink it."""
    common: dict[str, Any] = {
        "delta_min": -0.47,
        "delta_mean": -0.38,
        "mean_valid_fraction": 1.0,
        "observation_count": 2,
        "days_since_last": 45.8,
        "agreement": None,
        "agreement_details": {"basis": "optical-only"},
    }
    without = compute_assessment(
        **common, stability=None, stability_details={"state": "insufficient-data"}
    )
    transient = compute_assessment(
        **common, stability=0.0, stability_details={"state": "transient"}
    )
    persistent = compute_assessment(
        **common, stability=1.0, stability_details={"state": "persistent"}
    )
    assert without.level == "high"  # the fused-v2-equivalent verdict
    assert transient.score < without.score
    assert transient.level != "high"
    assert persistent.score >= without.score


def test_assess_events_appends_explained_rows(db_session: Session) -> None:
    aoi = make_aoi(db_session)
    methodology = make_methodology(db_session)
    make_candidate(
        db_session,
        aoi,
        methodology,
        day=1,
        ring=_PATCH,
        area_m2=10_000.0,
        delta_min=-0.5,
        delta_mean=-0.3,
        valid_pixel_fraction=0.9,
    )
    make_candidate(
        db_session,
        aoi,
        methodology,
        day=8,
        ring=_PATCH_GROWN,
        area_m2=15_000.0,
        delta_min=-0.4,
        delta_mean=-0.25,
        valid_pixel_fraction=0.7,
    )
    track_events_for_aoi(db_session, aoi=aoi)

    appended = assess_events_for_aoi(db_session, aoi=aoi, now=datetime(2026, 1, 10, tzinfo=UTC))

    assert appended == 1
    row = db_session.execute(select(ConfidenceAssessment)).scalar_one()
    assert row.rule_version == RULE_VERSION
    # Deepest drop across the event's candidates, averaged coverage.
    assert row.inputs["factors"]["magnitude"]["delta_min"] == -0.5
    assert row.inputs["factors"]["coverage"]["mean_valid_fraction"] == pytest.approx(0.8)
    assert row.inputs["factors"]["persistence"]["observation_count"] == 2
    # No radar lineage exists at all: the other kind never looked, so the
    # agreement factor is missing (not zero) and the basis stays optical-only.
    assert row.inputs["factors"]["agreement"]["basis"] == "optical-only"
    assert row.inputs["subscores"]["agreement"] is None
    assert "agreement" in row.inputs["missing"]
    assert row.level in ("low", "medium", "high")
    assert 0.0 <= row.score <= 1.0


def test_unchanged_conclusions_are_not_reappended(db_session: Session) -> None:
    aoi = make_aoi(db_session)
    methodology = make_methodology(db_session)
    make_candidate(
        db_session, aoi, methodology, day=1, ring=_PATCH, area_m2=10_000.0, delta_min=-0.5
    )
    track_events_for_aoi(db_session, aoi=aoi)
    now = datetime(2026, 1, 10, tzinfo=UTC)

    assert assess_events_for_aoi(db_session, aoi=aoi, now=now) == 1
    # Same moment, same evidence: the conclusion did not move — no new row.
    assert assess_events_for_aoi(db_session, aoi=aoi, now=now) == 0

    # Months later the currency factor has decayed: the score moved, history grows.
    assert assess_events_for_aoi(db_session, aoi=aoi, now=datetime(2026, 6, 1, tzinfo=UTC)) == 1
    rows = db_session.execute(select(ConfidenceAssessment)).scalars().all()
    assert len(rows) == 2


_FAR_PATCH = [(0.6, 0.6), (0.7, 0.6), (0.7, 0.7), (0.6, 0.7), (0.6, 0.6)]


def _latest_assessment(session: Session, event_id: int) -> ConfidenceAssessment:
    return session.execute(
        select(ConfidenceAssessment)
        .where(ConfidenceAssessment.event_id == event_id)
        .order_by(ConfidenceAssessment.id.desc())
        .limit(1)
    ).scalar_one()


def test_overlapping_lineages_classify_both_on_both_events(db_session: Session) -> None:
    aoi = make_aoi(db_session)
    optical = make_methodology(db_session)
    radar = make_radar_methodology(db_session)
    make_candidate(db_session, aoi, optical, day=1, ring=_PATCH, area_m2=10_000.0, delta_min=-0.5)
    make_candidate(db_session, aoi, radar, day=3, ring=_PATCH, area_m2=10_000.0, sensor="S1GRD")
    track_events_for_aoi(db_session, aoi=aoi)
    events = db_session.execute(select(DisturbanceEvent)).scalars().all()
    # Same footprint, but lineages are methodology-scoped: two events.
    assert len(events) == 2

    assess_events_for_aoi(db_session, aoi=aoi, now=datetime(2026, 1, 10, tzinfo=UTC))

    for event in events:
        row = _latest_assessment(db_session, event.id)
        agreement = row.inputs["factors"]["agreement"]
        assert agreement["basis"] == "both"
        assert agreement["matching_candidate_ids"]
        assert row.inputs["subscores"]["agreement"] == 1.0
        assert "agreement" not in row.inputs["missing"]


def test_disjoint_lineages_classify_single_source(db_session: Session) -> None:
    aoi = make_aoi(db_session)
    optical = make_methodology(db_session)
    radar = make_radar_methodology(db_session)
    make_candidate(db_session, aoi, optical, day=1, ring=_PATCH, area_m2=10_000.0, delta_min=-0.5)
    make_candidate(db_session, aoi, radar, day=3, ring=_FAR_PATCH, area_m2=10_000.0, sensor="S1GRD")
    track_events_for_aoi(db_session, aoi=aoi)
    assess_events_for_aoi(db_session, aoi=aoi, now=datetime(2026, 1, 10, tzinfo=UTC))

    events = db_session.execute(select(DisturbanceEvent)).scalars().all()
    by_methodology = {event.methodology_version_id: event for event in events}
    optical_row = _latest_assessment(db_session, by_methodology[optical.id].id)
    radar_row = _latest_assessment(db_session, by_methodology[radar.id].id)
    # The other lineage looked (and even detected, elsewhere) without overlap:
    # genuine disagreement, scored 0 — never recorded as missing.
    assert optical_row.inputs["factors"]["agreement"]["basis"] == "optical-only"
    assert radar_row.inputs["factors"]["agreement"]["basis"] == "radar-only"
    for row in (optical_row, radar_row):
        assert row.inputs["subscores"]["agreement"] == 0.0
        assert row.inputs["factors"]["agreement"]["matching_candidate_ids"] == []
        assert "agreement" not in row.inputs["missing"]


def test_other_kind_coverage_without_detection_scores_zero(db_session: Session) -> None:
    aoi = make_aoi(db_session)
    optical = make_methodology(db_session)
    make_candidate(db_session, aoi, optical, day=1, ring=_PATCH, area_m2=10_000.0, delta_min=-0.5)
    # A radar acquisition inside the window with no detection at all: the other
    # sensor looked and saw nothing — disagreement (0.0), not a missing factor.
    make_observation(db_session, aoi, day=5, sensor="S1GRD", source_scene_id="S1GRD-scene-5")
    track_events_for_aoi(db_session, aoi=aoi)
    assess_events_for_aoi(db_session, aoi=aoi, now=datetime(2026, 1, 10, tzinfo=UTC))

    event = db_session.execute(select(DisturbanceEvent)).scalar_one()
    row = _latest_assessment(db_session, event.id)
    agreement = row.inputs["factors"]["agreement"]
    assert agreement["basis"] == "optical-only"
    assert agreement["other_kind_observations"] == 1
    assert row.inputs["subscores"]["agreement"] == 0.0
    assert "agreement" not in row.inputs["missing"]


def test_assess_records_stability_from_retained_cogs(db_session: Session, tmp_path: Path) -> None:
    """#168 end-to-end: the stability factor flows from real NBR COGs on disk
    through trajectory into the recorded, explainable assessment inputs."""
    from forest_sentinel.models import Aoi
    from tests.test_trajectory import _setup

    event = _setup(db_session, tmp_path, post={10: 0.55, 15: 0.58})  # bounce-back
    aoi = db_session.get(Aoi, event.aoi_id)
    assert aoi is not None

    appended = confidence.assess_events_for_aoi(db_session, aoi=aoi)
    assert appended == 1
    from sqlalchemy import select as _select

    from forest_sentinel.models import ConfidenceAssessment

    row = db_session.execute(_select(ConfidenceAssessment)).scalar_one()
    stability = row.inputs["factors"]["stability"]
    assert stability["state"] == "transient"
    assert stability["usable_dates"] == 3
    assert row.inputs["subscores"]["stability"] == 0.0
