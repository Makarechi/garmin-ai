"""Exercise real native web sleep analysis with synthetic provider responses and data."""

import argparse
import json
import os
import selectors
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import httpx
from capture_reference import isolated_engine
from check_web_chat_model import CONSENT, INTEGRATION, KEY, set_model_categories, synthetic_https
from check_web_sleep_history import (
    API_TOKENS,
    DAY,
    HEADERS,
    ROUTE,
    ZONE,
    Clock,
    SleepPlanner,
    change,
    history,
    provision,
    read,
    save,
)
from fastapi.testclient import TestClient
from sqlalchemy import text

from garmin_ai.agent import AgentStep, Interpretation, ReadCall
from garmin_ai.api import create_app
from garmin_ai.config import Settings

POINT = "Когда я заснул в ночь на 16 октября?"
SUMMARY = "Сколько главных интервалов сна записано за два дня?"


class Planner(SleepPlanner):
    def structured(self, instruction, prompt, schema):
        if schema is not AgentStep:
            return super().structured(instruction, prompt, schema)
        requested = json.loads(prompt)
        offered = [tool["name"] for tool in requested["tools"]]
        assert "analysis_sleep" in offered or not offered
        if requested["question"] != SUMMARY:
            return super().structured(instruction, prompt, schema)
        if not requested["evidence"]:
            return AgentStep(
                calls=[
                    ReadCall(
                        name="analysis_sleep",
                        arguments_json=json.dumps({"start": DAY, "end": "2026-10-17"}),
                    )
                ],
                response_language="ru",
            )
        result = requested["evidence"][0]["result"]
        assert result["summary"]["timed_main_sessions"] == 1
        return AgentStep(
            response_language="ru",
            evidence_ids=[1],
            numeric_claims=[
                {"evidence_id": 1, "path": ["summary", "timed_main_sessions"], "value": 1}
            ],
        )


def output(prompt):
    schema = AgentStep if "question" in prompt else Interpretation
    return Planner().structured("", json.dumps(prompt), schema)


def submit(client, question):
    payload = {"client_message_id": str(uuid4()), "text": question}
    result = client.post(ROUTE, headers=HEADERS, json=payload)
    assert result.status_code == 200, result.text
    return payload, result.json()["reply"]


def verify(engine, python, native):
    _, point_oracle = submit(python, POINT)
    request, point = submit(native, POINT)
    assert point["text"] == point_oracle["text"] and "23:42" in point["text"], point
    _, summary_oracle = submit(python, SUMMARY)
    _, summary = submit(native, SUMMARY)
    assert summary["text"] == summary_oracle["text"], (summary, summary_oracle)
    assert "timed_main_sessions: 1" in summary["text"]
    point_id, summary_id = point["id"], summary["id"]

    def native_replies():
        return {reply["id"]: reply["state"] for reply in history(native)["replies"]}

    assert native_replies()[point_id] == "queued" and native_replies()[summary_id] == "queued"
    with engine.begin() as replay:
        replay.execute(text("SELECT pg_advisory_xact_lock(72104619)"))
        replies = native_replies()
        assert point_id in replies and summary_id not in replies
        read(native, point_id)
        read(native, point_id)
        assert native_replies()[point_id] == "read"
    save(engine, "replay:generation", {"id": "synthetic-unrelated-generation"})
    assert point_id in native_replies()
    with engine.begin() as context_change:
        context_change.execute(text("SELECT pg_advisory_xact_lock(72104643)"))
        assert point_id not in native_replies()
        read(native, point_id, 409)
    assert point_id in native_replies()
    set_model_categories(engine, ["diary"])
    assert point_id not in native_replies()
    read(native, point_id, 409)
    set_model_categories(engine, ["health", "diary"])
    assert point_id in native_replies()
    change(engine, "UPDATE timeline_intervals SET confirmed=false WHERE id=:id", id=f"sleep:{DAY}")
    replies = native_replies()
    assert point_id not in replies and summary_id in replies, replies
    change(
        engine,
        "UPDATE timeline_intervals SET start=start+interval '1 minute' WHERE id=:id",
        id=f"sleep:{DAY}",
    )
    replies = native_replies()
    assert point_id not in replies and summary_id not in replies, replies
    read(native, point_id, 409)
    retry = native.post(ROUTE, headers=HEADERS, json=request)
    assert retry.status_code == 200 and retry.json()["duplicate"] and retry.json()["reply"] is None
    with engine.connect() as connection:
        count = connection.scalar(
            text("SELECT count(*) FROM message_delivery_receipts WHERE outbox_message_id=:id"),
            {"id": point_id},
        )
        assert count == 1
    print("Matched native sleep timing and summary, and preserved one verified read during replay")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    settings = Settings(
        _env_file=None,
        api_tokens=API_TOKENS,
        locale="ru",
        timezone=ZONE,
        integrations=[INTEGRATION],
        llm_enabled=True,
    )
    try:
        set_model_categories(engine, ["health", "diary"])
        change(engine, "UPDATE people SET timezone=:zone,locale='ru' WHERE singleton", zone=ZONE)
        with (
            TemporaryDirectory(prefix="synthetic-native-web-sleep-") as path,
            patch("garmin_ai.web_chat.datetime", Clock),
            patch("garmin_ai.integrations.create_model_provider", return_value=Planner()),
            patch("check_web_chat_model.output", output),
            TestClient(create_app(settings, engine=engine)) as python,
        ):
            provision(engine, python)
            directory = Path(path)
            with (
                synthetic_https(directory, [], engine) as (ca, _, _),
                subprocess.Popen(
                    [args.binary.resolve(strict=True), "serve", "--port", "0"],
                    cwd=directory,
                    env={
                        "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                        "GA_API_TOKENS": json.dumps(API_TOKENS),
                        "GA_LOCALE": "ru",
                        "GA_TIMEZONE": ZONE,
                        "GA_LLM_ENABLED": "true",
                        "GA_INTEGRATIONS": json.dumps([INTEGRATION]),
                        "GA_PLUGIN_WEB_CHAT_SYNTHETIC": KEY,
                        "GA_LLM_CONSENT": json.dumps(CONSENT),
                        "SSL_CERT_FILE": str(ca),
                    },
                    stdout=subprocess.PIPE,
                    text=True,
                ) as server,
            ):
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(server.stdout, selectors.EVENT_READ)
                        assert selector.select(timeout=5)
                    url = server.stdout.readline().strip().removeprefix("Listening on ")
                    assert url.startswith("http://127.0.0.1:")
                    with httpx.Client(base_url=url, timeout=10, trust_env=False) as native:
                        verify(engine, python, native)
                finally:
                    server.terminate()
                    server.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
