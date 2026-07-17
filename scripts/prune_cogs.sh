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

exec uv run forest-sentinel cogs prune "$@"
