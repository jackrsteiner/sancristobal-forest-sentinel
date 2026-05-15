#!/usr/bin/env bash
# Prepare a Codespace / dev container to run the walking skeleton.
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="${HOME}/.local/bin:${PATH}"

uv sync
uv run alembic upgrade head

echo "Dev container ready. Run: uv run forest-sentinel run --aoi examples/aoi-sample.geojson"
