"""Compare HTTP definition discovery and version paging with the Python baseline."""

import argparse
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.definitions import (
    DefinitionSpec,
    activate_definition,
    create_definition_draft,
    definition_state,
    propose_definition_revision,
    retire_definition,
    version_state,
)

ROUTE = "/definitions"
TOKENS = [
    {"key": "synthetic-definitions-reader-token", "scopes": ["read:diary"]},
    {"key": "synthetic-definitions-manager-token", "scopes": ["manage:definitions"]},
    {"key": "synthetic-definitions-health-token", "scopes": ["read:health"]},
]
READER, MANAGER, LIMITED = [{"Authorization": f"Bearer {token['key']}"} for token in TOKENS]
VERSIONED = "user.synthetic_versioned"
RETIRED = "user.synthetic_retired"


def spec(key, version=1):
    return DefinitionSpec(
        key=key,
        labels={"en": f"Synthetic definition version {version}"},
        topology="point",
        privacy="private",
        allowed_operations={"create", "query"},
        schema={
            "type": "object",
            "properties": {"value": {"type": "integer", "minimum": 0, "maximum": 50}},
            "required": ["value"],
            "additionalProperties": False,
        },
        fields={
            "value": {
                "id": f"{key}.value",
                "labels": {"en": "Count"},
                "semantic": "count",
                "unit": "count",
            }
        },
    )


def seed(engine):
    with Session(engine) as session, session.begin():
        versioned = create_definition_draft(
            session, spec(VERSIONED), actor="synthetic", authorized=True
        )
        for version in range(1, 8):
            if version > 1:
                propose_definition_revision(
                    session,
                    versioned.id,
                    versioned.revision,
                    spec(VERSIONED, version),
                    actor="synthetic",
                    authorized=True,
                )
            activate_definition(
                session, versioned.id, versioned.revision, actor="synthetic", authorized=True
            )
        retired = create_definition_draft(
            session, spec(RETIRED), actor="synthetic", authorized=True
        )
        activate_definition(
            session, retired.id, retired.revision, actor="synthetic", authorized=True
        )
        retire_definition(session, retired.id, retired.revision, authorized=True)


def compare(python, native, status=200, *, headers=READER, **params):
    source, result = [
        client.get(ROUTE, headers=headers, params=params) for client in (python, native)
    ]
    assert source.status_code == result.status_code == status, [source.text, result.text]
    assert source.json() == result.json(), [source.text, result.text]
    assert source.headers.get("x-next-cursor") == result.headers.get("x-next-cursor")
    return result.json(), result.headers.get("x-next-cursor")


def exercise(python, native):
    compare(python, native, 401, headers={})
    compare(python, native, 403, headers=LIMITED)
    default, cursor = compare(python, native, headers=MANAGER)
    assert len(default) == 10 and cursor == default[-1]["key"]
    assert compare(python, native, headers=READER) == (default, cursor)
    combined, full_cursor = compare(python, native, limit=50)
    assert full_cursor is None
    pages = []
    after = {}
    while True:
        page, cursor = compare(python, native, limit=3, **after)
        pages.extend(page)
        if cursor is None:
            break
        assert cursor != after.get("after_key")
        after = {"after_key": cursor}
    assert pages == combined
    assert any(row["status"] == "retired" and row["key"] == RETIRED for row in pages)
    active, cursor = compare(python, native, definition_key=VERSIONED)
    assert cursor is None and len(active) == 1
    definition = active[0]
    assert definition["current_version"] == 7 and definition["versions_before"] == 3
    assert [version["version"] for version in definition["versions"]] == list(range(3, 8))
    history, cursor = compare(
        python, native, definition_key=VERSIONED, before_version=definition["versions_before"]
    )
    assert cursor is None and history[0]["contract"] == definition["contract"]
    assert [version["version"] for version in history[0]["versions"]] == [1, 2]
    assert history[0]["versions_before"] is None
    assert compare(python, native, definition_key="user.absent")[0] == []
    assert compare(python, native, definition_key=VERSIONED, limit="3.0")[0] == active
    compare(python, native, 422, limit=51, before_version=0)
    compare(python, native, 422, limit="not-a-number")
    compare(python, native, 422, limit="")

    for client in (python, native):
        tool = client.post(
            "/tools/event_definitions",
            headers=READER,
            json={"arguments": {"definition_key": VERSIONED}},
        )
        assert tool.status_code == 200, tool.text
        assert tool.json() == {"rows": active, "next_cursor": None}
    print(
        "Matched definition permissions, page cursors, retired rows, history and the existing tool"
    )


def reference(engine, operation):
    with Session(engine) as session:
        outcome = operation(session)
        session.rollback()
        return outcome


def check_mutations(engine, python, native):
    key = "user.synthetic_management"
    draft = spec(key).model_dump(mode="json", by_alias=True)
    for client in (python, native):
        assert client.post(ROUTE, json=draft).status_code == 401
        assert client.post(ROUTE, json=draft, headers=READER).status_code == 403

    expected = reference(
        engine,
        lambda session: definition_state(
            create_definition_draft(session, draft, actor="api", authorized=True)
        ),
    )
    response = native.post(ROUTE, json=draft, headers=MANAGER)
    assert response.status_code == 200, response.text
    created = response.json()
    identity = created["id"]
    assert created == {**expected, "id": identity}
    assert created["status"] == "draft" and created["revision"] == 1

    item = f"{ROUTE}/{identity}"
    for client in (python, native):
        response = client.post(ROUTE, json=draft, headers=MANAGER)
        assert response.status_code == 422 and response.json() == {"detail": "Invalid arguments"}
        absent = f"{ROUTE}/{uuid4()}/activate"
        response = client.post(absent, json={"revision": 1}, headers=MANAGER)
        assert response.status_code == 404 and response.json() == {"detail": "Record not found"}

    revised_spec = spec(key, 2).model_dump(mode="json", by_alias=True)
    expected = reference(
        engine,
        lambda session: definition_state(
            propose_definition_revision(
                session, identity, 1, revised_spec, actor="api", authorized=True
            )
        ),
    )
    response = native.put(item, json={"revision": 1, "spec": revised_spec}, headers=MANAGER)
    assert response.status_code == 200 and response.json() == expected, response.text
    assert expected["status"] == "proposed" and expected["revision"] == 2
    for client in (python, native):
        stale = client.put(item, json={"revision": 1, "spec": revised_spec}, headers=MANAGER)
        assert stale.status_code == 409
        assert stale.json() == {"detail": "Definition changed; reload before editing"}
        stale = client.post(item + "/activate", json={"revision": 1}, headers=MANAGER)
        assert stale.status_code == 409
        assert stale.json() == {"detail": "Definition changed; reload before activation"}

    expected = reference(
        engine,
        lambda session: version_state(
            activate_definition(session, identity, 2, actor="api", authorized=True)
        ),
    )
    response = native.post(item + "/activate", json={"revision": 2}, headers=MANAGER)
    assert response.status_code == 200, response.text
    activated = response.json()
    assert activated == {**expected, "id": activated["id"]}
    assert activated["definition_id"] == identity and activated["version"] == 1
    current, _ = compare(python, native, definition_key=key)
    assert current[0]["revision"] == 3 and current[0]["contract"] == activated

    for client in (python, native):
        repeated = client.post(item + "/activate", json={"revision": 3}, headers=MANAGER)
        assert repeated.status_code == 422 and repeated.json() == {"detail": "Invalid arguments"}
        stale = client.post(item + "/retire", json={"revision": 2}, headers=MANAGER)
        assert stale.status_code == 409
        assert stale.json() == {"detail": "Definition changed; reload before retirement"}
    expected = reference(
        engine,
        lambda session: definition_state(retire_definition(session, identity, 3, authorized=True)),
    )
    response = native.post(item + "/retire", json={"revision": 3}, headers=MANAGER)
    assert response.status_code == 200 and response.json() == expected, response.text
    assert expected["status"] == "retired" and expected["revision"] == 4
    retired, _ = compare(python, native, definition_key=key)
    assert retired[0]["status"] == "retired" and retired[0]["contract"] == activated
    print("Matched draft, proposal, activation snapshot, revision conflicts and retirement")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        settings = Settings(_env_file=None, api_tokens=TOKENS)
        with (
            TestClient(create_app(settings, engine)) as python,
            TemporaryDirectory(prefix="synthetic-definitions-http-") as directory,
            subprocess.Popen(
                [args.binary.resolve(strict=True), "serve", "--port", "0"],
                cwd=directory,
                env={
                    "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
                    "GA_API_TOKENS": json.dumps(TOKENS),
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as server,
        ):
            try:
                seed(engine)
                url = server.stdout.readline().strip().removeprefix("Listening on ")
                assert url.startswith("http://127.0.0.1:")
                with httpx.Client(base_url=url, timeout=6, trust_env=False) as native:
                    exercise(python, native)
                    check_mutations(engine, python, native)
            finally:
                server.terminate()
                server.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
