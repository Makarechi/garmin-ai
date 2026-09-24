from datetime import UTC, datetime

from sqlalchemy import func, select

from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.config import Settings
from garmin_ai.models import AppState, Event
from garmin_ai.telegram import handle_button, process_message, save_update
from garmin_ai.telegram_history import history_page, selected_action
from garmin_ai.tracker_chat_form import advance_chat_form, begin_chat_form
from garmin_ai.tracker_chat_selection import select_tracker_actions
from garmin_ai.tracker_forms import (
    FormSubmission,
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    form_for_action,
    preview_tracker,
    submit_form,
)

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def _form(db):
    draft = TrackerSetupDraft(
        key="focus_chat",
        name="Focus chat",
        locale="ru",
        fields=[
            TrackerFieldDraft(key="rating", label="Оценка", kind="scale", minimum=1, maximum=5),
            TrackerFieldDraft(
                key="count",
                label="Количество",
                kind="integer",
                unit="count",
                minimum=0,
                maximum=100,
                metric_semantics="event_count",
            ),
            TrackerFieldDraft(key="note", label="Заметка", kind="text"),
        ],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    return form_for_action(db, created["action"]["id"], locale="ru")


def test_guided_form_writes_three_fields_without_model(db):
    form = _form(db)
    pending = AppState(
        key="conversation:pending",
        value={"definition_version_id": str(form.action.definition_version_id)},
    )
    db.add(pending)
    assert "Когда" in begin_chat_form(pending, form, timezone="UTC", locale="ru")

    values = {"rating": "4", "count": "2", "note": "Нормально"}
    answers = ["сейчас", *[values[field.name] for field in form.fields]]
    outcomes = [
        advance_chat_form(
            db, pending, answer, actor="telegram:test", now=NOW, source="telegram_text"
        )
        for answer in answers
    ]

    assert [item.get("written", False) for item in outcomes] == [False, False, False, True], (
        outcomes
    )
    event = db.scalar(select(Event).where(Event.kind == "user.focus_chat"))
    assert event is not None
    assert {key: event.payload[key] for key in ("rating", "count", "note")} == {
        "rating": 4,
        "count": 2,
        "note": "Нормально",
    }
    assert (
        db.scalar(select(func.count()).select_from(Event).where(Event.kind == "user.focus_chat"))
        == 1
    )


def test_guided_form_uses_regional_english_locale(db):
    form = _form(db)
    pending = AppState(
        key="conversation:pending",
        value={"definition_version_id": str(form.action.definition_version_id)},
    )
    db.add(pending)
    prompt = begin_chat_form(pending, form, timezone="UTC", locale="en-US")

    assert prompt.startswith("When did the entry start?")


def test_ordinary_tracker_text_opens_guided_form_without_model(db, db_engine):
    form = _form(db)
    incoming = {
        "update_id": 5950,
        "message": {
            "message_id": 5950,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": "Записал Focus chat",
        },
    }
    assert save_update(db, incoming, 42)
    db.commit()

    response = process_message(db_engine, None, Settings(telegram_user_id=42), 5950)

    assert "Когда" in response
    db.expire_all()
    pending = db.get(AppState, "conversation:pending")
    assert pending.value["definition_version_id"] == str(form.action.definition_version_id)
    assert pending.value["chat_form"]["step"] == 0


def test_ambiguous_tracker_text_requires_numbered_choice(db, db_engine):
    _form(db)
    draft = TrackerSetupDraft(
        key="focus_other",
        name="Focus other",
        locale="ru",
        fields=[TrackerFieldDraft(key="score", label="Оценка", kind="scale", minimum=1, maximum=5)],
    )
    preview = preview_tracker(db, draft)
    second = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )

    def send(update_id, text):
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
        return process_message(db_engine, None, Settings(telegram_user_id=42), update_id)

    response = send(5960, "Focus")
    assert "1." in response and "2." in response
    db.expire_all()
    pending = db.get(AppState, "conversation:pending")
    assert pending.value["button"] == "tracker_select"

    response = send(5961, "2")
    assert "Оценка" in response
    db.expire_all()
    pending = db.get(AppState, "conversation:pending")
    assert pending.value["definition_version_id"] == second["action"]["definition_version_id"]
    assert pending.value["button"] == "tracker_form"


def test_ordinary_text_does_not_disclose_sensitive_tracker_without_channel_consent(db):
    draft = TrackerSetupDraft(
        key="private_focus",
        name="Private Focus",
        locale="ru",
        privacy="sensitive",
        fields=[TrackerFieldDraft(key="score", label="Оценка", kind="scale", minimum=1, maximum=5)],
    )
    preview = preview_tracker(db, draft)
    confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )

    assert not select_tracker_actions(
        db, "Private Focus", locale="ru", destination="telegram:primary"
    )


def test_guided_form_retries_invalid_value_without_advancing(db):
    form = _form(db)
    pending = AppState(
        key="conversation:pending",
        value={"definition_version_id": str(form.action.definition_version_id)},
    )
    db.add(pending)
    begin_chat_form(pending, form, timezone="UTC", locale="ru")
    advance_chat_form(db, pending, "сейчас", actor="telegram:test", now=NOW, source="telegram_text")
    values = {"rating": "4", "count": "2", "note": "Нормально"}
    for field in form.fields:
        if field.name == "rating":
            break
        advance_chat_form(
            db, pending, values[field.name], actor="telegram:test", now=NOW, source="telegram_text"
        )

    rejected = advance_chat_form(
        db, pending, "7", actor="telegram:test", now=NOW, source="telegram_text"
    )

    assert "максимума" in rejected["response"]
    assert pending.value["chat_form"]["step"] == 1 + [field.name for field in form.fields].index(
        "rating"
    )
    assert (
        db.scalar(select(func.count()).select_from(Event).where(Event.kind == "user.focus_chat"))
        == 0
    )


def test_guided_field_never_reaches_model_safety_screen(db, db_engine):
    form = _form(db)
    db.info["channel_destination_instance_id"] = "telegram:primary"
    handle_button(
        db, form.id, Settings(telegram_user_id=42), "telegram:42", 5900, datetime.now(UTC)
    )
    pending = db.get(AppState, "conversation:pending")
    begin_chat_form(pending, form, timezone="UTC", locale="ru")
    db.commit()

    class DenyProvider:
        def structured(self, *_args, **_kwargs):
            raise AssertionError("Custom field must not reach model safety screen")

    incoming = {
        "update_id": 5901,
        "message": {
            "message_id": 5901,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": "сейчас",
        },
    }
    assert save_update(db, incoming, 42)
    db.commit()
    response = process_message(db_engine, DenyProvider(), Settings(telegram_user_id=42), 5901)
    assert "Оценка" in response or "Количество" in response or "Заметка" in response


def test_telegram_generated_form_survives_messages_without_model(db, db_engine):
    form = _form(db)
    db.info["channel_destination_instance_id"] = "telegram:primary"
    opened = handle_button(
        db, form.id, Settings(telegram_user_id=42), "telegram:42", 6000, datetime.now(UTC)
    )
    assert "Оценка" in opened
    db.commit()

    replies = []

    def send(update_id, answer, *, voice=False):
        incoming = {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": int(datetime.now(UTC).timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                **({"voice": {"file_id": "synthetic-audio"}} if voice else {"text": answer}),
            },
        }
        assert save_update(db, incoming, 42)
        db.commit()
        replies.append(
            process_message(
                db_engine,
                None,
                Settings(telegram_user_id=42),
                update_id,
                transcript=answer if voice else None,
            )
        )

    send(6001, "начать")
    db.expire_all()
    order = db.get(AppState, "conversation:pending").value["chat_form"]["field_order"]
    values = {"rating": "4", "count": "2", "note": "Нормально"}
    answers = ["сейчас", *[values[name] for name in order]]
    for update_id, answer in enumerate(answers, 6002):
        send(update_id, answer, voice=update_id == 6001 + len(answers))

    assert "Когда" in replies[0]
    assert replies[-1] == "Запись сохранена.", replies
    db.expire_all()
    assert (
        db.scalar(select(func.count()).select_from(Event).where(Event.kind == "user.focus_chat"))
        == 1
    )
    assert process_message(db_engine, None, Settings(telegram_user_id=42), 6005) == replies[-1]
    db.expire_all()
    assert (
        db.scalar(select(func.count()).select_from(Event).where(Event.kind == "user.focus_chat"))
        == 1
    )


def test_history_edits_pinned_custom_entry_and_rejects_stale_selector(db, db_engine):
    form = _form(db)
    current = datetime.now(UTC)
    original = submit_form(
        db,
        form.id,
        FormSubmission(
            action_id=form.id,
            schema_hash=form.schema_hash,
            submission_id=form.submission_id,
            start=current,
            timezone="UTC",
            values={"rating": 3, "count": 2, "note": "Прежде"},
            units={"count": "count"},
        ),
        actor="test",
    )
    db.info["channel_instance"] = ChannelInstanceRef(channel="telegram", instance_id="primary")
    db.info["channel_destination_instance_id"] = "telegram:primary"
    db.info["conversation_now"] = current
    db.info["locale"] = "ru"
    history_page(db, current)
    selector = next(
        row.key.removeprefix("telegram:selection:")
        for row in db.scalars(
            select(AppState).where(AppState.key.startswith("telegram:selection:"))
        )
        if row.value["action"] == "edit"
    )
    prompt = selected_action(db, "h:" + selector, current, "telegram:test")
    assert "Когда" in prompt
    pending = db.get(AppState, "conversation:pending")
    order = pending.value["chat_form"]["field_order"]
    answers = {"rating": "5", "count": "=", "note": "="}
    db.commit()
    response = None
    for update_id, answer in enumerate(["=", *[answers[name] for name in order]], 6101):
        incoming = {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": int(datetime.now(UTC).timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": answer,
            },
        }
        assert save_update(db, incoming, 42)
        db.commit()
        response = process_message(db_engine, None, Settings(telegram_user_id=42), update_id)

    assert response == "Запись исправлена."
    db.expire_all()
    db.refresh(original)
    assert original.revision == 2
    assert (original.payload["rating"], original.payload["count"], original.payload["note"]) == (
        5,
        2,
        "Прежде",
    )
    assert "уже изменилась" in selected_action(db, "h:" + selector, current, "telegram:test")
    undo_update = {
        "update_id": 6105,
        "message": {
            "message_id": 6105,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": "/undo",
        },
    }
    assert save_update(db, undo_update, 42)
    db.commit()
    assert "отменено" in process_message(db_engine, None, Settings(telegram_user_id=42), 6105)
    db.refresh(original)
    assert original.revision == 3 and original.payload["rating"] == 3
