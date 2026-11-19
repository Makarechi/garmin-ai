"""Verify native delivery of a real Python web sleep reply from synthetic evidence."""

import argparse
import json
import os
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import httpx
from capture_reference import isolated_engine
from check_web_chat_read import API_TOKENS, HEADERS, ROUTE, verify_receipts
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.agent import AgentStep, Interpretation, ReadCall, SafetyScreen, TimestampClaim
from garmin_ai.api import create_app
from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.ingest import ingest
from garmin_ai.personal_goals import GoalSelection, select_goals
from garmin_ai.scenario_packs import ensure_scenario_packs

DAY = "2026-10-16"
NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
ZONE = "Europe/Bratislava"


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


class SleepPlanner:
    def structured(self, instruction, prompt, schema):
        if schema is Interpretation:
            return Interpretation(intent="question", confidence=1, response_language="ru")
        if schema is SafetyScreen:
            return SafetyScreen(urgent=False)
        assert schema is AgentStep
        evidence = json.loads(prompt)["evidence"]
        if evidence:
            start = evidence[0]["result"]["rows"][0]["main_interval"]["start"]
            claim = TimestampClaim(
                evidence_id=1, path=["rows", 0, "main_interval", "start"], value=start
            )
            return AgentStep(response_language="ru", evidence_ids=[1], timestamp_claims=[claim])
        arguments = json.dumps({"start": DAY, "end": DAY, "timing_only": True})
        return AgentStep(calls=[ReadCall(name="analysis_sleep", arguments_json=arguments)])

    def close(self):
        pass


def history(client):
    result = client.get(ROUTE, headers=HEADERS)
    assert result.status_code == 200, result.text
    return result.json()


def read(client, identity, status=200):
    result = client.post(f"{ROUTE}/{identity}/read", headers=HEADERS)
    assert result.status_code == status, result.text
    if status == 200:
        assert result.json() == {"status": "read"}


def change(engine, statement, **values):
    with engine.begin() as connection:
        connection.execute(text(statement), values)


def save(engine, key, value):
    change(
        engine,
        "INSERT INTO app_state(key,value) VALUES (:key,CAST(:value AS jsonb)) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        key=key,
        value=json.dumps(value),
    )


def provision(engine, python):
    with TemporaryDirectory() as archive, Session(engine) as session, session.begin():
        sleep = ensure_scenario_packs(session, legacy_install=True)["sleep"]
        sleep.collection_enabled = sleep.llm_enabled = True
        result = ingest(
            session,
            LocalArchive(Path(archive)),
            "sleep",
            DAY,
            {
                "dailySleepDTO": {
                    "calendarDate": DAY,
                    "sleepStartTimestampGMT": 1792100557000,
                    "sleepEndTimestampGMT": 1792128612000,
                }
            },
            ZONE,
        )
        assert result["status"] == "normalized", result
    message = {"client_message_id": str(uuid4()), "text": "Когда я заснул сегодня?"}
    result = python.post(ROUTE, json=message, headers=HEADERS)
    assert result.status_code == 200, result.text
    reply = result.json()["reply"]
    assert reply and "15.10.2026 в 23:42" in reply["text"], result.json()
    return message, reply["id"]


def compare(engine, python, native, message, identity):
    def visible():
        expected = history(python)
        assert [reply["id"] for reply in expected["replies"]] == [identity]
        assert history(native) == expected
        return expected["replies"][0]

    def hidden():
        assert history(native) == history(python)
        assert not history(native)["replies"]
        read(python, identity, 409)
        read(native, identity, 409)

    assert visible()["state"] == "queued"
    duplicate = native.post(ROUTE, json=message, headers=HEADERS)
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True, duplicate.text
    assert duplicate.json()["reply"] == visible()
    verify_receipts(engine, ())
    with engine.begin() as replay:
        replay.execute(text("SELECT pg_advisory_xact_lock(72104619)"))
        assert visible()["state"] == "queued"
        read(native, identity)
        read(python, identity)
        assert visible()["state"] == "read"
    verify_receipts(engine, (identity,))
    save(engine, "replay:generation", {"id": "synthetic-other-generation"})
    assert visible()["state"] == "read"
    with engine.begin() as forgetting:
        forgetting.execute(text("SELECT pg_advisory_xact_lock(72104643)"))
        hidden()
    visible()

    with Session(engine) as session, session.begin():
        ensure_scenario_packs(session)["sleep"].llm_enabled = False
    hidden()
    change(engine, "UPDATE module_configs SET llm_enabled=true WHERE pack_key='sleep'")
    visible()
    save(engine, "preferences:onboarding", {"model_categories": ["diary"]})
    hidden()
    save(engine, "preferences:onboarding", {"model_categories": ["diary", "health"]})
    visible()
    change(engine, "UPDATE people SET timezone='UTC' WHERE singleton")
    hidden()
    change(engine, "UPDATE people SET timezone=:zone WHERE singleton", zone=ZONE)
    visible()
    change(engine, "UPDATE timeline_intervals SET confirmed=false WHERE id=:id", id=f"sleep:{DAY}")
    hidden()
    change(engine, "UPDATE timeline_intervals SET confirmed=true WHERE id=:id", id=f"sleep:{DAY}")
    visible()
    save(engine, "analysis:conversation", {"epochs": {"web:local": str(uuid4())}})
    hidden()
    save(engine, "analysis:conversation", {"epoch": str(uuid4()), "epochs": {"web:local": None}})
    visible()
    change(engine, "DELETE FROM app_state WHERE key='analysis:conversation'")
    visible()
    with engine.connect() as connection:
        key, proof = connection.execute(
            text("SELECT key,value FROM app_state WHERE starts_with(key,'sleep-timing:')")
        ).one()
    change(engine, "DELETE FROM app_state WHERE key=:key", key=key)
    hidden()
    save(engine, key, proof)
    visible()
    with Session(engine) as session, session.begin():
        select_goals(session, GoalSelection(revision=0, goals=["sleep"]), NOW)
    hidden()
    verify_receipts(engine, (identity,))


@contextmanager
def native_server(binary):
    env = {
        "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
        "GA_API_TOKENS": json.dumps(API_TOKENS),
    }
    with subprocess.Popen(
        [binary.resolve(strict=True), "serve", "--port", "0"],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    ) as process:
        try:
            line = process.stdout.readline().strip()
            assert line.startswith("Listening on http://127.0.0.1:"), line
            with httpx.Client(
                base_url=line.removeprefix("Listening on "), timeout=5, trust_env=False
            ) as native:
                yield native
        finally:
            process.terminate()
            process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    settings = Settings(
        _env_file=None,
        api_tokens=API_TOKENS,
        timezone=ZONE,
        integrations=[{"id": "model:gemini:offline", "kind": "model", "provider": "gemini"}],
    )
    try:
        with (
            patch("garmin_ai.web_chat.datetime", Clock),
            patch("garmin_ai.integrations.create_model_provider", return_value=SleepPlanner()),
            TestClient(create_app(settings, engine=engine)) as python,
        ):
            message, identity = provision(engine, python)
            with native_server(args.binary) as native:
                compare(engine, python, native, message, identity)
    finally:
        engine.dispose()
    print("Matched Python sleep reply visibility, replay-safe receipt, and delivery fences")


if __name__ == "__main__":
    main()
