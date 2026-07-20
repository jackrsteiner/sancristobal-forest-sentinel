#!/usr/bin/env bash
# Run the optical-change pipeline over a rolling window. Intended to be invoked
# on a schedule (systemd timer / cron on the VM, or a CI job). Loads configuration
# from a .env file, computes --since/--until from WINDOW_DAYS, and calls the CLI.
#
# Configure via the env file (default: ./.env) or the environment:
#   AOI_PATH      path to a single AOI GeoJSON           (default: examples/aoi-sample.geojson)
#   FOREST_SENTINEL_AOIS_DIR  directory of AOI GeoJSONs  (default: config/aois).
#                 Every *.geojson in it runs, sequentially, in addition to
#                 AOI_PATH — committed files and dashboard uploads share this
#                 directory (#81).
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
AOIS_DIR="${FOREST_SENTINEL_AOIS_DIR:-config/aois}"
WINDOW_DAYS="${WINDOW_DAYS:-30}"

# Image mode (#96): APP_IMAGE (config/instance.env -> .env) runs the CLI from
# the CI-published container instead of the source checkout; blank (default)
# keeps the from-source uv path. The COG root is mounted at the same path so
# recorded cog_paths stay valid, and config/ is mounted read-write so AOI files
# (committed seeds and dashboard uploads) are visible to the container.
app_run() {
    if [ -n "${APP_IMAGE:-}" ]; then
        local cog_root="${FOREST_SENTINEL_COG_ROOT:-/data/cogs}"
        local docker_args=(run --rm --network host)
        [ -f "${ENV_FILE}" ] && docker_args+=(--env-file "${ENV_FILE}")
        docker_args+=(-v "${cog_root}:${cog_root}" -v "$(pwd)/config:/app/config")
        docker "${docker_args[@]}" "${APP_IMAGE}" "$@"
    else
        uv run "$@"
    fi
}

UNTIL="$(date -u +%Y-%m-%d)"
SINCE="$(date -u -d "-${WINDOW_DAYS} days" +%Y-%m-%d)"

# The AOI list: the single AOI_PATH (backward compatible) plus every *.geojson
# in AOIS_DIR — committed seeds and dashboard uploads alike — deduped by
# canonical path. All AOIs share this one run's PIPELINE_TIMEOUT budget; each
# is processed by its own CLI invocation (own connection, own per-AOI lock).
aoi_files=()
[ -f "${AOI_PATH}" ] && aoi_files+=("${AOI_PATH}")
if [ -d "${AOIS_DIR}" ]; then
    while IFS= read -r file; do
        # Dashboard-disabled AOIs (#149): a sidecar marker skips the AOI while
        # keeping its file, row, and event history in place.
        if [ -e "${file}.disabled" ]; then
            echo "==> Skipping disabled AOI ${file}"
            continue
        fi
        aoi_files+=("${file}")
    done < <(find "${AOIS_DIR}" -maxdepth 1 -name '*.geojson' | sort)
fi

common_args=(--since "${SINCE}" --until "${UNTIL}")
[ -n "${BASELINE_WINDOW:-}" ] && common_args+=(--baseline-window "${BASELINE_WINDOW}")
[ -n "${THRESHOLD:-}" ] && common_args+=(--threshold "${THRESHOLD}")
[ -n "${MIN_AREA:-}" ] && common_args+=(--min-area "${MIN_AREA}")

if [ "${#aoi_files[@]}" -eq 0 ]; then
    # Preserve the single-AOI error path: let the CLI report the missing file.
    echo "==> forest-sentinel run --aoi ${AOI_PATH} ${common_args[*]}"
    app_run forest-sentinel run --aoi "${AOI_PATH}" "${common_args[@]}"
    exit "$?"
fi

overall=0
seen=""
for file in "${aoi_files[@]}"; do
    canonical="$(readlink -f "${file}")"
    case "${seen}" in *"|${canonical}|"*) continue ;; esac
    seen="${seen}|${canonical}|"
    echo "==> forest-sentinel run --aoi ${file} ${common_args[*]}"
    # One failing AOI must not starve the others; the exit code still alerts
    # the scheduler if any AOI failed.
    app_run forest-sentinel run --aoi "${file}" "${common_args[@]}" || overall=1
done
exit "${overall}"
