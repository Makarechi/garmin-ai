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
from garmin_ai.events import deletion_response
from garmin_ai.models import Event
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
    compare_deletion(python, native, engine, hidden)


def compare_deletion(python, native, engine, hidden):
    system = native.post(PATH, headers=headers("both", "fixture-rust"), json=CAFFEINE).json()
    identity = UUID(system["id"])
    endpoint = f"{PATH}/{identity}"
    for client in (python, native):
        for role, code in [(None, 401), ("reader", 403), ("writer", 403)]:
            assert (
                client.delete(f"{endpoint}?revision=1", headers=headers(role)).status_code == code
            )
        for suffix in ("", "?revision=0", "?revision=-1", "?revision=untrusted"):
            assert client.delete(endpoint + suffix, headers=headers("both")).status_code == 422
        stale = client.delete(f"{endpoint}?revision=2", headers=headers("both"))
        assert stale.status_code == 409
        assert stale.json() == {"detail": "Event changed; reload before deleting"}
        assert client.get(endpoint, headers=headers("reader")).json() == system

    deleted = native.delete(f"{endpoint}?revision=9&%72evision=%2B1", headers=headers("both"))
    assert deleted.status_code == 200 and deleted.headers["cache-control"] == "no-store"
    with Session(engine) as session:
        row = session.get(Event, identity)
        assert row.deleted and row.revision == 2
        assert deleted.json() == deletion_response(session, row)
        audit = session.execute(
            text("SELECT action,actor,before,after FROM audit_log WHERE event_id=:id ORDER BY id"),
            {"id": identity},
        ).all()
        assert [(record.action, record.actor) for record in audit] == [
            ("create", "api"),
            ("delete", "api"),
        ]
        assert audit[1].before == audit[0].after
        assert audit[1].after["deleted"] and audit[1].after["revision"] == 2
    for client in (python, native):
        assert client.delete(f"{endpoint}?revision=2", headers=headers("both")).status_code == 404
        assert client.get(endpoint, headers=headers("reader")).status_code == 404
        replay = client.post(PATH, headers=headers("both", "fixture-rust"), json=CAFFEINE)
        assert replay.json() == deleted.json()

    protected = f"{PATH}/{hidden}?revision=1"
    response = native.delete(protected, headers=headers("both"))
    assert response.status_code == 200
    assert response.json() == {"id": str(hidden), "revision": 2, "deleted": True}
    for client in (python, native):
        assert client.delete(protected, headers=headers("both")).status_code == 404


def compare_tracker_creation(python, native, engine):
    path = "/entries"
    entry = {
        "definition_key": "user.synthetic_private",
        "start": NOTE["start"],
        "timezone": "UTC",
        "values": {"value": 2},
        "units": {"value": "count"},
    }
    for client in (python, native):
        assert client.post(path, headers=headers("writer"), json=entry).status_code == 403
        assert (
            client.post(
                path, headers=headers("both"), json={**entry, "units": {"value": "steps"}}
            ).status_code
            == 422
        )
        assert (
            client.post(
                path,
                headers=headers("both"),
                json={**entry, "definition_key": "user.synthetic_unknown"},
            ).status_code
            == 404
        )
    assert counts(engine) == (3, 2)

    for first, second, key in [
        (python, native, "fixture-python-tracker"),
        (native, python, "fixture-native-tracker"),
    ]:
        request = headers("both", key)
        recorded = first.post(path, headers=request, json=entry)
        retried = second.post(path, headers=request, json=entry)
        assert recorded.status_code == retried.status_code == 200
        assert retried.json() == recorded.json()
        stored = retried.json()
        assert stored["payload"] == {"type": entry["definition_key"], "value": 2}
        target = f"{PATH}/{stored['id']}"
        for client in (python, native):
            assert client.get(target, headers=headers("reader")).status_code == 404
            changed = client.post(path, headers=request, json={**entry, "values": {"value": 3}})
            assert changed.status_code == 409
        assert python.delete(f"{target}?revision=1", headers=headers("both")).status_code == 200
        for client in (python, native):
            assert client.post(path, headers=request, json=entry).json() == stored
    assert counts(engine) == (5, 4)


def run(binary):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            row = ensure_scenario_packs(session, legacy_install=False)["caffeine"]
            configure_scenario_pack(
                session,
                "caffeine",
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
                    compare_tracker_creation(python, native, engine)
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
