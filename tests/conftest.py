import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from garmin_ai.models import Base


@pytest.fixture(scope="session")
def db_engine():
    url = os.environ.get("GA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set GA_TEST_DATABASE_URL to a dedicated PostgreSQL database ending in _test")
    engine = create_engine(url, hide_parameters=True)
    if not engine.url.database.endswith("_test"):
        raise ValueError("Refusing to run destructive tests outside a dedicated _test database")
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
