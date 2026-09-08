from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from garmin_ai.config import Settings
from garmin_ai.models import AppState


def make_engine(settings: Settings | None = None):
    url = (settings or Settings()).database_url.get_secret_value()
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("GA_DATABASE_URL must use postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)


class MaintenanceMode(RuntimeError):
    pass


def writer_guard(session):
    session.execute(text("SELECT pg_advisory_xact_lock_shared(72104622)"))
    if session.get(AppState, "maintenance:erased", populate_existing=True):
        raise MaintenanceMode("Storage is disabled after erasure")


@contextmanager
def transaction(engine):
    with Session(engine, expire_on_commit=False) as session, session.begin():
        writer_guard(session)
        yield session


SCHEMA_REVISION = "4c9e28f110ab"
