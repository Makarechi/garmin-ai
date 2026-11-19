"""Exercise native hypothesis registration, history and stop through HTTP."""

import argparse
import copy
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import check_coffee_sleep as coffee_fixture
import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai import hypotheses
from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.models import AppState, HealthDay, SourcePayload

TOKENS = [
    {
        "key": "synthetic-hypothesis-full-writer-proof",
        "scopes": ["read:health", "read:diary", "write:diary"],
    },
    {"key": "synthetic-hypothesis-both-reader-proof", "scopes": ["read:health", "read:diary"]},
    {"key": "synthetic-hypothesis-health-reader-proof", "scopes": ["read:health"]},
    {"key": "synthetic-hypothesis-diary-reader-proof", "scopes": ["read:diary"]},
]
WRITER, READER, HEALTH, DIARY = [token["key"] for token in TOKENS]
IDENTITIES = [UUID(int=7400001), UUID(int=7400002)]
CHECKED = [UUID(int=7400003), UUID(int=7400004)]


def normalize(value):
    if isinstance(value, dict):
        return {
            key: "clock"
            if key in {"created_at", "checked_at", "last_checked_at", "stopped_at"}
            else normalize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, str) and value in {str(identity) for identity in IDENTITIES + CHECKED}:
        return "synthetic-protocol"
    return value


def protocol(identity, today):
    return {
        "id": str(identity),
        "question": "Synthetic prospective HTTP question",
        "discovery_start": str(today - timedelta(days=9)),
        "discovery_end": str(today - timedelta(days=2)),
        "validation_start": str(today + timedelta(days=3)),
        "validation_end": str(today + timedelta(days=30)),
        "expires": str(today + timedelta(days=40)),
        "timezone": "UTC",
        "direction": "lower",
    }


def exercise(engine, python, native, today):
    count = 0
    clients = [python, native]
    specs = [protocol(identity, today) for identity in IDENTITIES]

    def compare(method, suffix, status=200, token=WRITER, bodies=None, identities=IDENTITIES):
        nonlocal count
        responses = []
        for index, (client, identity) in enumerate(zip(clients, identities, strict=True)):
            options = {"headers": {"Authorization": "Bearer " + token} if token else {}}
            if bodies is not None:
                options["json"] = bodies[index]
            response = client.request(method, "/hypotheses" + suffix.format(id=identity), **options)
            assert response.status_code == status, (
                method,
                suffix,
                status,
                response.status_code,
                response.text[:500],
            )
            responses.append(response)
            count += 1
        assert normalize(responses[0].json()) == normalize(responses[1].json()), (
            method,
            suffix,
            responses[0].json(),
            responses[1].json(),
        )
        assert responses[0].headers.get("retry-after") == responses[1].headers.get("retry-after")
        return [response.json() for response in responses]

    compare("POST", "", 401, token=None, bodies=specs)
    for token in [READER, HEALTH, DIARY]:
        compare("POST", "", 403, token=token, bodies=specs)
    begin = datetime.now(UTC)
    registered = compare("POST", "", bodies=specs)
    end = datetime.now(UTC)
    assert all(begin <= datetime.fromisoformat(value["created_at"]) <= end for value in registered)
    assert compare("POST", "", bodies=specs) == registered
    compare("POST", "", 409, bodies=[{**spec, "direction": "higher"} for spec in specs])
    assert compare("GET", "/{id}", token=READER) == registered
    compare("GET", "/{id}", 401, token=None)
    for token in [HEALTH, DIARY]:
        compare("GET", "/{id}", 403, token=token)
    compare("POST", "/{id}/recheck", 422)
    compare("POST", "/{id}/stop", 403, token=READER)
    stopped = compare("POST", "/{id}/stop")
    assert compare("POST", "/{id}/stop") == stopped
    assert compare("POST", "", bodies=specs) == stopped
    compare("POST", "/{id}/recheck", 409)
    compare("GET", "/{id}", 404, identities=[UUID(int=7400991), UUID(int=7400992)])

    created = today - timedelta(days=31)
    old_specs = [protocol(identity, created) for identity in CHECKED]
    with Session(engine) as session, session.begin():
        session.info["timezone"] = "UTC"
        for spec in old_specs:
            hypotheses.register(
                session,
                hypotheses.HypothesisSpec.model_validate(spec),
                datetime.combine(created, datetime.min.time(), UTC),
            )
    first = compare("POST", "/{id}/recheck", identities=CHECKED)
    assert all(value["check_count"] == 1 for value in first)
    repeated = compare("POST", "/{id}/recheck", identities=CHECKED)
    assert all(
        value["checks"] == previous["checks"]
        for value, previous in zip(repeated, first, strict=True)
    )
    changed_day = coffee_fixture.BASE + timedelta(days=5)
    with Session(engine) as session, session.begin():
        score = session.get(HealthDay, changed_day).sleep_score
        session.execute(
            text("UPDATE health_days SET sleep_score=99 WHERE day=:day"), {"day": changed_day}
        )
    revised = compare("POST", "/{id}/recheck", identities=CHECKED)
    assert all(
        value["check_count"] == 2 and value["checks"][0] == previous["checks"][0]
        for value, previous in zip(revised, first, strict=True)
    )
    with Session(engine) as session, session.begin():
        session.execute(
            text("UPDATE health_days SET sleep_score=:score WHERE day=:day"),
            {"score": score, "day": changed_day},
        )
    restored = compare("POST", "/{id}/recheck", identities=CHECKED)
    assert all(
        value["check_count"] == 2
        and value["current_check_index"] == 0
        and value["checks"] == previous["checks"]
        for value, previous in zip(restored, revised, strict=True)
    )

    def replace(values):
        with Session(engine) as session, session.begin():
            for identity, value in zip(CHECKED, values, strict=True):
                session.get(AppState, "hypothesis:" + str(identity)).value = value

    for field, value in [
        ("normalization_parser_version", -1),
        ("projection_generation", "synthetic-change"),
        ("method_version", "synthetic-change"),
    ]:
        replace([{**copy.deepcopy(before), field: value} for before in restored])
        compare("POST", "/{id}/recheck", 409, identities=CHECKED)
        replace(copy.deepcopy(restored))
    saturated = []
    for before in restored:
        value = copy.deepcopy(before)
        value["checks"] = [
            {
                **copy.deepcopy(before["checks"][0]),
                "evidence": {
                    **before["checks"][0]["evidence"],
                    "evidence_hash": "synthetic-cap-" + str(index),
                },
            }
            for index in range(10)
        ]
        value["check_count"] = 10
        saturated.append(value)
    replace(saturated)
    compare("POST", "/{id}/recheck", 422, identities=CHECKED)
    assert compare("GET", "/{id}", identities=CHECKED) == saturated
    replace(restored)

    with Session(engine) as session, session.begin():
        session.add(
            SourcePayload(
                source="garmin_connect",
                endpoint="daily",
                source_key=str(today),
                payload_hash="d" * 64,
                archive_key="sha256:" + "d" * 64,
                fetched_at=datetime.now(UTC),
                parser_version=0,
                status="pending",
                payload={"totalSteps": 0},
            )
        )
    fresh = [{**spec, "id": str(UUID(int=7400100 + index))} for index, spec in enumerate(specs)]
    compare("POST", "", 503, bodies=fresh)
    assert compare("POST", "", bodies=specs) == stopped
    compare("POST", "/{id}/recheck", 503, identities=CHECKED)
    assert compare("GET", "/{id}", identities=CHECKED) == restored
    compare("POST", "/{id}/stop", identities=CHECKED)
    print("Matched hypothesis HTTP lifecycle requests", count, flush=True)


def run(binary):
    engine = isolated_engine()
    try:
        today = datetime.now(UTC).date()
        coffee_fixture.BASE = today - timedelta(days=30)
        coffee_fixture.seed_coffee(engine)
        settings = Settings(
            _env_file=None, timezone="UTC", api_tokens=[ApiToken(**token) for token in TOKENS]
        )
        with (
            TestClient(create_app(settings, engine)) as python,
            subprocess.Popen(
                [binary, "serve", "--port", "0"],
                env={
                    "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                    "GA_TIMEZONE": "UTC",
                    "GA_API_TOKENS": json.dumps(TOKENS),
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as server,
        ):
            try:
                url = server.stdout.readline().strip().removeprefix("Listening on ")
                assert url.startswith("http://127.0.0.1:")
                with httpx.Client(base_url=url, timeout=120, trust_env=False) as native:
                    exercise(engine, python, native, today)
            finally:
                server.terminate()
                server.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
