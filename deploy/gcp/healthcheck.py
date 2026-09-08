"""Check the committed worker heartbeat without importing the application stack."""

import os

import psycopg


def main():
    url = os.environ.get("GA_DATABASE_URL", "")
    if not url.startswith("postgresql+psycopg://"):
        return 1
    try:
        with psycopg.connect(
            url.replace("postgresql+psycopg://", "postgresql://", 1),
            connect_timeout=5,
            options="-c statement_timeout=5000",
        ) as connection:
            ready = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM app_state
                    WHERE key = 'runtime:heartbeat'
                      AND (value->>'at')::timestamptz <= now()
                      AND (value->>'at')::timestamptz > now() - interval '180 seconds'
                ) AND NOT EXISTS (
                    SELECT 1 FROM app_state WHERE key = 'maintenance:erased'
                )
                """
            ).fetchone()[0]
        return 0 if ready else 1
    except Exception:
        # Never include connection details or private state in health logs.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
