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


@pytest.mark.parametrize(
    "text,hour,payload,accepted",
    [
        ("Принял аспирин сейчас", 12, {"name": "аспирин", "dose": 500, "unit": "mg"}, False),
        (
            "Принял таблетку сейчас, название и дозу не помню",
            -12,
            {"name": "аспирин", "dose": 500, "unit": "mg"},
            False,
        ),
        ("Принял таблетку в 11 ночи, название не помню", 23, {}, True),
        ("Принял таблетку в 11 ночи, название не помню", 11, {}, False),
        ("Принял неизвестную таблетку в 10:00–11:00", 10, {}, False),
        ("Принял неизвестную таблетку с 10 до 11", 11, {}, False),
        ("Принял таблетку сейчас, но, возможно, от неё тошнит, название не помню", 12, {}, True),
    ],
)
def test_review_literal_evidence_cases(db, text, hour, payload, accepted):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW + timedelta(hours=hour),
                        timezone="UTC",
                        payload={"type": "medication", **payload},
                    )
                ],
            )

    now = NOW.replace(hour=12 if "сейчас" in text else 23)
    assert (
        interpret(db, Provider(), text, Settings(timezone="UTC"), now).intent == "log"
    ) == accepted


@pytest.mark.parametrize("reply", ["Принял в 11", "Принял в 11, дозу не помню"])
@pytest.mark.parametrize("name", [None, "ибупрофен", "аспирин"])
def test_pending_name_only_is_preserved(db, reply, name):
    from garmin_ai.agent import Interpretation, apply_command, interpret
    from garmin_ai.config import Settings

    apply_command(
        db,
        Interpretation(intent="clarify", confidence=1, clarification="Когда приняли?"),
        text="аспирин",
        update_id=999,
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
                        payload={"type": "medication", "name": name},
                    )
                ],
            )

    assert (
        interpret(
            db, Provider(), reply, Settings(timezone="UTC"), NOW.replace(hour=12, minute=5)
        ).intent
        == "log"
    ) == (name == "аспирин")


@pytest.mark.parametrize("invent_dose", [False, True])
def test_known_and_separate_unknown_intake_share_clock(db, invent_dose):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, instruction, prompt, schema):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=11),
                        timezone="UTC",
                        payload={"type": "medication", **payload},
                    )
                    for payload in [
                        {"name": "аспирин", "dose": 500, "unit": "mg"},
                        {"dose": 500, "unit": "mg"} if invent_dose else {},
                    ]
                ],
            )

    result = interpret(
        db,
        Provider(),
        "Принял аспирин 500 мг и одну неизвестную таблетку в 11",
        Settings(timezone="UTC"),
        NOW.replace(hour=12),
    )
    assert (result.intent == "log") == (not invent_dose)


@pytest.mark.parametrize(
    "text,payload,accepted",
    [
        (
            "Принял аспирин 500 сейчас, единицу измерения не помню",
            {"name": "аспирин", "dose": 500},
            True,
        ),
        ("Принял аспирин 500 сейчас, единицу измерения не помню", {"name": "аспирин"}, False),
        ("Принял аспирин сейчас, доза 500 мг", {"name": "аспирин"}, False),
        (
            "Принял аспирин сейчас, доза 500 мг",
            {"name": "аспирин", "dose": 500, "unit": "mg"},
            True,
        ),
        ("Неизвестную таблетку принял сейчас", {}, True),
    ],
)
def test_partial_dose_continuations_and_object_order(db, text, payload, accepted):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(start=NOW, timezone="UTC", payload={"type": "medication", **payload})
                ],
            )

    assert (
        interpret(db, Provider(), text, Settings(timezone="UTC"), NOW).intent == "log"
    ) == accepted


@pytest.mark.parametrize(
    "history,name,accepted",
    [
        (["мигрень"], None, True),
        (["аспирин", "нет, ибупрофен"], "ибупрофен", True),
        (["аспирин", "нет, ибупрофен"], "аспирин", False),
    ],
)
def test_medication_pending_scope_and_corrections(db, history, name, accepted):
    from garmin_ai.agent import Interpretation, apply_command, interpret
    from garmin_ai.config import Settings

    for i, text in enumerate(history):
        apply_command(
            db,
            Interpretation(
                intent="clarify",
                confidence=1,
                clarification="Сила боли?" if text == "мигрень" else "Когда приняли?",
            ),
            text=text,
            update_id=4000 + i,
            actor="owner",
            now=NOW.replace(hour=12) + timedelta(minutes=i),
        )

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=11),
                        timezone="UTC",
                        payload={"type": "medication", "name": name},
                    )
                ],
            )

    text = "Принял таблетку в 11, название не помню" if history == ["мигрень"] else "Принял в 11"
    assert (
        interpret(
            db, Provider(), text, Settings(timezone="UTC"), NOW.replace(hour=12, minute=5)
        ).intent
        == "log"
    ) == accepted


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("wrong", [False, True])
def test_coordinated_calendar_and_relative_times(db, relative, wrong):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    text = (
        "Принял аспирин в 10 и ибупрофен час назад"
        if relative
        else "Вчера принял аспирин в 10 и ибупрофен в 11"
    )
    starts = [NOW.replace(hour=10), NOW.replace(hour=11)]
    if not relative:
        starts = [stamp - timedelta(days=1) for stamp in starts]
    if wrong:
        if relative:
            starts.reverse()
        else:
            starts[1] += timedelta(days=1)

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=start, timezone="UTC", payload={"type": "medication", "name": name}
                    )
                    for start, name in zip(starts, ["аспирин", "ибупрофен"], strict=True)
                ],
            )

    assert (
        interpret(db, Provider(), text, Settings(timezone="UTC"), NOW.replace(hour=12)).intent
        == "log"
    ) == (not wrong)


@pytest.mark.parametrize("count", [0, 1, 2])
def test_all_reported_medication_objects_must_be_emitted(db, count):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            events = [
                EventInput(
                    start=NOW.replace(hour=11),
                    timezone="UTC",
                    payload={"type": "medication", "name": name},
                )
                for name in ["аспирин", "ибупрофен"][:count]
            ]
            return Interpretation(
                intent="log",
                confidence=1,
                events=events or [EventInput(start=NOW, payload={"type": "migraine"})],
            )

    assert (
        interpret(
            db,
            Provider(),
            "Принял аспирин и ибупрофен в 11",
            Settings(timezone="UTC"),
            NOW.replace(hour=12),
        ).intent
        == "log"
    ) == (count == 2)


@pytest.mark.parametrize("wrong", [False, True])
def test_comma_contrast_intakes_keep_separate_times(db, wrong):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            names = ["ибупрофен", "аспирин"] if wrong else ["аспирин", "ибупрофен"]
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=hour),
                        timezone="UTC",
                        payload={"type": "medication", "name": name},
                    )
                    for hour, name in zip([10, 11], names, strict=True)
                ],
            )

    assert (
        interpret(
            db,
            Provider(),
            "Принял аспирин в 10, а ибупрофен в 11",
            Settings(timezone="UTC"),
            NOW.replace(hour=12),
        ).intent
        == "log"
    ) == (not wrong)


@pytest.mark.parametrize("day", [8, 10])
@pytest.mark.parametrize("date_text", ["8 сентября", "8 сентября 2026 года"])
def test_russian_calendar_date_is_bound_to_medication(db, day, date_text):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(day=day, hour=11),
                        timezone="UTC",
                        payload={"type": "medication", "name": "аспирин"},
                    )
                ],
            )

    assert (
        interpret(
            db,
            Provider(),
            f"Принял аспирин {date_text} в 11",
            Settings(timezone="UTC"),
            NOW.replace(hour=12),
        ).intent
        == "log"
    ) == (day == 8)


@pytest.mark.parametrize(
    "text,payload",
    [
        ("Принял аспирин 5 таблеток в 11", {"name": "аспирин", "dose": 5, "unit": "tablet"}),
        ("Принял Но-шпу в 11", {"name": "Но-шпу"}),
        ("Принял аспирин 0,5 мг в 11", {"name": "аспирин", "dose": 0.5, "unit": "mg"}),
        ("Принял «Нурофен» в 11", {"name": "Нурофен"}),
    ],
)
def test_natural_medication_name_and_dose_punctuation(db, text, payload):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=11),
                        timezone="UTC",
                        payload={"type": "medication", **payload},
                    )
                ],
            )

    assert (
        interpret(db, Provider(), text, Settings(timezone="UTC"), NOW.replace(hour=12)).intent
        == "log"
    )


def test_yearless_date_uses_recent_previous_year(db):
    from garmin_ai.intake_assertion import reported_intake_times

    now = datetime(2027, 1, 10, 12, tzinfo=UTC)
    assert reported_intake_times("Принял аспирин 31 декабря в 11", now, "UTC") == {
        datetime(2026, 12, 31, 11, tzinfo=UTC)
    }


@pytest.mark.parametrize(
    "dose,reported,accepted", [(None, False, True), (500, False, False), (500, True, True)]
)
def test_update_does_not_invent_medication_dose(db, dose, reported, accepted):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    row = create_event(
        db, EventInput(start=NOW, timezone="UTC", payload={"type": "medication"}), actor="owner"
    )

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="update",
                confidence=1,
                target_event_id=row.id,
                changed_fields=["payload.name", "payload.dose"],
                events=[
                    EventInput(
                        start=NOW,
                        timezone="UTC",
                        payload={"type": "medication", "name": "аспирин", "dose": dose},
                    )
                ],
            )

    text = "исправь название на аспирин" + (", дозу на 500" if reported else "")
    assert (
        interpret(db, Provider(), text, Settings(timezone="UTC"), NOW).intent == "update"
    ) == accepted


@pytest.mark.parametrize(
    "text,name,hour,accepted",
    [
        ("Принял неизвестный препарат сейчас", None, 23, True),
        ("Принял неизвестный препарат сейчас", "препарат", 23, False),
        ("I took an unknown drug now", None, 23, True),
        ("I took an unknown drug now", "drug", 23, False),
        ("Принял аспирин после обеда в 15:00", "аспирин", 15, True),
        ("I took an unknown pill at 11 pm", None, 23, True),
        ("I took an unknown pill at 11 pm", None, 11, False),
        ("Принял аспирин в 11 часов", "аспирин", 11, True),
        ("Да принял неизвестную таблетку в 11", None, 11, True),
        ("Yes I took an unknown pill at 11 am", None, 11, True),
    ],
)
def test_generic_medications_and_explicit_time_qualifiers(db, text, name, hour, accepted):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=hour),
                        timezone="UTC",
                        payload={"type": "medication", "name": name},
                    )
                ],
            )

    assert (
        interpret(db, Provider(), text, Settings(timezone="UTC"), NOW.replace(hour=23)).intent
        == "log"
    ) == accepted


def test_update_target_covers_restated_intake(db):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    row = create_event(
        db,
        EventInput(start=NOW.replace(hour=11), timezone="UTC", payload={"type": "medication"}),
        actor="owner",
    )

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="update",
                confidence=1,
                target_event_id=row.id,
                changed_fields=["payload.name"],
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
            "Принял аспирин в 11, исправь эту запись",
            Settings(timezone="UTC"),
            NOW.replace(hour=12),
        ).intent
        == "update"
    )


@pytest.mark.parametrize(
    "text,hour,minute,accepted",
    [
        ("Принял аспирин в 11:30", 11, 30, True),
        ("Принял аспирин в 11:30", 11, 0, False),
        ("Принял аспирин в 11 часов вечера", 23, 0, True),
        ("Принял аспирин в 11 часов вечера", 11, 0, False),
        ("Принял аспирин от боли и тошноты в 11", 11, 0, True),
        ("Принял аспирин не в 10, а в 11", 11, 0, True),
        ("Принял аспирин не в 10, а в 11", 10, 0, False),
        ("Принял аспирин примерно в 11", 11, 0, False),
        ("I've taken aspirin at 11 am", 11, 0, True),
    ],
)
def test_precise_and_corrected_intake_clock_phrases(db, text, hour, minute, accepted):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=hour, minute=minute),
                        timezone="UTC",
                        payload={
                            "type": "medication",
                            "name": "aspirin" if "aspirin" in text else "аспирин",
                        },
                    )
                ],
            )

    assert (
        interpret(db, Provider(), text, Settings(timezone="UTC"), NOW.replace(hour=23)).intent
        == "log"
    ) == accepted


@pytest.mark.parametrize(
    "text,accepted",
    [("Неизвестный препарат принял сейчас", True), ("Принял таблетку около часа назад", False)],
)
def test_generic_prefix_and_approximate_relative_intake(db, text, accepted):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW if accepted else NOW - timedelta(hours=1),
                        timezone="UTC",
                        payload={"type": "medication"},
                    )
                ],
            )

    assert (
        interpret(db, Provider(), text, Settings(timezone="UTC"), NOW).intent == "log"
    ) == accepted


@pytest.mark.parametrize("second_name", [None, "аспирин"])
def test_repeated_time_keeps_shared_medication_name(db, second_name):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=hour),
                        timezone="UTC",
                        payload={"type": "medication", "name": name},
                    )
                    for hour, name in [(10, "аспирин"), (11, second_name)]
                ],
            )

    assert (
        interpret(
            db,
            Provider(),
            "Принял аспирин в 10 и в 11",
            Settings(timezone="UTC"),
            NOW.replace(hour=12),
        ).intent
        == "log"
    ) == (second_name == "аспирин")


def test_unit_only_medication_correction(db):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    row = create_event(
        db,
        EventInput(start=NOW, timezone="UTC", payload={"type": "medication", "dose": 500}),
        actor="owner",
    )

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="update",
                confidence=1,
                target_event_id=row.id,
                changed_fields=["payload.unit"],
                events=[
                    EventInput(
                        start=NOW,
                        timezone="UTC",
                        payload={"type": "medication", "dose": 500, "unit": "mg"},
                    )
                ],
            )

    assert (
        interpret(db, Provider(), "исправь единицу на mg", Settings(timezone="UTC"), NOW).intent
        == "update"
    )


@pytest.mark.parametrize(
    "text,name,dose,unit,accepted",
    [
        ("I took no aspirin at 11", "no aspirin", None, None, False),
        ("I took no aspirin at 11", "aspirin", None, None, False),
        ("Принял капсулу в 11", None, None, None, True),
        ("Принял капсулу в 11", "капсулу", None, None, False),
        ("I took a capsule at 11", None, None, None, True),
        ("I took a capsule at 11", "capsule", None, None, False),
        ("Принял аспирин в 11!", "аспирин", None, None, True),
        ("Принял аспирин в 11!", None, None, None, False),
        ("Принял аспирин (в 11)!", "аспирин", None, None, True),
        ("Принял какое-то лекарство в 11", None, None, None, True),
        ("Принял какое-то лекарство в 11", "какое-то", None, None, False),
        ("Принял ацетилсалициловую кислоту в 11", "ацетилсалициловая кислота", None, None, True),
        ("Принял ацетилсалициловую кислоту в 11", "ацетилсалициловая сода", None, None, False),
        ("Принял аспирин в 11, 500 мг", "аспирин", 500, "mg", True),
        ("Принял аспирин в 11, 500 мг", "аспирин", None, None, False),
        ("Принял аспирин, 500 мг, в 11", "аспирин", 500, "mg", True),
        ("Принял аспирин от боли или тошноты в 11", "аспирин", None, None, True),
        ("Принял аспирин в 10 или в 11", "аспирин", None, None, False),
        ("Принял Омега 3 в 11", "Омега-3", None, None, True),
        ("Принял Омега 3 в 11", "Омега", 3, None, False),
        ("Принял Омега 3 в 11", "Омега", None, None, False),
        ("Принял звонок в 11", "звонок", None, None, False),
        ("I took a call at 11", "call", None, None, False),
        ("I took 2 tablets of aspirin at 11", "aspirin", 2, "tablet", True),
        ("I took 2 tablets of aspirin at 11", "of aspirin", 2, "tablet", False),
        ("Муж пришёл домой и принял аспирин в 11", "аспирин", None, None, False),
        ("He came home and took aspirin at 11", "aspirin", None, None, False),
        ("Муж пришёл домой и я принял аспирин в 11", "аспирин", None, None, True),
        ("Принял 2 таблетки аспирина по 100 мг в 11", "аспирин", 2, "tablet", False),
        ("Принял 2 таблетки аспирина по 100 мг в 11", "аспирин", 100, "mg", False),
        ("Я принял свою таблетку в 11", None, None, None, True),
        ("Я принял свою таблетку в 11", "свою", None, None, False),
        ("I took my pill at 11", None, None, None, True),
        ("I took my pill at 11", "my", None, None, False),
        ("Принял аспирин после еды 500 мг в 11", "аспирин", 500, "mg", True),
        ("Принял аспирин после еды 500 мг в 11", "аспирин", None, None, False),
        ("I took aspirin 1,000 mg at 11", "aspirin", 1000, "mg", True),
        ("I took aspirin 1,000 mg at 11", "aspirin", 1, "mg", False),
        ("Принял аспирин 1,5 мг в 11", "аспирин", 1.5, "mg", True),
        ("Принял аспирин, дозировка 500 мг, в 11", "аспирин", 500, "mg", True),
        ("Принял витамин в 11", "витамин", None, None, True),
        ("Принял витамин В 11", "витамин", None, None, False),
        ("Принял аспирин 1 1/2 таблетки в 11", "аспирин", 1.5, "tablet", True),
        ("Принял аспирин 1 1/2 таблетки в 11", "аспирин", 0.5, "tablet", False),
        ("Одну таблетку принял в 11", None, 1, "tablet", True),
        ("Одну таблетку принял в 11", None, None, None, False),
        ("Принял лекарства в 11", None, None, None, True),
        ("Принял лекарства в 11", "лекарства", None, None, False),
        ("I took pills at 11", None, None, None, True),
        ("I took pills at 11", "pills", None, None, False),
        ("Принял ещё таблетку в 11", None, None, None, True),
        ("Принял ещё таблетку в 11", "ещё", None, None, False),
        ("Принял Но-шпу форте в 11", "Но-шпа форте", None, None, True),
        ("Принял Но-шпу форте в 11", "Но-шпа макс", None, None, False),
        ("Таблетку приняла медсестра в 11", "медсестра", None, None, False),
        ("Таблетку принял медбрат в 11", None, None, None, False),
        ("Я пил аспирин в 11", "аспирин", None, None, True),
        ("Я пила аспирин в 11", "аспирин", None, None, True),
        ("Я пил в 11", None, None, None, False),
        ("Принял аспирин в 11, хотя обычно пью его утром", "аспирин", None, None, True),
        ("Обычно пил аспирин в 11", "аспирин", None, None, False),
        ("Принял аспирин по 1 таблетке в 11", "аспирин", 1, "tablet", True),
        ("Принял аспирин по 1 таблетке в 11", "аспирин", None, None, False),
        ("Принял аспирин от головы 500 мг в 11", "аспирин", 500, "mg", True),
        ("Принял аспирин от головы 500 мг в 11", "аспирин", None, None, False),
        ("Принял участие в 11", "участие", None, None, False),
        ("Таблетку принял врач в 11", "врач", None, None, False),
        ("Принял аспирин в 11, дозу не помню, единица мг", "аспирин", None, "mg", True),
        ("Принял аспирин в 11, дозу не помню, единица мг", "аспирин", None, None, False),
        ("Например. Я принял таблетку в 11.", None, None, None, False),
        ("Принял таблетку 1/2 таблетки в 11", None, 0.5, "tablet", True),
        ("Принял таблетку 1/2 таблетки в 11", None, 2, "tablet", False),
        ("Принял аспирин с водой в 11", "аспирин", None, None, True),
        ("Принял аспирин с едой в 11", "аспирин", None, None, True),
        ("I took aspirin at 13 pm", "aspirin", None, None, False),
        ("Таблетку принял Иван в 11", "Иван", None, None, False),
        ("Принял не аспирин, а ибупрофен в 11", "ибупрофен", None, None, True),
        ("Принял аспирин не 100 мг, а 500 мг в 11", "аспирин", 500, "mg", True),
        ("Принял витамин В 12", "витамин", None, None, False),
        ("Не помню, какую таблетку принял в 11", None, None, None, True),
        ("Принял душ в 11", "душ", None, None, False),
        ("Принял Но-шпу в 11", "Но-шпа", None, None, True),
        ("Принял таблетку, но не помню название, в 11", None, None, None, True),
        ("Принял аспирин (500 мг) в 11", "аспирин", 500, "mg", True),
        ("Принял аспирин в 11, примерно через час стало лучше", "аспирин", None, None, True),
        ("Выпил неизвестный препарат в 11", None, None, None, True),
        ("После еды я принял аспирин в 11", "аспирин", None, None, True),
        ("Принял аспирин в 11, как обычно", "аспирин", None, None, True),
        ("Принял аспирин в 11, как всегда", "аспирин", None, None, True),
        ("Обычно принимал аспирин в 11", "аспирин", None, None, False),
        ("Принял таблетку аспирина в 11", "аспирин", None, None, True),
        ("Принял таблетку аспирина в 11", "ибупрофен", None, None, False),
        ("Принял аспирин две таблетки в 11", "аспирин", 2, "tablet", True),
        ("Принял аспирин две таблетки в 11", "аспирин", None, None, False),
        ("Мигрень началась в 10 и в 11 принял аспирин", "аспирин", None, None, True),
    ],
)
def test_factual_intake_phrasing_preserves_details(db, text, name, dose, unit, accepted):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=11),
                        timezone="UTC",
                        payload={"type": "medication", "name": name, "dose": dose, "unit": unit},
                    )
                ],
            )

    assert (
        interpret(db, Provider(), text, Settings(timezone="UTC"), NOW.replace(hour=12)).intent
        == "log"
    ) == accepted


@pytest.mark.parametrize(
    "text,allowed",
    [
        ("исправь время на 11", False),
        ("исправь дату на 11 сентября", False),
        ("исправь дозу на 11", True),
        ("исправь на 11 мг", True),
    ],
)
def test_dose_correction_requires_dose_evidence(text, allowed):
    from garmin_ai.intake_assertion import unsupported_medication_update

    event = EventInput(start=NOW, timezone="UTC", payload={"type": "medication", "dose": 11})
    assert unsupported_medication_update(
        event, ["payload.dose"], {"dose": None, "unit": "mg"}, text
    ) == (not allowed)


@pytest.mark.parametrize("field,allowed", [("dose", False), ("name", True)])
def test_unknown_correction_is_scoped_to_its_field(field, allowed):
    from garmin_ai.intake_assertion import unsupported_medication_update

    event = EventInput(start=NOW, timezone="UTC", payload={"type": "medication"})
    assert unsupported_medication_update(
        event,
        ["payload." + field],
        {"dose": 50, "unit": "mg", "name": "synthetic"},
        "дозу оставь 50 мг, название не помню",
    ) == (not allowed)


@pytest.mark.parametrize(
    "text,name,dose,unit,allowed",
    [
        (
            "Исправь название на ибупрофен. Принял парацетамол 500 мг в 11",
            "ибупрофен",
            500,
            "mg",
            False,
        ),
        ("исправь название на Но-шпу", "Но-шпа", None, None, True),
        ("исправь дату на 12 сентября 2025 г", None, 2025, "g", False),
    ],
)
def test_correction_details_stay_in_target_clause(text, name, dose, unit, allowed):
    from garmin_ai.intake_assertion import unsupported_medication_update

    event = EventInput(
        start=NOW,
        timezone="UTC",
        payload={"type": "medication", "name": name, "dose": dose, "unit": unit},
    )
    fields = [
        "payload." + key
        for key, val in {"name": name, "dose": dose, "unit": unit}.items()
        if val is not None
    ]
    assert unsupported_medication_update(event, fields, {}, text) == (not allowed)


def test_fractional_relative_and_abbreviated_year_times():
    from garmin_ai.intake_assertion import reported_intake_times

    assert reported_intake_times("Принял таблетку 1,5 часа назад", NOW, "UTC") == {
        NOW - timedelta(minutes=90)
    }
    assert reported_intake_times("Принял аспирин 12 сентября 2025 г. в 11", NOW, "UTC") == {
        datetime(2025, 9, 12, 11, tzinfo=UTC)
    }


@pytest.mark.parametrize("legacy", [False, True])
def test_timed_pending_intake_accepts_name_reply(legacy):
    from garmin_ai.intake_assertion import missing_reported_details, missing_reported_intakes

    at = NOW.replace(hour=11)
    event = EventInput(start=at, timezone="UTC", payload={"type": "medication", "name": "аспирин"})
    pending = {"text": "Принял таблетку в 11", "created_at": NOW.replace(hour=12).isoformat()}
    if legacy:
        pending["messages"] = [{"text": pending["text"], "question": "Какое лекарство?"}]
    assert not missing_reported_details(event, "аспирин", NOW.replace(hour=12), "UTC", pending)
    assert not missing_reported_intakes([event], "аспирин", NOW.replace(hour=12), "UTC", pending)


@pytest.mark.parametrize("clock", ["13 pm", "0 am", "24 am"])
def test_invalid_meridiem_clock_has_no_intake_evidence(clock):
    from garmin_ai.intake_assertion import reported_intake_times

    assert not reported_intake_times("I took aspirin at " + clock, NOW.replace(hour=23), "UTC")


@pytest.mark.parametrize("count", [1, 2, 3])
def test_simultaneous_unknown_intakes_preserve_multiplicity(db, count):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=10), timezone="UTC", payload={"type": "medication"}
                    )
                    for _ in range(count)
                ],
            )

    result = interpret(
        db,
        Provider(),
        "Принял неизвестную таблетку в 10 и принял неизвестный препарат в 10",
        Settings(timezone="UTC"),
        NOW.replace(hour=12),
    )
    assert (result.intent == "log") == (count == 2)


@pytest.mark.parametrize(
    "doses,accepted",
    [([100, 500], True), ([500, 100], True), ([100, 100], False), ([500, 500], False)],
)
def test_repeated_intake_details_match_one_to_one(db, doses, accepted):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
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
                            "dose": dose,
                            "unit": "mg",
                        },
                    )
                    for dose in doses
                ],
            )

    result = interpret(
        db,
        Provider(),
        "Принял аспирин 100 мг в 11 и принял аспирин 500 мг в 11",
        Settings(timezone="UTC"),
        NOW.replace(hour=12),
    )
    assert (result.intent == "log") == accepted


@pytest.mark.parametrize("old_hour,include_new", [(10, False), (11, False), (10, True), (11, True)])
def test_correction_cannot_cover_separate_new_intake(db, old_hour, include_new):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    row = create_event(
        db,
        EventInput(
            start=NOW.replace(hour=old_hour),
            timezone="UTC",
            payload={"type": "medication", "name": "аспирин"},
        ),
        actor="owner",
    )

    class Provider:
        def structured(self, *args):
            target = EventInput(
                start=NOW.replace(hour=11),
                timezone="UTC",
                payload={"type": "medication", "name": "аспирин", "dose": 100, "unit": "mg"},
            )
            new = EventInput(
                start=NOW.replace(hour=11),
                timezone="UTC",
                payload={"type": "medication", "name": "аспирин"},
            )
            return Interpretation(
                intent="update",
                confidence=1,
                target_event_id=row.id,
                changed_fields=["payload.dose", "payload.unit"],
                events=[target, new] if include_new else [target],
            )

    result = interpret(
        db,
        Provider(),
        "Исправь дозу выбранной записи на 100 мг. Принял ещё аспирин в 11",
        Settings(timezone="UTC"),
        NOW.replace(hour=12),
    )
    assert (result.intent == "update") == include_new


@pytest.mark.parametrize("reported", [False, True])
def test_requested_correction_cannot_keep_unknown_details(db, reported):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    row = create_event(
        db, EventInput(start=NOW, timezone="UTC", payload={"type": "medication"}), actor="owner"
    )

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="update",
                confidence=1,
                target_event_id=row.id,
                changed_fields=["payload.dose", "payload.unit"],
                events=[
                    EventInput(
                        start=NOW,
                        timezone="UTC",
                        payload={
                            "type": "medication",
                            "dose": 500 if reported else None,
                            "unit": "mg" if reported else None,
                        },
                    )
                ],
            )

    result = interpret(db, Provider(), "исправь дозу на 500 мг", Settings(timezone="UTC"), NOW)
    assert (result.intent == "update") == reported


@pytest.mark.parametrize(
    "name,accepted", [("аспирин", True), ("название", False), ("исправь", False)]
)
def test_name_correction_excludes_instruction_words(name, accepted):
    from garmin_ai.intake_assertion import unsupported_medication_update

    event = EventInput(start=NOW, timezone="UTC", payload={"type": "medication", "name": name})
    assert unsupported_medication_update(
        event, ["payload.name"], {"name": None}, "исправь название на аспирин"
    ) == (not accepted)


@pytest.mark.parametrize("omit_first", [False, True])
def test_shared_dose_applies_to_each_named_medication(db, omit_first):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[
                    EventInput(
                        start=NOW.replace(hour=11),
                        timezone="UTC",
                        payload={
                            "type": "medication",
                            "name": name,
                            "dose": None if i == 0 and omit_first else 1,
                            "unit": None if i == 0 and omit_first else "tablet",
                        },
                    )
                    for i, name in enumerate(["аспирин", "ибупрофен"])
                ],
            )

    result = interpret(
        db,
        Provider(),
        "Принял аспирин и ибупрофен по 1 таблетке в 11",
        Settings(timezone="UTC"),
        NOW.replace(hour=12),
    )
    assert (result.intent == "log") == (not omit_first)


def test_untimed_intake_cannot_disappear_behind_migraine(db):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="log",
                confidence=1,
                events=[EventInput(start=NOW, timezone="UTC", payload={"type": "migraine"})],
            )

    assert (
        interpret(
            db,
            Provider(),
            "Мигрень началась сейчас. Принял аспирин.",
            Settings(timezone="UTC"),
            NOW,
        ).intent
        == "clarify"
    )


@pytest.mark.parametrize(
    "text,name", [("исправь на аспирин", "аспирин"), ("change it to aspirin", "aspirin")]
)
def test_unlabeled_name_correction(text, name):
    from garmin_ai.intake_assertion import unsupported_medication_update

    event = EventInput(start=NOW, timezone="UTC", payload={"type": "medication", "name": name})
    assert not unsupported_medication_update(event, ["payload.name"], {"name": None}, text)


@pytest.mark.parametrize("include_second", [False, True])
def test_comma_coordinated_medications_are_all_required(db, include_second):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            events = [
                EventInput(
                    start=NOW.replace(hour=hour),
                    timezone="UTC",
                    payload={"type": "medication", "name": name},
                )
                for hour, name in [(10, "аспирин"), (11, "ибупрофен")]
            ]
            return Interpretation(
                intent="log", confidence=1, events=events if include_second else events[:1]
            )

    result = interpret(
        db,
        Provider(),
        "Принял аспирин в 10, ибупрофен в 11",
        Settings(timezone="UTC"),
        NOW.replace(hour=12),
    )
    assert (result.intent == "log") == include_second


@pytest.mark.parametrize("include_second", [False, True])
def test_omitted_object_first_name_is_discovered_without_provider(db, include_second):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    class Provider:
        def structured(self, *args):
            events = [
                EventInput(
                    start=NOW.replace(hour=hour),
                    timezone="UTC",
                    payload={"type": "medication", "name": name},
                )
                for hour, name in [(10, "аспирин"), (11, "ибупрофен")]
            ]
            return Interpretation(
                intent="log", confidence=1, events=events if include_second else events[:1]
            )

    result = interpret(
        db,
        Provider(),
        "Принял аспирин в 10. Ибупрофен принял в 11",
        Settings(timezone="UTC"),
        NOW.replace(hour=12),
    )
    assert (result.intent == "log") == include_second


@pytest.mark.parametrize(
    "fields", [["payload.dose"], ["payload.unit"], ["payload.dose", "payload.unit"]]
)
def test_explicit_correction_requires_both_dose_and_unit(db, fields):
    from garmin_ai.agent import Interpretation, interpret
    from garmin_ai.config import Settings

    row = create_event(
        db, EventInput(start=NOW, timezone="UTC", payload={"type": "medication"}), actor="owner"
    )

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="update",
                confidence=1,
                target_event_id=row.id,
                changed_fields=fields,
                events=[
                    EventInput(
                        start=NOW,
                        timezone="UTC",
                        payload={"type": "medication", "dose": 500, "unit": "mg"},
                    )
                ],
            )

    result = interpret(db, Provider(), "исправь дозу на 500 мг", Settings(timezone="UTC"), NOW)
    assert (result.intent == "update") == (len(fields) == 2)


@pytest.mark.parametrize(
    "text",
    [
        "Принял аспирин сейчас или в 11",
        "Принял аспирин час или два часа назад",
        "I took aspirin an hour or two hours ago",
    ],
)
def test_alternative_intake_times_remain_ambiguous(text):
    from garmin_ai.intake_assertion import reported_intake_times

    assert not reported_intake_times(text, NOW.replace(hour=12), "UTC")


@pytest.mark.parametrize(
    "field,text",
    [
        ("name", "исправь название на аспирин"),
        ("dose", "исправь дозу на 500"),
        ("unit", "исправь единицу на мг"),
    ],
)
def test_explicit_correction_cannot_be_omitted_from_changed_fields(field, text):
    from garmin_ai.intake_assertion import unsupported_medication_update

    event = EventInput(
        start=NOW,
        timezone="UTC",
        payload={"type": "medication", "name": "аспирин", "dose": 500, "unit": "mg"},
    )
    previous = {"name": None, "dose": None, "unit": None}
    assert unsupported_medication_update(event, [], previous, text)
    assert not unsupported_medication_update(event, ["payload." + field], previous, text)
