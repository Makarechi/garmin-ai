from datetime import UTC, datetime, timedelta
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
        ("Если я принял таблетку сейчас, мне станет лучше", False),
        ("Was the medicine taken now", False),
        ("Голова не болела, но таблетку приняла сейчас, название не помню", True),
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


@pytest.mark.parametrize("intent", ["update", "close", "acknowledge"])
def test_compound_mutations_cannot_add_denied_incomplete_intake(db, intent):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent=intent,
                target_event_id=uuid4(),
                confidence=1,
                events=[
                    EventInput(start=NOW, timezone="UTC", payload={"type": "migraine"}),
                    EventInput(start=NOW, timezone="UTC", payload={"type": "medication"}),
                ],
            )

    result = interpret(
        db,
        Provider(),
        "Мигрень закончилась сейчас, ничего не принимал",
        Settings(timezone="UTC"),
        NOW,
    )
    assert result.intent == "clarify"


@pytest.mark.parametrize(
    "text,start",
    [
        ("Принял таблетку часа два назад", NOW - timedelta(hours=2)),
        ("Приняла таблетку два часа назад, название не помню", NOW - timedelta(hours=2)),
        ("I took medicine two hours ago", NOW - timedelta(hours=2)),
        ("Принял таблетку 2026-09-08T12:00:00+00:00", datetime(2026, 9, 8, 12, tzinfo=UTC)),
    ],
)
def test_reported_relative_and_explicit_date_intakes(db, text, start):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[EventInput(start=start, timezone="UTC", payload={"type": "medication"})],
            )

    assert interpret(db, Provider(), text, Settings(timezone="UTC"), NOW).intent == "log"


def test_incomplete_medication_correction_does_not_require_new_intake(db):
    from garmin_ai.agent import Interpretation, apply_command, interpret
    from garmin_ai.config import Settings

    row = create_event(
        db, EventInput(start=NOW, timezone="UTC", payload={"type": "medication"}), actor="owner"
    )

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="update",
                confidence=1,
                target_event_id=row.id,
                changed_fields=["payload.name"],
                events=[
                    EventInput(
                        start=NOW,
                        timezone="UTC",
                        payload={"type": "medication", "name": "synthetic"},
                    )
                ],
            )

    result = interpret(
        db, Provider(), "исправь название на synthetic", Settings(timezone="UTC"), NOW
    )
    assert result.intent == "update"
    apply_command(
        db, result, text="исправь название на synthetic", update_id=999, actor="owner", now=NOW
    )
    assert row.payload["name"] == "synthetic"
    assert row.payload["dose"] is None
    assert db.scalar(select(func.count()).select_from(Event)) == 1


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Мигрень началась в 14, через 20 минут принял таблетку, название не помню", {(14, 20)}),
        ("Таблетку принял, название не помню, в 11", {(11, 0)}),
        ("Принял «Нурофен» сейчас, дозу не помню", {(NOW.hour, NOW.minute)}),
        ("Он сказал «принял таблетку сейчас»", set()),
        ("Таблетку не принял, название не помню, в 11", set()),
        ("Принял таблетку, мигрень началась в 14", set()),
    ],
)
def test_intake_evidence_keeps_related_details_without_borrowing_other_times(text, expected):
    from garmin_ai.intake_assertion import reported_intake_times

    times = reported_intake_times(text, NOW, "UTC")
    assert {(stamp.hour, stamp.minute) for stamp in times} == expected


@pytest.mark.parametrize(
    "initial,reply,expected",
    [
        (
            "Принял таблетку, название не помню",
            "в 11",
            (NOW - timedelta(days=1)).replace(hour=11, minute=0),
        ),
        ("Принял таблетку сейчас", "дозу не помню", NOW),
    ],
)
def test_pending_intake_can_be_completed_without_repeating_assertion(db, initial, reply, expected):
    from garmin_ai.agent import Interpretation, apply_command, interpret
    from garmin_ai.config import Settings

    apply_command(
        db,
        Interpretation(intent="clarify", confidence=1, clarification="Время?"),
        text=initial,
        update_id=991,
        actor="owner",
        now=NOW,
    )

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[EventInput(start=expected, timezone="UTC", payload={"type": "medication"})],
            )

    later = NOW + timedelta(minutes=5)
    assert interpret(db, Provider(), reply, Settings(timezone="UTC"), later).intent == "log"
    assert (
        interpret(db, Provider(), "нет, ничего не принимал", Settings(timezone="UTC"), later).intent
        == "clarify"
    )
