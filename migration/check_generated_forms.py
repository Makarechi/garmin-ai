"""Compare tracker form reads and the exact saved-version boundaries with pinned Python."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
from capture_reference import isolated_engine
from check_tracker_actions import DIARY, HEALTH, seed
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.definitions import (
    activate_definition,
    create_definition_draft,
)


def seed_complex(engine):
    properties = {
        "nested": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {"$ref": "#/$defs/payload"},
        },
        "literal": {"type": "string", "const": "Синтетика 🧪", "maxLength": 100},
        "decimal": {
            "type": "array",
            "minItems": 2,
            "maxItems": 3,
            "items": {"type": "number", "exclusiveMinimum": 1.23, "exclusiveMaximum": 1.24},
        },
        "rating": {"$ref": "#/$defs/range", "minimum": 2},
        "numeric_choice": {"$ref": "#/$defs/numeric_choices", "enum": [1.0]},
        "select": {"type": "string", "enum": ["первый", "второй"], "maxLength": 100},
    }
    draft = {
        "key": "user.synthetic_form_projection",
        "labels": {"ru": "Синтетическая форма", "en": "Synthetic nested form"},
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": ["nested", "literal"],
            "$defs": {
                "payload": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "description": {
                            "type": "string",
                            "const": "тест 🧪",
                            "maxLength": 100,
                        },
                        "count": {"type": "integer", "minimum": 1_000_000, "maximum": 1_000_001},
                    },
                    "required": ["description", "count"],
                },
                "range": {"type": "integer", "minimum": 1, "maximum": 8},
                "numeric_choices": {"type": "number", "enum": [1, 3.5]},
            },
        },
        "fields": {
            name: {
                "id": f"user.synthetic_form_projection.{name}",
                "labels": {"ru": f"Поле {name}", "en": f"Field {name}"},
                "semantic": "ordinal" if name == "rating" else "nominal",
            }
            for name in properties
        },
        "topology": "bounded_interval",
    }
    with Session(engine) as session:
        definition = create_definition_draft(
            session, draft, actor="synthetic-fixture", authorized=True
        )
        activate_definition(
            session, definition.id, definition.revision, actor="synthetic-fixture", authorized=True
        )
        session.commit()


def verify(engine, binary):
    entries = seed(engine)
    seed_complex(engine)
    tokens = [{"key": DIARY, "scopes": ["read:diary"]}, {"key": HEALTH, "scopes": ["read:health"]}]
    settings = Settings(_env_file=None, api_tokens=[ApiToken(**item) for item in tokens])
    with TestClient(create_app(settings, engine)) as python:
        with subprocess.Popen(
            [binary, "serve", "--port", "0"],
            env={
                "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                "GA_API_TOKENS": json.dumps(tokens),
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
    auth = {"Authorization": f"Bearer {DIARY}"}

    def form(action, status, locale=None, key=DIARY):
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        params = {"locale": locale} if locale else {}
        first, second = [
            client.get(f"/forms/{action}", params=params, headers=headers)
            for client in (python, rust)
        ]
        assert first.status_code == second.status_code == status, (first.text, second.text)
        original, candidate = first.json(), second.json()
        nonce = None
        if status == 200:
            if action.startswith("create:"):
                original_nonce = original.pop("submission_id")
                nonce = candidate.pop("submission_id")
                assert all(re.fullmatch(r"[0-9a-f]{32}", item) for item in (original_nonce, nonce))
            else:
                assert candidate["submission_id"] is None
        assert original == candidate, (original, candidate)
        return candidate, nonce

    actions = {
        row["definition_key"]: row["id"]
        for row in python.get("/actions", headers=auth).json()["actions"]
    }
    simple = actions["user.active_actions"]
    deep = actions["user.synthetic_form_projection"]
    form(simple, 401, key=None)
    form(simple, 403, key=HEALTH)
    form(simple, 200)
    english, nonce = form(deep, 200, locale="en-GB")
    russian, repeated = form(deep, 200, locale="ru-RU")
    assert english["title"] != russian["title"] and nonce != repeated
    assert any(field["complex_json"] for field in russian["fields"])
    assert any(field["min_json_length"] for field in russian["fields"])
    assert next(field for field in russian["fields"] if field["name"] == "numeric_choice")[
        "options"
    ] == [1]
    for key in ("active_actions", "retired_actions"):
        action = python.get(f"/actions/events/{entries[key]}", headers=auth).json()
        current, _ = form(action["id"], 200, locale="en")
        assert current["initial_values"] == {"description": "Synthetic existing entry"}
        stale = f"edit:{entries[key]}:{action['revision'] + 1}"
        form(stale, 409)
        if key == "retired_actions":
            form(f"create:{action['definition_version_id']}", 409)
    form(f"edit:{entries['system']}:1", 404)
    form(f"create:{uuid4()}", 409)
    form("invalid", 404)
    print("Matched thirteen generated tracker-form responses and nested field limits")


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
