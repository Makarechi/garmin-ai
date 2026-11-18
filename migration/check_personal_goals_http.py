"""Compare goal preference routes against pinned Python with synthetic accounts."""

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.models import AppState
from garmin_ai.personal_goals import KEY

ROUTE = "/preferences/goals"
TOKENS = {name: f"synthetic-goal-tests-{name}-" + "a" * 32 for name in ("read", "write", "health")}
SCOPED = [
    {"key": TOKENS["read"], "scopes": ["read:diary"]},
    {"key": TOKENS["write"], "scopes": ["read:diary", "write:diary"]},
    {"key": TOKENS["health"], "scopes": ["read:health"]},
]


def run(binary):
    reference = isolated_engine()
    with patch.dict(
        os.environ, {"GA_TEST_DATABASE_URL": os.environ["GA_TEST_DATABASE_NATIVE_URL"]}
    ):
        native_database = isolated_engine()
    try:
        with (
            TestClient(
                create_app(Settings(api_tokens=SCOPED, api_key=""), engine=reference)
            ) as python,
            subprocess.Popen(
                [binary, "serve", "--port", "0"],
                env={
                    "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_NATIVE_URL"],
                    "GA_API_TOKENS": json.dumps(SCOPED),
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as process,
        ):
            try:
                line = process.stdout.readline().strip()
                assert line.startswith("Listening on http://127.0.0.1:"), line
                with httpx.Client(base_url=line.split(" on ", 1)[1], trust_env=False) as native:
                    cases = [
                        ("GET", "read", None),
                        ("GET", "write", None),
                        ("GET", None, None),
                        ("GET", "health", None),
                        ("DELETE", "write", None),
                        ("PUT", "read", {"revision": 0, "goals": ["sleep"]}),
                        ("PUT", None, {"revision": 0, "goals": ["sleep"]}),
                    ]
                    cases += [
                        ("PUT", "write", body)
                        for body in [
                            None,
                            {},
                            [],
                            {"revision": True, "goals": []},
                            {"revision": "1", "goals": []},
                            {"revision": -1, "goals": []},
                            {"revision": 0, "goals": None},
                            {"revision": 0, "goals": ["sleep", "sleep"]},
                            {"revision": 0, "goals": ["future", 5]},
                            {"revision": 0, "goals": [1, 2, 3, 4, 5]},
                            {"revision": 0, "goals": [], "extra": 1},
                            {"revision": 0, "goals": ["wellbeing", "running"]},
                            {"revision": 0, "goals": []},
                            {"revision": 1, "goals": ["running", "wellbeing"]},
                            {"revision": 1, "goals": []},
                            {"revision": 1, "goals": ["sleep"]},
                        ]
                    ]
                    cases.append(("GET", "read", None))
                    for method, auth, body in cases:
                        compare(python, native, method, auth, body)
                    for engine in (reference, native_database):
                        with Session(engine) as session, session.begin():
                            row = session.get(AppState, KEY)
                            assert row.value["owner_id"]
                            row.value = {
                                key: value for key, value in row.value.items() if key != "owner_id"
                            }
                    compare(python, native, "GET", "read", None)
                    for engine in (reference, native_database):
                        with Session(engine) as session:
                            assert session.scalar(
                                select(AppState).where(AppState.key == KEY)
                            ).value["owner_id"]
                    print(
                        f"Matched {len(cases) + 1} HTTP goal preference responses and legacy owner binding"
                    )
            finally:
                process.terminate()
    finally:
        native_database.dispose()
        reference.dispose()


def compare(python, native, method, auth, body):
    headers = {"Authorization": "Bearer " + TOKENS[auth]} if auth else {}
    payload = {"json": body} if body is not None else {}
    actual = native.request(method, ROUTE, headers=headers, **payload)
    options = {}
    if method == "PUT" and actual.status_code == 200:
        at = datetime.fromisoformat(actual.json()["updated_at"])

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None, *, instant=at):
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

        options["datetime"] = Clock
    with (
        patch.multiple("garmin_ai.personal_goals", **options)
        if options
        else patch.dict(os.environ, {})
    ):
        expected = python.request(method, ROUTE, headers=headers, **payload)
    assert actual.status_code == expected.status_code, (
        method,
        auth,
        body,
        actual.text,
        expected.text,
    )
    assert actual.json() == expected.json(), (method, auth, body, actual.text, expected.text)
    for name in ("cache-control", "allow"):
        assert actual.headers.get(name) == expected.headers.get(name), (method, auth, body, name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
