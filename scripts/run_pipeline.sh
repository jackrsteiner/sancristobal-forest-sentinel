#!/usr/bin/env bash
# Run the optical-change pipeline over a rolling window. Intended to be invoked
# on a schedule (systemd timer / cron on the VM, or a CI job). Loads configuration
# from a .env file, computes --since/--until from WINDOW_DAYS, and calls the CLI.
#
# Configure via the env file (default: ./.env) or the environment:
#   AOI_PATH      path to the AOI GeoJSON                (default: examples/aoi-sample.geojson)
#   WINDOW_DAYS   size of the rolling window, in days    (default: 30)
#   BASELINE_WINDOW  --baseline-window passthrough       (optional)
#   THRESHOLD        --threshold passthrough             (optional)
#   MIN_AREA         --min-area passthrough              (optional)
#   plus the app's own vars: FOREST_SENTINEL_GEE_PROJECT,
#   FOREST_SENTINEL_GCS_STAGING_BUCKET, FOREST_SENTINEL_COG_ROOT,
#   FOREST_SENTINEL_DATABASE_URL, GOOGLE_APPLICATION_CREDENTIALS
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

AOI_PATH="${AOI_PATH:-examples/aoi-sample.geojson}"
WINDOW_DAYS="${WINDOW_DAYS:-30}"

UNTIL="$(date -u +%Y-%m-%d)"
SINCE="$(date -u -d "-${WINDOW_DAYS} days" +%Y-%m-%d)"

args=(run --aoi "${AOI_PATH}" --since "${SINCE}" --until "${UNTIL}")
[ -n "${BASELINE_WINDOW:-}" ] && args+=(--baseline-window "${BASELINE_WINDOW}")
[ -n "${THRESHOLD:-}" ] && args+=(--threshold "${THRESHOLD}")
[ -n "${MIN_AREA:-}" ] && args+=(--min-area "${MIN_AREA}")

echo "==> forest-sentinel ${args[*]}"
exec uv run forest-sentinel "${args[@]}"
