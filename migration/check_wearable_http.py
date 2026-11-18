"""Exercise the immutable wearable HTTP receipt contract on a disposable TimescaleDB."""

import argparse
import json
import selectors
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.events import delete_event
from garmin_ai.scenario_packs import PackSelection, configure_scenario_pack, ensure_scenario_packs

PATH = "/wearable/marks"
DEVICE = str(UUID(int=401))
SECOND = str(UUID(int=402))
TOKENS = {
    "device": "synthetic-wearable-original-" + "x" * 12,
    "rotated": "synthetic-wearable-rotation-" + "x" * 12,
    "second": "synthetic-wearable-secondary-" + "x" * 12,
    "admin": "synthetic-wearable-admin-" + "x" * 12,
}
SCOPED = [
    {
        "key": key,
        "scopes": ["admin" if name == "admin" else "write:wearable"],
        "wearable_device_id": None if name == "admin" else SECOND if name == "second" else DEVICE,
    }
    for name, key in TOKENS.items()
]


def mark(identity, **fields):
    return {
        "id": str(UUID(int=identity)),
        "device_time": "2026-10-25T02:30:00.012345+05:45",
        "timezone": "Asia/Kathmandu",
        "clock_uncertainty_seconds": 1234,
        "payload": {"type": "caffeine", "beverage": "синтетический ☕"},
        **fields,
    }


def post(client, name, *marks):
    headers = {} if name is None else {"Authorization": "Bearer " + TOKENS[name]}
    return client.post(PATH, headers=headers, json={"marks": list(marks)})


def collection(engine, key, enabled):
    with Session(engine) as session, session.begin():
        row = ensure_scenario_packs(session, legacy_install=False)[key]
        configure_scenario_pack(
            session,
            key,
            PackSelection(
                revision=row.revision,
                tracking_enabled=row.tracking_enabled,
                collection_enabled=enabled,
                reminders_enabled=row.reminders_enabled,
                visible=row.visible,
                llm_enabled=row.llm_enabled,
                outcome_goal=row.outcome_goal,
            ),
        )


def counts(engine):
    with engine.connect() as connection:
        return connection.execute(
            text("""
            SELECT (SELECT count(*) FROM events),
                   (SELECT count(*) FROM audit_log WHERE actor LIKE 'wearable:%'),
                   (SELECT count(*) FROM app_state WHERE starts_with(key,'wearable-receipt:'))
        """)
        ).one()


def compare(python, native, engine):
    sample = mark(501)
    for name in (None, "admin"):
        left, right = post(python, name, sample), post(native, name, sample)
        assert left.status_code == right.status_code == 401
        assert left.json() == right.json()
    for invalid in [
        mark(501, clock_uncertainty_seconds=True),
        mark(501, clock_uncertainty_seconds=1.0),
        mark(501, device_time="2026-10-25T02:30:00"),
        mark(501, device_id=SECOND),
        mark(501, payload={"type": "note", "description": "synthetic"}),
        mark(
            501,
            payload={
                "type": "medication",
                "name": "synthetic",
                "dose": 2,
                "unit": "mg",
                "reason_event_id": str(UUID(int=400)),
            },
        ),
        mark(501, payload={"type": "medication", "dose": 2, "unit": "mg"}),
    ]:
        assert post(python, "device", invalid).status_code == 422
        denied = post(native, "device", invalid)
        assert denied.status_code == 422 and denied.json() == {"detail": "Invalid arguments"}
    assert counts(engine) == (0, 0, 0)
    header = {"Authorization": "Bearer " + TOKENS["device"]}
    assert (
        python.get("/tools", headers=header).json()
        == native.get("/tools", headers=header).json()
        == []
    )
    for tool in ("events", "health_range"):
        path = f"/tools/{tool}"
        for client in (python, native):
            assert client.post(path, headers=header, json={"arguments": {}}).status_code == 403

    initial = post(python, "device", sample)
    assert initial.status_code == 200, initial.json()
    response = post(native, "rotated", sample)
    assert response.status_code == 200 and response.json() == initial.json(), response.json()
    key = f"wearable-receipt:{DEVICE}:{sample['id']}"
    with Session(engine) as session, session.begin():
        event_id = session.scalar(
            text("SELECT value->>'event_id' FROM app_state WHERE key=:key"), {"key": key}
        )
        delete_event(session, UUID(event_id), revision=1, actor="synthetic-owner")
    response = post(native, "device", sample)
    assert response.status_code == 200 and response.json() == initial.json()
    assert response.json() == {"acknowledgements": [{"id": sample["id"], "accepted": True}]}

    medication = mark(
        502,
        device_time="2026-10-25T20:06:00Z",
        clock_uncertainty_seconds=3600,
        payload={"type": "medication", "name": "synthetic", "dose": 3, "unit": "mg"},
    )
    accepted = post(native, "device", medication, medication)
    assert accepted.status_code == 200, accepted.json()
    retry = post(python, "rotated", medication, medication)
    assert retry.status_code == 200 and retry.json() == accepted.json(), retry.json()
    with engine.connect() as connection:
        event = connection.execute(
            text("""
            SELECT source,status,confidence,time_precision,clock_uncertainty_seconds,
                   assertion_kind,transport,deleted,original_text
            FROM events WHERE kind='medication'
        """)
        ).one()
        assert event[:8] == (
            "wearable",
            "needs_confirmation",
            0,
            "unknown",
            3600,
            "device_measurement",
            "connector",
            False,
        )
        assert "требуют подтверждения владельца" in event.original_text
    assert counts(engine) == (2, 2, 2)

    changed = mark(501, payload={"type": "caffeine", "beverage": "изменённый пример"})
    for client in (python, native):
        reply = post(client, "device", mark(503), changed)
        assert reply.status_code == 409
        assert reply.json() == {
            "detail": "Wearable mark ID was already used with different content"
        }
        assert counts(engine) == (2, 2, 2)
    assert post(native, "second", changed).status_code == 200
    assert counts(engine) == (3, 3, 3)

    collection(engine, "caffeine", False)
    assert post(native, "device", sample).json() == initial.json()
    for client in (python, native):
        assert post(client, "device", mark(504)).status_code == 403
    assert counts(engine) == (3, 3, 3)

    collection(engine, "caffeine", True)
    with engine.begin() as connection:
        connection.execute(
            text("""
            CREATE FUNCTION refuse_wearable_fixture_receipt() RETURNS trigger AS $$
            BEGIN
                IF starts_with(NEW.key, 'wearable-receipt:') THEN
                    RAISE EXCEPTION 'synthetic wearable receipt failure';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        )
        connection.execute(
            text("""
            CREATE TRIGGER refuse_wearable_fixture_receipt BEFORE INSERT ON app_state
            FOR EACH ROW EXECUTE FUNCTION refuse_wearable_fixture_receipt()
        """)
        )
    try:
        for client in (python, native):
            assert post(client, "device", mark(505)).status_code == 503
            assert counts(engine) == (3, 3, 3)
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TRIGGER refuse_wearable_fixture_receipt ON app_state"))
            connection.execute(text("DROP FUNCTION refuse_wearable_fixture_receipt()"))
    assert post(native, "device", mark(505)).status_code == 200
    assert counts(engine) == (4, 4, 4)


def run(binary):
    engine = isolated_engine()
    try:
        collection(engine, "caffeine", True)
        collection(engine, "migraine", True)
        settings = Settings(api_tokens=SCOPED, api_key="", timezone="UTC")
        with (
            TestClient(create_app(settings, engine=engine)) as python,
            TemporaryDirectory() as directory,
            subprocess.Popen(
                [str(binary), "serve", "--port", "0"],
                cwd=directory,
                env={
                    "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
                    "GA_API_TOKENS": json.dumps(SCOPED),
                    "GA_TIMEZONE": "UTC",
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as process,
        ):
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    assert selector.select(timeout=5)
                line = process.stdout.readline().strip()
                assert line.startswith("Listening on http://127.0.0.1:"), line
                with httpx.Client(base_url=line.split(" on ", 1)[1], trust_env=False) as native:
                    compare(python, native, engine)
            finally:
                process.terminate()
                process.wait(timeout=5)
    finally:
        engine.dispose()
    print("Matched wearable authorization, immutable cross-runtime receipts, and atomic replay")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    run(args.binary.resolve(strict=True))
