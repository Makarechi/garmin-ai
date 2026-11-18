"""Compare confirmed community packs, retained history and protected dependencies with pinned Python."""

import argparse
import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import text

from garmin_ai.api import create_app
from garmin_ai.community_packs import catalog
from garmin_ai.config import ApiToken, Settings

ADMIN = "synthetic-community-pack-administrator"
MANAGER = "synthetic-community-pack-definition-manager"
READER = "synthetic-community-pack-diary-reader"
TOKENS = [
    {"key": ADMIN, "scopes": ["admin"]},
    {"key": MANAGER, "scopes": ["manage:definitions"]},
    {"key": READER, "scopes": ["read:diary"]},
]
PATH = "/community-packs"


def exercise(engine, python, native):
    def compare(method, suffix="", body=None, status=200, token=ADMIN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        replies = [
            client.request(method, PATH + suffix, json=body, headers=headers)
            for client in (python, native)
        ]
        assert all(reply.status_code == status for reply in replies), (
            method,
            suffix,
            status,
            [reply.text for reply in replies],
        )
        original, candidate = [reply.json() for reply in replies]
        if status == 200 and suffix == "/preview":
            tokens = original.pop("confirmation_token"), candidate.pop("confirmation_token")
            assert original == candidate, (original, candidate)
            return tokens, candidate
        if status != 422:
            assert original == candidate, (original, candidate)
        return candidate

    compare("GET", token=None, status=401)
    compare("GET", token=READER, status=403)
    assert compare("GET", token=MANAGER)["packs"] == catalog()
    headers = {"Authorization": f"Bearer {ADMIN}"}
    for pack in catalog():
        compare("POST", "/preview", pack, 403, token=READER)
        (_, _), protected = compare("POST", "/preview", pack, token=MANAGER)
        assert all(
            row["tracking_enabled"] is None and row["collection_enabled"] is None
            for row in protected["required_packs"]
        )
        (left, right), preview = compare("POST", "/preview", pack)
        assert preview["permissions"] == {
            "create_trackers": True,
            "enable_reminders": False,
            "enable_external_sources": False,
            "grant_model_or_channel_access": False,
        }
        result = native.post(
            PATH + "/import", json={"pack": pack, "confirmation_token": left}, headers=headers
        )
        assert result.status_code == 200, result.text
        created = result.json()["created"]
        assert len(created) == 1 and created[0]["tracker"]["reminder_enabled"] is False
        repeated = python.post(
            PATH + "/import", json={"pack": pack, "confirmation_token": right}, headers=headers
        )
        assert repeated.status_code == 200 and repeated.json()["created"] == [], repeated.text
        assert repeated.json()["changes"][0]["status"] == "already_installed"
        compare("POST", "/import", {"pack": pack, "confirmation_token": left}, 409)
        (left, _), preview = compare("POST", "/preview", pack)
        assert preview["changes"][0]["status"] == "already_installed"
        result = native.post(
            PATH + "/import", json={"pack": pack, "confirmation_token": left}, headers=headers
        )
        assert result.status_code == 200 and result.json()["created"] == [], result.text
    with engine.connect() as connection:
        original = connection.execute(
            text(
                "SELECT key,id,current_version FROM event_definitions WHERE namespace='user' ORDER BY key"
            )
        ).all()
        assert len(original) == 3 and all(row.current_version == 1 for row in original)
        assert (
            connection.scalar(
                text("SELECT count(*) FROM app_state WHERE starts_with(key,'tracker-consent:')")
            )
            == 0
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM tracker_configs WHERE reminder_enabled"))
            == 0
        )
    changed = deepcopy(catalog()[0])
    changed["version"] = 2
    changed["trackers"][0]["fields"][0]["label"] = "Different synthetic field"
    (left, _), preview = compare("POST", "/preview", changed)
    assert preview["changes"][0]["status"] == "conflict"
    compare("POST", "/import", {"pack": changed, "confirmation_token": left}, 409)
    same_version = deepcopy(catalog()[0])
    same_version["title"] = "Changed synthetic pack title"
    (_, _), preview = compare("POST", "/preview", same_version)
    assert preview["changes"][0]["status"] == "version_conflict"
    extension = deepcopy(catalog()[2])
    extension["version"] = 2
    additional = deepcopy(extension["trackers"][0])
    additional["key"] = "synthetic_extra_walk"
    additional["name"] = "Synthetic extra walk"
    extension["trackers"].append(additional)
    (left, right), preview = compare("POST", "/preview", extension)
    assert [row["status"] for row in preview["changes"]] == ["already_installed", "create"]
    result = native.post(
        PATH + "/import", json={"pack": extension, "confirmation_token": left}, headers=headers
    )
    assert result.status_code == 200 and len(result.json()["created"]) == 1, result.text
    repeated = python.post(
        PATH + "/import", json={"pack": extension, "confirmation_token": right}, headers=headers
    )
    assert repeated.status_code == 200 and repeated.json()["created"] == [], repeated.text
    bad = deepcopy(catalog()[0])
    for changes in (
        {"privacy": "private"},
        {"reminder_enabled": True, "reminder_time": "08:00"},
        {"shortcut": "\t"},
    ):
        invalid = deepcopy(bad)
        invalid["trackers"][0].update(changes)
        compare("POST", "/preview", invalid, 422)
    compare("POST", "/preview", dict(bad, version=1.0), 422)
    compare("POST", "/preview", dict(bad, required_packs=["missing"]), 422)
    compare("POST", "/preview", dict(bad, required_packs=[]), 422)
    invalid = deepcopy(bad)
    invalid["analysis"][0]["method"] = "mean"
    compare("POST", "/preview", invalid, 422)
    invalid = deepcopy(bad)
    invalid["limitations"] = ["🧪" * 16001]
    compare("POST", "/preview", invalid, 413)
    compare("POST", "/preview", invalid, 413, token=None)
    compare("POST", "/import", {"pack": invalid, "confirmation_token": "0" * 64}, 413)
    bulk = {
        "key": "synthetic_bulk",
        "version": 1,
        "title": "Synthetic maximum pack",
        "description": "Synthetic definitions at the supported tracker and field limits",
        "trackers": [
            {
                "key": f"synthetic_bulk_{index}",
                "name": f"Synthetic tracker {index}",
                "privacy": "sensitive",
                "fields": [
                    {"key": f"field_{field}", "label": f"Synthetic field {field}", "kind": "text"}
                    for field in range(32)
                ],
            }
            for index in range(8)
        ],
    }
    (left, right), _ = compare("POST", "/preview", bulk)
    installed = native.post(
        PATH + "/import", json={"pack": bulk, "confirmation_token": left}, headers=headers
    )
    assert installed.status_code == 200 and len(installed.json()["created"]) == 8, installed.text
    repeated = python.post(
        PATH + "/import", json={"pack": bulk, "confirmation_token": right}, headers=headers
    )
    assert repeated.status_code == 200 and repeated.json()["created"] == [], repeated.text
    (left, _), _ = compare("POST", "/preview", bad)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE app_state SET value=jsonb_set(value,'{expires_at}',to_jsonb('2000-01-01T00:00:00+00:00'::text)) WHERE key=:key"
            ),
            {"key": "community-pack-preview:" + left},
        )
    compare("POST", "/import", {"pack": bad, "confirmation_token": left}, 409)
    with engine.connect() as connection:
        retained = connection.execute(
            text(
                "SELECT key,id,current_version FROM event_definitions WHERE namespace='user' AND key=ANY(:keys) ORDER BY key"
            ),
            {"keys": [row.key for row in original]},
        ).all()
        assert retained == original
    print(
        "Matched community catalog, two-way confirmations, protected dependencies and unchanged historical versions"
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
