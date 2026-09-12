from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.events import EventInput, delete_event, update_event
from garmin_ai.models import AppState, Event

KEY = "synthetic-wearable-key-00000000000000"
DEVICE = uuid4()


def client_for(engine, key=KEY, device=DEVICE):
    return TestClient(
        create_app(
            Settings(
                api_tokens=[
                    ApiToken(
                        key=key,
                        scopes={"write:wearable"},
                        wearable_device_id=device,
                    )
                ]
            ),
            engine,
        )
    ), {"Authorization": "Bearer " + key}


def mark():
    return {
        "id": str(uuid4()),
        "device_time": "2026-09-01T12:00:00Z",
        "timezone": "UTC",
        "payload": {"type": "caffeine", "beverage": "synthetic"},
    }


def test_offline_replay_and_key_rotation_never_duplicate_or_reveal_diary_state(db, db_engine):
    client, headers = client_for(db_engine)
    item = mark()
    first = client.post("/wearable/marks", headers=headers, json={"marks": [item]})
    assert first.status_code == 200
    assert first.json() == {"acknowledgements": [{"id": item["id"], "accepted": True}]}
    rotated, new_headers = client_for(db_engine, key="synthetic-rotated-wearable-key-000000")
    assert (
        rotated.post("/wearable/marks", headers=new_headers, json={"marks": [item]}).json()
        == first.json()
    )
    assert (
        rotated.post("/wearable/marks", headers=headers, json={"marks": [item]}).status_code == 401
    )
    assert db.scalar(select(func.count()).select_from(Event)) == 1
    event = db.scalar(select(Event))
    assert (
        event.status == "needs_confirmation"
        and event.confidence == 0
        and event.source == "wearable"
    )
    assert event.start == datetime(2026, 9, 1, 12, tzinfo=UTC)
    assert (
        db.get(AppState, f"wearable-receipt:{DEVICE}:{item['id']}").value["time_status"]
        == "unverified_device_clock"
    )
    update_event(
        db,
        event.id,
        EventInput(
            start=event.start,
            timezone="UTC",
            source="wearable",
            payload={"type": "caffeine", "beverage": "owner correction"},
        ),
        revision=event.revision,
        actor="owner",
    )
    delete_event(db, event.id, revision=event.revision, actor="owner")
    db.commit()
    assert (
        client.post("/wearable/marks", headers=headers, json={"marks": [item]}).json()
        == first.json()
    )
    db.expire_all()
    assert db.get(Event, event.id).deleted
    assert db.scalar(select(func.count()).select_from(Event)) == 1


def test_changed_replay_rolls_back_whole_batch(db, db_engine):
    client, headers = client_for(db_engine)
    existing = mark()
    assert (
        client.post("/wearable/marks", headers=headers, json={"marks": [existing]}).status_code
        == 200
    )
    existing["payload"]["beverage"] = "changed"
    response = client.post("/wearable/marks", headers=headers, json={"marks": [mark(), existing]})
    assert response.status_code == 409
    assert db.scalar(select(func.count()).select_from(Event)) == 1
    assert (
        db.scalar(
            select(func.count())
            .select_from(AppState)
            .where(AppState.key.like("wearable-receipt:%"))
        )
        == 1
    )


def test_wearable_key_cannot_read_or_mutate_existing_records(db, db_engine):
    from garmin_ai.tools import TOOLS

    client, headers = client_for(db_engine)
    assert client.get("/tools", headers=headers).json() == []
    for name in TOOLS:
        assert (
            client.post("/tools/" + name, headers=headers, json={"arguments": {}}).status_code
            == 403
        )
    for path in ("/exports/diary", "/operations", "/metrics", "/events/" + str(uuid4())):
        assert client.get(path, headers=headers).status_code == 403
    body = {
        "start": "2026-09-01T12:00:00Z",
        "payload": {"type": "note", "description": "synthetic"},
    }
    assert client.post("/events", headers=headers, json=body).status_code == 403
    assert (
        client.delete(
            "/events/" + str(uuid4()), headers=headers, params={"revision": 1}
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"device_id": str(uuid4())},
        {"timezone": "Invalid/Synthetic"},
        {"device_time": "2026-09-01T12:00:00"},
        {"clock_uncertainty_seconds": True},
        {"payload": {"type": "note", "description": "unsupported"}},
        {
            "payload": {
                "type": "medication",
                "name": "synthetic",
                "dose": 1,
                "unit": "mg",
                "reason_event_id": str(uuid4()),
            }
        },
    ],
)
def test_invalid_upload_is_rejected_without_writes(db, db_engine, changes):
    client, headers = client_for(db_engine)
    item = {**mark(), **changes}
    assert (
        client.post("/wearable/marks", headers=headers, json={"marks": [item]}).status_code == 422
    )
    assert db.scalar(select(func.count()).select_from(Event)) == 0


def test_repeated_medication_mark_in_one_batch_is_one_pending_report(db, db_engine):
    client, headers = client_for(db_engine)
    item = mark()
    item.update(
        clock_uncertainty_seconds=3600,
        payload={"type": "medication", "name": "synthetic", "dose": 1, "unit": "tablet"},
    )
    assert (
        client.post("/wearable/marks", headers=headers, json={"marks": [item, item]}).status_code
        == 200
    )
    event = db.scalar(select(Event))
    assert event.kind == "medication" and event.status == "needs_confirmation"
    assert db.scalar(select(func.count()).select_from(Event)) == 1


@pytest.mark.parametrize(
    "scopes,device",
    [({"write:wearable"}, None), ({"write:wearable", "admin"}, DEVICE), ({"read:health"}, DEVICE)],
)
def test_wearable_credentials_are_always_narrow(scopes, device):
    with pytest.raises(ValidationError):
        ApiToken(key=KEY, scopes=scopes, wearable_device_id=device)
