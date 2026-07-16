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

from forest_sentinel.models import MethodologyVersion

AUTO_VERSION_PREFIX = "auto-"


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


def auto_version(parameters: dict[str, Any]) -> str:
    """Deterministic content-derived version string for a parameter set."""
    canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    return AUTO_VERSION_PREFIX + hashlib.sha256(canonical.encode()).hexdigest()[:10]


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

    created = MethodologyVersion(name=name, version=version, parameters=parameters)
    session.add(created)
    session.flush()
    return created
