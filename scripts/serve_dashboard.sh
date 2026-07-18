#!/usr/bin/env bash
# Serve the dashboard (the forest-sentinel-dashboard systemd unit's ExecStart).
# Loads configuration from a .env file (like run_pipeline.sh) and starts
# uvicorn — from the source checkout via uv by default, or from the
# CI-published container when APP_IMAGE is set (#96).
#
# Configure via the env file (default: ./.env) or the environment:
#   DASHBOARD_PORT  port to listen on                     (default: 8000)
#   APP_IMAGE       published app image (blank = from-source uv mode)
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

serve=(uvicorn forest_sentinel.dashboard.app:app --host 0.0.0.0 --port "${DASHBOARD_PORT:-8000}")

# Image mode (#96): the COG root is mounted at the same path (evidence paths in
# the catalog stay valid) and config/ read-write (dashboard AOI uploads must
# land on the host, where the pipeline and the sync_aois workflow read them).
if [ -n "${APP_IMAGE:-}" ]; then
    cog_root="${FOREST_SENTINEL_COG_ROOT:-/data/cogs}"
    docker_args=(run --rm --network host)
    [ -f "${ENV_FILE}" ] && docker_args+=(--env-file "${ENV_FILE}")
    docker_args+=(-v "${cog_root}:${cog_root}" -v "$(pwd)/config:/app/config")
    exec docker "${docker_args[@]}" "${APP_IMAGE}" "${serve[@]}"
fi
exec uv run "${serve[@]}"
