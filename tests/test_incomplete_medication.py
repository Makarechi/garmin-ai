from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from garmin_ai.events import EventInput, Medication, create_event, undo_last, update_event
from garmin_ai.models import Audit, Event
from garmin_ai.telegram import diary_label

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def test_unknown_intake_can_be_completed_and_undone_without_inventing_history(db):
    event = EventInput(
        start=NOW,
        timezone="UTC",
        payload={"type": "medication"},
        original_text="Принял таблетку, название не помню",
    )
    row = create_event(db, event, actor="synthetic", idempotency_key="synthetic-intake")
    assert row.payload["name"] is None and row.payload["dose"] is None
    assert "название неизвестно" in diary_label(row)
    assert "доза неизвестна" in diary_label(row)
    complete = event.model_copy(
        update={"payload": Medication(name="synthetic", dose=1, unit="tablet")}
    )
    update_event(db, row.id, complete, revision=row.revision, actor="synthetic")
    assert row.payload["dose"] == 1
    undo_last(db, actor="synthetic")
    db.refresh(row)
    assert row.payload["name"] is None and row.payload["dose"] is None
    assert row.original_text == event.original_text
    assert db.scalar(select(func.count()).select_from(Event)) == 1
    assert db.scalar(select(func.count()).select_from(Audit)) == 3


@pytest.mark.parametrize(
    "payload",
    [{"name": ""}, {"dose": 0}, {"dose": -1}, {"dose": float("nan")}, {"unit": "guessed"}],
)
def test_partial_medication_still_rejects_invalid_known_details(payload):
    with pytest.raises(ValidationError):
        Medication(**payload)


@pytest.mark.parametrize(
    "text,accepted",
    [
        ("Ничего не принимал", False),
        ("I haven't taken medicine now", False),
        ("Принимала ли я таблетку сейчас", False),
        ("Я принимала таблетку", False),
        ("Название не знаю, но таблетку приняла сейчас", True),
        ("Я не принял таблетку", False),
        ("Принять таблетку?", False),
        ("Я принял таблетку?", False),
        ("I did not take medicine", False),
        ("Лекарство", False),
        ("Принял таблетку сейчас, название не помню", True),
        ("Выпила таблетку сейчас", True),
    ],
)
def test_empty_model_medication_requires_explicit_intake(db, text, accepted):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[EventInput(start=NOW, timezone="UTC", payload={"type": "medication"})],
            )

    result = interpret(db, Provider(), text, Settings(timezone="UTC"), NOW)
    assert (result.intent == "log") == accepted
    assert db.scalar(select(func.count()).select_from(Event)) == 0


@pytest.mark.parametrize("missing", ["name", "dose", "unit"])
def test_wearable_medication_still_requires_complete_mark(missing):

    from garmin_ai.wearable import WearableMark

    payload = {"type": "medication", "name": "synthetic", "dose": 1, "unit": "tablet"}
    del payload[missing]
    with pytest.raises(ValidationError, match="require name, dose and unit"):
        WearableMark(id=uuid4(), device_time=NOW, timezone="UTC", payload=payload)


@pytest.mark.parametrize("intent", ["update", "close"])
def test_compound_mutations_cannot_add_denied_incomplete_intake(db, intent):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent=intent,
                target_event_id=uuid4(),
                confidence=1,
                events=[EventInput(start=NOW, timezone="UTC", payload={"type": "medication"})],
            )

    result = interpret(
        db,
        Provider(),
        "Мигрень закончилась сейчас, ничего не принимал",
        Settings(timezone="UTC"),
        NOW,
    )
    assert result.intent == "clarify"
