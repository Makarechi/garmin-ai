"""Verify that an older compound sleep reply needs its complete recorded evidence."""

import argparse
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from capture_reference import isolated_engine
from check_web_sleep_history import (
    API_TOKENS,
    DAY,
    HEADERS,
    NOW,
    ROUTE,
    ZONE,
    Clock,
    SleepPlanner,
    change,
    history,
    native_server,
    provision,
    read,
    save,
)
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.agent import AgentStep, ReadCall
from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.models import SourcePayload


class CompoundPlanner(SleepPlanner):
    def structured(self, instruction, prompt, schema):
        evidence = json.loads(prompt).get("evidence", []) if schema is AgentStep else []
        if len(evidence) == 1:
            arguments = json.dumps(
                {"start": NOW.isoformat(), "end": (NOW + timedelta(hours=1)).isoformat()}
            )
            return AgentStep(
                response_language="ru",
                calls=[ReadCall(name="events", arguments_json=arguments)],
            )
        step = super().structured(instruction, prompt, schema)
        if len(evidence) > 1:
            step.evidence_ids = [1, 2]
        return step


def verify(engine, python, native, compound_body, compound_id):
    with engine.connect() as connection:
        key, proof = connection.execute(
            text("SELECT key,value FROM app_state WHERE starts_with(key,'analysis-sleep-timing:')")
        ).one()
    assert len(proof["projection"]["sleep_timing_dependencies"]) == 1
    with patch("garmin_ai.integrations.create_model_provider", return_value=SleepPlanner()):
        point = python.post(
            ROUTE, json=compound_body | {"client_message_id": str(uuid4())}, headers=HEADERS
        )
    assert point.status_code == 200 and "23:42" in point.json()["reply"]["text"]
    point_id = point.json()["reply"]["id"]

    def matching(*expected):
        reference = history(python)
        assert {reply["id"] for reply in reference["replies"]} == set(expected)
        assert history(native) == reference

    matching(compound_id, point_id)
    repeat = native.post(ROUTE, json=compound_body, headers=HEADERS)
    assert repeat.status_code == 200 and repeat.json()["duplicate"] is True
    assert repeat.json()["reply"]["id"] == compound_id
    read(native, compound_id)
    matching(compound_id, point_id)
    for holder in ("pg_advisory_xact_lock", "pg_advisory_xact_lock_shared"):
        with engine.begin() as replay:
            replay.execute(text(f"SELECT {holder}(72104619)"))
            matching(point_id)
    matching(compound_id, point_id)
    save(engine, "replay:generation", {"id": "synthetic-replay-replacement"})
    matching(point_id)
    read(python, compound_id, 409)
    read(native, compound_id, 409)
    change(engine, "DELETE FROM app_state WHERE key='replay:generation'")
    matching(compound_id, point_id)
    old_source = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            SourcePayload(
                id=old_source,
                source="garmin_connect",
                endpoint="stress",
                source_key=DAY,
                archive_key="synthetic/unrelated-replay.json",
                payload_hash="a" * 64,
                payload={},
                fetched_at=NOW,
                parser_version=1,
                status="normalized",
            )
        )
    matching(point_id)
    change(engine, "DELETE FROM source_payloads WHERE id=:id", id=old_source)
    matching(compound_id, point_id)
    change(engine, "UPDATE timeline_intervals SET confirmed=false WHERE id=:id", id=f"sleep:{DAY}")
    matching()
    read(native, compound_id, 409)
    change(engine, "UPDATE timeline_intervals SET confirmed=true WHERE id=:id", id=f"sleep:{DAY}")
    matching(compound_id, point_id)
    invalid = {**proof, "projection": {**proof["projection"], "sleep_timing_dependencies": []}}
    save(engine, key, invalid)
    matching(point_id)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM message_delivery_receipts")) == 1


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
            patch("garmin_ai.integrations.create_model_provider", return_value=CompoundPlanner()),
            TestClient(create_app(settings, engine=engine)) as python,
            native_server(args.binary) as native,
        ):
            body, identity = provision(engine, python)
            verify(engine, python, native, body, identity)
    finally:
        engine.dispose()
    print("Matched compound history, replay fences, invalidation and receipt deduplication")


if __name__ == "__main__":
    main()
