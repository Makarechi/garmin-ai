"""Compare scoped action discovery for active and historical trackers with pinned Python."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.definitions import CustomEntryInput, create_custom_event, retire_definition
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import EventDefinition
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)

DIARY = "synthetic-generated-actions-diary-reader"
HEALTH = "synthetic-generated-actions-health-reader"


def seed(engine):
    now = datetime.now(UTC)
    with Session(engine) as session:
        entries = {}
        for key, name, locale, shortcut in (
            ("active_actions", "Synthetic diary tracker", "en", None),
            ("shortcut_actions", "Синтетический трекер", "ru", "Test shortcut"),
            ("retired_actions", "Synthetic archived tracker", "en", None),
        ):
            draft = TrackerSetupDraft(
                key=key,
                name=name,
                locale=locale,
                shortcut=shortcut,
                fields=[TrackerFieldDraft(key="description", label="Synthetic field", kind="text")],
            )
            preview = preview_tracker(session, draft)
            result = confirm_tracker(
                session,
                TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
                actor="synthetic-fixture",
            )
            event = create_custom_event(
                session,
                CustomEntryInput(
                    definition_key=f"user.{key}",
                    start=now,
                    timezone="UTC",
                    values={"description": "Synthetic existing entry"},
                ),
                actor="synthetic-fixture",
            )
            entries[key] = str(event.id)
            if key == "retired_actions":
                definition_id = UUID(str(result["tracker"]["definition_id"]))
                definition = session.get(EventDefinition, definition_id)
                retire_definition(session, definition_id, definition.revision, authorized=True)
        system = create_event(
            session,
            EventInput(
                start=now,
                timezone="UTC",
                payload={"type": "note", "description": "Synthetic system entry"},
            ),
            actor="synthetic-fixture",
        )
        entries["system"] = str(system.id)
        session.commit()
    return entries


def verify(engine, binary):
    entries = seed(engine)
    token_specs = [
        {"key": DIARY, "scopes": ["read:diary"]},
        {"key": HEALTH, "scopes": ["read:health"]},
    ]
    settings = Settings(_env_file=None, api_tokens=[ApiToken(**spec) for spec in token_specs])
    with TestClient(create_app(settings, engine)) as python:
        with subprocess.Popen(
            [binary, "serve", "--port", "0"],
            env={
                "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                "GA_API_TOKENS": json.dumps(token_specs),
            },
            stdout=subprocess.PIPE,
            text=True,
        ) as server:
            try:
                line = server.stdout.readline().strip()
                assert line.startswith("Listening on http://127.0.0.1:")
                with httpx.Client(
                    base_url=line.removeprefix("Listening on "), timeout=5, trust_env=False
                ) as rust:
                    compare(python, rust, entries)
            finally:
                server.terminate()
                server.wait(timeout=5)


def compare(python, rust, entries):
    def request(path, status, key=DIARY, locale=None):
        auth = {"Authorization": f"Bearer {key}"} if key else {}
        args = {"locale": locale} if locale else {}
        old, new = [client.get(path, headers=auth, params=args) for client in (python, rust)]
        assert old.status_code == new.status_code == status, (old.text, new.text)
        assert old.json() == new.json(), (old.text, new.text)
        return new.json()

    request("/actions", 401, key=None)
    request("/actions", 403, key=HEALTH)
    for locale in (None, "ru-RU", "en-GB", "sk"):
        actions = request("/actions", 200, locale=locale)["actions"]
        assert [action["definition_key"] for action in actions] == [
            "user.active_actions",
            "user.shortcut_actions",
        ]
        assert actions[1]["label"] == "Test shortcut"
    for label in ("active_actions", "shortcut_actions", "retired_actions"):
        action = request(f"/actions/events/{entries[label]}", 200, locale="ru-RU")
        assert action["kind"] == "edit_entry"
        assert action["definition_key"] == f"user.{label}"
    request(f"/actions/events/{entries['system']}", 404)
    request(f"/actions/events/{uuid4()}", 404)
    print("Matched eleven scoped tracker action queries, including a retired definition")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        verify(engine, args.binary.resolve(strict=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
