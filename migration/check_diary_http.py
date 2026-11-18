"""Compare the owner HTTP diary routes with pinned Python and shared receipts."""

import argparse
import json
import selectors
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.definitions import activate_definition, create_custom_event, create_definition_draft
from garmin_ai.scenario_packs import PackSelection, configure_scenario_pack, ensure_scenario_packs

PATH = "/events"
SCOPES = {
    "reader": ["read:diary"],
    "writer": ["write:diary"],
    "both": ["read:diary", "write:diary"],
    "health": ["read:health"],
}
KEYS = {label: f"synthetic-diary-http-{label}-" + "a" * 20 for label in SCOPES}
TOKENS = [{"key": KEYS[label], "scopes": scopes} for label, scopes in SCOPES.items()]
NOTE = {
    "start": "2026-10-25T13:15:00.123456Z",
    "timezone": "Europe/Bratislava",
    "original_text": "Синтетическая запись с чаем",
    "payload": {"type": "note", "description": "synthetic note", "tags": ["fixture"]},
}
CAFFEINE = {
    "start": NOTE["start"],
    "timezone": "UTC",
    "source": "wearable",
    "payload": {
        "type": "caffeine",
        "beverage": "synthetic tea",
        "servings": 2,
        "dose_basis": "per_serving",
        "caffeine_mg_estimate": 12.5,
    },
}


def headers(role, key=None):
    result = {"Authorization": "Bearer " + KEYS[role]} if role else {}
    if key is not None:
        result["idempotency-key"] = key
    return result


def counts(engine):
    with engine.connect() as connection:
        return connection.execute(
            text("""
            SELECT (SELECT count(*) FROM events),
                   (SELECT count(*) FROM audit_log WHERE actor='api' AND action='create')
        """)
        ).one()


def hidden_id(engine):
    with Session(engine) as session, session.begin():
        draft = create_definition_draft(
            session,
            {
                "key": "user.synthetic_private",
                "topology": "point",
                "labels": {"en": "Synthetic private"},
                "schema": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "integer", "minimum": 0, "maximum": 10}},
                    "additionalProperties": False,
                },
                "fields": {
                    "value": {
                        "id": "user.synthetic_private.value",
                        "semantic": "count",
                        "unit": "count",
                        "labels": {"en": "Synthetic value"},
                    }
                },
                "allowed_operations": ["create", "delete"],
            },
            actor="fixture",
            authorized=True,
        )
        activate_definition(session, draft.id, draft.revision, actor="fixture", authorized=True)
        row = create_custom_event(
            session,
            {
                "definition_key": "user.synthetic_private",
                "start": NOTE["start"],
                "values": {"value": 1},
            },
            actor="fixture",
        )
        return row.id


def compare(python, native, engine):
    for role, status in [(None, 401), ("reader", 403), ("writer", 403), ("health", 403)]:
        expected = python.post(PATH, headers=headers(role), json=NOTE)
        actual = native.post(PATH, headers=headers(role), json=NOTE)
        assert expected.status_code == actual.status_code == status
        assert expected.json() == actual.json()
    assert counts(engine) == (0, 0)
    for invalid in [
        {**NOTE, "start": "2026-10-25T12:00:00"},
        {**NOTE, "unknown": "synthetic"},
        {**NOTE, "timezone": "Invalid/Synthetic"},
    ]:
        assert python.post(PATH, headers=headers("both"), json=invalid).status_code == 422
        denied = native.post(PATH, headers=headers("both"), json=invalid)
        assert denied.status_code == 422 and denied.json() == {"detail": "Invalid arguments"}

    for first, second, body, key in [
        (python, native, NOTE, "fixture-python"),
        (native, python, CAFFEINE, "fixture-rust"),
    ]:
        request_headers = headers("both", key)
        created = first.post(PATH, headers=request_headers, json=body)
        retried = second.post(PATH, headers=request_headers, json=body)
        assert created.status_code == retried.status_code == 200, (
            key,
            created.status_code,
            created.json(),
            retried.status_code,
            retried.json(),
        )
        assert created.json() == retried.json()
        document = created.json()
        identity = f"{PATH}/{document['id']}"
        assert document["canonical"]["provenance"]["transport"] == "api"
        assert document["canonical"]["provenance"]["assertion_kind"] == "user_report"
        for client in (python, native):
            found = client.get(identity, headers=headers("reader"))
            assert found.status_code == 200 and found.json() == document
            assert client.get(identity, headers=headers("writer")).status_code == 403
        assert (
            native.get(identity, headers=headers("reader")).headers["cache-control"] == "no-store"
        )
        if body is CAFFEINE:
            assert document["caffeine_total"]["estimate"] == 25
    assert counts(engine) == (2, 2)
    for client in (python, native):
        changed = client.post(PATH, headers=headers("both", "fixture-python"), json=CAFFEINE)
        assert changed.status_code == 409
        assert changed.json() == {"detail": "Idempotency key already used for different data"}

    hidden = hidden_id(engine)
    for identity in (hidden, UUID(int=123)):
        for client in (python, native):
            missing = client.get(f"{PATH}/{identity}", headers=headers("reader"))
            assert missing.status_code == 404 and missing.json() == {"detail": "Record not found"}
    assert counts(engine) == (3, 2)
    deleted = python.post(PATH, headers=headers("both", "fixture-python"), json=NOTE).json()
    endpoint = f"{PATH}/{deleted['id']}"
    assert python.delete(f"{endpoint}?revision=1", headers=headers("both")).status_code == 200
    assert native.get(endpoint, headers=headers("reader")).status_code == 404
    assert native.post(PATH, headers=headers("both", "fixture-python"), json=NOTE).json()["deleted"]
    assert counts(engine) == (3, 2)
    compare_system_updates(python, native, engine, hidden, endpoint)


def compare_system_updates(python, native, engine, hidden, deleted_endpoint):
    amended = {
        **NOTE,
        "start": "2026-10-25T02:15:00.123456+02:00",
        "original_text": "Синтетическое уточнение",
        "payload": {**NOTE["payload"], "description": "synthetic correction"},
    }
    request = {"revision": 1, "event": amended}
    target = python.post(PATH, headers=headers("both"), json=NOTE).json()
    identity = UUID(target["id"])
    endpoint = f"{PATH}/{identity}"
    for client in (python, native):
        for role, status in [(None, 401), ("reader", 403), ("writer", 403)]:
            assert client.put(endpoint, headers=headers(role), json=request).status_code == status
        missing = client.put(deleted_endpoint, headers=headers("both"), json=request)
        assert missing.status_code == 404 and missing.json() == {"detail": "Record not found"}
        assert (
            client.put(f"{PATH}/{hidden}", headers=headers("both"), json=request).status_code == 422
        )
        stale = client.put(endpoint, headers=headers("both"), json={**request, "revision": 2})
        assert stale.status_code == 409
        assert stale.json() == {"detail": "Event changed; reload before editing"}
        self_reference = {
            **amended,
            "payload": {
                "type": "medication",
                "name": "synthetic medication",
                "reason_event_id": str(identity),
            },
        }
        invalid_relation = client.put(
            endpoint, headers=headers("both"), json={"revision": 1, "event": self_reference}
        )
        assert invalid_relation.status_code == 409
        assert invalid_relation.json() == {"detail": "A medication cannot reference itself"}
        assert client.get(endpoint, headers=headers("reader")).json() == target

    updated = native.put(endpoint, headers=headers("both"), json=request)
    assert updated.status_code == 200 and updated.headers["cache-control"] == "no-store"
    document = updated.json()
    assert document["revision"] == 2 and document["start"] == amended["start"]
    assert document["canonical"]["observed"]["start"] == amended["start"]
    assert document["payload"]["description"] == amended["payload"]["description"]
    assert document["original_text"] == amended["original_text"]
    assert (
        native.get(endpoint, headers=headers("reader")).json()
        == python.get(endpoint, headers=headers("reader")).json()
    )
    for client in (python, native):
        assert client.put(endpoint, headers=headers("both"), json=request).status_code == 409
    with engine.connect() as connection:
        audits = connection.execute(
            text("SELECT action,actor,before,after FROM audit_log WHERE event_id=:id ORDER BY id"),
            {"id": identity},
        ).all()
    assert [(audit.action, audit.actor) for audit in audits] == [
        ("create", "api"),
        ("update", "api"),
    ]
    assert audits[1].before == audits[0].after
    assert audits[1].after["revision"] == 2 and audits[1].after["start"] == amended["start"]

    python_target = python.post(PATH, headers=headers("both"), json=NOTE).json()
    reference = python.put(f"{PATH}/{python_target['id']}", headers=headers("both"), json=request)
    assert reference.status_code == 200
    expected = reference.json()
    for name in ("id", "created_at", "updated_at", "recorded_at", "ingested_at"):
        document.pop(name)
        expected.pop(name)
    for name in ("recorded_at", "ingested_at"):
        document["canonical"].pop(name)
        expected["canonical"].pop(name)
    assert document == expected


def run(binary):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            packs = ensure_scenario_packs(session, legacy_install=False)
            for key in ("caffeine", "migraine"):
                row = packs[key]
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
        settings = Settings(api_tokens=TOKENS, api_key="")
        with (
            TestClient(create_app(settings, engine=engine)) as python,
            TemporaryDirectory() as directory,
            subprocess.Popen(
                [str(binary), "serve", "--port", "0"],
                cwd=directory,
                env={
                    "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
                    "GA_API_TOKENS": json.dumps(TOKENS),
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as process,
        ):
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    assert selector.select(timeout=5)
                line = process.stdout.readline().strip()
                assert line.startswith("Listening on http://127.0.0.1:"), line
                with httpx.Client(base_url=line.split(" on ", 1)[1], trust_env=False) as native:
                    compare(python, native, engine)
            finally:
                process.terminate()
                process.wait(timeout=5)
    finally:
        engine.dispose()
    print("Verified native diary HTTP authorization, canonical responses and durable retries")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    opts = parser.parse_args()
    run(opts.binary.resolve(strict=True))
