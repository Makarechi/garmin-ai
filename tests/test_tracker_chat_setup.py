from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from garmin_ai.accounts import bind_channel
from garmin_ai.config import Settings
from garmin_ai.models import (
    AppState,
    ChannelBinding,
    EventDefinition,
    EventDefinitionVersion,
    TrackerConfig,
)
from garmin_ai.proactive import notification_decision
from garmin_ai.share_policy import list_tracker_shares
from garmin_ai.telegram import process_message, save_update


def test_proactive_notification_defers_while_tracker_setup_is_active(db):
    db.add(AppState(key="tracker:chat-setup:telegram:primary", value={"step": "name"}))
    decision = notification_decision(
        db,
        Settings(proactive_enabled=True, timezone="UTC"),
        datetime.now(UTC),
        include_budget=False,
        destination_instance_id="telegram:primary",
    )
    assert decision.action == "defer" and decision.reason == "tracker_setup_pending"


def test_proactive_notification_ignores_other_channel_setup(db):
    db.add(AppState(key="tracker:chat-setup:telegram:secondary", value={"step": "name"}))
    decision = notification_decision(
        db,
        Settings(proactive_enabled=True, timezone="UTC"),
        datetime(2026, 9, 20, 12, tzinfo=UTC),
        include_budget=False,
        destination_instance_id="telegram:primary",
    )
    assert decision.reason != "tracker_setup_pending"


def _send(db, engine, update_id: int, text: str) -> str:
    incoming = {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": text,
        },
    }
    assert save_update(db, incoming, 42)
    db.commit()
    return process_message(engine, None, Settings(telegram_user_id=42), update_id)


def test_paired_owner_creates_three_field_tracker_with_explicit_preview(db, db_engine):
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()

    assert "назвать" in _send(db, db_engine, 8101, "/newtracker")
    assert "поле" in _send(db, db_engine, 8102, "Фокус")
    assert "добавлено" in _send(db, db_engine, 8103, "Оценка | шкала 1-5")
    assert "добавлено" in _send(db, db_engine, 8104, "Количество | счётчик 0-100")
    assert "добавлено" in _send(db, db_engine, 8105, "Заметка | текст")
    preview = _send(db, db_engine, 8106, "/preview")
    assert all(name in preview for name in ("Фокус", "Оценка", "Количество", "Заметка"))
    assert "scale 1–5" in preview and "integer 0–100 count" in preview
    assert db.scalar(select(func.count()).select_from(TrackerConfig)) == 0
    assert "обновлена" in _send(db, db_engine, 8107, "/privacy sensitive")
    assert "Сначала" in _send(db, db_engine, 8108, "/confirm_tracker")
    sensitive_preview = _send(db, db_engine, 8109, "/preview")
    assert "sensitive" in sensitive_preview
    assert "недоступен в Telegram" in sensitive_preview

    created = _send(db, db_engine, 8110, "/confirm_tracker")
    assert created == "Трекер создан: Фокус"
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(TrackerConfig)) == 1
    definition = db.scalar(select(EventDefinition).where(EventDefinition.key.like("user.chat_%")))
    assert definition is not None and definition.current_version == 1
    version = db.scalar(
        select(EventDefinitionVersion).where(EventDefinitionVersion.definition_id == definition.id)
    )
    assert version.privacy == "sensitive"
    assert list_tracker_shares(db) == []
    assert process_message(db_engine, None, Settings(telegram_user_id=42), 8110) == created
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(TrackerConfig)) == 1
    assert db.get(AppState, "tracker:chat-setup:telegram:primary") is None


def test_setup_rejects_field_that_would_exceed_total_schema_limit(db, monkeypatch):
    from garmin_ai import tracker_chat_setup

    db.info["channel_destination_instance_id"] = "telegram:primary"
    monkeypatch.setattr(tracker_chat_setup, "_paired_owner", lambda *_args: True)
    row = AppState(
        key="tracker:chat-setup:telegram:primary",
        value={
            "key": "chat_synthetic",
            "name": "Synthetic",
            "fields": [],
            "locale": "en",
            "timezone": "UTC",
            "privacy": "private",
            "confirmation_token": None,
        },
    )
    db.add(row)
    db.flush()
    options = ", ".join(f"{index:02d}" + "x" * 98 for index in range(20))
    for index in range(32):
        before = len(row.value["fields"])
        result = tracker_chat_setup.advance_setup(
            db, f"Choice {index} | choice {options}", sender_id=42, actor="test", locale="en"
        )
        if "Field added" not in result:
            assert len(row.value["fields"]) == before
            assert before > 1
            break
    else:
        pytest.fail("Setup accepted a schema larger than 32 KiB")

    assert "Preview" in tracker_chat_setup.advance_setup(
        db, "/preview", sender_id=42, actor="test", locale="en"
    )


@pytest.mark.parametrize("name", ["Stroke", "Seizure", "Heart attack"])
def test_setup_accepts_emergency_term_as_tracker_name(db, db_engine, name):
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8201, "/newtracker")

    response = _send(db, db_engine, 8202, name)

    assert "поле" in response or "field" in response
    db.expire_all()
    assert db.get(AppState, "tracker:chat-setup:telegram:primary").value["name"] == name


def test_setup_creation_response_escapes_tracker_name(db, db_engine):
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8115, "/newtracker")
    _send(db, db_engine, 8116, "*Focus*")
    _send(db, db_engine, 8117, "Rating | scale 1-5")
    _send(db, db_engine, 8118, "/preview")
    assert _send(db, db_engine, 8119, "/confirm_tracker") == r"Трекер создан: \*Focus\*"


def test_setup_cancel_does_not_create_tracker(db, db_engine):
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()

    _send(db, db_engine, 8201, "/newtracker")
    _send(db, db_engine, 8202, "Фокус")
    _send(db, db_engine, 8203, "Оценка | шкала 1-5")
    assert _send(db, db_engine, 8204, "/cancel") == "Черновик удалён."
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(TrackerConfig)) == 0


def test_setup_can_select_sensitive_privacy_before_name(db, db_engine):
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8211, "/newtracker")
    assert "обновлена" in _send(db, db_engine, 8212, "/privacy sensitive")
    draft = db.get(AppState, "tracker:chat-setup:telegram:primary")
    assert draft.value["privacy"] == "sensitive"
    assert draft.value["name"] is None


def test_voice_caption_cancel_overrides_transcript_during_setup(db, db_engine):
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8215, "/newtracker")
    incoming = {
        "update_id": 8216,
        "message": {
            "message_id": 8216,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "voice": {"file_id": "synthetic"},
            "caption": "/cancel",
        },
    }
    assert save_update(db, incoming, 42)
    db.commit()
    assert (
        process_message(
            db_engine, None, Settings(telegram_user_id=42), 8216, transcript="a different name"
        )
        == "Черновик удалён."
    )


def test_setup_rejects_huge_count_bound_without_retrying(db, db_engine):
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8221, "/newtracker")
    _send(db, db_engine, 8222, "Focus")
    assert "Добавьте поле" in _send(db, db_engine, 8223, "Count | count 0-" + "9" * 400)


def test_setup_rejects_count_bounds_beyond_exact_metric_range(db, db_engine):
    from pydantic import ValidationError

    from garmin_ai.tracker_forms import TrackerFieldDraft

    with pytest.raises(ValidationError, match="exact float range"):
        TrackerFieldDraft(
            key="count",
            label="Count",
            kind="integer",
            unit="count",
            minimum=9_007_199_254_740_993,
            maximum=9_007_199_254_740_993,
        )
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8224, "/newtracker")
    _send(db, db_engine, 8225, "Focus")
    assert "Добавьте поле" in _send(
        db, db_engine, 8226, "Count | count 9007199254740993-9007199254740993"
    )
    db.expire_all()
    assert db.get(AppState, "tracker:chat-setup:telegram:primary").value["fields"] == []


def test_setup_preview_keeps_exact_large_integer_bound():
    from garmin_ai.tracker_chat_setup import _field_preview

    bound = 9_007_199_254_740_993
    assert str(bound) in _field_preview(
        {"kind": "integer", "minimum": bound, "maximum": bound, "label": "Count"}
    )


def test_explicit_setup_cancel_in_analytic_reply_discards_draft(db, db_engine, monkeypatch):
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8231, "/newtracker")
    monkeypatch.setattr("garmin_ai.conversation.is_analytic_reply", lambda *_args: True)

    assert _send(db, db_engine, 8232, "/cancel") == "Черновик удалён."
    db.expire_all()
    assert db.get(AppState, "tracker:chat-setup:telegram:primary") is None


def test_unpaired_owner_can_discard_an_existing_setup_draft(db, db_engine):
    binding = bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8236, "/newtracker")
    db.delete(binding)
    db.commit()

    assert _send(db, db_engine, 8237, "/cancel") == "Черновик удалён."
    db.expire_all()
    assert db.get(AppState, "tracker:chat-setup:telegram:primary") is None
    assert db.scalar(select(func.count()).select_from(ChannelBinding)) == 0


def test_unsupported_setup_locale_falls_back_to_english():
    from garmin_ai.tracker_chat_setup import _say

    assert _say("de", "Русский", "English") == "English"


def test_setup_preserves_urgent_and_global_commands(db, db_engine):
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8251, "/newtracker")

    assert "112" in _send(db, db_engine, 8252, "внезапная сильная боль")
    assert "контекст" in _send(db, db_engine, 8253, "/conversation").casefold()
    assert db.get(AppState, "tracker:chat-setup:telegram:primary") is not None
    assert "поле" in _send(db, db_engine, 8254, "Фокус")
    assert "Добавьте поле" in _send(db, db_engine, 8255, "Заметка |")


def test_symptom_label_is_accepted_as_setup_field(db, db_engine):
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8256, "/newtracker")
    _send(db, db_engine, 8257, "Focus")
    response = _send(db, db_engine, 8258, "Stroke symptoms | yes/no")
    assert "Поле добавлено" in response


def test_queued_setup_field_label_is_not_mistaken_for_emergency(db, db_engine):
    from garmin_ai.telegram import DiaryDeferred

    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8260, "/newtracker")
    _send(db, db_engine, 8261, "Focus")
    for update_id, text in ((8262, "earlier message"), (8263, "Stroke symptoms | yes/no")):
        assert save_update(
            db,
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": int(datetime.now(UTC).timestamp()),
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": text,
                },
            },
            42,
        )
    db.commit()

    with pytest.raises(DiaryDeferred):
        process_message(db_engine, None, Settings(telegram_user_id=42), 8263)


def test_setup_refuses_existing_pending_form(db, db_engine):
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.add(
        AppState(
            key="conversation:pending",
            value={
                "created_at": datetime.now(UTC).isoformat(),
                "channel_instance_id": "telegram:primary",
                "button": "note",
            },
        )
    )
    db.commit()

    assert "/cancel" in _send(db, db_engine, 8261, "/newtracker")
    assert db.get(AppState, "tracker:chat-setup:telegram:primary") is None


def test_setup_rejects_name_and_scale_that_break_button_or_unit_limits(db, db_engine):
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8271, "/newtracker")
    assert "64" in _send(db, db_engine, 8272, "A" * 65)
    assert "поле" in _send(db, db_engine, 8273, "Focus")
    assert "Добавьте поле" in _send(
        db, db_engine, 8274, "Rating | scale 1234567890123-1234567890124"
    )
    assert "Добавьте поле" in _send(db, db_engine, 8275, "/preview")
    assert "Добавьте поле" in _send(db, db_engine, 8276, "Count | count 0-" + "9" * 400)


def test_unpaired_channel_cannot_start_definition_setup(db, db_engine):
    response = _send(db, db_engine, 8301, "/newtracker")
    assert "доступ" in response.casefold() or "прав" in response.casefold()
    db.expire_all()
    assert db.get(AppState, "tracker:chat-setup:telegram:primary") is None


@pytest.mark.anyio
async def test_sensitive_setup_voice_stays_local_before_transcription(db, db_engine):
    from garmin_ai.llm import ProviderConsentRequired
    from garmin_ai.runtime import cached_transcription

    db.add(
        AppState(
            key="tracker:chat-setup:telegram:primary",
            value={"privacy": "sensitive"},
        )
    )
    db.add(AppState(key="telegram:transcript:8401", value={"text": "cached"}))
    db.commit()

    class Provider:
        instance_id = "model:gemini:primary"

        def transcribe(self, *_args):
            raise AssertionError("Sensitive setup must not reach the model")

    with pytest.raises(ProviderConsentRequired):
        await cached_transcription(db_engine, object(), Provider(), {"file_id": "synthetic"}, 8401)


@pytest.mark.anyio
async def test_same_message_privacy_caption_blocks_audio_before_transcription(db, db_engine):
    from garmin_ai.llm import ProviderConsentRequired
    from garmin_ai.runtime import cached_transcription

    db.add(AppState(key="tracker:chat-setup:telegram:primary", value={"privacy": "private"}))
    db.commit()

    class Provider:
        instance_id = "model:gemini:primary"

        def transcribe(self, *_args):
            raise AssertionError("Privacy-changing audio must not reach the model")

    with pytest.raises(ProviderConsentRequired):
        await cached_transcription(
            db_engine,
            object(),
            Provider(),
            {"file_id": "synthetic"},
            8402,
            caption="/privacy sensitive",
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "privacy, caption",
    [("private", "/privacy sensitive"), ("sensitive", "/preview")],
)
async def test_captioned_setup_command_blocks_audio_even_on_analytic_reply(
    db, db_engine, monkeypatch, privacy, caption
):
    from garmin_ai.llm import ProviderConsentRequired
    from garmin_ai.runtime import cached_transcription

    db.add(AppState(key="tracker:chat-setup:telegram:primary", value={"privacy": privacy}))
    db.commit()
    monkeypatch.setattr("garmin_ai.conversation.is_analytic_reply", lambda *_args: True)

    class Provider:
        instance_id = "model:gemini:primary"

        def transcribe(self, *_args):
            raise AssertionError("Setup command audio must not reach the model")

    with pytest.raises(ProviderConsentRequired):
        await cached_transcription(
            db_engine,
            object(),
            Provider(),
            {"file_id": "synthetic"},
            8403,
            caption=caption,
            reply_to_message_id=123,
        )


def test_setup_uses_one_voice_answer_and_preserves_analytic_reply(db, db_engine, monkeypatch):
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8501, "/newtracker")
    voice = {
        "update_id": 8502,
        "message": {
            "message_id": 8502,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "voice": {"file_id": "synthetic"},
            "caption": "Focus",
        },
    }
    assert save_update(db, voice, 42)
    db.commit()
    process_message(db_engine, None, Settings(telegram_user_id=42), 8502, transcript="Focus")
    db.expire_all()
    assert db.get(AppState, "tracker:chat-setup:telegram:primary").value["name"] == "Focus"

    monkeypatch.setattr("garmin_ai.conversation.is_analytic_reply", lambda *_args: True)
    monkeypatch.setattr("garmin_ai.telegram.answer_question", lambda *_args, **_kwargs: "analysis")
    reply = {
        "update_id": 8503,
        "message": {
            "message_id": 8503,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": "What changed?",
            "reply_to_message": {"message_id": 401},
        },
    }
    assert save_update(db, reply, 42)
    db.commit()
    response = process_message(db_engine, object(), Settings(telegram_user_id=42), 8503)
    assert response == "analysis"
    db.expire_all()
    assert db.get(AppState, "tracker:chat-setup:telegram:primary").value["fields"] == []


def test_stalled_diary_does_not_send_setup_answer_to_model(db, db_engine):
    from datetime import timedelta

    from garmin_ai.models import Job
    from garmin_ai.telegram import DiaryDeferred

    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8600, "/newtracker")
    _send(db, db_engine, 8601, "Focus")
    _send(db, db_engine, 8602, "/privacy sensitive")
    now = datetime.now(UTC)
    assert save_update(
        db,
        {
            "update_id": 8603,
            "message": {
                "message_id": 8603,
                "date": int(now.timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "older diary update",
            },
        },
        42,
    )
    older = db.scalar(select(Job).where(Job.dedup_key == "telegram:8603"))
    older.run_at = now + timedelta(hours=1)
    assert save_update(
        db,
        {
            "update_id": 8604,
            "message": {
                "message_id": 8604,
                "date": int(now.timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "Label | text",
            },
        },
        42,
    )
    db.commit()

    class NoModel:
        def structured(self, *_args, **_kwargs):
            raise AssertionError("Setup answers must remain local while waiting")

    with pytest.raises(DiaryDeferred):
        process_message(db_engine, NoModel(), Settings(telegram_user_id=42), 8604)


def test_pending_setup_start_defers_following_name_before_model(db, db_engine):
    from garmin_ai.models import Job
    from garmin_ai.telegram import DiaryDeferred

    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    for update_id, text in ((8611, "/newtracker"), (8612, "Focus")):
        assert save_update(
            db,
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": int(datetime.now(UTC).timestamp()),
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": text,
                },
            },
            42,
        )
    assert db.scalar(select(Job).where(Job.dedup_key == "telegram:8611")) is not None
    db.commit()

    class NoModel:
        def structured(self, *_args, **_kwargs):
            raise AssertionError("Setup name must wait locally")

    with pytest.raises(DiaryDeferred):
        process_message(db_engine, NoModel(), Settings(telegram_user_id=42), 8612)


@pytest.mark.parametrize("captioned_start", [False, True])
def test_provider_cooldown_keeps_pending_setup_ahead_of_local_diary(db, db_engine, captioned_start):
    from garmin_ai.models import Event, Job
    from garmin_ai.provider_gate import KEY, configuration_key
    from garmin_ai.telegram import DiaryDeferred

    settings = Settings(telegram_user_id=42)
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    for update_id, text in ((8613, "/newtracker"), (8614, "кофе")):
        message = {
            "message_id": update_id,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
        }
        if captioned_start and update_id == 8613:
            message.update(voice={"file_id": "synthetic-audio"}, caption=text)
        else:
            message["text"] = text
        assert save_update(
            db,
            {"update_id": update_id, "message": message},
            42,
        )
    assert db.scalar(select(Job).where(Job.dedup_key == "telegram:8613")) is not None
    db.add(
        AppState(
            key=KEY,
            value={
                "configuration": configuration_key(settings),
                "reason": "quota",
                "blocked_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
    )
    db.commit()

    with pytest.raises(DiaryDeferred):
        process_message(db_engine, None, settings, 8614)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Event)) == 0


def test_setup_preview_escapes_owner_supplied_markdown():
    from garmin_ai.tracker_chat_setup import _field_preview, _literal

    assert _literal("*Focus*") == r"\*Focus\*"
    assert (
        _field_preview({"kind": "choice", "label": "*Pain* | type", "options": ["[none]", "`yes`"]})
        == r"\*Pain\* \| type | choice \[none\], \`yes\`"
    )


def test_setup_voice_without_transcript_requests_text(db, db_engine):
    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    db.commit()
    _send(db, db_engine, 8701, "/newtracker")
    update = {
        "update_id": 8702,
        "message": {
            "message_id": 8702,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "voice": {"file_id": "synthetic"},
        },
    }
    assert save_update(db, update, 42)
    db.commit()

    reply = process_message(db_engine, None, Settings(telegram_user_id=42), 8702, transcript="")
    assert "Напишите ответ текстом" in reply
    assert db.get(AppState, "tracker:chat-setup:telegram:primary").value["name"] is None


def test_setup_voice_without_transcript_uses_english_for_unknown_locale(db, db_engine):
    from garmin_ai.accounts import owner

    bind_channel(
        db, channel="telegram", channel_instance_id="primary", external_id="42", confirmed=True
    )
    owner(db).locale = "de"
    db.add(AppState(key="preferences:onboarding", value={}))
    db.commit()
    _send(db, db_engine, 8711, "/newtracker")
    update = {
        "update_id": 8712,
        "message": {
            "message_id": 8712,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "voice": {"file_id": "synthetic"},
        },
    }
    assert save_update(db, update, 42)
    db.commit()
    reply = process_message(db_engine, None, Settings(telegram_user_id=42), 8712, transcript="")
    assert "Voice is unavailable" in reply
