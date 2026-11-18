"""Compare tracker form submissions on two isolated databases with pinned Python."""

import argparse
import json
import os
import subprocess
from pathlib import Path

import httpx
from capture_reference import isolated_engine
from check_generated_forms import seed_complex
from check_tracker_actions import DIARY, seed
from fastapi.testclient import TestClient
from sqlalchemy import text

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings

WRITER = "synthetic-generated-forms-diary-writer"
TOKENS = [
    {"key": WRITER, "scopes": ["read:diary", "write:diary"]},
    {"key": DIARY, "scopes": ["read:diary"]},
]
AUTH = {"Authorization": f"Bearer {WRITER}"}


def normalized(value):
    if isinstance(value, dict):
        return {
            field: normalized(item)
            for field, item in value.items()
            if field
            not in {
                "id",
                "definition_id",
                "definition_version_id",
                "ingested_at",
                "recorded_at",
                "created_at",
                "updated_at",
            }
        }
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


def exercise(client, entries):
    actions = {
        row["definition_key"]: row["id"]
        for row in client.get("/actions", headers=AUTH).json()["actions"]
    }
    simple = actions["user.active_actions"]
    nested = actions["user.synthetic_form_projection"]
    replies = []

    def form(action):
        response = client.get(f"/forms/{action}", headers=AUTH)
        assert response.status_code == 200, response.text
        return response.json()

    def submit(action, body, status, token=WRITER):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = client.post(f"/forms/{action}/submit", json=body, headers=headers)
        assert response.status_code == status, (action, status, response.text)
        result = response.json()
        replies.append(normalized(result))
        return result

    active_form = form(simple)
    valid = {
        "action_id": simple,
        "schema_hash": active_form["schema_hash"],
        "submission_id": "e9c516c87a2c4fbd8d519861fc2736b9",
        "start": "2026-03-29T03:22:00+02:00",
        "timezone": "Europe/Bratislava",
        "values": {"description": "Synthetic added entry 🧪"},
    }
    submit(simple, valid, 401, token=None)
    submit(simple, valid, 403, token=DIARY)
    submit(simple, dict(valid, action_id="invalid"), 409)
    submit(simple, dict(valid, schema_hash="0" * 64), 409)
    submit(simple, dict(valid, submission_id=None), 422)
    submit(simple, dict(valid, values={"unknown": "synthetic"}), 422)
    submit(simple, dict(valid, units={"description": "unexpected"}), 422)
    submit(simple, dict(valid, end="2026-03-29T03:23:00+02:00"), 422)
    saved = submit(simple, valid, 200)
    repeated = submit(simple, valid, 200)
    assert saved["id"] == repeated["id"] and repeated["revision"] == 1
    submit(simple, dict(valid, values={"description": "Different synthetic value"}), 409)

    nested_form = form(nested)
    complex_body = dict(
        valid,
        action_id=nested,
        schema_hash=nested_form["schema_hash"],
        submission_id=None,
        operation_id="synthetic-bounded-form-operation",
        end="2026-03-29T03:24:00+02:00",
        values={
            "nested": [{"description": "тест 🧪", "count": 1_000_001}],
            "literal": "Синтетика 🧪",
            "decimal": [1.231, 1.239],
            "rating": 5,
            "select": "первый",
        },
    )
    submit(nested, dict(complex_body, end=None), 422)
    submit(nested, dict(complex_body, values={"nested": [{}]}), 422)
    submit(nested, complex_body, 200)

    def edit(entry, value):
        action = client.get(f"/actions/events/{entry}", headers=AUTH).json()["id"]
        contract = form(action)
        body = {
            "action_id": action,
            "schema_hash": contract["schema_hash"],
            "start": valid["start"],
            "timezone": valid["timezone"],
            "values": {"description": value},
        }
        result = submit(action, body, 200)
        assert result["id"] == entry and result["revision"] == 2
        submit(action, body, 409)

    edit(saved["id"], "Synthetic corrected entry")
    edit(entries["retired_actions"], "Synthetic corrected retired entry")
    return replies


def audit_counts(engine):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT action,count(*) FROM audit_log WHERE actor='api' GROUP BY action ORDER BY action"
            )
        ).all()


def verify(original, candidate, binary, candidate_url):
    original_entries = seed(original)
    candidate_entries = seed(candidate)
    seed_complex(original)
    seed_complex(candidate)
    settings = Settings(_env_file=None, api_tokens=[ApiToken(**spec) for spec in TOKENS])
    with TestClient(create_app(settings, original)) as python:
        expected = exercise(python, original_entries)
    with subprocess.Popen(
        [binary, "serve", "--port", "0"],
        env={"GA_DATABASE_URL": candidate_url, "GA_API_TOKENS": json.dumps(TOKENS)},
        stdout=subprocess.PIPE,
        text=True,
    ) as server:
        try:
            line = server.stdout.readline().strip()
            assert line.startswith("Listening on http://127.0.0.1:")
            with httpx.Client(
                base_url=line.removeprefix("Listening on "), timeout=5, trust_env=False
            ) as rust:
                actual = exercise(rust, candidate_entries)
        finally:
            server.terminate()
            server.wait(timeout=5)
    differences = [
        (index, before, after)
        for index, (before, after) in enumerate(zip(expected, actual, strict=True))
        if before != after
    ]
    assert not differences, differences
    audit = audit_counts(original)
    assert audit == audit_counts(candidate) and sum(count for _, count in audit) == 4
    print(f"Matched {len(actual)} tracker submissions and one audit for each saved revision")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    original = isolated_engine()
    candidate_url = os.environ["GA_CANDIDATE_DATABASE_URL"]
    try:
        os.environ["GA_TEST_DATABASE_URL"] = candidate_url
        candidate = isolated_engine()
        try:
            verify(original, candidate, args.binary.resolve(strict=True), candidate_url)
        finally:
            candidate.dispose()
    finally:
        original.dispose()


if __name__ == "__main__":
    main()
