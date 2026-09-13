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
        (
            "Если завтра не станет лучше, позвоню врачу. Таблетку принял сейчас, название не помню",
            True,
        ),
        ("Муж принял таблетку сейчас, название не помню", False),
        ("He took medicine now", False),
        ("Она выпила таблетку сейчас", False),
        ("Иван принял таблетку сейчас, название не помню", False),
        ("Alex took medicine now", False),
        ("Я обычно принимал таблетку сейчас, название не помню", False),
        ("Кажется, я принял таблетку сейчас, название не помню", False),
        ("Возможно, принял таблетку сейчас", False),
        ("I think I took medicine now", False),
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
        ("Я принял таблетку в 2 приёма, название не помню", set()),
        ("Я обычно принимал таблетку в 11, название не помню", set()),
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
        (
            "Принял таблетку, название не помню",
            "два часа назад",
            NOW - timedelta(hours=2) + timedelta(minutes=5),
        ),
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


@pytest.mark.parametrize(
    "source,status",
    [
        ("inferred", "inferred"),
        ("inferred", "confirmed"),
        ("manual", "inferred"),
        ("manual", "needs_confirmation"),
    ],
)
def test_incomplete_medication_requires_reported_confirmed_source(source, status):
    with pytest.raises(ValidationError, match="confirmed reported intake"):
        EventInput(start=NOW, source=source, status=status, payload={"type": "medication"})


@pytest.mark.parametrize("missing", ["dose", "unit", "name"])
def test_incomplete_extraction_cannot_discard_explicit_dose(db, missing):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    payload = {"type": "medication", "name": "аспирин", "dose": 500, "unit": "mg"}
    payload[missing] = None

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log", confidence=1, events=[EventInput(start=NOW, payload=payload)]
            )

    assert (
        interpret(
            db, Provider(), "Принял аспирин 500 мг сейчас", Settings(timezone="UTC"), NOW
        ).intent
        == "clarify"
    )


@pytest.mark.parametrize("complete_first", [False, True])
def test_single_assertion_cannot_create_duplicate_incomplete_intakes(db, complete_first):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW,
                        payload={
                            "type": "medication",
                            **(
                                {"name": "synthetic", "dose": 1, "unit": "tablet"}
                                if complete_first
                                else {}
                            ),
                        },
                    ),
                    EventInput(start=NOW, payload={"type": "medication"}),
                ],
            )

    assert (
        interpret(
            db,
            Provider(),
            "Принял таблетку сейчас, название не помню",
            Settings(timezone="UTC"),
            NOW,
        ).intent
        == "clarify"
    )


def test_two_named_medications_at_same_time_are_distinct(db):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW, timezone="UTC", payload={"type": "medication", "name": name}
                    )
                    for name in ("аспирин", "ибупрофен")
                ],
            )

    assert (
        interpret(
            db,
            Provider(),
            "Принял аспирин и ибупрофен сейчас, дозы не помню",
            Settings(timezone="UTC"),
            NOW,
        ).intent
        == "log"
    )


def test_explicit_name_without_numeric_dose_cannot_be_dropped(db):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[EventInput(start=NOW, payload={"type": "medication"})],
            )

    assert (
        interpret(
            db, Provider(), "Принял аспирин сейчас, дозу не помню", Settings(timezone="UTC"), NOW
        ).intent
        == "clarify"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Аспирин принял сейчас, дозу не помню",
        "Принял аспирин сейчас после 500 мл воды, дозу лекарства не помню",
    ],
)
def test_named_medication_object_and_adjunct_quantity(db, text):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW, timezone="UTC", payload={"type": "medication", "name": "аспирин"}
                    )
                ],
            )

    assert interpret(db, Provider(), text, Settings(timezone="UTC"), NOW).intent == "log"


def test_medication_names_cannot_be_swapped_between_times(db):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    now = NOW.replace(hour=12)

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=hour),
                        timezone="UTC",
                        payload={"type": "medication", "name": name},
                    )
                    for hour, name in [(10, "ибупрофен"), (11, "аспирин")]
                ],
            )

    assert (
        interpret(
            db,
            Provider(),
            "Принял аспирин в 10. Принял ибупрофен в 11",
            Settings(timezone="UTC"),
            now,
        ).intent
        == "clarify"
    )


@pytest.mark.parametrize(
    "text,payload,hour,accepted",
    [
        ("Принял неизвестную таблетку сейчас", {}, 12, True),
        ("Принял одну неизвестную таблетку сейчас", {}, 12, True),
        ("Принял таблетку сейчас и не помню название", {}, 12, True),
        ("Иван принял сейчас таблетку, название не помню", {"name": "Иван"}, 12, False),
        (
            "Принял аспирин сейчас, дозу не помню",
            {"name": "аспирин", "dose": 500, "unit": "mg"},
            12,
            False,
        ),
        ("Принял таблетку сейчас после тренировки в 10, название не помню", {}, 10, False),
        ("Принял таблетку сейчас после тренировки в 10, название не помню", {}, 12, True),
        ("Выпила сейчас", {}, 12, False),
    ],
)
def test_literal_intake_qualifiers_do_not_invent_evidence(db, text, payload, hour, accepted):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=hour),
                        timezone="UTC",
                        payload={"type": "medication", **payload},
                    )
                ],
            )

    result = interpret(db, Provider(), text, Settings(timezone="UTC"), NOW.replace(hour=12))
    assert (result.intent == "log") == accepted


def test_unknown_dose_is_scoped_to_its_intake(db):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=10),
                        timezone="UTC",
                        payload={
                            "type": "medication",
                            "name": "аспирин",
                            "dose": 500,
                            "unit": "mg",
                        },
                    ),
                    EventInput(
                        start=NOW.replace(hour=11),
                        timezone="UTC",
                        payload={"type": "medication", "name": "ибупрофен"},
                    ),
                ],
            )

    assert (
        interpret(
            db,
            Provider(),
            "Принял аспирин 500 мг в 10. В 11 принял ибупрофен, дозу не помню",
            Settings(timezone="UTC"),
            NOW.replace(hour=12),
        ).intent
        == "log"
    )


@pytest.mark.parametrize(
    "dose,unit,accepted", [(50, "mg", False), (500, "ml", False), (500, "mg", True)]
)
def test_literal_dose_value_and_unit_must_match(db, dose, unit, accepted):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW,
                        timezone="UTC",
                        payload={"type": "medication", "dose": dose, "unit": unit},
                    )
                ],
            )

    assert (
        interpret(
            db,
            Provider(),
            "Принял неизвестную таблетку 500 мг сейчас",
            Settings(timezone="UTC"),
            NOW,
        ).intent
        == "log"
    ) == accepted


@pytest.mark.parametrize("complete", [False, True])
def test_pending_dose_is_checked_with_time_reply(db, complete):
    from garmin_ai.agent import Interpretation, apply_command, interpret
    from garmin_ai.config import Settings

    apply_command(
        db,
        Interpretation(intent="clarify", confidence=1, clarification="Время?"),
        text="Принял аспирин 500 мг, дозировка точная",
        update_id=995,
        actor="owner",
        now=NOW.replace(hour=12),
    )

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=11),
                        timezone="UTC",
                        payload={
                            "type": "medication",
                            "name": "аспирин",
                            **({"dose": 500, "unit": "mg"} if complete else {}),
                        },
                    )
                ],
            )

    assert interpret(
        db, Provider(), "в 11", Settings(timezone="UTC"), NOW.replace(hour=12, minute=5)
    ).intent == ("log" if complete else "clarify")


@pytest.mark.parametrize("hour", [10, 11])
def test_alternative_intake_times_are_not_confirmed(db, hour):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=hour), timezone="UTC", payload={"type": "medication"}
                    )
                ],
            )

    assert (
        interpret(
            db,
            Provider(),
            "Принял одну неизвестную таблетку — 10:00 или 11:00, точно не помню",
            Settings(timezone="UTC"),
            NOW.replace(hour=12),
        ).intent
        == "clarify"
    )


@pytest.mark.parametrize(
    "text,specs,accepted",
    [
        ("Вчера в 11 принял таблетку, название не помню", [(-1, 11, {})], True),
        ("Вчера в 11 принял таблетку, название не помню", [(0, 11, {})], False),
        (
            "Принял аспирин в 10 и ибупрофен в 11, дозы не помню",
            [(0, 10, {"name": "ибупрофен"}), (0, 11, {"name": "аспирин"})],
            False,
        ),
        (
            "Принял аспирин в 10 и ибупрофен в 11, дозы не помню",
            [(0, 10, {"name": "аспирин"}), (0, 11, {"name": "ибупрофен"})],
            True,
        ),
        (
            "Принял аспирин в 11. Принял ибупрофен в 11",
            [(0, 11, {"name": "аспирин"}), (0, 11, {"name": "ибупрофен"})],
            True,
        ),
        (
            "Принял аспирин сейчас, доза 5, единицу не помню",
            [(0, 12, {"name": "аспирин", "dose": 5, "unit": "mg"})],
            False,
        ),
        (
            "Принял аспирин 500 сейчас, единицу измерения не помню",
            [(0, 12, {"name": "аспирин", "dose": 500, "unit": "mg"})],
            False,
        ),
    ],
)
def test_intake_days_coordination_and_unknown_units(db, text, specs, accepted):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=hour) + timedelta(days=day),
                        timezone="UTC",
                        payload={"type": "medication", **payload},
                    )
                    for day, hour, payload in specs
                ],
            )

    assert (
        interpret(db, Provider(), text, Settings(timezone="UTC"), NOW.replace(hour=12)).intent
        == "log"
    ) == accepted


def test_known_detail_reply_completes_pending_intake(db):
    from garmin_ai.agent import Interpretation, apply_command, interpret
    from garmin_ai.config import Settings

    apply_command(
        db,
        Interpretation(intent="clarify", confidence=1, clarification="Время?"),
        text="Принял таблетку",
        update_id=996,
        actor="owner",
        now=NOW.replace(hour=12),
    )

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=11),
                        timezone="UTC",
                        payload={"type": "medication", "name": "аспирин"},
                    )
                ],
            )

    assert (
        interpret(
            db,
            Provider(),
            "аспирин, в 11",
            Settings(timezone="UTC"),
            NOW.replace(hour=12, minute=5),
        ).intent
        == "log"
    )


def test_oversized_relative_number_requests_clarification(db):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[EventInput(start=NOW, timezone="UTC", payload={"type": "medication"})],
            )

    assert (
        interpret(
            db,
            Provider(),
            "Принял таблетку " + "9" * 4500 + " минут назад",
            Settings(timezone="UTC"),
            NOW,
        ).intent
        == "clarify"
    )


def test_clarification_history_has_a_fixed_bound(db):
    import json

    from garmin_ai.agent import Interpretation, apply_command
    from garmin_ai.models import AppState

    for index in range(20):
        apply_command(
            db,
            Interpretation(intent="clarify", confidence=1, clarification="Время?"),
            text="synthetic " * 800,
            update_id=2000 + index,
            actor="owner",
            now=NOW + timedelta(minutes=index),
        )
    db.expire_all()
    messages = db.get(AppState, "conversation:pending").value["messages"]
    assert len(messages) <= 8
    assert len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) <= 32000


@pytest.mark.parametrize(
    "text,hour,payload,accepted",
    [
        (
            "Принял таблетку сейчас, забыл название и дозировку",
            12,
            {"name": "аспирин", "dose": 500, "unit": "mg"},
            False,
        ),
        (
            "Принял таблетку сейчас, название лекарства не помню",
            12,
            {"name": "аспирин", "dose": 500, "unit": "mg"},
            False,
        ),
        ("Принял таблетку в 11 вечера, название не помню", 23, {}, True),
        ("Принял таблетку в 11 утра, название не помню", 11, {}, True),
        ("Принял таблетку от мигрени в 11 утра, название не помню", 11, {}, True),
        ("Принял неизвестную таблетку час назад", 22, {}, True),
        ("I took an unknown pill an hour ago", 22, {}, True),
    ],
)
def test_natural_unknown_and_time_phrases(db, text, hour, payload, accepted):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=hour),
                        timezone="UTC",
                        payload={"type": "medication", **payload},
                    )
                ],
            )

    now = NOW.replace(hour=12 if "сейчас" in text else 23)
    assert (
        interpret(db, Provider(), text, Settings(timezone="UTC"), now).intent == "log"
    ) == accepted


@pytest.mark.parametrize("complete", [False, True])
def test_pending_name_and_dose_compose_with_intake_reply(db, complete):
    from garmin_ai.agent import Interpretation, apply_command, interpret
    from garmin_ai.config import Settings

    apply_command(
        db,
        Interpretation(intent="clarify", confidence=1, clarification="Когда приняли?"),
        text="аспирин 500 мг",
        update_id=998,
        actor="owner",
        now=NOW.replace(hour=12),
    )

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=11),
                        timezone="UTC",
                        payload={
                            "type": "medication",
                            **({"name": "аспирин", "dose": 500, "unit": "mg"} if complete else {}),
                        },
                    )
                ],
            )

    assert interpret(
        db, Provider(), "Принял в 11", Settings(timezone="UTC"), NOW.replace(hour=12, minute=5)
    ).intent == ("log" if complete else "clarify")
