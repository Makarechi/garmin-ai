"""Compare native owner onboarding, profile, and atomic tracker setup with Python."""

import argparse
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.definitions import create_definition_draft
from garmin_ai.models import (
    ChannelBinding,
    Conversation,
    EventDefinition,
    EventDefinitionVersion,
    Person,
    SourceConnection,
)
from garmin_ai.onboarding import apply_onboarding
from garmin_ai.tracker_forms import TrackerSetupDraft, definition_spec

TOKENS = [
    {"key": "synthetic-onboarding-full-owner-token", "scopes": ["admin"]},
    {"key": "synthetic-onboarding-definition-manager", "scopes": ["manage:definitions"]},
    {
        "key": "synthetic-onboarding-preferences-token",
        "scopes": ["manage:definitions", "manage:integrations"],
    },
]
ADMIN, MANAGER, SETTINGS = [{"Authorization": f"Bearer {token['key']}"} for token in TOKENS]
INTEGRATIONS = [
    {"id": "source:garmin:daily", "kind": "source", "provider": "garmin"},
    {
        "id": "channel:telegram:paired",
        "kind": "channel",
        "provider": "telegram",
        "enabled": False,
    },
    {
        "id": "model:anthropic:declared",
        "kind": "model",
        "provider": "anthropic",
        "secret_refs": {"api_key": "GA_PLUGIN_SYNTHETIC_ONBOARDING_SECRET"},
        "config": {"model": "synthetic-configuration-value"},
    },
    {"id": "source:fixture_unknown:primary", "kind": "source", "provider": "fixture_unknown"},
]
WEB = {"channel": "web", "instance_id": "local"}
FALLBACK = {"channel": "telegram", "instance_id": "paired"}
KEY = "preferences:onboarding"


def draft(key):
    return {
        "key": key,
        "name": "Synthetic onboarding tracker",
        "locale": "en",
        "fields": [
            {
                "key": "value",
                "label": "Synthetic count",
                "kind": "integer",
                "minimum": 0,
                "maximum": 30,
            }
        ],
    }


def status(client, headers=SETTINGS):
    response = client.get("/onboarding", headers=headers)
    assert response.status_code == 200, response.text
    assert "GA_PLUGIN_SYNTHETIC_ONBOARDING_SECRET" not in response.text
    assert "synthetic-configuration-value" not in response.text
    data = response.json()
    for integration in data["integrations"]:
        integration["capabilities"].sort()
    return data


def owner_state(engine):
    with engine.connect() as connection:
        profile = connection.execute(
            text("SELECT locale,timezone,units FROM people WHERE singleton")
        ).one()
        value = connection.scalar(text("SELECT value FROM app_state WHERE key=:key"), {"key": KEY})
        packs = connection.execute(
            text(
                "SELECT pack_key,revision,llm_enabled,tracking_enabled FROM module_configs ORDER BY pack_key"
            )
        ).all()
        return profile, value, packs


def remove_generated(value):
    if isinstance(value, list):
        return [remove_generated(item) for item in value]
    if isinstance(value, dict):
        return {
            key: remove_generated(item)
            for key, item in value.items()
            if key
            not in {"id", "definition_id", "definition_version_id", "form_id", "completed_at"}
        }
    return value


def save(engine, native, plan):
    before = owner_state(engine)
    with Session(engine) as session:
        expected = apply_onboarding(session, plan)
        session.rollback()
    assert owner_state(engine) == before
    response = native.put("/onboarding", headers=SETTINGS, json=plan)
    assert response.status_code == 200, response.text
    actual = response.json()
    for decision in (expected, actual):
        for created in decision["created_trackers"]:
            action = created["action"]
            assert action["id"] == action["form_id"] == f"create:{action['definition_version_id']}"
    assert remove_generated(expected) == remove_generated(actual), (expected, actual)
    assert owner_state(engine)[1] == actual["preferences"]
    return actual


def exercise(engine, python, native, owner_id):
    for route, access, expected in (
        ("/onboarding", {}, 401),
        ("/onboarding", MANAGER, 403),
        ("/tracker-profile", {}, 401),
    ):
        left, right = [client.get(route, headers=access) for client in (python, native)]
        assert left.status_code == right.status_code == expected
        assert left.json() == right.json()

    initial = status(native)
    assert initial == status(python)
    assert not initial["complete"] and initial["can_finish_without_sources"]
    assert [entry["available"] for entry in initial["integrations"]] == [True, False, True, False]
    plan = {
        "locale": "ru",
        "timezone": "Europe/Bratislava",
        "units": "imperial",
        "selected_packs": ["general_diary", "caffeine"],
        "reminder_packs": ["caffeine"],
        "trackers": [draft("fixture_onboarding")],
        "source_instance_ids": [],
        "channel": WEB,
        "fallback_channels": [FALLBACK],
        "model_categories": ["health", "diary"],
    }
    before = owner_state(engine)
    for invalid in (
        {"timezone": "invalid/fixture"},
        {"reminder_packs": ["sleep"]},
        {"selected_packs": ["not_a_pack"]},
        {"fallback_channels": [WEB]},
        {"fallback_channels": [FALLBACK, FALLBACK]},
        {"trackers": [plan["trackers"][0], plan["trackers"][0]]},
    ):
        left, right = [
            client.put("/onboarding", headers=SETTINGS, json=plan | invalid)
            for client in (python, native)
        ]
        assert left.status_code == right.status_code == 422, (left.text, right.text)
        assert left.json() == right.json(), (left.text, right.text)
    assert owner_state(engine) == before

    created = save(engine, native, plan)
    assert created["complete"] and created["preferences"]["source_instance_ids"] == []
    assert created["preferences"]["revision"] == 1 and len(created["created_trackers"]) == 1
    completed = status(native)
    assert completed == status(python) and completed["complete"]
    for client in (python, native):
        profile = client.get("/tracker-profile", headers=MANAGER)
        assert profile.status_code == 200
        assert profile.json() == {"locale": "ru", "timezone": "Europe/Bratislava"}

    with Session(engine) as session, session.begin():
        assert session.scalars(select(Person.id)).one() == owner_id
        assert session.scalar(select(SourceConnection.id)) is None
        assert session.scalar(select(ChannelBinding.id)) is None
        definition_id, version_id = session.execute(
            select(EventDefinition.id, EventDefinitionVersion.id)
            .select_from(EventDefinitionVersion)
            .join(EventDefinition)
            .where(EventDefinition.key == "user.fixture_onboarding")
        ).one()
        assert str(version_id) == created["created_trackers"][0]["action"]["definition_version_id"]
        remembered = Conversation(
            owner_id=owner_id,
            channel="web",
            channel_instance_id="local",
            state={"analysis_turns": [{"question": "Synthetic retained state"}]},
        )
        session.add(remembered)
        session.flush()
        conversation_id, epoch = remembered.id, remembered.memory_epoch
    saved = save(
        engine,
        native,
        plan
        | {
            "source_instance_ids": ["source:garmin:daily"],
            "model_categories": [],
            "reminder_packs": [],
        },
    )
    assert saved["preferences"]["revision"] == 2 and not saved["created_trackers"]
    assert saved["preferences"]["source_instance_ids"] == ["source:garmin:daily"]
    assert all(not pack["llm_enabled"] and not pack["reminders_enabled"] for pack in saved["packs"])
    with Session(engine) as session, session.begin():
        version = session.get(EventDefinitionVersion, version_id)
        assert version is not None and version.definition_id == definition_id
        retired = session.get(Conversation, conversation_id)
        assert retired.memory_epoch != epoch and retired.state == {}
        create_definition_draft(
            session,
            definition_spec(TrackerSetupDraft.model_validate(draft("fixture_collision"))),
            actor="synthetic",
            authorized=True,
        )
    before = owner_state(engine)
    invalid_tracker = plan | {
        "locale": "en",
        "trackers": [draft("fixture_before_collision"), draft("fixture_collision")],
    }
    left, right = [
        client.put("/onboarding", headers=ADMIN, json=invalid_tracker)
        for client in (python, native)
    ]
    assert left.status_code == right.status_code == 422, (
        left.status_code,
        left.text,
        right.status_code,
        right.text,
    )
    assert left.json() == right.json() and owner_state(engine) == before
    with Session(engine) as session:
        assert (
            session.scalar(
                select(EventDefinition.id).where(
                    EventDefinition.key == "user.fixture_before_collision"
                )
            )
            is None
        )
    print(
        "Matched Garmin-free owner setup, source replacement, retained tracker version, "
        "declared integrations, revocation and atomic rollback"
    )


@contextmanager
def native_client(binary, engine, directory, integrations, **settings):
    with subprocess.Popen(
        [binary.resolve(strict=True), "serve", "--port", "0"],
        cwd=directory,
        env={
            "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
            "GA_API_TOKENS": json.dumps(TOKENS),
            "GA_INTEGRATIONS": json.dumps(integrations),
            **settings,
        },
        stdout=subprocess.PIPE,
        text=True,
    ) as server:
        try:
            url = server.stdout.readline().strip().removeprefix("Listening on ")
            assert url.startswith("http://127.0.0.1:")
            with httpx.Client(base_url=url, timeout=8, trust_env=False) as native:
                yield native
        finally:
            server.terminate()
            server.wait(timeout=5)


def verify_extensions(binary, engine, directory):
    extension = directory / "fixture-extension"
    marker = directory / "launched"
    extension.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    extension.chmod(0o700)
    kinds = {
        "source": ("GA_SOURCE_PLUGIN_EXECUTABLES", ["observations"]),
        "channel": ("GA_CHANNEL_PLUGIN_EXECUTABLES", ["initiatives", "text"]),
        "model": ("GA_NATIVE_MODEL_PLUGINS", ["structured_output"]),
    }
    instances = {}
    registries = {}
    for kind, (variable, capabilities) in kinds.items():
        registry = {}
        unregistered = "integration provider is not registered"
        for name, reason in (
            ("registered", None),
            ("disabled", "integration is disabled"),
            ("relative", unregistered),
            ("missing", unregistered),
        ):
            identity = f"{kind}:fixture:{name}"
            instances[identity] = (
                {
                    "id": identity,
                    "kind": kind,
                    "provider": "fixture",
                    "enabled": name != "disabled",
                },
                capabilities if reason is None else [],
                reason,
            )
            if name != "missing":
                registry[identity] = str(extension) if name != "relative" else extension.name
        registries[variable] = json.dumps(registry)
    for declaration, _capabilities, _reason in instances.values():
        declaration["secret_refs"] = {"api_key": "GA_PLUGIN_SYNTHETIC_ONBOARDING_SECRET"}
        declaration["config"] = {"model": "synthetic-configuration-value"}
    with native_client(
        binary, engine, directory, [entry[0] for entry in instances.values()], **registries
    ) as client:
        found = status(client)["integrations"]
        assert [item["instance_id"] for item in found] == list(instances)
        for item in found:
            _spec, capabilities, reason = instances[item["instance_id"]]
            assert item["available"] == bool(capabilities)
            assert item["capabilities"] == capabilities and item["reason"] == reason
            assert item["verification_level"] == ("declared" if capabilities else "unavailable")
            assert item["contract_version"] == (1 if capabilities else None)
        assert str(extension) not in client.get("/onboarding", headers=SETTINGS).text
    assert not marker.exists()
    print(
        "Matched registered native extension declarations without reading secrets or launching them"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    settings = Settings(_env_file=None, api_tokens=TOKENS, integrations=INTEGRATIONS)
    try:
        with TemporaryDirectory(prefix="synthetic-onboarding-") as directory:
            directory = Path(directory)
            with native_client(args.binary, engine, directory, INTEGRATIONS) as native:
                assert not status(native)["complete"]
                with Session(engine) as session:
                    owner_id = session.scalars(select(Person.id)).one()
                    assert session.scalar(select(SourceConnection.id)) is None
                    assert session.scalar(select(ChannelBinding.id)) is None
                with TestClient(create_app(settings, engine)) as python:
                    exercise(engine, python, native, owner_id)
            verify_extensions(args.binary, engine, directory)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
