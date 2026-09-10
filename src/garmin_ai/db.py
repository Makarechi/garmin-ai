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


SCHEMA_REVISION = "a637902bf114"


@contextmanager
def exclusive_ingestion(engine, *, allow_erased=False):
    """Coordinate standalone probes with workers and erasure for their full lifetime."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        if not conn.scalar(text("SELECT pg_try_advisory_lock(72104620)")):
            raise ValueError("Stop the worker or other probe before standalone ingestion")
        try:
            if (
                not allow_erased
                and conn.scalar(text("SELECT to_regclass('app_state')"))
                and conn.scalar(text("SELECT 1 FROM app_state WHERE key='maintenance:erased'"))
            ):
                raise MaintenanceMode("Restore or resume storage before ingestion")
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(72104620)"))
