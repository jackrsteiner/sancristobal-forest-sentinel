#!/usr/bin/env bash
# Apply the COG retention policy (#80). Intended to be invoked on a schedule
# (the forest-sentinel-prune systemd timer). Loads configuration from a .env
# file (like run_pipeline.sh) and calls the CLI, which prunes catalog COGs
# older than the effective retention — never database rows, and never files
# inside the scheduler's active window (see docs/architecture.md §7).
#
# Configure via the env file (default: ./.env) or the environment:
#   COG_RETENTION_DAYS  days of COGs to keep (blank/0 = keep forever)
#   WINDOW_DAYS         the scheduler's rolling window; the retention floor
#   FOREST_SENTINEL_COG_ROOT  the store to prune (default: data/cogs)
set -euo pipefail

cd "$(dirname "$0")/.."

# systemd services get a minimal PATH that excludes ~/.local/bin, where
# vm_setup.sh installs uv — make sure it is reachable.
export PATH="${HOME}/.local/bin:${PATH}"

ENV_FILE="${ENV_FILE:-.env}"
if [ -f "${ENV_FILE}" ]; then
    set -a
    # shellcheck disable=SC1090
    . "${ENV_FILE}"
    set +a
fi
# Dashboard settings edits (#162): sourced AFTER .env so COG_RETENTION_DAYS
# edits apply on the next prune without an update-instance in between.
OVERRIDES_FILE="${OVERRIDES_FILE:-config/overrides.env}"
if [ -f "${OVERRIDES_FILE}" ]; then
    set -a
    # shellcheck disable=SC1090
    . "${OVERRIDES_FILE}"
    set +a
fi

# Image mode (#96): APP_IMAGE (config/instance.env -> .env) runs the CLI from
# the CI-published container; blank (default) keeps the from-source uv path.
# The COG root is mounted at the same path so the store being pruned is the
# host's canonical one.
if [ -n "${APP_IMAGE:-}" ]; then
    cog_root="${FOREST_SENTINEL_COG_ROOT:-/data/cogs}"
    docker_args=(run --rm --network host)
    [ -f "${ENV_FILE}" ] && docker_args+=(--env-file "${ENV_FILE}")
    # Last --env-file wins in docker: same precedence as the sourcing above.
    [ -f "${OVERRIDES_FILE}" ] && docker_args+=(--env-file "${OVERRIDES_FILE}")
    docker_args+=(-v "${cog_root}:${cog_root}")
    exec docker "${docker_args[@]}" "${APP_IMAGE}" forest-sentinel cogs prune "$@"
fi
exec uv run forest-sentinel cogs prune "$@"
