"""Compare synthetic tracker consent management over pinned and native HTTP."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx
from capture_reference import isolated_engine
from check_tracker_consent_writes import DEFINITION, MODEL, SECONDARY, SYSTEM, seed, snapshot
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import Settings

COLLECTION = "/tracker-sharing-consents"
CREATE = "/tracker-sharing/consents"
DEFINITIONS = "manage:definitions"
INTEGRATIONS = "manage:integrations"
CREDENTIALS = {
    "health": ["read:health"],
    "definitions": [DEFINITIONS],
    "integrations": [INTEGRATIONS],
    "both": [INTEGRATIONS, DEFINITIONS],
    "admin": ["admin"],
}
TOKENS = {name: f"synthetic-tracker-http-{name}-" + "x" * 16 for name in CREDENTIALS}
SCOPED = [{"key": TOKENS[name], "scopes": scopes} for name, scopes in CREDENTIALS.items()]


def at(kind, instance):
    return f"{COLLECTION}/{DEFINITION}/{kind}/{instance}"


def cases():
    model = {
        "definition_id": str(DEFINITION),
        "destination_kind": "model",
        "destination_instance_id": MODEL,
        "categories": ["facts", "schema", "schema"],
        "granted_at": "2026-03-12T12:00:00.2+05:30",
    }
    channel = {
        **model,
        "destination_kind": "channel",
        "destination_instance_id": SECONDARY,
        "policy_revision": 1,
    }
    yield "GET", COLLECTION, None, None, 401, True
    yield "GET", COLLECTION, "health", None, 403, True
    yield "GET", COLLECTION, "definitions", None, 403, True
    yield "GET", COLLECTION, "integrations", None, 200, True
    yield "POST", CREATE, "integrations", model, 403, True
    yield "POST", CREATE, "definitions", model, 403, True
    yield "PUT", COLLECTION, "health", model, 403, True
    yield "PUT", COLLECTION, "integrations", model, 200, True
    yield "GET", COLLECTION, "both", None, 200, True
    yield "POST", CREATE, "both", channel, 200, True
    yield "GET", COLLECTION, "admin", None, 200, True
    yield "PUT", COLLECTION, "integrations", {**channel, "categories": ["schema"]}, 200, True
    yield (
        "PUT",
        COLLECTION,
        "integrations",
        {**model, "granted_at": "2099-01-01T00:00:00Z"},
        422,
        True,
    )
    yield (
        "PUT",
        COLLECTION,
        "integrations",
        {**model, "definition_id": str(UUID(int=501))},
        404,
        True,
    )
    yield "PUT", COLLECTION, "integrations", {**model, "definition_id": str(SYSTEM)}, 404, True
    yield "PUT", COLLECTION, "integrations", {**model, "policy_revision": 2}, 422, False
    yield "PUT", COLLECTION, "integrations", {**model, "categories": []}, 422, False
    yield "PUT", COLLECTION, "integrations", {**model, "extra": "synthetic extra"}, 422, False
    yield (
        "PUT",
        COLLECTION,
        "integrations",
        {**model, "granted_at": "2026-11-05T11:00:00"},
        422,
        False,
    )
    yield "POST", COLLECTION, "both", model, 405, True
    yield "DELETE", at("channel", SECONDARY), "definitions", None, 403, True
    yield "DELETE", at("channel", "UPPERCASE"), "integrations", None, 200, True
    yield "DELETE", at("channel", SECONDARY), "integrations", None, 200, True
    yield "DELETE", at("channel", SECONDARY), "integrations", None, 200, True
    yield "GET", COLLECTION, "integrations", None, 200, True
    yield "DELETE", at("model", MODEL), "integrations", None, 200, True
    yield "GET", COLLECTION, "integrations", None, 200, True


def normalize(document):
    if isinstance(document, dict):
        if "consents" in document:
            return {"consents": [normalize(item) for item in document["consents"]]}
        if "categories" in document:
            return {**document, "categories": sorted(document["categories"])}
    return document


def compare(python, native, expected_row):
    method, path, token, payload, status, compare_content = expected_row
    headers = {"Authorization": "Bearer " + TOKENS[token]} if token else {}
    kwargs = {"headers": headers}
    if payload is not None:
        kwargs["json"] = payload
    expected = python.request(method, path, **kwargs)
    actual = native.request(method, path, **kwargs)
    assert expected.status_code == actual.status_code == status, (
        method,
        path,
        status,
        expected.status_code,
        actual.status_code,
        actual.text,
    )
    for name in ("cache-control", "allow"):
        assert expected.headers.get(name) == actual.headers.get(name), (method, path, name)
    if compare_content:
        assert normalize(expected.json()) == normalize(actual.json()), (
            method,
            path,
            expected.text,
            actual.text,
        )
    else:
        assert actual.json() == {"detail": "Invalid arguments"}


def run(binary, native_database):
    baseline_url = os.environ["GA_TEST_DATABASE_URL"]
    parsed = make_url(baseline_url)
    assert native_database.endswith("_test") and native_database != parsed.database
    native_url = parsed.set(database=native_database).render_as_string(hide_password=False)
    python_engine = isolated_engine()
    os.environ["GA_TEST_DATABASE_URL"] = native_url
    try:
        native_engine = isolated_engine()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = baseline_url
    engines = (python_engine, native_engine)
    try:
        for engine in engines:
            seed(engine)
        settings = Settings(api_tokens=SCOPED, api_key="", timezone="UTC")
        with (
            TestClient(create_app(settings, engine=python_engine)) as python,
            TemporaryDirectory() as directory,
            subprocess.Popen(
                [str(binary), "serve", "--port", "0"],
                cwd=directory,
                env={
                    "GA_DATABASE_URL": native_url,
                    "GA_API_TOKENS": json.dumps(SCOPED),
                    "GA_TIMEZONE": "UTC",
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as process,
        ):
            try:
                line = process.stdout.readline().strip()
                assert line.startswith("Listening on http://127.0.0.1:"), line
                with httpx.Client(base_url=line.split(" on ", 1)[1], trust_env=False) as native:
                    count = 0
                    for row in cases():
                        compare(python, native, row)
                        count += 1
                        if count == 11:
                            for engine in engines:
                                with engine.begin() as connection:
                                    connection.execute(
                                        text(
                                            "UPDATE app_state SET value=value-'policy_revision' WHERE key LIKE 'tracker-consent:%'"
                                        )
                                    )
                            compare(python, native, ("GET", COLLECTION, "admin", None, 200, True))
                            count += 1
                    print(f"{count} paired tracker consent HTTP responses matched")
            finally:
                process.terminate()
                process.wait(timeout=5)
        snapshots = []
        for engine in engines:
            with Session(engine) as session:
                snapshots.append(snapshot(session))
        assert snapshots[0] == snapshots[1]
        print("Stored grants and affected conversation and delivery state matched")
    finally:
        for engine in engines:
            engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("native_database")
    args = parser.parse_args()
    run(args.binary, args.native_database)
