"""The settings catalogue (Slice 7 bead 7.1, #134)."""

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from forest_sentinel import settings
from forest_sentinel.settings import CATEGORIES, OVERRIDES_PATH_ENV_VAR, catalogue
from tests.fakes import make_methodology


@pytest.fixture(autouse=True)
def _isolated_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERRIDES_PATH_ENV_VAR, str(tmp_path / "overrides.env"))


def _by_key(payload: dict) -> dict:  # type: ignore[type-arg]
    return {entry["key"]: entry for entry in payload["settings"]}


def test_catalogue_covers_the_four_categories(db_session: Session) -> None:
    payload = catalogue(db_session)
    assert payload["categories"] == list(CATEGORIES)
    present = {entry["category"] for entry in payload["settings"]}
    assert present == set(CATEGORIES)


def test_resolution_layers_override_env_default(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RESOLVED_AFTER_DAYS", raising=False)
    entry = _by_key(catalogue(db_session))["RESOLVED_AFTER_DAYS"]
    assert entry["resolved"] == "90"
    assert entry["source"] == "default"

    monkeypatch.setenv("RESOLVED_AFTER_DAYS", "120")
    entry = _by_key(catalogue(db_session))["RESOLVED_AFTER_DAYS"]
    assert (entry["resolved"], entry["source"]) == ("120", "environment")

    (tmp_path / "overrides.env").write_text("RESOLVED_AFTER_DAYS=45\n")
    entry = _by_key(catalogue(db_session))["RESOLVED_AFTER_DAYS"]
    assert (entry["resolved"], entry["source"]) == ("45", "override")


def test_database_url_is_redacted(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    # Scoped patch: the fixture teardown (alembic) must see the real URL again.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(
            "FOREST_SENTINEL_DATABASE_URL",
            "postgresql+psycopg://user:secret@dbhost:5432/forest_sentinel",
        )
        entry = _by_key(catalogue(db_session))["FOREST_SENTINEL_DATABASE_URL"]
    assert "secret" not in (entry["resolved"] or "")
    assert "user" not in (entry["resolved"] or "")
    assert "dbhost:5432/forest_sentinel" in (entry["resolved"] or "")


def test_footguns_and_identity_are_display_only(db_session: Session) -> None:
    entries = _by_key(catalogue(db_session))
    for key in (
        "FOREST_SENTINEL_DATABASE_URL",
        "FOREST_SENTINEL_COG_ROOT",
        "FOREST_SENTINEL_GEE_PROJECT",
        "FOREST_SENTINEL_SETTINGS_EDIT",
    ):
        assert entries[key]["editability"] == "display-only"


def test_methodology_entries_carry_the_recorded_parameter(db_session: Session) -> None:
    make_methodology(db_session, version="auto-x", parameters={"delta_nbr_threshold": -0.31})
    entry = _by_key(catalogue(db_session))["THRESHOLD"]
    assert entry["editability"] == "guarded"
    assert entry["recorded"] == -0.31


def test_constants_resolve_from_code(db_session: Session) -> None:
    entries = _by_key(catalogue(db_session))
    assert entries["SCALE_M"]["resolved"] == "30"
    assert entries["SCALE_M"]["source"] == "code"
    assert "cloud" in (entries["MASKED_CATEGORIES"]["resolved"] or "")


def test_overrides_file_parsing_skips_comments_and_blanks(tmp_path: Path) -> None:
    path = tmp_path / "overrides.env"
    path.write_text("# comment\n\nWINDOW_DAYS=45\nbroken line\nTHRESHOLD=-0.3\n")
    assert settings.read_overrides() == {"WINDOW_DAYS": "45", "THRESHOLD": "-0.3"}


def test_export_timeout_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bead 7.4 (#138): unset/garbage/non-positive fall back to the default."""
    from forest_sentinel.storage import EXPORT_TIMEOUT_ENV_VAR, _export_timeout_from_env

    monkeypatch.delenv(EXPORT_TIMEOUT_ENV_VAR, raising=False)
    assert _export_timeout_from_env() == 3600.0
    monkeypatch.setenv(EXPORT_TIMEOUT_ENV_VAR, "7200")
    assert _export_timeout_from_env() == 7200.0
    monkeypatch.setenv(EXPORT_TIMEOUT_ENV_VAR, "not-a-number")
    assert _export_timeout_from_env() == 3600.0
    monkeypatch.setenv(EXPORT_TIMEOUT_ENV_VAR, "-5")
    assert _export_timeout_from_env() == 3600.0


def test_export_timeout_is_an_editable_pipeline_knob(db_session: Session) -> None:
    entry = _by_key(catalogue(db_session))["FOREST_SENTINEL_EXPORT_TIMEOUT_SECONDS"]
    assert entry["category"] == "pipeline-tuning"
    assert entry["editability"] == "editable"
    changed = settings.apply_change(
        db_session, key="FOREST_SENTINEL_EXPORT_TIMEOUT_SECONDS", value="7200"
    )
    assert changed["new"] == "7200"
