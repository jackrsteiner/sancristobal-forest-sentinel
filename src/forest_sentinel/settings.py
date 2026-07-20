"""The settings catalogue and guarded writes (Slice 7, beads 7.1/7.2).

The catalogue is the live counterpart of ``docs/config-inventory.md``: every
runtime configuration value, grouped into the audit's four categories, with its
purpose, its *resolved* value on this instance, and the implications of
changing it. Resolution is layered the way the next pipeline run will see it:
``config/overrides.env`` (dashboard edits) → process environment → default —
the dashboard process's own environment can be stale (it loaded ``.env`` at
start), so the overrides file is read directly.

Writes (bead 7.2) are an **allowlist, not a blocklist**: only keys registered
as ``editable`` or ``guarded`` exist for the write path at all. Instance
identity and the silent-re-export footguns (``FOREST_SENTINEL_DATABASE_URL``,
``FOREST_SENTINEL_COG_ROOT``) are display-only and rejected as unknown keys.
Methodology keys are ``guarded``: changing one mints a new content-addressed
methodology version on the next run (rasters are reused — Finding 1 — but
event lineages split at the boundary), so the write requires an explicit
confirmation flag and the consequence text rides the error until it is given.

Edits land in ``config/overrides.env``; ``vm_setup.sh`` appends that file when
regenerating ``.env`` (after ``instance.env`` — "last assignment wins" — but
before the world-open forced-off guard lines, which must always win). Every
accepted change appends a ``settings_change`` row; with tunnel-as-auth there
is no "who", and the UI says so.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.models import MethodologyVersion, SettingsChange

OVERRIDES_PATH_ENV_VAR = "FOREST_SENTINEL_OVERRIDES_PATH"
DEFAULT_OVERRIDES_PATH = "config/overrides.env"

CATEGORY_INSTANCE = "instance"
CATEGORY_PIPELINE = "pipeline-tuning"
CATEGORY_METHODOLOGY = "methodology"
CATEGORY_LIFECYCLE = "lifecycle"
CATEGORIES = (CATEGORY_INSTANCE, CATEGORY_PIPELINE, CATEGORY_METHODOLOGY, CATEGORY_LIFECYCLE)

EDITABLE = "editable"
GUARDED = "guarded"  # editable, but only with an explicit consequence confirmation
DISPLAY_ONLY = "display-only"

# When an edit takes effect: most knobs on the next pipeline run; a few are
# rendered into systemd units and need an Update-instance run to roll out
# (bead 7.5) — the dispatch sync then fires with update_vm so the rollout is
# automatic when configured.
APPLIES_NEXT_RUN = "next-run"
APPLIES_UPDATE_INSTANCE = "update-instance"


# Space-free systemd calendar forms only: the value crosses a shell-sourced
# env file and a sed rendering, and the generated .env is also consumed by
# docker --env-file — no quoting convention survives all three, so full
# OnCalendar expressions with date parts stay a hand edit (bead 7.5, #139).
_SCHEDULE_PATTERN = r"[A-Za-z]+|\d{1,2}:\d{2}(:\d{2})?"


class SettingsError(ValueError):
    """A rejected settings write (unknown key, bad value, missing confirmation)."""


# The consequence copy echoed by the UI and required-by-error for guarded keys
# (config-inventory Finding 7 / architecture §5.9 operator note).
METHODOLOGY_CHANGE_CONSEQUENCE = (
    "Changing a methodology parameter mints a new content-addressed methodology version on "
    "the next run: existing events stop growing and will auto-resolve, and the new lineage "
    "starts fresh events. Rasters are reused (raster/detection split) — candidates "
    "re-extract without new Earth Engine exports."
)


@dataclass(frozen=True)
class Setting:
    """One catalogue entry; ``key`` is the env-file key edits are written under."""

    key: str
    category: str
    purpose: str
    implications: str
    editability: str = DISPLAY_ONLY
    default: str | None = None
    value_type: str = "str"  # str | int | float | choice
    choices: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    redact: bool = False
    # Regex a str-typed value must fullmatch. Doubles as injection defense:
    # values reach shell-sourced env files and a unit-rendering sed, so
    # patterns must never admit whitespace, quotes, or '#'.
    pattern: str | None = None
    # When the edit takes effect (APPLIES_NEXT_RUN / APPLIES_UPDATE_INSTANCE).
    applies: str = APPLIES_NEXT_RUN
    # For methodology keys: the parameter name recorded in methodology_version
    # rows, so the catalogue can show what the current lineage actually used.
    methodology_parameter: str | None = None
    # Fixed values (code constants) have no env key to resolve; show this.
    constant: str | None = None


def _registry() -> tuple[Setting, ...]:
    # Imported lazily: cli imports settings' siblings; keep import edges simple.
    from forest_sentinel import candidates, change, cli, forestmask, indices, qa, radar
    from forest_sentinel.hls import HLS_COLLECTIONS

    return (
        # --- instance (display-only: provisioning identity and footguns) ---
        Setting(
            key="FOREST_SENTINEL_GEE_PROJECT",
            category=CATEGORY_INSTANCE,
            purpose="Cloud project Earth Engine bills quota against.",
            implications="Billing/quota only; results are identical regardless of project.",
        ),
        Setting(
            key="FOREST_SENTINEL_GCS_STAGING_BUCKET",
            category=CATEGORY_INSTANCE,
            purpose="Transient staging bucket EE exports pass through.",
            implications="Contents are transient (1-day TTL); safe to swap between runs.",
        ),
        Setting(
            key="FOREST_SENTINEL_COG_ROOT",
            category=CATEGORY_INSTANCE,
            purpose="Canonical local COG store.",
            implications=(
                "Footgun: changing it without moving files fails every reuse check and "
                "silently re-exports the whole window (the pipeline warns at run start). "
                "Not editable here."
            ),
        ),
        Setting(
            key="FOREST_SENTINEL_DATABASE_URL",
            category=CATEGORY_INSTANCE,
            purpose="The PostGIS catalog — the reproduction recipe.",
            implications=(
                "Treat as immutable per instance: repointing orphans all history and "
                "re-exports the window. Not editable here."
            ),
            redact=True,
        ),
        Setting(
            key="FOREST_SENTINEL_AOIS_DIR",
            category=CATEGORY_INSTANCE,
            purpose="Directory of AOI GeoJSONs (seeds + dashboard uploads).",
            implications="Path plumbing; uploads land here and sync back to the repo.",
            default="config/aois",
        ),
        Setting(
            key="FOREST_SENTINEL_CONTEXT_DIR",
            category=CATEGORY_INSTANCE,
            purpose="Directory of context-layer GeoJSONs.",
            implications="Path plumbing; harvested at every run start.",
            default="config/context",
        ),
        Setting(
            key="FOREST_SENTINEL_SETTINGS_EDIT",
            category=CATEGORY_INSTANCE,
            purpose="Write guard for this settings surface.",
            implications=(
                "Security posture, deliberately not editable from the surface it guards; "
                "forced to 0 on a world-open dashboard."
            ),
            default="1",
            choices=("0", "1"),
            value_type="choice",
        ),
        # --- pipeline tuning (freely editable; never affects results) ---
        Setting(
            key="FOREST_SENTINEL_MAX_CONCURRENT_EXPORTS",
            category=CATEGORY_PIPELINE,
            purpose="Earth Engine batch exports kept in flight together.",
            implications=(
                "Throughput/cost only; applies on the next run. Raise toward your EE "
                "tier's task limit for faster windows."
            ),
            editability=EDITABLE,
            default=str(cli.DEFAULT_MAX_CONCURRENT_EXPORTS),
            value_type="int",
            minimum=1,
            maximum=64,
        ),
        Setting(
            key="FOREST_SENTINEL_EXPORT_TIMEOUT_SECONDS",
            category=CATEGORY_PIPELINE,
            purpose="Seconds to wait for one Earth Engine export before giving up.",
            implications=(
                "Robustness only; applies on the next run. A timed-out export marks just "
                "its observation failed — the next run retries it."
            ),
            editability=EDITABLE,
            default="3600",
            value_type="int",
            minimum=60,
            maximum=86_400,
        ),
        Setting(
            key="PIPELINE_TIMEOUT",
            category=CATEGORY_PIPELINE,
            purpose="systemd budget for one pipeline run (TimeoutStartSec).",
            implications=(
                "Rendered into the systemd unit; rolls out on the next Update-instance "
                "run (requested automatically when the repo sync is configured). "
                "A single systemd time span like 20h, 90min, or 86400s."
            ),
            editability=EDITABLE,
            default="20h",
            pattern=r"\d+(s|min|h|d)",
            applies=APPLIES_UPDATE_INSTANCE,
        ),
        Setting(
            key="PIPELINE_SCHEDULE",
            category=CATEGORY_PIPELINE,
            purpose="When the daily pipeline timer fires (UTC).",
            implications=(
                "Rendered into the systemd timer; rolls out on the next Update-instance "
                "run (requested automatically when the repo sync is configured). A time "
                "of day (HH:MM[:SS], runs daily) or a systemd shorthand like hourly or "
                "daily; expressions with date parts need a hand edit of the timer "
                "template."
            ),
            editability=EDITABLE,
            default="03:00:00",
            pattern=_SCHEDULE_PATTERN,
            applies=APPLIES_UPDATE_INSTANCE,
        ),
        Setting(
            key="PRUNE_SCHEDULE",
            category=CATEGORY_PIPELINE,
            purpose="When the daily COG-retention prune timer fires (UTC).",
            implications=(
                "Rendered into the systemd timer; rolls out on the next Update-instance "
                "run (requested automatically when the repo sync is configured). Keep it "
                "before the pipeline schedule so disk headroom is reclaimed first. Same "
                "accepted forms as PIPELINE_SCHEDULE."
            ),
            editability=EDITABLE,
            default="02:30:00",
            pattern=_SCHEDULE_PATTERN,
            applies=APPLIES_UPDATE_INSTANCE,
        ),
        # --- methodology (guarded: next run mints a new version) ---
        Setting(
            key="THRESHOLD",
            category=CATEGORY_METHODOLOGY,
            purpose="ΔNBR drop that makes a pixel a disturbance candidate.",
            implications=METHODOLOGY_CHANGE_CONSEQUENCE,
            editability=GUARDED,
            default=str(candidates.DEFAULT_DELTA_NBR_THRESHOLD),
            value_type="float",
            minimum=-2.0,
            maximum=0.0,
            methodology_parameter="delta_nbr_threshold",
        ),
        Setting(
            key="MIN_AREA",
            category=CATEGORY_METHODOLOGY,
            purpose="Minimum candidate polygon area (m²); smaller patches are noise.",
            implications=METHODOLOGY_CHANGE_CONSEQUENCE,
            editability=GUARDED,
            default=str(candidates.DEFAULT_MIN_AREA_M2),
            value_type="float",
            minimum=0,
            methodology_parameter="min_area_m2",
        ),
        Setting(
            key="BASELINE_WINDOW",
            category=CATEGORY_METHODOLOGY,
            purpose="Prior observations reduced into the trailing-median baseline.",
            implications=(
                METHODOLOGY_CHANGE_CONSEQUENCE
                + " Baseline window is a raster-lineage input: unlike threshold changes, "
                "this one re-exports the window from Earth Engine."
            ),
            editability=GUARDED,
            default=str(change.DEFAULT_BASELINE_WINDOW),
            value_type="int",
            minimum=1,
            maximum=30,
            methodology_parameter="baseline_window",
        ),
        Setting(
            key="FOREST_SENTINEL_FOREST_MASK",
            category=CATEGORY_METHODOLOGY,
            purpose="Forest mask restricting candidates to forested pixels.",
            implications=METHODOLOGY_CHANGE_CONSEQUENCE,
            editability=GUARDED,
            default=forestmask.DEFAULT_SOURCE,
            value_type="choice",
            choices=(
                forestmask.SOURCE_HANSEN,
                forestmask.SOURCE_WORLDCOVER,
                forestmask.SOURCE_NONE,
            ),
        ),
        Setting(
            key="FOREST_SENTINEL_FOREST_MASK_ASSET",
            category=CATEGORY_METHODOLOGY,
            purpose="Earth Engine asset the forest mask is built from.",
            implications=METHODOLOGY_CHANGE_CONSEQUENCE,
            editability=GUARDED,
            default=forestmask.DEFAULT_HANSEN_ASSET,
        ),
        Setting(
            key="FOREST_SENTINEL_FOREST_MASK_CANOPY_PCT",
            category=CATEGORY_METHODOLOGY,
            purpose="Hansen canopy-cover percent counted as forest.",
            implications=METHODOLOGY_CHANGE_CONSEQUENCE,
            editability=GUARDED,
            default=str(forestmask.DEFAULT_CANOPY_THRESHOLD_PCT),
            value_type="float",
            minimum=0,
            maximum=100,
        ),
        Setting(
            key="FOREST_SENTINEL_RADAR",
            category=CATEGORY_METHODOLOGY,
            purpose="Enable the Sentinel-1 radar lineage.",
            implications=(
                "Enabling adds a separate radar-change methodology lineage (new S1 work, "
                "no optical impact); disabling stops new radar detections."
            ),
            editability=GUARDED,
            default="0",
            value_type="choice",
            choices=("0", "1"),
        ),
        Setting(
            key="FOREST_SENTINEL_RADAR_THRESHOLD",
            category=CATEGORY_METHODOLOGY,
            purpose="VV backscatter drop (dB) that makes a radar candidate.",
            implications=METHODOLOGY_CHANGE_CONSEQUENCE,
            editability=GUARDED,
            default=str(radar.DEFAULT_DELTA_VV_DB_THRESHOLD),
            value_type="float",
            minimum=-30.0,
            maximum=0.0,
            methodology_parameter="delta_vv_db_threshold",
        ),
        Setting(
            key="SCALE_M",
            category=CATEGORY_METHODOLOGY,
            purpose="Export/vectorize resolution (HLS native grid).",
            implications="Code constant; recorded in every lineage.",
            constant=str(indices.DEFAULT_SCALE_METERS),
        ),
        Setting(
            key="MASKED_CATEGORIES",
            category=CATEGORY_METHODOLOGY,
            purpose="Fmask QA categories masked out of every observation.",
            implications="Code constant; recorded in every lineage.",
            constant=", ".join(qa.MASK_CATEGORIES),
        ),
        Setting(
            key="COLLECTIONS",
            category=CATEGORY_METHODOLOGY,
            purpose="Source imagery collections.",
            implications="Code constant; recorded in every lineage.",
            constant=", ".join(sorted(HLS_COLLECTIONS)),
        ),
        Setting(
            key="SCRIPT_VERSIONS",
            category=CATEGORY_METHODOLOGY,
            purpose="Per-stage EE code pins (raster / detection).",
            implications=(
                "Bumping the raster pin re-exports lineages; the detection pin only "
                "re-extracts candidates (Finding 4)."
            ),
            constant=(
                f"raster={cli.RASTER_SCRIPT_VERSION}, detection={cli.EE_SCRIPT_VERSION}, "
                f"radar={cli.RADAR_SCRIPT_VERSION}"
            ),
        ),
        # --- lifecycle & interpretation (freely editable) ---
        Setting(
            key="WINDOW_DAYS",
            category=CATEGORY_LIFECYCLE,
            purpose="How many trailing days each scheduled run scans.",
            implications=(
                "Scan scope, not methodology. Enlarging backfills older scenes (new EE "
                "work, not redundant); shrinking discards nothing. Also floors COG "
                "retention at WINDOW_DAYS + 14."
            ),
            editability=EDITABLE,
            default="30",
            value_type="int",
            minimum=1,
            maximum=365,
        ),
        Setting(
            key="COG_RETENTION_DAYS",
            category=CATEGORY_LIFECYCLE,
            purpose="Days of local COG files kept before pruning (0 = keep forever).",
            implications=(
                "Storage-for-compute trade: pruned COGs re-export on demand from recorded "
                "provenance, and local re-extraction needs the file on disk. Floored at "
                "WINDOW_DAYS + 14 at prune time."
            ),
            editability=EDITABLE,
            default="90",
            value_type="int",
            minimum=0,
            maximum=3650,
        ),
        Setting(
            key="RESOLVED_AFTER_DAYS",
            category=CATEGORY_LIFECYCLE,
            purpose="Quiet days (plus a clear look) before an event auto-resolves.",
            implications=(
                "Interpretation, not methodology (never re-exports). Lengthening does not "
                "revive already-resolved events."
            ),
            editability=EDITABLE,
            default="90",
            value_type="int",
            minimum=1,
            maximum=3650,
        ),
        Setting(
            key="CONTEXT_BUFFER_M",
            category=CATEGORY_LIFECYCLE,
            purpose="Search distance for nearby context features (meters).",
            implications="Presentation only; relations are recomputed wholesale each run.",
            editability=EDITABLE,
            default="5000",
            value_type="int",
            minimum=1,
            maximum=1_000_000,
        ),
    )


def catalogue(session: Session) -> dict[str, Any]:
    """Every setting with its layered resolved value, grouped for the UI."""
    recorded = _recorded_methodology_parameters(session)
    overrides = read_overrides()
    entries = []
    for setting in _registry():
        entry: dict[str, Any] = {
            "key": setting.key,
            "category": setting.category,
            "purpose": setting.purpose,
            "implications": setting.implications,
            "editability": setting.editability,
            "default": setting.default,
            "value_type": setting.value_type if setting.constant is None else "constant",
            "choices": list(setting.choices) if setting.choices else None,
            "minimum": setting.minimum,
            "maximum": setting.maximum,
            "resolved": _resolve(setting, overrides),
            "source": _source(setting, overrides),
            "applies": setting.applies,
        }
        if setting.methodology_parameter is not None:
            entry["recorded"] = recorded.get(setting.methodology_parameter)
        entries.append(entry)
    return {"categories": list(CATEGORIES), "settings": entries}


def _resolve(setting: Setting, overrides: dict[str, str]) -> str | None:
    if setting.constant is not None:
        return setting.constant
    value = overrides.get(setting.key) or os.environ.get(setting.key) or setting.default
    if value is not None and setting.redact:
        return _redacted(value)
    return value


def _source(setting: Setting, overrides: dict[str, str]) -> str:
    if setting.constant is not None:
        return "code"
    if overrides.get(setting.key):
        return "override"
    if os.environ.get(setting.key):
        return "environment"
    return "default"


def _redacted(value: str) -> str:
    """Host/database only for connection URLs — never credentials."""
    match = re.match(r"^[a-z0-9+]+://(?:[^@/]*@)?(?P<rest>.*)$", value)
    return f"…@{match.group('rest')}" if match else "(redacted)"


def _recorded_methodology_parameters(session: Session) -> dict[str, Any]:
    """Parameters of the most recently minted methodology per name, flattened.

    Radar keys never collide with optical ones (distinct parameter names), so a
    single flat mapping serves the catalogue.
    """
    recorded: dict[str, Any] = {}
    rows = (
        session.execute(select(MethodologyVersion).order_by(MethodologyVersion.id)).scalars().all()
    )
    latest_by_name: dict[str, MethodologyVersion] = {row.name: row for row in rows}
    for row in latest_by_name.values():
        recorded.update(row.parameters)
    return recorded


def overrides_path() -> Path:
    return Path(os.environ.get(OVERRIDES_PATH_ENV_VAR, DEFAULT_OVERRIDES_PATH))


def read_overrides() -> dict[str, str]:
    """KEY=value pairs from the overrides file (missing file = no overrides)."""
    path = overrides_path()
    if not path.exists():
        return {}
    overrides: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        overrides[key.strip()] = value.strip()
    return overrides


# --- bead 7.2 (#135): guarded writes -----------------------------------------


def apply_change(
    session: Session,
    *,
    key: str,
    value: str,
    confirm_methodology_change: bool = False,
) -> dict[str, Any]:
    """Validate and persist one settings change; returns ``{key, old, new}``.

    Allowlist semantics: keys that are not registered editable/guarded do not
    exist for this function — including every display-only instance value —
    and are rejected identically to typos. Guarded (methodology) keys require
    ``confirm_methodology_change``; the rejection carries the consequence copy
    so the UI can show exactly what is being agreed to.
    """
    setting = _writable().get(key)
    if setting is None:
        raise SettingsError(f"unknown or non-editable setting {key!r}")
    if setting.editability == GUARDED and not confirm_methodology_change:
        raise SettingsError(
            f"{key} is a methodology parameter; confirm the change to proceed. "
            + METHODOLOGY_CHANGE_CONSEQUENCE
        )
    normalized = _validate(setting, value)

    overrides = read_overrides()
    old = _resolve(setting, overrides)
    _write_override(setting.key, normalized)
    session.add(
        SettingsChange(
            key=setting.key,
            category=setting.category,
            old_value=old,
            new_value=normalized,
        )
    )
    session.flush()
    return {"key": setting.key, "old": old, "new": normalized, "applies": setting.applies}


def _writable() -> dict[str, Setting]:
    return {s.key: s for s in _registry() if s.editability in (EDITABLE, GUARDED)}


def _validate(setting: Setting, value: str) -> str:
    value = value.strip()
    if not value:
        raise SettingsError(f"{setting.key} requires a value")
    if setting.value_type == "choice":
        if setting.choices and value not in setting.choices:
            raise SettingsError(f"{setting.key} must be one of {', '.join(setting.choices or ())}")
        return value
    if setting.value_type in ("int", "float"):
        try:
            number = int(value) if setting.value_type == "int" else float(value)
        except ValueError as exc:
            raise SettingsError(f"{setting.key} must be a {setting.value_type}") from exc
        if setting.minimum is not None and number < setting.minimum:
            raise SettingsError(f"{setting.key} must be >= {setting.minimum:g}")
        if setting.maximum is not None and number > setting.maximum:
            raise SettingsError(f"{setting.key} must be <= {setting.maximum:g}")
        _check_cross_rules(setting, float(number))
        return str(number)
    if setting.pattern is not None and re.fullmatch(setting.pattern, value) is None:
        raise SettingsError(f"{setting.key} does not match the accepted forms")
    return value


def _check_cross_rules(setting: Setting, number: float) -> None:
    """Cross-value rules mirroring runtime behavior, so the UI tells the truth."""
    if setting.key == "COG_RETENTION_DAYS" and number != 0:
        overrides = read_overrides()
        window_raw = overrides.get("WINDOW_DAYS") or os.environ.get("WINDOW_DAYS") or "30"
        try:
            floor = int(window_raw) + 14
        except ValueError:
            floor = 44
        if number < floor:
            raise SettingsError(
                f"COG_RETENTION_DAYS below the WINDOW_DAYS + 14 floor ({floor}) would be "
                "raised back at prune time; set it to at least the floor (or 0 to keep "
                "everything)"
            )


def _write_override(key: str, value: str) -> None:
    """Read-modify-write the overrides file, preserving other keys, sorted."""
    path = overrides_path()
    overrides = read_overrides()
    overrides[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Dashboard settings edits (Slice 7). Appended after instance.env by",
        "# vm_setup.sh when regenerating .env — last assignment wins; the",
        "# world-open guard lines still win over this file.",
    ]
    lines += [f"{k}={overrides[k]}" for k in sorted(overrides)]
    path.write_text("\n".join(lines) + "\n")
