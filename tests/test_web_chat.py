"""Real HTTP ingress, durable replies and local read evidence for web chat."""

from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.models import AppState, Event, OutboxMessage, TelegramUpdate

OWNER_KEY = "synthetic-owner-" + "a" * 32
LIMITED_KEY = "synthetic-limited-" + "b" * 32


def client(engine):
    settings = Settings(
        api_tokens=[
            ApiToken(key=OWNER_KEY, scopes={"admin"}),
            ApiToken(key=LIMITED_KEY, scopes={"read:diary", "write:diary"}),
        ]
    )
    return TestClient(create_app(settings, engine))


def headers(key=OWNER_KEY):
    return {"Authorization": "Bearer " + key}


def test_web_reply_is_rechecked_when_http_body_is_sent(db, db_engine, monkeypatch):
    from fastapi import FastAPI

    from garmin_ai import web_chat

    db.commit()
    allowed = {"value": True}
    monkeypatch.setattr(web_chat, "_intent_allowed", lambda *_args: allowed["value"])
    app = FastAPI()

    @app.get("/probe")
    def probe():
        response = web_chat._fenced_response(
            db_engine,
            {"reply": {"id": "synthetic", "text": "private fictional reply"}},
            {"synthetic": {}},
        )
        allowed["value"] = False
        return response

    result = TestClient(app).get("/probe")
    assert result.status_code == 200
    assert result.json() == {"reply": None}


def test_web_mixed_correction_undoes_the_whole_operation(db):
    from garmin_ai.agent import Interpretation, apply_command
    from garmin_ai.events import EventInput, create_event, undo_last
    from garmin_ai.models import Audit

    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    original = create_event(
        db,
        EventInput(start=now, payload={"type": "note", "description": "before"}),
        actor="web:local",
    )
    operation = uuid4()
    apply_command(
        db,
        Interpretation(
            intent="update",
            confidence=1,
            target_event_id=original.id,
            changed_fields=["payload.description"],
            events=[
                EventInput(start=now, payload={"type": "note", "description": "after"}),
                EventInput(start=now, payload={"type": "note", "description": "additional"}),
            ],
        ),
        text="synthetic correction and additional note",
        update_id="synthetic-mixed",
        actor="web:local",
        now=now,
        operation_id=operation,
    )
    db.flush()
    assert (
        db.scalar(select(func.count()).select_from(Audit).where(Audit.operation_id == operation))
        == 2
    )
    undo_last(db, actor="web:local")
    db.expire_all()
    assert db.get(Event, original.id).payload["description"] == "before"
    assert db.info["undo_count"] == 2


def test_web_chat_requires_owner_token_and_has_private_static_shell(db, db_engine):
    db.commit()
    api = client(db_engine)
    assert api.get("/chat").status_code == 200
    assert "connect-src 'self'" in api.get("/chat").headers["content-security-policy"]
    assert api.get("/web-chat/messages").status_code == 401
    assert api.get("/web-chat/messages", headers=headers(LIMITED_KEY)).status_code == 403
    assert (
        api.post(
            "/web-chat/messages",
            json={"client_message_id": str(uuid4()), "text": "/note synthetic"},
            headers=headers(LIMITED_KEY),
        ).status_code
        == 403
    )


def test_note_retry_pause_and_read_receipt_through_http(db, db_engine):
    db.commit()
    api = client(db_engine)
    identity = str(uuid4())
    body = {"client_message_id": identity, "text": "/note Synthetic walk in the park"}
    first = api.post("/web-chat/messages", json=body, headers=headers())
    assert first.status_code == 200, first.text
    assert first.json()["duplicate"] is False
    assert first.json()["reply"]["state"] == "queued"
    repeated = api.post("/web-chat/messages", json=body, headers=headers())
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["duplicate"] is True
    assert repeated.json()["reply"]["id"] == first.json()["reply"]["id"]
    changed = api.post(
        "/web-chat/messages",
        json={"client_message_id": identity, "text": "/note Different text"},
        headers=headers(),
    )
    assert changed.status_code == 409
    assert db.scalar(select(func.count()).select_from(Event)) == 1
    visible = api.get("/web-chat/messages", headers=headers())
    assert visible.status_code == 200
    assert visible.json()["replies"][0]["state"] == "queued"
    assert db.scalar(select(OutboxMessage)).state == "queued"
    outbox_id = first.json()["reply"]["id"]
    assert (
        api.post(f"/web-chat/messages/{outbox_id}/read", json={}, headers=headers()).status_code
        == 200
    )
    assert (
        api.post(f"/web-chat/messages/{outbox_id}/read", json={}, headers=headers()).status_code
        == 200
    )
    assert api.get("/web-chat/messages", headers=headers()).json()["replies"][0]["state"] == "read"

    paused = api.post(
        "/web-chat/messages",
        json={"client_message_id": str(uuid4()), "text": "/pause"},
        headers=headers(),
    )
    assert paused.status_code == 200, paused.text
    assert "остановлены" in paused.json()["reply"]["text"]
    db.expire_all()
    assert db.get(AppState, "proactive:enabled").value["enabled"] is False
    resumed = api.post(
        "/web-chat/messages",
        json={"client_message_id": str(uuid4()), "text": "/resume"},
        headers=headers(),
    )
    assert resumed.status_code == 200, resumed.text
    db.expire_all()
    assert db.get(AppState, "proactive:enabled").value["enabled"] is True


def test_web_clarification_is_separate_from_telegram(db, db_engine, monkeypatch):
    from garmin_ai.agent import Interpretation
    from garmin_ai.config import IntegrationInstance

    class Provider:
        instance_id = "model:gemini:primary"

        def structured(self, _instruction, _prompt, _schema):
            return Interpretation(intent="clarify", confidence=0.2, clarification="Уточните время")

        def close(self):
            pass

    from garmin_ai import integrations, onboarding

    monkeypatch.setattr(integrations, "create_model_provider", lambda *_args: Provider())
    monkeypatch.setattr(onboarding, "model_category_selected", lambda *_args: True)
    db.commit()
    api = TestClient(
        create_app(
            Settings(
                api_tokens=[ApiToken(key=OWNER_KEY, scopes={"admin"})],
                integrations=[
                    IntegrationInstance(id="model:gemini:primary", kind="model", provider="gemini")
                ],
            ),
            db_engine,
        )
    )
    result = api.post(
        "/web-chat/messages",
        json={"client_message_id": str(uuid4()), "text": "Synthetic ambiguous event"},
        headers=headers(),
    )
    assert result.status_code == 200, result.text
    assert "Уточните время" in result.json()["reply"]["text"]
    db.expire_all()
    assert db.get(AppState, "conversation:pending:web:local") is not None
    assert db.get(AppState, "conversation:pending") is None


def test_urgent_note_is_not_saved_as_a_diary_fact(db, db_engine):
    db.commit()
    api = client(db_engine)
    result = api.post(
        "/web-chat/messages",
        json={
            "client_message_id": str(uuid4()),
            "text": "/note Внезапная сильная боль в груди и трудно дышать",
        },
        headers=headers(),
    )
    assert result.status_code == 200, result.text
    assert "112" in result.json()["reply"]["text"]
    assert db.scalar(select(func.count()).select_from(Event)) == 0


def test_real_telegram_webhook_and_web_chat_share_confirmed_diary(db, db_engine):
    from garmin_ai.agent import Interpretation
    from garmin_ai.events import EventInput
    from garmin_ai.telegram import process_message

    class Provider:
        def structured(self, _instruction, _prompt, _schema):
            return Interpretation(
                intent="log",
                confidence=0.99,
                events=[
                    EventInput(
                        start=datetime.now(ZoneInfo("Europe/Bratislava")),
                        payload={"type": "note", "description": "synthetic Telegram note"},
                    )
                ],
            )

    secret = "synthetic-webhook-secret-12345"
    settings = Settings(
        api_tokens=[ApiToken(key=OWNER_KEY, scopes={"admin"})],
        telegram_user_id=42,
        telegram_webhook_secret=secret,
    )
    db.commit()
    api = TestClient(create_app(settings, db_engine))
    update = {
        "update_id": 701,
        "message": {
            "message_id": 701,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": "заметка сейчас",
        },
    }
    ingested = api.post(
        "/telegram/webhook",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
    )
    assert ingested.status_code == 200, ingested.text
    assert ingested.json()["accepted"] is True
    db.expire_all()
    telegram_row = db.scalar(
        select(TelegramUpdate).where(TelegramUpdate.payload["update_id"].as_integer() == 701)
    )
    assert telegram_row is not None
    assert "Сохранил" in process_message(db_engine, Provider(), settings, telegram_row.id)
    response = api.post(
        "/web-chat/messages",
        json={"client_message_id": str(uuid4()), "text": "/history"},
        headers=headers(),
    )
    assert response.status_code == 200, response.text
    assert "note" in response.json()["reply"]["text"]


def test_analysis_context_promotes_only_after_browser_read(db, db_engine, monkeypatch):
    from garmin_ai.agent import AgentStep, Interpretation, ReadCall, SafetyScreen
    from garmin_ai.config import IntegrationInstance

    class Provider:
        instance_id = "model:gemini:primary"

        def __init__(self):
            self.answer_calls = 0

        def structured(self, _instruction, _prompt, schema):
            if schema is Interpretation:
                return Interpretation(intent="question", confidence=1)
            if schema is SafetyScreen:
                return SafetyScreen(urgent=False)
            self.answer_calls += 1
            if self.answer_calls == 1:
                return AgentStep(
                    calls=[
                        ReadCall(
                            name="events",
                            arguments_json='{"start":"2026-10-07T00:00:00Z","end":"2026-10-08T00:00:00Z"}',
                        )
                    ]
                )
            return AgentStep(answer="Данных для ответа пока нет.", evidence_ids=[1])

        def close(self):
            pass

    from garmin_ai import integrations, onboarding

    monkeypatch.setattr(integrations, "create_model_provider", lambda *_args: Provider())
    monkeypatch.setattr(onboarding, "model_category_selected", lambda *_args: True)
    db.commit()
    api = TestClient(
        create_app(
            Settings(
                api_tokens=[ApiToken(key=OWNER_KEY, scopes={"admin"})],
                integrations=[
                    IntegrationInstance(id="model:gemini:primary", kind="model", provider="gemini")
                ],
            ),
            db_engine,
        )
    )
    identity = str(uuid4())
    response = api.post(
        "/web-chat/messages",
        json={"client_message_id": identity, "text": "Сколько записей?"},
        headers=headers(),
    )
    assert response.status_code == 200, response.text
    assert "Данных" in response.json()["reply"]["text"]
    db.expire_all()
    assert db.get(AppState, "analysis:conversation:pending:web:local") is not None
    history = db.get(AppState, "analysis:conversation")
    assert history is None or not history.value.get("turns")
    outbox_id = response.json()["reply"]["id"]
    assert (
        api.post(f"/web-chat/messages/{outbox_id}/read", json={}, headers=headers()).status_code
        == 200
    )
    db.expire_all()
    turns = db.get(AppState, "analysis:conversation").value["turns"]
    assert len(turns) == 1
    assert turns[0]["channel_instance_id"] == "web:local"
