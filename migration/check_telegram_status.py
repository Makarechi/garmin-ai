"""Record the pinned Python Telegram status response for a synthetic installation."""

from capture_reference import isolated_engine
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.config import Settings
from garmin_ai.telegram import _process_message, save_update


def capture():
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            session.execute(
                text("""
                INSERT INTO health_days(day,sources) VALUES ('2026-10-24','{}'),('2026-10-25','{}')
                """)
            )
            session.execute(
                text("""
                INSERT INTO app_state(key,value) VALUES
                  ('integration:garmin','{"status":"reauth_required","reason_class":"AccountMismatch"}'),
                  ('freshness:synthetic:live','{"source_key":"live","success_at":"2026-10-25T00:30:00+00:00"}'),
                  ('freshness:historical:2000-01-02','{"source_key":"2000-01-02","success_at":"2026-11-17T12:00:00+00:00"}')
                """)
            )
            wire = {
                "update_id": 51,
                "message": {
                    "message_id": 51,
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "date": 1794916800,
                    "text": "/status",
                },
            }
            assert save_update(session, wire, 42)
        settings = Settings(telegram_user_id=42, timezone="Europe/Bratislava")
        output = _process_message(engine, None, settings, 51)
        assert output
        return output
    finally:
        engine.dispose()


if __name__ == "__main__":
    print(capture())
