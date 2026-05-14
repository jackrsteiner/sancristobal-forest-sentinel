from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from forest_sentinel.db import get_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def alembic_config() -> Config:
    """Alembic configuration loaded from the project's alembic.ini."""
    return Config(str(PROJECT_ROOT / "alembic.ini"))


@pytest.fixture
def db_engine() -> Iterator[Engine]:
    """Engine for the configured database; skips the test if it is unreachable."""
    engine = get_engine()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError:
        engine.dispose()
        pytest.skip("PostgreSQL is not reachable; run `docker compose up -d`")
    yield engine
    engine.dispose()


@pytest.fixture
def clean_database(alembic_config: Config, db_engine: Engine) -> Iterator[Engine]:
    """Provide a database downgraded to base before and after the test."""
    command.downgrade(alembic_config, "base")
    yield db_engine
    command.downgrade(alembic_config, "base")
