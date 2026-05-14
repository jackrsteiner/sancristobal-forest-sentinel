"""Database connectivity for Open Forest Sentinel.

The prototype runs PostgreSQL + PostGIS (see ``docker-compose.yml``). The
connection URL is read from the ``FOREST_SENTINEL_DATABASE_URL`` environment
variable, falling back to the local development default.
"""

import os

from sqlalchemy import Engine, create_engine

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://forest_sentinel:forest_sentinel@localhost:5432/forest_sentinel"
)

DATABASE_URL_ENV_VAR = "FOREST_SENTINEL_DATABASE_URL"


def get_database_url() -> str:
    """Return the configured database URL, or the local development default."""
    return os.environ.get(DATABASE_URL_ENV_VAR, DEFAULT_DATABASE_URL)


def get_engine() -> Engine:
    """Create a SQLAlchemy engine for the configured database."""
    return create_engine(get_database_url())
