"""Compare portable tracker exports with pinned Python and verify their data boundaries."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from capture_reference import isolated_engine
from check_tracker_setup import ADMIN, DRAFT, READER, SETUP, TOKENS, post
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.definitions import (
    activate_definition,
    propose_definition_revision,
    retire_definition,
)
from garmin_ai.models import EventDefinition
from garmin_ai.tracker_forms import TrackerSetupDraft, definition_spec


def seed(engine, python):
    draft = dict(DRAFT, key="synthetic_portable_tracker")
    preview = post(python, "/preview", draft)
    created = post(
        python, "", {"draft": draft, "confirmation_token": preview["confirmation_token"]}
    )
    identity = UUID(created["tracker"]["definition_id"])
    with Session(engine) as session, session.begin():
        definition = session.get(EventDefinition, identity)
        updated = definition_spec(TrackerSetupDraft.model_validate(draft))
        updated.labels["en"] = "Synthetic changed label"
        definition = propose_definition_revision(
            session,
            identity,
            definition.revision,
            updated,
            actor="synthetic-export",
            authorized=True,
        )
        activate_definition(
            session, identity, definition.revision, actor="synthetic-export", authorized=True
        )
        session.refresh(definition)
        retire_definition(session, identity, definition.revision, authorized=True)
    second = dict(
        DRAFT,
        key="synthetic_portable_active",
        topology="point",
        derived_duration=False,
        fields=[DRAFT["fields"][0]],
        shortcut=None,
    )
    preview = post(python, "/preview", second)
    active = post(
        python, "", {"draft": second, "confirmation_token": preview["confirmation_token"]}
    )
    return [str(identity), active["tracker"]["definition_id"]]


def exercise(engine, python, native):
    identities = seed(engine, python)

    def compare(body, status=200, token=ADMIN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        original, candidate = [
            client.post("/tracker-packs/export", json=body, headers=headers)
            for client in (python, native)
        ]
        assert original.status_code == candidate.status_code == status, (
            body,
            original.text,
            candidate.text,
        )
        if status == 422:
            return None
        assert original.json() == candidate.json(), (original.json(), candidate.json())
        return candidate.json()

    request = {"definition_ids": identities}
    compare(request, 401, token=None)
    compare(request, 403, token=READER)
    document = compare(request, token=SETUP)
    assert document["format"] == "garmin-ai-tracker-pack-v1"
    assert len(document["trackers"]) == 2
    archived = next(row for row in document["trackers"] if row["status"] == "retired")
    assert len(archived["versions"]) == 2
    assert len(archived["metrics"]) == 12 and len(archived["mappings"]) == 12
    assert compare({"definition_ids": identities + identities}) == document
    assert compare({"definition_ids": [identities[0]] * 32})["trackers"] == [archived]
    compare({"definition_ids": [identities[0]] * 33}, 422)
    compare({"definition_ids": []}, 422)
    compare({"definition_ids": ["invalid"]}, 422)
    compare({"definition_ids": identities, "extra": True}, 422)
    compare({"definition_ids": [str(uuid4())]}, 404)
    with Session(engine) as session:
        system = session.scalar(
            select(EventDefinition.id).where(EventDefinition.namespace == "system")
        )
    if system is not None:
        compare({"definition_ids": [str(system)]}, 404)
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO app_state(key,value) VALUES('synthetic-export-private',
            '{"description":"synthetic-private-health-fact","credential":"synthetic-secret-only"}'::jsonb)""")
        )
    again = compare(request)
    assert again == document
    wire = json.dumps(again)
    assert "synthetic-private-health-fact" not in wire and "synthetic-secret-only" not in wire
    assert all(str(identity) not in wire for identity in identities)
    print(
        "Matched archived and current tracker packs, historical metrics, duplicate selections and private data boundaries"
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
