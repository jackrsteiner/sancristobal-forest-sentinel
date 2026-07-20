import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forest_sentinel.methodology import (
    AUTO_VERSION_PREFIX,
    MethodologyVersionMismatch,
    auto_version,
    get_or_create_methodology_version,
    raster_parameters,
    resolve_methodology_version,
    resolve_raster_lineage,
)
from forest_sentinel.models import MethodologyVersion

_PARAMS = {
    "ee_script_version": "slice1-v1",
    "collections": ["NASA/HLS/HLSL30/v002", "NASA/HLS/HLSS30/v002"],
    "delta_nbr_threshold": -0.25,
}


def test_creates_when_absent(db_session: Session) -> None:
    created = get_or_create_methodology_version(
        db_session, name="optical-change", version="1.0.0", parameters=_PARAMS
    )
    db_session.commit()
    assert created.id is not None
    assert db_session.execute(select(MethodologyVersion)).scalars().all() == [created]


def test_returns_existing_for_identical_inputs(db_session: Session) -> None:
    first = get_or_create_methodology_version(
        db_session, name="optical-change", version="1.0.0", parameters=_PARAMS
    )
    second = get_or_create_methodology_version(
        db_session, name="optical-change", version="1.0.0", parameters=_PARAMS
    )
    assert first.id == second.id
    assert len(db_session.execute(select(MethodologyVersion)).scalars().all()) == 1


def test_identical_inputs_are_key_order_insensitive(db_session: Session) -> None:
    first = get_or_create_methodology_version(
        db_session, name="m", version="1", parameters={"a": 1, "b": 2}
    )
    second = get_or_create_methodology_version(
        db_session, name="m", version="1", parameters={"b": 2, "a": 1}
    )
    assert first.id == second.id


def test_distinct_versions_create_distinct_rows(db_session: Session) -> None:
    a = get_or_create_methodology_version(db_session, name="m", version="1", parameters={})
    b = get_or_create_methodology_version(db_session, name="m", version="2", parameters={})
    assert a.id != b.id


def test_parameter_mismatch_raises(db_session: Session) -> None:
    get_or_create_methodology_version(
        db_session, name="m", version="1", parameters={"threshold": -0.25}
    )
    with pytest.raises(MethodologyVersionMismatch, match="different"):
        get_or_create_methodology_version(
            db_session, name="m", version="1", parameters={"threshold": -0.30}
        )


def test_parameters_carry_ee_provenance(db_session: Session) -> None:
    row = get_or_create_methodology_version(
        db_session, name="optical-change", version="1.0.0", parameters=_PARAMS
    )
    db_session.commit()
    assert row.parameters["ee_script_version"] == "slice1-v1"
    assert "NASA/HLS/HLSL30/v002" in row.parameters["collections"]


def test_db_level_unique_constraint(db_session: Session) -> None:
    lineage = resolve_raster_lineage(db_session, name="m", parameters={})
    db_session.add(
        MethodologyVersion(name="m", version="1", parameters={}, raster_lineage_id=lineage.id)
    )
    db_session.flush()
    db_session.add(
        MethodologyVersion(name="m", version="1", parameters={}, raster_lineage_id=lineage.id)
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_resolve_reuses_any_version_with_equal_parameters(db_session: Session) -> None:
    """Content-addressing must match pre-existing hand-versioned rows (e.g. the
    1.0.0 an instance already has), so upgrading never triggers a recompute."""
    existing = get_or_create_methodology_version(
        db_session, name="optical-change", version="1.0.0", parameters=_PARAMS
    )
    resolved = resolve_methodology_version(
        db_session, name="optical-change", parameters=dict(_PARAMS)
    )
    assert resolved.id == existing.id
    assert resolved.version == "1.0.0"
    assert len(db_session.execute(select(MethodologyVersion)).scalars().all()) == 1


def test_resolve_mints_auto_version_for_new_parameters(db_session: Session) -> None:
    get_or_create_methodology_version(
        db_session, name="optical-change", version="1.0.0", parameters=_PARAMS
    )
    changed = {**_PARAMS, "delta_nbr_threshold": -0.15}
    minted = resolve_methodology_version(db_session, name="optical-change", parameters=changed)
    assert minted.version.startswith(AUTO_VERSION_PREFIX)
    assert minted.parameters == changed
    assert len(db_session.execute(select(MethodologyVersion)).scalars().all()) == 2

    # Resolving the same parameters again reuses the minted row (deterministic),
    # and flipping back to the original set re-matches 1.0.0.
    again = resolve_methodology_version(db_session, name="optical-change", parameters=changed)
    assert again.id == minted.id
    back = resolve_methodology_version(db_session, name="optical-change", parameters=_PARAMS)
    assert back.version == "1.0.0"


def test_auto_version_is_deterministic_and_key_order_insensitive() -> None:
    assert auto_version({"a": 1, "b": [2, 3]}) == auto_version({"b": [2, 3], "a": 1})
    assert auto_version({"a": 1}) != auto_version({"a": 2})


def test_resolve_with_explicit_version_stays_strict(db_session: Session) -> None:
    resolve_methodology_version(db_session, name="m", parameters={"threshold": -0.25}, version="1")
    with pytest.raises(MethodologyVersionMismatch, match="bump the version"):
        resolve_methodology_version(
            db_session, name="m", parameters={"threshold": -0.30}, version="1"
        )


def test_first_mint_gets_display_version_1_0_0(db_session: Session) -> None:
    created = get_or_create_methodology_version(
        db_session, name="optical-change", version="1.0.0", parameters=_PARAMS
    )
    assert created.display_version == "1.0.0"


def test_parameter_tweak_bumps_the_patch_version(db_session: Session) -> None:
    get_or_create_methodology_version(
        db_session,
        name="m",
        version="a",
        parameters={"ee_script_version": "s1", "threshold": -0.25},
    )
    tweaked = get_or_create_methodology_version(
        db_session,
        name="m",
        version="b",
        parameters={"ee_script_version": "s1", "threshold": -0.3},
    )
    assert tweaked.display_version == "1.0.1"


def test_ee_script_change_bumps_the_minor_version(db_session: Session) -> None:
    get_or_create_methodology_version(
        db_session, name="m", version="a", parameters={"ee_script_version": "s1"}
    )
    get_or_create_methodology_version(
        db_session,
        name="m",
        version="b",
        parameters={"ee_script_version": "s1", "threshold": -0.3},
    )
    new_script = get_or_create_methodology_version(
        db_session, name="m", version="c", parameters={"ee_script_version": "s2"}
    )
    assert new_script.display_version == "1.1.0"


def test_display_versions_are_independent_per_name(db_session: Session) -> None:
    get_or_create_methodology_version(db_session, name="m", version="a", parameters={"x": 1})
    other = get_or_create_methodology_version(
        db_session, name="radar-change", version="a", parameters={"x": 1}
    )
    assert other.display_version == "1.0.0"


def test_reuse_keeps_the_existing_display_version(db_session: Session) -> None:
    first = get_or_create_methodology_version(
        db_session, name="m", version="a", parameters={"x": 1}
    )
    again = resolve_methodology_version(db_session, name="m", parameters={"x": 1})
    assert again.id == first.id
    assert again.display_version == "1.0.0"


def test_raster_parameters_subset_and_legacy_fallback() -> None:
    """Only raster-shaping keys reach the lineage; pre-split rows' ee_script_version
    doubles as the raster pin so their lineages content-match post-split ones."""
    full = {
        "ee_script_version": "detect-v1",
        "raster_script_version": "raster-v1",
        "collections": ["A", "B"],
        "scale_m": 30,
        "masked_categories": ["cloud"],
        "baseline_window": 5,
        "delta_nbr_threshold": -0.25,
        "min_area_m2": 4500.0,
        "forest_mask": {"source": "hansen"},
    }
    subset = raster_parameters(full)
    assert subset == {
        "raster_script_version": "raster-v1",
        "collections": ["A", "B"],
        "scale_m": 30,
        "masked_categories": ["cloud"],
        "baseline_window": 5,
    }
    legacy = {"ee_script_version": "slice1-optical-change-v1", "scale_m": 30}
    assert raster_parameters(legacy) == {
        "raster_script_version": "slice1-optical-change-v1",
        "scale_m": 30,
    }


def test_detection_changes_share_a_raster_lineage(db_session: Session) -> None:
    base = {"raster_script_version": "r1", "scale_m": 30, "baseline_window": 5}
    strict = resolve_methodology_version(
        db_session, name="optical-change", parameters={**base, "delta_nbr_threshold": -0.25}
    )
    loose = resolve_methodology_version(
        db_session, name="optical-change", parameters={**base, "delta_nbr_threshold": -0.15}
    )
    assert strict.id != loose.id
    assert strict.raster_lineage_id == loose.raster_lineage_id


def test_raster_changes_mint_a_new_lineage(db_session: Session) -> None:
    base = {"raster_script_version": "r1", "scale_m": 30, "delta_nbr_threshold": -0.25}
    short = resolve_methodology_version(
        db_session, name="optical-change", parameters={**base, "baseline_window": 3}
    )
    long = resolve_methodology_version(
        db_session, name="optical-change", parameters={**base, "baseline_window": 7}
    )
    repinned = resolve_methodology_version(
        db_session,
        name="optical-change",
        parameters={**base, "baseline_window": 3, "raster_script_version": "r2"},
    )
    assert len({short.raster_lineage_id, long.raster_lineage_id, repinned.raster_lineage_id}) == 3
