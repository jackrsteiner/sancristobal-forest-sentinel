# The published app image (#96): CLI + migrations + dashboard in one container,
# built and pushed to ghcr.io/<owner>/open-forest-sentinel by CI on main.
# Instances opt in via APP_IMAGE in config/instance.env; building from source
# with uv on the VM remains the default.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# Dependency layer first: cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
# The rasterio/pyproj wheels vendor GDAL/PROJ but still link these system
# libraries, which the slim image does not guarantee (libexpat1 is genuinely
# absent; the others are pinned so a base-image change can't break the import).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 libstdc++6 zlib1g \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:${PATH}"
# Any venv entrypoint works as the command: `forest-sentinel …`,
# `alembic upgrade head`, `uvicorn forest_sentinel.dashboard.app:app …`.
CMD ["forest-sentinel", "--help"]
