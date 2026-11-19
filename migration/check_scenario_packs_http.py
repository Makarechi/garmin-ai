"""Compare scenario pack preferences through native HTTP against the Python baseline."""

import argparse
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.scenario_packs import configure_scenario_pack

ROUTE = "/scenario-packs"
TOKENS = [
    {"key": "synthetic-scenario-packs-http-owner", "scopes": ["admin"]},
    {"key": "synthetic-scenario-packs-http-reader", "scopes": ["read:diary"]},
    {"key": "synthetic-scenario-packs-http-writer", "scopes": ["read:diary", "write:diary"]},
    {
        "key": "synthetic-scenario-packs-http-settings",
        "scopes": ["read:diary", "write:diary", "manage:integrations"],
    },
    {"key": "synthetic-scenario-packs-http-health", "scopes": ["read:health"]},
]


def headers(index):
    return {} if index is None else {"Authorization": f"Bearer {TOKENS[index]['key']}"}


def compare(python, native, method, path, access, status, **options):
    reports = [
        client.request(method, path, headers=headers(access), **options)
        for client in (python, native)
    ]
    assert [response.status_code for response in reports] == [status, status], [
        (response.status_code, response.text) for response in reports
    ]
    reference, candidate = [response.json() for response in reports]
    assert reference == candidate, (reference, candidate)
    return candidate


def exercise(engine, python, native, initial):
    for access, status in ((None, 401), (4, 403), (1, 200), (0, 200)):
        received = compare(python, native, "GET", ROUTE, access, status)
        if status == 200:
            assert received == initial
    route = ROUTE + "/caffeine"
    desired = {
        "revision": 1,
        "tracking_enabled": "true",
        "collection_enabled": 0,
        "reminders_enabled": False,
        "visible": "off",
        "llm_enabled": True,
        "outcome_goal": "Synthetic preference",
    }
    for access, status in ((None, 401), (1, 403), (2, 403), (4, 403)):
        compare(python, native, "PUT", route, access, status, json=desired)
    for invalid in (
        {},
        {"revision": 0, "tracking_enabled": []},
        desired | {"tracking_enabled": 0.3, "visible": "unknown", "unexpected": True},
        desired | {"revision": 1.0, "outcome_goal": "é" * 501},
    ):
        compare(python, native, "PUT", route, 3, 422, json=invalid)
    compare(python, native, "PUT", ROUTE + "/unknown", 0, 404, json=desired)
    compare(python, native, "PUT", route, 3, 409, json=desired | {"revision": 2**70})

    with Session(engine) as session:
        reference = configure_scenario_pack(session, "caffeine", desired)
        session.rollback()
    response = native.put(route, headers=headers(3), json=desired)
    assert response.status_code == 200 and response.json() == reference, response.text
    assert reference["revision"] == 2
    assert reference["tracking_enabled"] and reference["llm_enabled"]
    assert not any(reference[key] for key in ("collection_enabled", "reminders_enabled", "visible"))
    compare(python, native, "PUT", route, 3, 409, json=desired)
    persisted = compare(python, native, "GET", ROUTE, 1, 200)
    assert persisted["packs"] == [
        reference if pack["key"] == "caffeine" else pack for pack in initial["packs"]
    ]

    with Session(engine) as session:
        next_selection = desired | {"revision": 2, "llm_enabled": False, "outcome_goal": None}
        expected = configure_scenario_pack(session, "caffeine", next_selection)
        session.rollback()
    response = native.put(route, headers=headers(0), json=next_selection)
    assert response.status_code == 200 and response.json() == expected, response.text
    assert expected["revision"] == 3 and not expected["llm_enabled"]
    assert expected["outcome_goal"] is None
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM app_state WHERE starts_with(key,'tracker-consent:')")
            )
            == 0
        )
    print("Matched pack defaults, independent settings, scopes, revision checks and Python errors")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with (
            TemporaryDirectory(prefix="synthetic-scenario-packs-") as directory,
            subprocess.Popen(
                [args.binary.resolve(strict=True), "serve", "--port", "0"],
                cwd=directory,
                env={
                    "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
                    "GA_API_TOKENS": json.dumps(TOKENS),
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as server,
        ):
            try:
                url = server.stdout.readline().strip().removeprefix("Listening on ")
                assert url.startswith("http://127.0.0.1:")
                with httpx.Client(base_url=url, timeout=5, trust_env=False) as native:
                    response = native.get(ROUTE, headers=headers(1))
                    assert response.status_code == 200, response.text
                    initial = response.json()
                    assert len(initial["packs"]) == 6
                    settings = Settings(_env_file=None, api_tokens=TOKENS)
                    with TestClient(create_app(settings, engine)) as python:
                        exercise(engine, python, native, initial)
            finally:
                server.terminate()
                server.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
