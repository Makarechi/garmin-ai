from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from garmin_ai.config import Settings


def make_engine(settings: Settings | None = None):
    url = (settings or Settings()).database_url.get_secret_value()
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("GA_DATABASE_URL must use postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)


@contextmanager
def transaction(engine):
    with Session(engine, expire_on_commit=False) as session, session.begin():
        yield session


SCHEMA_REVISION = "4c9e28f110ab"
