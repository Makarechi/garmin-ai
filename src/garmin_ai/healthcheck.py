"""Exit status for container supervision; no personal data in output."""

from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.observability import snapshot


def main():
    engine = make_engine(Settings())
    try:
        with transaction(engine) as session:
            age = snapshot(session)["runtime_heartbeat_age_seconds"]
        return 0 if age is not None and 0 <= age < 180 else 1
    except Exception:
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
