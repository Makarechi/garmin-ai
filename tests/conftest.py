import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from garmin_ai.models import Base


def test_database_url():
    url = os.environ.get("GA_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GA_REQUIRE_TEST_DB") == "1":
            raise pytest.UsageError("GA_TEST_DATABASE_URL is required for database validation")
        return None
    try:
        parsed = make_url(url)
    except Exception:
        raise pytest.UsageError("Invalid test database URL") from None
    if parsed.query:
        raise pytest.UsageError("Test database URLs must not contain query parameters")
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").endswith("_test"):
        raise pytest.UsageError("Tests require a dedicated PostgreSQL database ending in _test")
    return url


def pytest_configure(config):
    test_database_url()


@pytest.fixture(scope="session")
def db_engine():
    url = test_database_url()
    if not url:
        pytest.skip("Set GA_TEST_DATABASE_URL to a dedicated PostgreSQL database ending in _test")
    engine = create_engine(url, hide_parameters=True)
    with engine.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine):
    names = ", ".join('"' + t.name + '"' for t in Base.metadata.sorted_tables)
    with db_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    with Session(db_engine, expire_on_commit=False) as session:
        yield session
        session.rollback()
