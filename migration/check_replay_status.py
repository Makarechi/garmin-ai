"""Capture pinned replay readiness and progress through a bounded synthetic history."""

import argparse
import json
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai import replay

RETAINED = "00000000-0000-4000-8000-000000001c84"
OWNER_KEY = "sample-owner:synthetic-replay-status"


def seed(engine):
    with engine.begin() as connection:
        for statement in (
            """
            INSERT INTO source_payloads (
                id,source,endpoint,source_key,payload_hash,payload,archive_key,parser_version,status
            )
            SELECT ('00000000-0000-4000-8000-' || lpad(to_hex(n),12,'0'))::uuid,
                   'garmin_connect','daily','synthetic-day-' || (n-1)%365,
                   lpad(to_hex(n),64,'0'),'{}'::jsonb,'synthetic-archive',
                   CASE WHEN n>7300 THEN 9 ELSE 8 END,'normalized'
            FROM generate_series(1,7665) AS n
            """,
            """
            INSERT INTO app_state (key,value)
            SELECT 'ingest:garmin_connect:daily:' || source_key,
                   jsonb_build_object('source_ref',id,'requested_at',fetched_at)
            FROM source_payloads WHERE parser_version=9
            """,
            """
            INSERT INTO measurements (ts,metric,source,local_date,value,unit,source_ref,quality,details)
            SELECT ts, 'heart_rate_bpm', 'garmin_connect', ts::date, 70, 'bpm',
                   '00000000-0000-4000-8000-000000001c85'::uuid, 'observed', '{}'::jsonb
            FROM generate_series(
                   '2026-11-01T00:00:00Z'::timestamptz,
                   '2026-11-01T00:00:00Z'::timestamptz + interval '52559 minutes',
                   interval '1 minute'
            ) AS ts
            """,
            """
            INSERT INTO jobs (id,kind,payload,dedup_key,status,run_at,attempts)
            VALUES (gen_random_uuid(),'raw_replay',jsonb_build_object('target_version',9),
                      'synthetic-ready-progress-1','pending',now(),0),
                   (gen_random_uuid(),'raw_replay',jsonb_build_object('target_version',9),
                      'synthetic-ready-progress-2','done',now(),1),
                   (gen_random_uuid(),'raw_replay',jsonb_build_object('target_version',8),
                      'synthetic-ready-progress-3','failed',now(),8)
            """,
            """
            INSERT INTO app_state (key,value) VALUES
              ('replay:synthetic-one:9',jsonb_build_object('target_version',9,'status','normalized')),
              ('replay:synthetic-two:9',jsonb_build_object('target_version',9,'status','superseded_revision')),
              ('replay:synthetic-three:8',jsonb_build_object('target_version',8,'status','error'))
            """,
            "INSERT INTO health_days(day,steps,sources) VALUES ('2026-10-25',123,'{}')",
        ):
            connection.execute(text(statement))


def capture(output):
    engine = isolated_engine()
    try:
        seed(engine)
        with Session(engine) as session:
            base = replay.replay_status(session)
            session.execute(
                text(
                    "INSERT INTO app_state (key,value) VALUES "
                    "(:key,jsonb_build_object('source_ref',cast(:ref as text)))"
                ),
                {"key": OWNER_KEY, "ref": RETAINED},
            )
            pending = replay.replay_status(session)
            session.execute(
                text("UPDATE source_payloads SET parser_version=9 WHERE id=:ref"),
                {"ref": RETAINED},
            )
            completed = replay.replay_status(session)
            assert base["ready"] and completed["ready"] and not pending["ready"]
            session.rollback()
        output.write_text(json.dumps([base, pending, completed]), encoding="utf-8")
        print("Captured replay readiness with 7300 old raws and 52560 measurements")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
