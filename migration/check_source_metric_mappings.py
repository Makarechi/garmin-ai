"""Compare owner-confirmed source mappings and source-identity changes with pinned Python."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, IntegrationInstance, Settings
from garmin_ai.models import AppState, MetricDefinition, MetricDefinitionVersion
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)

ADMIN = "synthetic-source-mapping-administrator"
LIMITED = "synthetic-source-mapping-settings-user"
TOKENS = [
    {"key": ADMIN, "scopes": ["admin"]},
    {"key": LIMITED, "scopes": ["manage:definitions", "manage:integrations"]},
]
SOURCE = {
    "id": "source:sample:one",
    "kind": "source",
    "provider": "sample",
    "config": {"device": "synthetic-one"},
    "secret_refs": {"key": "GA_PLUGIN_SAMPLE_KEY"},
}
PATH = "/source-metric-mappings"


def seed(engine):
    draft = TrackerSetupDraft(
        key="mapping",
        name="Synthetic source mapping",
        locale="en",
        fields=[
            {
                "key": "minutes",
                "label": "Synthetic minutes",
                "kind": "number",
                "unit": "minutes",
                "minimum": 0,
                "maximum": 240,
            }
        ],
    )
    with Session(engine) as session, session.begin():
        preview = preview_tracker(session, draft)
        confirm_tracker(
            session,
            TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
            actor="synthetic-source-mapping",
        )
        version = (
            session.query(MetricDefinitionVersion)
            .join(MetricDefinition)
            .filter(MetricDefinition.key == "user.mapping.minutes")
            .one()
        )
        return {
            "source_instance_id": SOURCE["id"],
            "metric_version_id": str(version.id),
            "value_path": ["данные 🧪", "minutes"],
            "unit_path": ["данные 🧪", "unit"],
            "unit": "minutes",
        }


def exercise(engine, python, native, draft, settings, directory):
    def compare(method, suffix="", body=None, status=200, token=ADMIN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        replies = [
            client.request(method, PATH + suffix, json=body, headers=headers)
            for client in (python, native)
        ]
        assert all(reply.status_code == status for reply in replies), (
            method,
            suffix,
            body,
            [reply.text for reply in replies],
        )
        original, candidate = [reply.json() for reply in replies]
        if status == 200 and suffix == "/preview":
            left, right = original.pop("confirmation_token"), candidate.pop("confirmation_token")
            assert original == candidate, (original, candidate)
            return left, right
        if status != 422:
            assert original == candidate, (original, candidate)
        return candidate

    compare("POST", "/preview", draft, 401, token=None)
    compare("POST", "/preview", draft, 403, token=LIMITED)
    first, second = compare("POST", "/preview", draft)
    with engine.connect() as connection, connection.begin():
        connection.execute(
            text("SELECT id FROM event_definitions WHERE key='user.mapping' FOR UPDATE")
        )
        compare("POST", body={"draft": draft, "confirmation_token": first}, status=409)
    headers = {"Authorization": f"Bearer {ADMIN}"}
    created = native.post(PATH, json={"draft": draft, "confirmation_token": first}, headers=headers)
    assert created.status_code == 200, created.text
    saved = created.json()
    reused = python.post(PATH, json={"draft": draft, "confirmation_token": second}, headers=headers)
    assert reused.status_code == 200 and reused.json() == saved, reused.text
    identity = saved["id"]
    key = f"source-metric-mapping:{identity}"
    assert saved["state"] == "confirmed_not_normalized"
    assert compare("GET")["mappings"] == [saved]
    compare("POST", body={"draft": draft, "confirmation_token": first}, status=409)
    first, _ = compare("POST", "/preview", draft)
    changed = dict(draft, value_path=["changed"])
    compare("POST", body={"draft": changed, "confirmation_token": first}, status=409)
    token, _ = compare("POST", "/preview", changed)
    compare("POST", body={"draft": changed, "confirmation_token": token}, status=409)
    for invalid in (
        dict(draft, unit_path=draft["value_path"]),
        dict(draft, value_path=["\u0000"]),
        dict(draft, value_path=["a"] * 9),
        dict(draft, unit="missing"),
        dict(draft, source_instance_id="source:missing:one"),
        dict(draft, metric_version_id=str(uuid4())),
    ):
        compare("POST", "/preview", invalid, 422)
    with Session(engine) as session, session.begin():
        row = session.get(AppState, key)
        row.value = {
            **row.value,
            "state": "normalized",
            "synthetic_retained_record": "same-history",
        }
    compare("DELETE", f"/{identity}", status=409)
    settings.integrations[0].config["device"] = "synthetic-two"
    source = settings.integrations[0].model_dump(mode="json")
    (directory / ".env").write_text("GA_INTEGRATIONS='" + json.dumps([source]) + "'\n")
    first, second = compare("POST", "/preview", draft)
    compare("POST", body={"draft": draft, "confirmation_token": first}, status=409)
    confirmed = native.post(
        PATH,
        json={"draft": draft, "confirmation_token": first, "reconfirm_source_identity": True},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    acknowledged = python.post(
        PATH,
        json={"draft": draft, "confirmation_token": second, "reconfirm_source_identity": True},
        headers=headers,
    )
    assert acknowledged.status_code == 200 and acknowledged.json() == confirmed.json(), (
        acknowledged.text
    )
    assert confirmed.json()["id"] == identity and confirmed.json()["state"] == "normalized"
    with Session(engine) as session:
        row = session.get(AppState, key)
        assert row.value["synthetic_retained_record"] == "same-history"
        assert (
            session.query(AppState)
            .filter(AppState.key.startswith("source-mapping-confirmation:"))
            .count()
            == 1
        )
    token, _ = compare("POST", "/preview", draft)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE app_state SET value=jsonb_set(value,'{expires_at}',to_jsonb('2000-01-01T00:00:00+00:00'::text)) WHERE key=:key"
            ),
            {"key": "source-mapping-preview:" + token},
        )
    compare("POST", body={"draft": draft, "confirmation_token": token}, status=409)
    with Session(engine) as session, session.begin():
        original = session.get(AppState, key).value
        for index in range(3):
            identity = f"{index + 1:064x}"
            value = {**original, "id": identity, "state": "confirmed_not_normalized"}
            session.add(AppState(key=f"source-metric-mapping:{identity}", value=value))
        session.add(
            AppState(
                key="source-metric-mapping:" + "f" * 64,
                value={**original, "id": "f" * 64, "owner_id": str(uuid4())},
            )
        )
    page = compare("GET", "?limit=2")
    assert len(page["mappings"]) == 2 and page["next_cursor"]
    page = compare("GET", "?limit=2&after_id=" + page["next_cursor"])
    assert len(page["mappings"]) == 2 and page["next_cursor"] is None
    compare("GET", "?limit=101", status=422)
    compare("GET", "?after_id=invalid", status=422)
    compare("DELETE", "/" + "f" * 64, status=404)
    deletable = f"{1:064x}"
    removed = native.delete(PATH + "/" + deletable, headers=headers)
    assert removed.status_code == 200 and removed.json() == {"deleted": True}
    compare("DELETE", "/" + deletable, status=404)
    with Session(engine) as session, session.begin():
        session.add(AppState(key="preferences:onboarding", value={"source_instance_ids": []}))
    compare("POST", "/preview", draft, 422)
    with Session(engine) as session, session.begin():
        session.delete(session.get(AppState, "preferences:onboarding"))
    settings.integrations = None
    settings.model_fields_set.discard("integrations")
    settings.token_dir = directory / "tokens" / "garmin"
    settings.token_dir.mkdir(parents=True)
    (settings.token_dir / "garmin_tokens.json").write_text('{"synthetic":"metadata-only"}')
    (directory / ".env").write_text("")
    compare("POST", "/preview", dict(draft, source_instance_id="source:garmin:primary"))
    print(
        "Matched two-way source mapping confirmation, source identity preservation, strict access and pagination"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        draft = seed(engine)
        settings = Settings(
            _env_file=None,
            integrations=[IntegrationInstance(**SOURCE)],
            api_tokens=[ApiToken(**token) for token in TOKENS],
        )
        with tempfile.TemporaryDirectory(prefix="ga-source-mapping-") as path:
            directory = Path(path)
            (directory / ".env").write_text("GA_INTEGRATIONS='" + json.dumps([SOURCE]) + "'\n")
            with (
                TestClient(create_app(settings, engine)) as python,
                subprocess.Popen(
                    [args.binary.resolve(strict=True), "serve", "--port", "0"],
                    cwd=directory,
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
                        exercise(engine, python, native, draft, settings, directory)
                finally:
                    server.terminate()
                    server.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
