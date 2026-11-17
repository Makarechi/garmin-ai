"""Capture the Python timing-only sleep result and delivery hash on synthetic nights."""

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.models import AppState, SourcePayload, TimelineInterval
from garmin_ai.sleep_timing import main_sleep_timing, timing_result_hash

DAY = date(2026, 10, 25)
START = datetime(2026, 10, 24, 22, 15, 2, 119000, tzinfo=UTC)
END = datetime(2026, 10, 25, 6, 23, 8, tzinfo=UTC)
CASES = (
    "verified",
    "empty_attempt",
    "wrong_version",
    "missing_interval",
    "bad_uuid",
    "missing_source",
    "different_scope",
    "other_provider",
    "raw_error",
    "unconfirmed",
    "different_watermark",
    "different_hash",
    "attempt_error",
    "attempt_pending",
    "malformed_attempt",
    "parser_failure",
    "different_calendar",
    "boolean_clock",
    "time_mismatch",
    "absent_calendar",
    "string_times",
    "old_failed_optional",
    "unicode_zone",
    "malformed_metadata",
    "bad_dto",
    "wrong_interval_provider",
)


def seed(session):
    for index, case in enumerate(CASES):
        day = DAY + timedelta(days=index)
        start = START + timedelta(days=index)
        end = END + timedelta(days=index)
        identity = UUID(int=index + 1)
        reference = str(identity)
        digest = f"{index + 1:064x}"
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        dto = {
            "calendarDate": str(day),
            "sleepStartTimestampGMT": start_ms,
            "sleepEndTimestampGMT": end_ms,
        }
        if case == "different_calendar":
            dto["calendarDate"] = str(DAY - timedelta(days=1))
        if case == "absent_calendar":
            del dto["calendarDate"]
        if case == "boolean_clock":
            dto["sleepStartTimestampGMT"] = True
        if case == "string_times":
            dto["sleepStartTimestampGMT"] = start.isoformat().replace("+00:00", "Z")
            dto["sleepEndTimestampGMT"] = end.isoformat().removesuffix("+00:00")
        if case != "missing_source":
            session.add(
                SourcePayload(
                    id=identity,
                    source="garmin_personal" if case == "other_provider" else "garmin_connect",
                    endpoint="sleep",
                    source_key=str(day if case != "different_scope" else DAY),
                    parser_version=8 if case == "wrong_version" else 9,
                    status="error" if case == "raw_error" else "normalized",
                    payload_hash=digest,
                    payload={"dailySleepDTO": [] if case == "bad_dto" else dto},
                    archive_key=f"{digest[:2]}/{digest}.json",
                    fetched_at=end,
                )
            )
        if case != "missing_interval":
            session.add(
                TimelineInterval(
                    id=f"sleep:{day}",
                    start=start + timedelta(seconds=case == "time_mismatch"),
                    end=end,
                    label="sleep",
                    source=(
                        "garmin_personal" if case == "wrong_interval_provider" else "garmin_connect"
                    ),
                    confirmed=case != "unconfirmed",
                    confidence=1,
                    evidence={"source_ref": "invalid" if case == "bad_uuid" else reference},
                )
            )
        state = {
            "hash": "0" * 64 if case == "different_hash" else digest,
            "source_ref": str(UUID(int=0)) if case == "different_watermark" else reference,
            "status": "empty" if case == "empty_attempt" else "normalized",
        }
        if case.startswith("attempt_"):
            state["latest_attempt"] = {"status": case.removeprefix("attempt_")}
        if case == "malformed_attempt":
            state["latest_attempt"] = []
        session.add(AppState(key=f"ingest:garmin_connect:sleep:{day}", value=state))
        if case in {"parser_failure", "old_failed_optional", "malformed_metadata"}:
            metadata = (
                []
                if case == "malformed_metadata"
                else {"failed_parser_version": 9 if case == "parser_failure" else 8}
            )
            session.add(AppState(key=f"ingest-meta:{reference}", value=metadata))


def capture(output):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            seed(session)
        rows = []
        with Session(engine) as session, session.begin():
            for index, case in enumerate(CASES):
                day = DAY + timedelta(days=index)
                timezone = "Café\u007f🧪" if case == "unicode_zone" else "Europe/Bratislava"
                result = main_sleep_timing(session, day, timezone)
                rows.append(
                    {
                        "case": case,
                        "day": str(day),
                        "timezone": timezone,
                        "result": result,
                        "hash": timing_result_hash(result),
                    }
                )
        output.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        print(f"Captured {len(rows)} synthetic main sleep decisions and hashes")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
