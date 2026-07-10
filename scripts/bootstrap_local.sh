#!/usr/bin/env bash
# Set up a local development environment: install dependencies, start
# PostgreSQL + PostGIS via Docker Compose, and apply database migrations.
# Idempotent — safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "error: 'uv' is not installed. See https://docs.astral.sh/uv/" >&2
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "error: 'docker' is not installed (needed for PostgreSQL + PostGIS)." >&2
    exit 1
fi

echo "==> Installing dependencies (uv sync)"
uv sync

echo "==> Starting PostgreSQL + PostGIS (docker compose up -d)"
docker compose up -d

echo "==> Waiting for the database to become healthy"
for _ in $(seq 1 30); do
    if docker compose exec -T db pg_isready -U forest_sentinel >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
if ! docker compose exec -T db pg_isready -U forest_sentinel >/dev/null 2>&1; then
    echo "error: database did not become ready in time" >&2
    exit 1
fi

echo "==> Applying database migrations (alembic upgrade head)"
uv run alembic upgrade head

cat <<'EOF'

Local environment ready.

Next steps:
  # Load an AOI (Slice 0 walking skeleton):
  uv run forest-sentinel run --aoi examples/aoi-sample.geojson

  # Run the full optical-change pipeline (needs Earth Engine credentials —
  # see DEPLOYMENT.md §3 "Configure credentials & store them safely"):
  uv run forest-sentinel run --aoi examples/aoi-sample.geojson \
      --since 2026-01-01 --until 2026-02-01

  # Launch the dashboard:
  uv run uvicorn forest_sentinel.dashboard.app:app --port 8000
EOF
