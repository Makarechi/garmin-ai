from datetime import UTC, datetime

from sqlalchemy import func, select

from garmin_ai.accounts import bind_channel
from garmin_ai.config import Settings
from garmin_ai.models import AppState, EventDefinition, EventDefinitionVersion, TrackerConfig
from garmin_ai.share_policy import list_tracker_shares
from garmin_ai.telegram import process_message, save_update


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
    assert db.scalar(select(func.count()).select_from(TrackerConfig)) == 0
    assert "обновлена" in _send(db, db_engine, 8107, "/privacy sensitive")
    assert "Сначала" in _send(db, db_engine, 8108, "/confirm_tracker")
    assert "sensitive" in _send(db, db_engine, 8109, "/preview")

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


def test_unpaired_channel_cannot_start_definition_setup(db, db_engine):
    response = _send(db, db_engine, 8301, "/newtracker")
    assert "доступ" in response.casefold() or "прав" in response.casefold()
    db.expire_all()
    assert db.get(AppState, "tracker:chat-setup:telegram:primary") is None
