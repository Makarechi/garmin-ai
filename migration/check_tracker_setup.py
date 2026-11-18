"""Compare tracker setup and cross-runtime confirmation tokens using pinned Python."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import text

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings

ADMIN = "synthetic-tracker-setup-administrator"
SETUP = "synthetic-tracker-setup-definitions"
READER = "synthetic-tracker-setup-diary-reader"
TOKENS = [
    {"key": ADMIN, "scopes": ["admin"]},
    {"key": SETUP, "scopes": ["manage:definitions"]},
    {"key": READER, "scopes": ["read:diary"]},
]
DRAFT = {
    "key": "synthetic_setup",
    "name": "Синтетический трекер 🧪",
    "locale": "ru-ru",
    "topology": "bounded_interval",
    "derived_duration": True,
    "shortcut": "Синтетика",
    "privacy": "sensitive",
    "fields": [
        {"key": "description", "label": "Описание", "kind": "text", "required": False},
        {"key": "flag", "label": "Флаг", "kind": "boolean"},
        {"key": "choice", "label": "Выбор", "kind": "choice", "options": ["один", "два"]},
        {"key": "count", "label": "Счёт", "kind": "integer", "minimum": "0", "maximum": 8},
        {"key": "rating", "label": "Оценка", "kind": "scale", "minimum": -2, "maximum": 3},
        {
            "key": "amount",
            "label": "Количество",
            "kind": "number",
            "unit": "mg",
            "minimum": "1.25",
            "maximum": 30.0,
            "metric_semantics": "interval_total",
        },
    ],
}


def post(client, suffix, body, expected=200, token=ADMIN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.post(f"/tracker-setups{suffix}", json=body, headers=headers)
    assert response.status_code == expected, (suffix, expected, response.text)
    return response.json()


def normalized(document):
    document = json.loads(json.dumps(document))
    token = document.pop("confirmation_token")
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    definition = document["definition"]
    definition["allowed_operations"] = sorted(definition["allowed_operations"])
    return document


def metadata(engine, action):
    with engine.connect() as connection:
        return connection.execute(
            text("""
            SELECT d.status,d.revision,v.version,count(m.id),v.schema_hash
              FROM event_definition_versions v JOIN event_definitions d ON d.id=v.definition_id
              LEFT JOIN event_metric_mappings m ON m.event_definition_version_id=v.id
             WHERE v.id=:version GROUP BY d.id,v.id
        """),
            {"version": action["definition_version_id"]},
        ).one()


def exercise(engine, python, native):
    assert post(python, "/preview", DRAFT, 401, token=None) == post(
        native, "/preview", DRAFT, 401, token=None
    )
    assert post(python, "/preview", DRAFT, 403, token=READER) == post(
        native, "/preview", DRAFT, 403, token=READER
    )
    original = post(python, "/preview", DRAFT, token=SETUP)
    candidate = post(native, "/preview", DRAFT, token=SETUP)
    assert normalized(original) == normalized(candidate), (
        normalized(original),
        normalized(candidate),
    )
    assert candidate["confirmation_token"] != original["confirmation_token"]
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM event_definitions WHERE key='user.synthetic_setup'")
            )
            == 0
        )
    form = {"draft": DRAFT, "confirmation_token": original["confirmation_token"]}
    wrong = dict(form, draft=dict(DRAFT, name="Changed synthetic draft"))
    assert post(python, "", wrong, 409) == post(native, "", wrong, 409)
    rust_created = post(native, "", form, token=SETUP)
    assert rust_created["tracker"]["shortcut"] == DRAFT["shortcut"]
    assert post(python, "", form, 409) == post(native, "", form, 409)
    rust_meta = metadata(engine, rust_created["action"])
    assert rust_meta[:4] == ("active", 2, 1, 6)

    reminder = dict(DRAFT, key="synthetic_reminder", reminder_enabled=True, reminder_time="08:05")
    receipt = post(native, "/preview", reminder, token=SETUP)["confirmation_token"]
    request = {"draft": reminder, "confirmation_token": receipt}
    assert post(python, "", request, 403, token=SETUP) == post(
        native, "", request, 403, token=SETUP
    )
    old_created = post(python, "", request)
    assert old_created["tracker"]["reminder_time"] == "08:05"
    assert metadata(engine, old_created["action"])[:4] == rust_meta[:4]
    post(native, "", request, 409)

    expired = dict(DRAFT, key="synthetic_expired")
    token = post(native, "/preview", expired)["confirmation_token"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE app_state SET value=jsonb_set(value,'{expires_at}',to_jsonb('2000-01-01T00:00:00+00:00'::text)) WHERE key=:key"
            ),
            {"key": f"tracker-preview:{token}"},
        )
    stale = {"draft": expired, "confirmation_token": token}
    assert post(python, "", stale, 409) == post(native, "", stale, 409)
    bad = dict(DRAFT, fields=[dict(DRAFT["fields"][-1], minimum=-1)])
    for client in (python, native):
        post(client, "/preview", bad, 422)
    print(
        "Matched tracker preview, two-way confirmation, metric bindings and stale or denied setup"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        settings = Settings(_env_file=None, api_tokens=[ApiToken(**spec) for spec in TOKENS])
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
