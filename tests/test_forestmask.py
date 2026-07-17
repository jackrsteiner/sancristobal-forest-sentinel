"""Forest-mask configuration and mask building (#82): environment parsing, the
methodology-parameters round trip, and dispatch onto the EE seam. Pure — no
database, no Earth Engine."""

import pytest

from forest_sentinel import forestmask
from forest_sentinel.forestmask import (
    DEFAULT_CANOPY_THRESHOLD_PCT,
    DEFAULT_HANSEN_ASSET,
    DEFAULT_WORLDCOVER_ASSET,
    PARAMETER_KEY,
    WORLDCOVER_TREE_CLASS,
    ForestMaskConfigError,
    build_mask,
    config_from_env,
    parameters_entry,
    resolve_config,
)
from forest_sentinel.models import MethodologyVersion
from tests.fakes import FakeEarthEngine


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        forestmask.SOURCE_ENV_VAR,
        forestmask.ASSET_ENV_VAR,
        forestmask.CANOPY_PCT_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_config_is_hansen(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    assert config_from_env() == {
        "source": "hansen",
        "asset": DEFAULT_HANSEN_ASSET,
        "canopy_threshold_pct": DEFAULT_CANOPY_THRESHOLD_PCT,
    }


def test_config_knobs_override_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(forestmask.ASSET_ENV_VAR, "UMD/hansen/global_forest_change_2024_v1_12")
    monkeypatch.setenv(forestmask.CANOPY_PCT_ENV_VAR, "50")
    assert config_from_env() == {
        "source": "hansen",
        "asset": "UMD/hansen/global_forest_change_2024_v1_12",
        "canopy_threshold_pct": 50.0,
    }


def test_worldcover_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(forestmask.SOURCE_ENV_VAR, "worldcover")
    assert config_from_env() == {
        "source": "worldcover",
        "asset": DEFAULT_WORLDCOVER_ASSET,
        "tree_class": WORLDCOVER_TREE_CLASS,
    }


def test_mask_off_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(forestmask.SOURCE_ENV_VAR, "none")
    assert config_from_env() == {"source": "none"}


def test_unknown_source_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(forestmask.SOURCE_ENV_VAR, "hansne")
    with pytest.raises(ForestMaskConfigError, match="hansne"):
        config_from_env()


@pytest.mark.parametrize("bad_pct", ["thirty", "-1", "101"])
def test_bad_canopy_threshold_fails_loudly(monkeypatch: pytest.MonkeyPatch, bad_pct: str) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(forestmask.CANOPY_PCT_ENV_VAR, bad_pct)
    with pytest.raises(ForestMaskConfigError):
        config_from_env()


def test_parameters_entry_records_active_masks_and_omits_none() -> None:
    active = {"source": "hansen", "asset": "a", "canopy_threshold_pct": 30.0}
    assert parameters_entry(active) == {PARAMETER_KEY: active}
    # Mask-off records nothing: pre-#82 methodology rows have no forest_mask key,
    # so a mask-off run content-addresses to the same methodology and keeps
    # reusing its artifacts.
    assert parameters_entry({"source": "none"}) == {}


def test_resolve_config_precedence() -> None:
    stored = {"source": "worldcover", "asset": "a", "tree_class": 10}
    override = {"source": "none"}
    with_mask = MethodologyVersion(name="m", version="1", parameters={PARAMETER_KEY: stored})
    without = MethodologyVersion(name="m", version="1", parameters={})

    assert resolve_config(with_mask, None) == stored
    assert resolve_config(with_mask, override) == override
    # Pre-#82 methodologies (and stored nulls) resolve to "no mask".
    assert resolve_config(without, None) == {"source": "none"}
    null_mask = MethodologyVersion(name="m", version="1", parameters={PARAMETER_KEY: None})
    assert resolve_config(null_mask, None) == {"source": "none"}


def test_build_mask_dispatches_hansen() -> None:
    fake = FakeEarthEngine()
    config = {"source": "hansen", "asset": "hansen/asset", "canopy_threshold_pct": 30.0}
    mask = build_mask(config, ee_module=fake)
    assert mask == {"forest_mask": config}
    assert fake.forest_mask_calls == [config]


def test_build_mask_dispatches_worldcover() -> None:
    fake = FakeEarthEngine()
    config = {"source": "worldcover", "asset": "esa/asset", "tree_class": 10}
    mask = build_mask(config, ee_module=fake)
    assert mask == {"forest_mask": config}
    assert fake.forest_mask_calls == [config]


def test_build_mask_none_builds_nothing() -> None:
    fake = FakeEarthEngine()
    assert build_mask({"source": "none"}, ee_module=fake) is None
    assert fake.forest_mask_calls == []


def test_build_mask_rejects_unknown_stored_source() -> None:
    with pytest.raises(ForestMaskConfigError, match="mystery"):
        build_mask({"source": "mystery"}, ee_module=FakeEarthEngine())
