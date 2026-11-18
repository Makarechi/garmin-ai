"""Compare native diary downloads with the pinned Python HTTP API."""

import argparse
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.events import EventInput, create_event

DIARY = "synthetic-export-diaries-key-0000"
HEALTH = "synthetic-export-analysis-key-000"
START = datetime(2026, 10, 25, tzinfo=UTC)
END = START + timedelta(days=1)
ORIGINAL = "synthetic original input excluded from diary download"


def seed(engine):
    with Session(engine) as session, session.begin():
        for offset, payload in [
            (timedelta(days=-2), {"type": "migraine", "severity": 4}),
            (
                timedelta(minutes=30),
                {"type": "note", "description": 'Synthetic "note" ☕\nline two'},
            ),
            (timedelta(hours=1, minutes=30), {"type": "caffeine", "beverage": "synthetic"}),
            (timedelta(days=1), {"type": "note", "description": "Excluded at the end boundary"}),
        ]:
            create_event(
                session,
                EventInput(
                    start=START + offset,
                    timezone="Europe/Bratislava",
                    payload=payload,
                    original_text=ORIGINAL,
                ),
                actor="synthetic-fixture",
                idempotency_key=f"synthetic-export-hidden:{offset}",
            )


def seed_limit_cases(engine):
    with engine.begin() as connection:
        for day, count, description in [
            ("2026-01-02T12:00:00Z", 1001, "small synthetic note"),
            ("2026-01-04T12:00:00Z", 300, "é" * 4000),
        ]:
            connection.execute(
                text("""
                    INSERT INTO events(id,kind,start,timezone,source,confidence,status,payload,revision,deleted)
                    SELECT gen_random_uuid(),'note',CAST(:day AS timestamptz),'UTC','manual',1,'confirmed',
                           jsonb_build_object('type','note','description',CAST(:description AS text)),1,false
                    FROM generate_series(1, CAST(:count AS int))
                """),
                {"day": day, "count": count, "description": description},
            )


def verify(engine, binary, directory):
    tokens = [
        ApiToken(key=DIARY, scopes=["read:diary"]),
        ApiToken(key=HEALTH, scopes=["read:health"]),
    ]
    seed(engine)
    oracle = TestClient(create_app(Settings(_env_file=None, api_tokens=tokens), engine))
    environment = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_API_TOKENS": json.dumps(
            [
                token.model_dump(mode="json") | {"key": key}
                for token, key in zip(tokens, (DIARY, HEALTH), strict=True)
            ]
        ),
    }
    with subprocess.Popen(
        [binary, "serve", "--port", "0"],
        cwd=directory,
        env=environment,
        stdout=subprocess.PIPE,
        text=True,
    ) as server:
        try:
            prefix = "Listening on "
            line = server.stdout.readline().strip()
            assert line.startswith(prefix)
            with httpx.Client(base_url=line.removeprefix(prefix), timeout=6) as native:
                base = {"start": START.isoformat(), "end": END.isoformat()}

                def compare(method, parameters, key, status):
                    headers = {"Authorization": f"Bearer {key}"} if key else {}
                    results = [
                        client.request(method, "/exports/diary", params=parameters, headers=headers)
                        for client in (oracle, native)
                    ]
                    old, new = results
                    assert old.status_code == new.status_code == status, (old.text, new.text)
                    for header in (
                        "content-type",
                        "content-disposition",
                        "x-content-type-options",
                        "allow",
                        "cache-control",
                    ):
                        assert old.headers.get(header) == new.headers.get(header), header
                    if parameters.get("format") == "csv" and status == 200:
                        assert old.content == new.content, (old.text, new.text)
                    elif method != "HEAD":
                        assert old.json() == new.json(), (old.text, new.text)
                    assert ORIGINAL not in new.text
                    assert "synthetic-export-hidden:" not in new.text
                    return new

                compare("GET", base, None, 401)
                compare("GET", base, HEALTH, 403)
                compare("HEAD", base, DIARY, 405)
                compare("GET", {}, DIARY, 422)
                compare("GET", base | {"start": "invalid"}, DIARY, 422)
                compare("GET", base | {"start": "2026-10-25", "format": "tsv"}, DIARY, 422)
                compare("GET", base | {"timezone": "Unknown/Zone"}, DIARY, 422)
                compare("GET", base | {"end": START.isoformat()}, DIARY, 422)
                compare("GET", base | {"end": (START + timedelta(days=32)).isoformat()}, DIARY, 422)
                compare("GET", base | {"start": "0", "end": "1"}, DIARY, 200)
                expected = compare("GET", base, DIARY, 200).json()
                for zone in ("Europe/Bratislava", "America/New_York", "UTC"):
                    query = base | {"timezone": zone}
                    report = compare("GET", query, DIARY, 200).json()
                    assert len(report["rows"]) == 3
                    assert any(row["missing_end"] for row in report["rows"])
                    compare("GET", query | {"format": "csv"}, DIARY, 200)
                    if zone == "Europe/Bratislava":
                        assert report == expected
                        display = {row["display_start"] for row in report["rows"]}
                        assert {"2026-10-25T02:30:00+02:00", "2026-10-25T02:30:00+01:00"} <= display
                seed_limit_cases(engine)
                for day in (2, 4):
                    lower = datetime(2026, 1, day, tzinfo=UTC)
                    query = {
                        "start": lower.isoformat(),
                        "end": (lower + timedelta(days=1)).isoformat(),
                    }
                    compare("GET", query, DIARY, 422)
        finally:
            server.terminate()
            assert server.wait(timeout=5) == 0
    print("Matched diary export permissions, range, daylight saving, CSV and record/byte limits")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as directory:
            verify(engine, args.binary.resolve(strict=True), Path(directory))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
