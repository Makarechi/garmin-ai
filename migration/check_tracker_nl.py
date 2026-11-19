"""Compare offline natural-language tracker forms and retained request context."""

import argparse
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.definitions import (
    CustomEntryInput,
    activate_definition,
    create_custom_event,
    create_definition_draft,
    retire_definition,
)
from garmin_ai.models import (
    AppState,
    Audit,
    Event,
    EventDefinition,
    EventDefinitionVersion,
    Person,
    TrackerConfig,
)
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    definition_spec,
    preview_tracker,
)

TOKENS = [
    {"key": f"synthetic-tracker-nl-fallback-api-{name}", "scopes": scopes}
    for name, scopes in [
        ("admin", ["admin"]),
        ("read", ["read:diary"]),
        ("write", ["read:diary", "write:diary"]),
        ("manage", ["manage:definitions"]),
        ("health", ["read:health"]),
    ]
]


def seed(engine, first, count):
    with Session(engine) as session:
        for index in range(first, first + count):
            draft = TrackerSetupDraft(
                key=f"synthetic_nl_{index:03}",
                name=f"Synthetic Focus {index}",
                locale="en",
                shortcut=f"Синтетический фокус {index}" if index % 2 else None,
                privacy="sensitive" if index % 3 == 0 else "private",
                fields=[
                    TrackerFieldDraft(key="value", label=f"Synthetic field {index}", kind="text")
                ],
            )
            preview = preview_tracker(session, draft)
            confirm_tracker(
                session,
                TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
                actor="synthetic-fixture",
            )
        session.commit()


def normalize(value):
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            if key == "submission_id" and item is not None:
                assert re.fullmatch(r"[0-9a-f]{32}", item)
                item = "synthetic-submission"
            output[key] = normalize(item)
        return output
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def operation_key(operation):
    return "nl-operation:" + sha256(f"api\0{operation}".encode()).hexdigest()


def snapshot(engine):
    with Session(engine) as session:
        counts = [
            session.scalar(select(func.count()).select_from(model)) for model in [Event, Audit]
        ]
        states = {
            row.key: row.value
            for row in session.scalars(select(AppState).where(AppState.key.startswith("nl-")))
        }
        return counts, states


def exercise(engine, python, native):
    number = 0

    def compare(text="Synthetic Focus", *, role="read", status=200, body=None):
        nonlocal number
        number += 1
        request = body or {"text": text, "operation_id": f"synthetic-case-{number}"}
        headers = (
            {"Authorization": f"Bearer synthetic-tracker-nl-fallback-api-{role}"} if role else {}
        )
        before, _ = snapshot(engine)
        clients = (python, native) if number % 2 else (native, python)
        first = clients[0].post("/natural-language/trackers", json=request, headers=headers)
        assert first.status_code == status, (number, first.status_code, first.text)
        retained = snapshot(engine)
        second = clients[1].post("/natural-language/trackers", json=request, headers=headers)
        assert second.status_code == status, (number, second.status_code, second.text)
        assert normalize(first.json()) == normalize(second.json()), (
            number,
            first.text,
            second.text,
        )
        assert snapshot(engine) == retained, f"Retry changed retained context at case {number}"
        assert retained[0] == before, "Offline fallback must preserve all facts and audits"
        return first.json(), request

    compare(role=None, status=401)
    compare(role="health", status=403)
    compare(role="read")
    compare(role="manage")
    seed(engine, 0, 3)
    for role in ["read", "write", "manage", "admin"]:
        for text in [
            "Focus",
            "Фокус",
            "the synthetic field 2",
            "I want to track Focus",
            "unrelated",
            " ",
        ]:
            compare(text, role=role)
    special = {}
    with Session(engine) as session:
        for name in ["unconfigured", "query_only"]:
            draft = TrackerSetupDraft(
                key=f"synthetic_nl_{name}",
                name=f"Synthetic {name}",
                locale="en",
                fields=[
                    TrackerFieldDraft(key="value", label="Synthetic special field", kind="text")
                ],
            )
            spec = definition_spec(draft).model_dump(mode="json", by_alias=True)
            if name == "query_only":
                spec["allowed_operations"] = ["query"]
            definition = create_definition_draft(
                session, spec, actor="synthetic-fixture", authorized=True
            )
            version = activate_definition(
                session,
                definition.id,
                definition.revision,
                actor="synthetic-fixture",
                authorized=True,
            )
            special[name] = str(version.id)
            if name == "query_only":
                session.add(
                    TrackerConfig(owner_id=definition.owner_id, definition_id=definition.id)
                )
        session.commit()
    compare(
        status=404,
        body={
            "text": "Unconfigured",
            "operation_id": "unconfigured-version",
            "selected_definition_version_id": special["unconfigured"],
        },
    )
    result, _ = compare(
        body={
            "text": "Query only",
            "operation_id": "query-only-version",
            "selected_definition_version_id": special["query_only"],
        }
    )
    assert result["forms"] == []
    with Session(engine) as session:
        version = session.scalar(
            select(EventDefinitionVersion)
            .join(EventDefinition)
            .where(EventDefinition.key == "user.synthetic_nl_000")
        )
        active = str(version.id)
        event = create_custom_event(
            session,
            CustomEntryInput(
                definition_key="user.synthetic_nl_000",
                start=datetime(2026, 9, 20, 20, tzinfo=UTC),
                timezone="UTC",
                values={"value": "Synthetic retained entry"},
            ),
            actor="synthetic-fixture",
        )
        event_id = str(event.id)
        session.commit()
    compare(
        body={
            "text": "Selected",
            "operation_id": "selected-version",
            "selected_definition_version_id": active,
        }
    )
    compare(
        role="manage",
        body={
            "text": "Selected",
            "operation_id": "manage-selected-version",
            "selected_definition_version_id": active,
        },
    )
    compare(
        body={"text": "Selected", "operation_id": "selected-event", "selected_event_id": event_id}
    )
    compare(
        role="manage",
        status=403,
        body={
            "text": "Selected",
            "operation_id": "denied-selected-event",
            "selected_event_id": event_id,
        },
    )
    for field in ["selected_event_id", "selected_definition_version_id"]:
        compare(
            status=404,
            body={"text": "Missing", "operation_id": f"missing-{field}", field: str(uuid4())},
        )
    with Session(engine) as session:
        definition = session.scalar(
            select(EventDefinition).where(EventDefinition.key == "user.synthetic_nl_000")
        )
        retire_definition(session, definition.id, definition.revision, authorized=True)
        session.commit()
    compare(
        status=404,
        body={
            "text": "Retired",
            "operation_id": "retired-version",
            "selected_definition_version_id": active,
        },
    )
    compare(
        body={"text": "Retired", "operation_id": "retired-entry", "selected_event_id": event_id}
    )
    _, request = compare("Pinned first context")
    compare(body=request)
    compare(status=422, body={**request, "text": "Changed request"})
    with Session(engine) as session:
        person = session.scalar(select(Person).where(Person.singleton))
        person.locale = "ru"
        person.timezone = "Europe/Bratislava"
        session.add(AppState(key="preferences:onboarding", value={}))
        session.commit()
    compare(body=request)
    compare("Синтетический фокус")
    with Session(engine) as session:
        person = session.scalar(select(Person).where(Person.singleton))
        person.timezone = "Unknown/Synthetic"
        session.commit()
    compare(status=422, body=request)
    with Session(engine) as session:
        person = session.scalar(select(Person).where(Person.singleton))
        person.timezone = "Europe/Bratislava"
        session.commit()
    for written in [False, True]:
        operation = f"synthetic-legacy-{written}"
        body = {"text": "Synthetic legacy receipt", "operation_id": operation}
        raw = json.dumps(
            {**body, "selected_event_id": None}, ensure_ascii=False, separators=(",", ":")
        )
        with Session(engine) as session:
            session.add(
                AppState(
                    key=operation_key(operation),
                    value={
                        "request_hash": sha256(raw.encode()).hexdigest(),
                        "result": {
                            "intent": "create_entry" if written else "none",
                            "written": written,
                        },
                    },
                )
            )
            session.commit()
        compare(role="write", body=body)
        compare(role="manage", status=403 if written else 200, body=body)
    seed(engine, 3, 53)
    for role in ["read", "write", "manage", "admin"]:
        for text in [
            "unrelated",
            "Synthetic Focus 5",
            "I want to track Focus",
            "хочу вести дневник",
        ]:
            result, _ = compare(text, role=role)
            assert len(result["forms"]) == (0 if role == "manage" else 50)
            assert result["selection_has_more"] is (role != "manage")
    result, _ = compare(
        body={
            "text": "Selected historical",
            "operation_id": "historical-at-scale",
            "selected_event_id": event_id,
        }
    )
    assert len(result["forms"]) == 1 and "selection_has_more" not in result
    headers = {"Authorization": "Bearer synthetic-tracker-nl-fallback-api-write"}
    for index, (issuer, receiver) in enumerate([(python, native), (native, python)]):
        response = issuer.post(
            "/natural-language/trackers",
            headers=headers,
            json={
                "text": "Synthetic fallback form",
                "operation_id": f"synthetic-cross-form-{index}",
            },
        )
        assert response.status_code == 200, response.text
        form = response.json()["forms"][0]
        body = {
            "action_id": form["id"],
            "schema_hash": form["schema_hash"],
            "submission_id": form["submission_id"],
            "start": f"2026-10-0{index + 1}T10:00:00+00:00",
            "timezone": "UTC",
            "values": {"value": f"Synthetic cross-runtime form {index}"},
        }
        before, _ = snapshot(engine)
        saved = receiver.post(f"/forms/{form['id']}/submit", headers=headers, json=body)
        assert saved.status_code == 200, saved.text
        after, _ = snapshot(engine)
        assert after == [count + 1 for count in before]
        repeated = issuer.post(f"/forms/{form['id']}/submit", headers=headers, json=body)
        assert repeated.status_code == 200 and repeated.json()["id"] == saved.json()["id"]
        assert snapshot(engine)[0] == after
    print(
        f"Matched {number} paired offline tracker HTTP cases, retained contexts, permissions, history and fifty-form limits",
        flush=True,
    )
    print("Forms returned by either runtime were saved by the other exactly once", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        settings = Settings(
            _env_file=None,
            locale="en",
            timezone="UTC",
            api_tokens=[ApiToken(**token) for token in TOKENS],
        )
        with (
            TestClient(create_app(settings, engine)) as python,
            subprocess.Popen(
                [args.binary.resolve(strict=True), "serve", "--port", "0"],
                env={
                    "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                    "GA_API_TOKENS": json.dumps(TOKENS),
                    "GA_LOCALE": "en",
                    "GA_TIMEZONE": "UTC",
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
                status = Path(f"/proc/{server.pid}/status")
                if status.exists():
                    for line in status.read_text().splitlines():
                        if line.startswith(("VmHWM:", "VmRSS:")):
                            print(f"Native HTTP process {line}", flush=True)
                server.terminate()
                server.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
