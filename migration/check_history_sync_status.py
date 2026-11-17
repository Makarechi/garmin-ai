"""Compare history status from the pinned application and the native read layer."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.backfill import history_status
from garmin_ai.models import AppState, Job

CLOCK = datetime(2026, 10, 25, 9, tzinfo=UTC)
RUNNING = UUID("11111111-1111-1111-1111-111111111111")
FAILED = UUID("22222222-2222-2222-2222-222222222222")
PRIVATE = "synthetic-account-hidden-from-status"


def seed(engine):
    with Session(engine) as session, session.begin():
        for identity, status in [(RUNNING, "running"), (FAILED, "failed")]:
            session.add(
                Job(
                    id=identity,
                    kind="garmin_endpoint",
                    payload={},
                    dedup_key=f"synthetic:{status}",
                    status=status,
                    run_at=CLOCK,
                )
            )
        session.flush()
        session.execute(
            text("""
            INSERT INTO app_state(key,value)
            SELECT concat('syncwindow:synthetic:',number), jsonb_build_object(
                'status', CASE number % 5 WHEN 0 THEN 'empty' WHEN 1 THEN 'superseded'
                            ELSE 'pending' END,
                'job_id', CASE number % 5 WHEN 2 THEN :running WHEN 3 THEN :failed END,
                'date', DATE '2026-10-25' - (number % 365),
                'completed_at', CASE WHEN number % 5 IN (0,1) THEN :at END,
                'source_status', CASE number % 5 WHEN 0 THEN 'empty' WHEN 1 THEN 'archived' END
            )
            FROM generate_series(0,7299) AS series(number)
            """),
            {"running": str(RUNNING), "failed": str(FAILED), "at": CLOCK.isoformat()},
        )
        session.add_all(
            [
                AppState(
                    key=f"activity_scan:{PRIVATE}:recent",
                    value={
                        "account": PRIVATE,
                        "anchor": [PRIVATE],
                        "scan_key": PRIVATE,
                        "status": "running",
                        "offset": 80,
                        "pages": 1,
                        "job_id": str(RUNNING),
                    },
                ),
                AppState(
                    key=f"activity_scan:{PRIVATE}:history",
                    value={
                        "account": PRIVATE,
                        "anchor": [PRIVATE],
                        "scan_key": PRIVATE,
                        "status": "running",
                        "offset": 0,
                        "pages": 3,
                        "job_id": "33333333-3333-3333-3333-333333333333",
                    },
                ),
                AppState(
                    key="activity_scan:previous:history",
                    value={"status": "complete", "pages": 4, "account": PRIVATE},
                ),
                AppState(
                    key=f"syncplan:daily:{PRIVATE}:10:initial",
                    value={
                        "account": PRIVATE,
                        "horizon_days": 10,
                        "history_start": "2026-10-15",
                        "right_through": "2026-10-24",
                    },
                ),
                AppState(
                    key=f"syncplan:daily:{PRIVATE}:365:expanded",
                    value={
                        "account": PRIVATE,
                        "horizon_days": 365,
                        "history_start": "2025-10-25",
                        "right_through": "2026-10-25",
                    },
                ),
            ]
        )


def run(binary):
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "history-sync.json"
            for populated in [False, True]:
                if populated:
                    seed(engine)
                with Session(engine) as session:
                    expected = history_status(session)
                if populated:
                    assert sum(expected["windows"].values()) == 7300
                    assert expected["windows"]["needs_attention"] == 1460
                    assert PRIVATE not in json.dumps(expected)
                path.write_text(json.dumps(expected), encoding="utf-8")
                subprocess.run(
                    [binary, "--ignored"],
                    check=True,
                    env={**os.environ, "GA_TEST_HISTORY_SYNC": str(path)},
                )
        print("Matched empty status and 7,300 history windows against Python")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
