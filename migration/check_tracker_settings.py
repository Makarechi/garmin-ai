"""Compare tracker settings, revision guards and reminder retirement with pinned Python."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import httpx
from capture_reference import isolated_engine
from check_tracker_setup import ADMIN, DRAFT, READER, SETUP, TOKENS, post
from fastapi.testclient import TestClient
from sqlalchemy import text

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.initiative_rules import TRACKER_RULE_NAMESPACE


def pending(engine, tracker):
    now = datetime.now(UTC)
    rule = uuid5(TRACKER_RULE_NAMESPACE, tracker)
    with engine.begin() as connection:
        owner = connection.scalar(text("SELECT id FROM people WHERE singleton"))
        conversation = uuid4()
        connection.execute(
            text("""
            INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch,state,share_owner_memory)
            VALUES(:id,:owner,'telegram','synthetic',:epoch,'{}'::jsonb,false)
        """),
            {"id": conversation, "owner": owner, "epoch": uuid4()},
        )
        for state in ("queued", "sending", "provider_accepted", "delivered"):
            identity = uuid4()
            connection.execute(
                text("""
                INSERT INTO outbox_messages(id,owner_id,conversation_id,operation_id,intent,dedup_key,state,attempts,next_attempt_at)
                VALUES(:id,:owner,:conversation,:operation,CAST(:intent AS jsonb),:dedup,:state,0,:at)
            """),
                dict(
                    id=identity,
                    owner=owner,
                    conversation=conversation,
                    operation=uuid4(),
                    intent=json.dumps({"evidence_refs": [f"rule:{rule}"]}),
                    dedup=f"synthetic:{identity}",
                    state=state,
                    at=now,
                ),
            )
        for state in ("pending", "sending", "uncertain", "sent"):
            identity = uuid4()
            connection.execute(
                text("""
                INSERT INTO pending_questions(id,kind,text,evidence,priority,earliest_send_at,expires_at,status,dedup_key,attempts)
                VALUES(:id,'tracker_reminder','Synthetic reminder',CAST(:evidence AS jsonb),1,:at,:expires,:state,:dedup,0)
            """),
                dict(
                    id=identity,
                    evidence=json.dumps({"tracker_id": tracker}),
                    at=now,
                    expires=now + timedelta(days=1),
                    state=state,
                    dedup=f"synthetic:{identity}",
                ),
            )


def reminders(engine, tracker):
    rule = uuid5(TRACKER_RULE_NAMESPACE, tracker)
    with engine.connect() as connection:
        questions = (
            connection.execute(
                text("""
            SELECT status FROM pending_questions WHERE evidence->>'tracker_id'=:id ORDER BY status
        """),
                {"id": tracker},
            )
            .scalars()
            .all()
        )
        outbox = connection.execute(
            text("""
            SELECT state,next_attempt_at IS NULL FROM outbox_messages
            WHERE intent->'evidence_refs' @> CAST(:refs AS jsonb) ORDER BY state
        """),
            {"refs": json.dumps([f"rule:{rule}"])},
        ).all()
        stamp = connection.scalar(
            text("SELECT updated_at FROM tracker_configs WHERE id=:id"), {"id": UUID(tracker)}
        )
    return questions, outbox, stamp


def exercise(engine, python, native):
    trackers = []
    for key in ("synthetic_settings_python", "synthetic_settings_rust"):
        draft = dict(DRAFT, key=key)
        token = post(python, "/preview", draft)["confirmation_token"]
        result = post(python, "", {"draft": draft, "confirmation_token": token})
        trackers.append(result["tracker"]["id"])
    bodies = [
        {"revision": 1, "reminder_enabled": False},
        {"revision": 1, "reminder_enabled": False},
        {
            "revision": 2,
            "reminder_enabled": "yes",
            "reminder_time": "08:05",
            "reminder_timezone": "Europe/Bratislava",
            "shortcut": "Синтетика 🧪",
        },
        {
            "revision": 3,
            "reminder_enabled": True,
            "reminder_time": "08:05",
            "reminder_timezone": "Europe/Bratislava",
            "shortcut": "Синтетика 🧪",
        },
        {"revision": 3, "reminder_enabled": False, "reminder_time": "08:05"},
    ]
    expected = (200, 409, 200, 200, 200)
    results = []
    missing = f"/tracker-setups/{uuid4()}/settings"
    for client in (python, native):
        response = client.put(missing, json=bodies[0], headers={"Authorization": f"Bearer {ADMIN}"})
        assert response.status_code == 404 and response.json() == {"detail": "Record not found"}
    for client, tracker in zip((python, native), trackers, strict=True):
        path = f"/tracker-setups/{tracker}/settings"

        def update(body, status=200, token=ADMIN, *, client=client, path=path, tracker=tracker):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            result = client.put(path, json=body, headers=headers)
            assert result.status_code == status, (body, status, result.text)
            document = result.json()
            if status == 200:
                assert document.pop("id") == tracker
            return document

        update(bodies[0], 401, token=None)
        update(bodies[0], 403, token=READER)
        update(bodies[2], 403, token=SETUP)
        pending(engine, tracker)
        responses = []
        for index, (body, status) in enumerate(zip(bodies, expected, strict=True)):
            before = reminders(engine, tracker)
            responses.append(update(body, status, token=SETUP if index == 4 else ADMIN))
            after = reminders(engine, tracker)
            if index in (1, 3):
                assert before == after
            else:
                assert after[2] >= before[2]
            assert after[0] == ["cancelled", "cancelled", "cancelled", "sent"]
            assert after[1] == [
                ("cancelled", True),
                ("delivered", False),
                ("provider_accepted", False),
                ("sending", False),
            ]
        for body in (
            {},
            {"revision": 4, "reminder_enabled": True},
            {"revision": 4.0, "reminder_enabled": False},
            {"revision": True, "reminder_enabled": False},
            {"revision": 4, "reminder_enabled": False, "reminder_time": "24:00"},
            {"revision": 4, "reminder_enabled": False, "reminder_timezone": "Missing/Zone"},
            {"revision": 4, "reminder_enabled": False, "shortcut": "a" * 65},
        ):
            update(body, 422)
        responses.append(update({"revision": 10**80, "reminder_enabled": False}, 409))
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE tracker_configs SET reminder_timezone=NULL WHERE id=:id"),
                {"id": UUID(tracker)},
            )
        responses.append(update({"revision": 4, "reminder_enabled": False}))
        results.append(responses)
    assert results[0] == results[1], results
    print(
        "Matched tracker settings, revision checks, Unicode, legacy timezone and reminder cancellation"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        settings = Settings(_env_file=None, api_tokens=[ApiToken(**token) for token in TOKENS])
        with (
            TestClient(create_app(settings, engine)) as python,
            subprocess.Popen(
                [args.binary.resolve(strict=True), "serve", "--port", "0"],
                env={
                    "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
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
                    exercise(engine, python, native)
            finally:
                server.terminate()
                server.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
