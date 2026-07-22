#!/usr/bin/env bash
# Offline confidence re-scoring wrapper (#168) for the forest-sentinel-assess
# systemd unit. Loads configuration from a .env file and calls the CLI's
# `assess` command — database + retained local COGs only, no Earth Engine.
#
# Configure via the env file (default: ./.env) or the environment:
#   ENV_FILE         env file to load             (default: .env)
#   OVERRIDES_FILE   dashboard settings overrides (default: config/overrides.env)
set -euo pipefail

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
# Dashboard settings edits (#162): sourced AFTER .env so overrides win.
OVERRIDES_FILE="${OVERRIDES_FILE:-config/overrides.env}"
if [ -f "${OVERRIDES_FILE}" ]; then
    set -a
    # shellcheck disable=SC1090
    . "${OVERRIDES_FILE}"
    set +a
fi

# Image mode (#96): APP_IMAGE runs the CLI from the CI-published container;
# blank (default) keeps the from-source uv path. The COG root is mounted at
# the same path so recorded cog_paths resolve for the stability factor.
if [ -n "${APP_IMAGE:-}" ]; then
    cog_root="${FOREST_SENTINEL_COG_ROOT:-/data/cogs}"
    docker_args=(run --rm --network host)
    [ -f "${ENV_FILE}" ] && docker_args+=(--env-file "${ENV_FILE}")
    # Last --env-file wins in docker: same precedence as the sourcing above.
    [ -f "${OVERRIDES_FILE}" ] && docker_args+=(--env-file "${OVERRIDES_FILE}")
    docker_args+=(-v "${cog_root}:${cog_root}")
    exec docker "${docker_args[@]}" "${APP_IMAGE}" forest-sentinel assess "$@"
fi
exec uv run forest-sentinel assess "$@"
