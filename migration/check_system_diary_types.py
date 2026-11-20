"""Compare ordinary HTTP creation and cross-runtime reads for system diary events."""

import argparse
import json
import selectors
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from capture_reference import isolated_engine
from check_diary_http import TOKENS, headers
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.models import Activity
from garmin_ai.scenario_packs import PackSelection, configure_scenario_pack, ensure_scenario_packs

START = "2026-10-25T02:30:00+02:00"
END = "2026-10-25T02:30:00+01:00"
ZONE = "Europe/Bratislava"
CONTEXT_TYPES = (
    "alcohol",
    "meal",
    "hydration",
    "illness",
    "nap",
    "stressor",
    "travel",
    "mood",
    "note",
    "context",
    "caffeine_absence",
    "caffeine_log_complete",
)
CASES = [
    ({"type": "migraine", "notes": "Synthetic episode"}, {"end": END}),
    ({"type": "migraine", "aura": False}, {}),
    ({"type": "symptom_observation", "severity": 2}, {}),
    ({"type": "medication"}, {}),
    ({"type": "medication", "name": "Synthetic name", "reason_event_id": None}, {}),
    (
        {
            "type": "caffeine",
            "beverage": "Synthetic coffee",
            "servings": 2,
            "dose_basis": "per_serving",
            "caffeine_mg_estimate": 40,
        },
        {},
    ),
    ({"type": "caffeine", "beverage": "Synthetic uncertainty", "caffeine_mg_max": 12}, {}),
    ({"type": "activity_effort", "activity_id": "synthetic", "perceived_exertion": 4}, {}),
    ({"type": "headache_observation", "headache": "unknown", "migraine": "no"}, {"end": END}),
    ({"type": "wellbeing_observation", "energy": 3}, {}),
] + [
    (
        {"type": kind, "description": "Synthetic diary value", "tags": ["synthetic"]},
        {"end": END}
        if kind
        in {
            "illness",
            "nap",
            "travel",
            "stressor",
            "context",
            "caffeine_absence",
            "caffeine_log_complete",
        }
        else {},
    )
    for kind in CONTEXT_TYPES
]


def seed(engine):
    with Session(engine) as session, session.begin():
        at = datetime.fromisoformat(START)
        session.add(
            Activity(
                id="synthetic",
                kind="running",
                start=at - timedelta(minutes=30),
                end=at,
                timezone=ZONE,
            )
        )
        for key, row in ensure_scenario_packs(session, legacy_install=False).items():
            configure_scenario_pack(
                session,
                key,
                PackSelection(
                    revision=row.revision,
                    tracking_enabled=True,
                    collection_enabled=row.collection_enabled,
                    reminders_enabled=row.reminders_enabled,
                    visible=row.visible,
                    llm_enabled=row.llm_enabled,
                    outcome_goal=row.outcome_goal,
                ),
            )


def verify_coverage_rejections(python, native, engine):
    def counts():
        with engine.connect() as connection:
            return connection.execute(
                text("SELECT (SELECT count(*) FROM events), (SELECT count(*) FROM audit_log)")
            ).one()

    before = counts()
    number = 0
    for payload, _ in CASES:
        if payload["type"] not in {
            "headache_observation",
            "caffeine_absence",
            "caffeine_log_complete",
        }:
            continue
        for closings in ({}, {"end": None}, {"end": START}, {"end": "2026-10-24T02:30:00+02:00"}):
            event = {"start": START, "timezone": ZONE, "payload": payload, **closings}
            edit = {"revision": 1, "event": event}
            for method, route, data in (
                ("POST", "/events", event),
                ("PUT", "/events/00000000-0000-0000-0000-000000000018", edit),
            ):
                access = headers("both", f"synthetic-invalid-coverage-{number}")
                expected, actual = (
                    client.request(method, route, headers=access, json=data)
                    for client in (python, native)
                )
                assert expected.status_code == actual.status_code == 422
                assert expected.json() == actual.json(), (data, expected.json(), actual.json())
                number += 1
    assert number == 24 and counts() == before
    print("Matched 24 incomplete coverage diagnostics and left diary records unchanged")


def exercise(python, native, engine):
    verify_coverage_rejections(python, native, engine)
    completed = set()
    for label, first, second in (("python", python, native), ("rust", native, python)):
        episode = None
        for index, (payload, options) in enumerate(CASES):
            kind = payload["type"]
            payload = payload.copy()
            if kind == "symptom_observation":
                payload["episode_id"] = episode
            if "reason_event_id" in payload:
                payload["reason_event_id"] = episode
            body = {"start": START, "timezone": ZONE, "payload": payload, **options}
            access = headers("both", f"synthetic-system-diary-{label}-{index}")
            saved = first.post("/events", headers=access, json=body)
            replay = second.post("/events", headers=access, json=body)
            assert saved.status_code == replay.status_code == 200, (
                label,
                kind,
                saved.status_code,
                saved.text,
                replay.status_code,
                replay.text,
            )
            event = saved.json()
            assert event == replay.json(), (label, kind)
            assert event["payload"]["type"] == kind
            if kind == "migraine" and options:
                episode = event["id"]
            for client in (python, native):
                reread = client.get(f"/events/{event['id']}", headers=headers("reader"))
                assert reread.status_code == 200 and reread.json() == event, (label, kind)
            if kind == "caffeine":
                expected = 80 if "servings" in payload else None
                assert event["caffeine_total"]["estimate"] == expected
            if kind == "medication":
                assert event["payload"]["dose"] is None
            completed.add(kind)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM events")) == len(CASES) * 2
        assert (
            connection.scalar(
                text("SELECT count(*) FROM audit_log WHERE action='create' AND actor='api'")
            )
            == len(CASES) * 2
        )
    assert len(completed) == 19
    print(
        f"Matched {len(CASES) * 2} paired creates, retries and reads across all {len(completed)} system diary types"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    engine = isolated_engine()
    try:
        seed(engine)
        environment = {
            "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
            "GA_API_TOKENS": json.dumps(TOKENS),
        }
        settings = Settings(_env_file=None, api_tokens=TOKENS, api_key="")
        with (
            TestClient(create_app(settings, engine)) as python,
            TemporaryDirectory() as root,
            subprocess.Popen(
                [binary, "serve", "--port", "0"],
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                text=True,
            ) as native,
        ):
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(native.stdout, selectors.EVENT_READ)
                    assert selector.select(timeout=5)
                line = native.stdout.readline().strip()
                assert line.startswith("Listening on http://127.0.0.1:")
                with httpx.Client(
                    base_url=line.removeprefix("Listening on "), trust_env=False
                ) as client:
                    exercise(python, client, engine)
            finally:
                native.terminate()
                native.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
