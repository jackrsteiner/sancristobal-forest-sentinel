"""Methodology-version provenance.

Every derived Slice 1 artifact references the ``methodology_version`` that produced
it. Because Slice 1 compute runs server-side in Earth Engine, the stored
``parameters`` must also pin the EE script version and input collection/asset IDs so
a run is reproducible.

Two entry points:

- ``resolve_methodology_version`` — what the CLI uses by default: the methodology is
  identified by its **parameter content**. Any existing row (whatever its version
  string) with an equal parameter set is reused; a genuinely new parameter set mints
  a new ``auto-<hash>`` version. Changing a knob (threshold, min area, baseline
  window, EE script version) therefore never errors — it starts a new provenance
  lineage; flipping the knob back re-matches the old row, whose artifacts are
  reusable again.
- ``get_or_create_methodology_version`` — the strict path for an explicitly named
  version: the same ``(name, version)`` must not silently map to divergent
  parameters (``MethodologyVersionMismatch``).
"""

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.models import MethodologyVersion, RasterLineage

AUTO_VERSION_PREFIX = "auto-"

# The methodology parameters that shape exported *raster content* — these hash
# into the raster lineage the index/change rasters key on, so changing any
# other parameter (threshold, min area, forest mask) reuses every COG
# (config-inventory Finding 1). ``raster_script_version`` is the raster-stage
# code pin (Finding 4): bumping the detection-stage ``ee_script_version`` for a
# vectorization-only change leaves the lineage — and every raster — intact.
RASTER_PARAM_KEYS = (
    "raster_script_version",
    "collections",
    "collection",
    "metric",
    "orbit_policy",
    "scale_m",
    "masked_categories",
    "baseline_window",
)
_RASTER_SCRIPT_KEY = "raster_script_version"


class MethodologyVersionMismatch(ValueError):
    """Raised when a ``(name, version)`` exists with different ``parameters``.

    Methodology versions are stable provenance records; the same identity must not
    silently map to divergent parameters. Bump the ``version`` instead.
    """


def resolve_methodology_version(
    session: Session,
    *,
    name: str,
    parameters: dict[str, Any],
    version: str | None = None,
) -> MethodologyVersion:
    """Resolve the methodology row for a run.

    With an explicit ``version`` this is the strict ``get_or_create`` (mismatched
    parameters raise). Without one, the methodology is content-addressed: the first
    existing row under ``name`` whose ``parameters`` are equal is reused —
    regardless of its version string, so pre-existing hand-versioned rows (e.g.
    ``1.0.0``) keep matching after an upgrade — and a new parameter set mints
    ``auto-<content hash>``.
    """
    if version is not None:
        return get_or_create_methodology_version(
            session, name=name, version=version, parameters=parameters
        )
    rows = (
        session.execute(
            select(MethodologyVersion)
            .where(MethodologyVersion.name == name)
            .order_by(MethodologyVersion.id)
        )
        .scalars()
        .all()
    )
    for row in rows:
        if row.parameters == parameters:
            return row
    return get_or_create_methodology_version(
        session, name=name, version=auto_version(parameters), parameters=parameters
    )


def parameter_hash(parameters: dict[str, Any], *, length: int = 10) -> str:
    """Deterministic content hash of a parameter dict (canonical-JSON SHA-256)."""
    canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:length]


def auto_version(parameters: dict[str, Any]) -> str:
    """Deterministic content-derived version string for a parameter set."""
    return AUTO_VERSION_PREFIX + parameter_hash(parameters)


def raster_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """The raster-lineage subset of a full methodology parameter dict.

    Parameter dicts that carry only ``ee_script_version`` (minted before the
    raster/detection split, or minimal test methodologies) let it double as the
    raster pin, so their lineages content-match the ones fully-specified
    parameter sets derive.
    """
    subset = {key: parameters[key] for key in RASTER_PARAM_KEYS if key in parameters}
    if _RASTER_SCRIPT_KEY not in subset and "ee_script_version" in parameters:
        subset[_RASTER_SCRIPT_KEY] = parameters["ee_script_version"]
    return subset


def resolve_raster_lineage(
    session: Session, *, name: str, parameters: dict[str, Any]
) -> RasterLineage:
    """Content-addressed get-or-create of the raster lineage for a parameter set.

    ``parameters`` is the FULL methodology dict; the lineage stores (and hashes)
    only its raster subset. Same subset → same row, whatever the detection
    parameters around it.
    """
    subset = raster_parameters(parameters)
    version = auto_version(subset)
    existing = session.execute(
        select(RasterLineage)
        .where(RasterLineage.name == name)
        .where(RasterLineage.version == version)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    created = RasterLineage(name=name, version=version, parameters=subset)
    session.add(created)
    session.flush()
    return created


_SCRIPT_VERSION_PARAM = "ee_script_version"


def next_display_version(session: Session, *, name: str, parameters: dict[str, Any]) -> str:
    """The next human-facing X.Y.Z label for a new version of ``name``.

    The content-addressed ``version`` remains the identity; this is an at-a-glance
    label attached to it at mint time. Per name, in mint order: the first version
    is ``1.0.0``; a changed ``ee_script_version`` (new band math / EE code) bumps
    the minor version; any other parameter change bumps the patch version.
    """
    latest = session.execute(
        select(MethodologyVersion)
        .where(MethodologyVersion.name == name)
        .where(MethodologyVersion.display_version.is_not(None))
        .order_by(MethodologyVersion.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None or latest.display_version is None:
        return "1.0.0"
    major, minor, patch = (int(part) for part in latest.display_version.split("."))
    if latest.parameters.get(_SCRIPT_VERSION_PARAM) != parameters.get(_SCRIPT_VERSION_PARAM):
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def get_or_create_methodology_version(
    session: Session,
    *,
    name: str,
    version: str,
    parameters: dict[str, Any],
) -> MethodologyVersion:
    """Return the row for ``(name, version)``, creating it if absent.

    Raises ``MethodologyVersionMismatch`` if a row with the same ``(name, version)``
    already stores different ``parameters``. Dict comparison is order-insensitive, so
    re-running with the same parameters in a different key order is treated as
    identical.
    """
    existing = session.execute(
        select(MethodologyVersion)
        .where(MethodologyVersion.name == name)
        .where(MethodologyVersion.version == version)
    ).scalar_one_or_none()

    if existing is not None:
        if existing.parameters != parameters:
            raise MethodologyVersionMismatch(
                f"methodology {name!r} version {version!r} already exists with different "
                "parameters; bump the version instead of mutating it"
            )
        return existing

    created = MethodologyVersion(
        name=name,
        version=version,
        display_version=next_display_version(session, name=name, parameters=parameters),
        parameters=parameters,
        raster_lineage_id=resolve_raster_lineage(session, name=name, parameters=parameters).id,
    )
    session.add(created)
    session.flush()
    return created
