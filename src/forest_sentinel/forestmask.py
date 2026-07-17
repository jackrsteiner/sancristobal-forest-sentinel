"""Forest masking for change detection (#82).

Without a forest mask, ΔNBR flags *any* vegetation loss — crop harvest cycles,
grassland senescence, wetland drawdown — which pollutes the candidate/event
tables and inflates ``reduceToVectors`` cost for AOIs drawn from loose
administrative boundaries (``docs/scaling.md`` §4). This module restricts
candidate extraction to forested pixels: the configured mask is applied to the
ΔNBR delta **at candidate thresholding only** (the exported index/change
rasters stay unmasked, preserving full context for review — decision recorded
in ``docs/architecture.md`` §5.7).

The mask configuration is a **methodology input**: it is recorded verbatim in
``methodology_version.parameters`` under :data:`PARAMETER_KEY`, and candidate
extraction resolves it back from the methodology row (like the ΔNBR threshold
and minimum area), so provenance always says which mask produced a candidate
set. Methodology rows that predate this key resolve to "no mask", keeping old
lineages reproducible.

Default source: the Hansen Global Forest Change composite — forest is
``treecover2000 >= canopy_threshold_pct`` (default 30%) minus pixels with any
recorded loss (``lossyear``), at 30 m matching HLS natively. ESA WorldCover's
tree-cover class is the class-based alternative; ``none`` disables masking for
non-forest use cases.
"""

import os
from typing import Any

from forest_sentinel import earthengine

# instance.env / .env knobs. WARNING: methodology inputs — changing them mints
# a new methodology version (see config/instance.env).
SOURCE_ENV_VAR = "FOREST_SENTINEL_FOREST_MASK"
ASSET_ENV_VAR = "FOREST_SENTINEL_FOREST_MASK_ASSET"
CANOPY_PCT_ENV_VAR = "FOREST_SENTINEL_FOREST_MASK_CANOPY_PCT"

SOURCE_HANSEN = "hansen"
SOURCE_WORLDCOVER = "worldcover"
SOURCE_NONE = "none"
DEFAULT_SOURCE = SOURCE_HANSEN

# Pinned dataset years/versions (overridable via ASSET_ENV_VAR) so the
# recorded provenance names an immutable asset, not "latest".
DEFAULT_HANSEN_ASSET = "UMD/hansen/global_forest_change_2023_v1_11"
DEFAULT_CANOPY_THRESHOLD_PCT = 30.0
DEFAULT_WORLDCOVER_ASSET = "ESA/WorldCover/v200"
WORLDCOVER_TREE_CLASS = 10  # "Tree cover" in the WorldCover legend

# The methodology_version.parameters key the mask config is recorded under.
PARAMETER_KEY = "forest_mask"


class ForestMaskConfigError(ValueError):
    """Raised for an unusable forest-mask configuration (bad source or threshold)."""


def config_from_env() -> dict[str, Any]:
    """The forest-mask methodology parameters from the environment.

    Fails loudly on a typo'd source or malformed threshold — silently monitoring
    unmasked (or with the wrong dataset) must not happen.
    """
    source = os.environ.get(SOURCE_ENV_VAR, "").strip().lower() or DEFAULT_SOURCE
    if source == SOURCE_NONE:
        return {"source": SOURCE_NONE}
    asset = os.environ.get(ASSET_ENV_VAR, "").strip()
    if source == SOURCE_HANSEN:
        raw_pct = os.environ.get(CANOPY_PCT_ENV_VAR, "").strip()
        try:
            pct = float(raw_pct) if raw_pct else DEFAULT_CANOPY_THRESHOLD_PCT
        except ValueError as exc:
            raise ForestMaskConfigError(
                f"{CANOPY_PCT_ENV_VAR} must be a number (canopy %), got {raw_pct!r}"
            ) from exc
        if not 0 <= pct <= 100:
            raise ForestMaskConfigError(
                f"{CANOPY_PCT_ENV_VAR} must be between 0 and 100, got {pct}"
            )
        return {
            "source": SOURCE_HANSEN,
            "asset": asset or DEFAULT_HANSEN_ASSET,
            "canopy_threshold_pct": pct,
        }
    if source == SOURCE_WORLDCOVER:
        return {
            "source": SOURCE_WORLDCOVER,
            "asset": asset or DEFAULT_WORLDCOVER_ASSET,
            "tree_class": WORLDCOVER_TREE_CLASS,
        }
    raise ForestMaskConfigError(
        f"{SOURCE_ENV_VAR} must be one of "
        f"{SOURCE_HANSEN!r}, {SOURCE_WORLDCOVER!r}, {SOURCE_NONE!r}; got {source!r}"
    )


def parameters_entry(config: dict[str, Any]) -> dict[str, Any]:
    """The methodology-parameters fragment recording ``config``.

    Mask-off records **nothing**: the absent key is what pre-#82 methodology
    rows have, so a mask-off run content-addresses to the same methodology and
    keeps reusing its artifacts (``resolve_config`` maps the absent key back to
    "none").
    """
    if config.get("source") == SOURCE_NONE:
        return {}
    return {PARAMETER_KEY: config}


def resolve_config(methodology: Any, override: dict[str, Any] | None) -> dict[str, Any]:
    """Mask config from the explicit override, else methodology parameters, else off.

    Methodology rows minted before this key existed resolve to "none": their
    candidates were extracted unmasked, and re-extraction under the same
    methodology must reproduce that.
    """
    if override is not None:
        return override
    value = methodology.parameters.get(PARAMETER_KEY)
    return value if value is not None else {"source": SOURCE_NONE}


def build_mask(config: dict[str, Any], *, ee_module: Any = earthengine) -> Any | None:
    """The configured forest mask as an EE image (1 = forest), or None when off."""
    source = config.get("source")
    if source in (None, SOURCE_NONE):
        return None
    if source == SOURCE_HANSEN:
        return ee_module.hansen_forest_mask(
            config["asset"], canopy_threshold_pct=float(config["canopy_threshold_pct"])
        )
    if source == SOURCE_WORLDCOVER:
        return ee_module.worldcover_forest_mask(
            config["asset"], tree_class=int(config["tree_class"])
        )
    raise ForestMaskConfigError(f"unsupported forest-mask source in methodology: {source!r}")
