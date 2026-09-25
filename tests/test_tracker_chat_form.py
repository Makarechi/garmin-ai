from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from garmin_ai.agent import SafetyScreen
from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.config import Settings
from garmin_ai.models import AppState, Event
from garmin_ai.telegram import handle_button, process_message, save_update
from garmin_ai.telegram_history import history_page, selected_action
from garmin_ai.tracker_chat_form import (
    FormAnswerError,
    _display_time,
    _prompt,
    _time,
    _value,
    advance_chat_form,
    advance_close_chat_form,
    begin_chat_form,
)
from garmin_ai.tracker_chat_selection import select_tracker_actions
from garmin_ai.tracker_forms import (
    FormFieldSpec,
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


def test_edit_timestamp_uses_event_timezone_and_copyable_format():
    assert _display_time("2026-09-20T12:00:00+00:00", "Europe/Budapest") == (
        "2026-09-20 14:00+02:00"
    )


def test_flexible_end_prompt_explains_point_result(db):
    form = _form(db).model_copy(update={"topology": "flexible"})
    assert "point entry" in _prompt(form, 1, locale="en")
    assert "точечную запись" in _prompt(form, 1, locale="ru")


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


def test_numeric_prompt_keeps_exact_large_integer_bound(db):
    field = FormFieldSpec(
        name="count",
        field_id="count",
        label="Count",
        input="integer",
        required=True,
        minimum=9007199254740993,
        maximum=9007199254740993,
    )
    form = _form(db).model_copy(update={"fields": [field]})
    assert _prompt(form, 1, locale="en").count("9007199254740993") == 2


def test_optional_json_constant_prompt_uses_json_literal(db):
    field = FormFieldSpec(
        name="dose",
        field_id="dose",
        label="Dose",
        input="json",
        required=False,
        has_const=True,
        const_value={"dose": 5},
    )
    form = _form(db).model_copy(update={"fields": [field]})
    prompt = _prompt(form, 1, locale="en")
    assert '"dose"' in prompt
    assert "'dose'" not in prompt


def test_edit_json_prompt_displays_copyable_json(db):
    field = FormFieldSpec(name="data", field_id="data", label="Data", input="json", required=True)
    form = _form(db).model_copy(update={"fields": [field]})
    prompt = _prompt(
        form,
        1,
        locale="en",
        state={"action_id": "edit:synthetic", "values": {"data": {"flag": True}}},
    )
    assert '"flag": true' in prompt
    assert "'flag': True" not in prompt


def test_optional_composed_field_can_be_skipped_in_chat_form(db):
    field = FormFieldSpec(
        name="note",
        field_id="note",
        label="Note",
        input="text",
        required=False,
        complex_json=True,
    )
    form = _form(db).model_copy(update={"fields": [field]})
    assert begin_chat_form(
        AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en"
    )


def test_referenced_object_required_properties_are_combined_for_json_limit(db):
    from garmin_ai.tracker_forms import _minimum_json_length

    schema = {
        "$ref": "#/$defs/base",
        "type": "object",
        "properties": {"second": {"type": "string", "minLength": 2200}},
        "required": ["second"],
    }
    definitions = {
        "base": {
            "type": "object",
            "properties": {"first": {"type": "string", "minLength": 2200}},
            "required": ["first"],
        }
    }
    assert _minimum_json_length(schema, definitions) > 4096
    field = FormFieldSpec(
        name="data",
        field_id="data",
        label="Data",
        input="json",
        required=True,
        min_json_length=_minimum_json_length(schema, definitions),
    )
    form = _form(db).model_copy(update={"fields": [field]})
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


def test_bounded_integer_array_json_limit_uses_numeric_width(db):
    from garmin_ai.tracker_forms import _minimum_json_length

    large = 10**307
    schema = {
        "type": "array",
        "minItems": 20,
        "items": {"type": "integer", "minimum": large, "maximum": large},
    }
    assert _minimum_json_length(schema, {}) > 4096


def test_fractional_number_array_json_limit_uses_decimal_width(db):
    from garmin_ai.tracker_forms import _minimum_json_length

    schema = {
        "type": "array",
        "minItems": 1000,
        "items": {"type": "number", "minimum": 0.001, "maximum": 0.002},
    }
    assert _minimum_json_length(schema, {}) > 4096


def test_flexible_end_prompt_describes_point_when_omitted(db):
    form = _form(db).model_copy(update={"topology": "flexible"})
    assert "point entry" in _prompt(form, 1, locale="en")


def test_composed_required_text_form_is_rejected(db):
    from garmin_ai.tracker_forms import _form_fields

    schema = {
        "required": ["note"],
        "properties": {
            "note": {
                "type": "string",
                "maxLength": 16000,
                "anyOf": [{"type": "string", "minLength": 5000, "maxLength": 16000}],
            }
        },
    }
    field = _form_fields(schema, {"note": {"id": "note", "labels": {"en": "Note"}}}, "en")[0]
    assert field.complex_json
    form = _form(db).model_copy(update={"fields": [field]})
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


def test_required_fields_over_aggregate_entry_limit_are_rejected(db):
    form = _form(db).model_copy(
        update={
            "fields": [
                FormFieldSpec(
                    name=f"note_{index}",
                    field_id=f"note_{index}",
                    label=f"Note {index}",
                    input="text",
                    required=True,
                    min_length=4000,
                )
                for index in range(20)
            ]
        }
    )
    with pytest.raises(FormAnswerError, match="64 KiB"):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


def test_unreferenced_composed_definition_does_not_block_simple_form(db):
    from garmin_ai.tracker_forms import _contains_oneof

    schema = {
        "type": "object",
        "properties": {"note": {"type": "string"}},
        "$defs": {"unused": {"oneOf": [{"const": "a"}, {"const": "b"}]}},
    }
    assert not _contains_oneof(schema, schema["$defs"])
    field = FormFieldSpec(name="note", field_id="note", label="Note", input="text", required=False)
    form = _form(db).model_copy(
        update={"fields": [field], "complex_schema": _contains_oneof(schema, schema["$defs"])}
    )
    assert begin_chat_form(
        AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en"
    )


def test_boolean_array_feasibility_uses_serialized_boolean_length():
    from garmin_ai.tracker_forms import _minimum_json_length

    schema = {"type": "array", "minItems": 1000, "items": {"type": "boolean"}}
    assert _minimum_json_length(schema, {}) == 5001


def test_bounded_number_array_feasibility_uses_numeric_width():
    from garmin_ai.tracker_forms import _minimum_json_length

    huge = {"type": "number", "minimum": 1e307, "maximum": 1e307}
    schema = {"type": "array", "minItems": 1000, "items": huge}
    assert _minimum_json_length(huge, {}) == len("1e+307")
    assert _minimum_json_length(schema, {}) > 4096
    assert _minimum_json_length({**schema, "minItems": 14}, {}) < 4096


def test_fractional_number_interval_uses_realizable_json_width():
    from garmin_ai.tracker_forms import _minimum_json_length

    bound = 0.12345678901234568
    schema = {"type": "number", "minimum": bound, "maximum": bound}
    assert _minimum_json_length(schema, {}) > 1


def test_root_composition_rejects_optional_field_with_unreachable_required_answer(db):
    from garmin_ai.tracker_forms import _contains_oneof, _form_fields

    schema = {
        "properties": {"note": {"type": "string", "maxLength": 16000}},
        "anyOf": [{"required": ["note"], "properties": {"note": {"minLength": 5000}}}],
    }
    field = _form_fields(schema, {"note": {"id": "note", "labels": {"en": "Note"}}}, "en")[0]
    assert not field.required
    form = _form(db).model_copy(
        update={"fields": [field], "complex_schema": _contains_oneof(schema, {})}
    )

    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


def test_root_const_cannot_hide_unreachable_optional_field(db):
    from garmin_ai.tracker_forms import _form_fields, _root_schema_complex

    schema = {
        "type": "object",
        "properties": {"note": {"type": "string", "maxLength": 16000}},
        "required": [],
        "const": {"note": "x" * 5000},
    }
    fields = _form_fields(schema, {"note": {"id": "note", "labels": {"en": "Note"}}}, "en")
    form = _form(db).model_copy(
        update={"fields": fields, "complex_schema": _root_schema_complex(schema)}
    )
    assert form.complex_schema
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


def test_property_names_that_resemble_conditionals_are_simple():
    from garmin_ai.tracker_forms import _contains_oneof

    schema = {
        "type": "object",
        "properties": {
            "if": {"type": "string"},
            "then": {"type": "integer"},
            "else": {"type": "boolean"},
        },
    }
    assert not _contains_oneof(schema, {})


def test_required_json_boolean_array_uses_encoded_boolean_width(db):
    from garmin_ai.tracker_forms import _minimum_json_length

    schema = {"type": "array", "minItems": 1000, "items": {"type": "boolean"}}
    minimum = _minimum_json_length(schema, {})
    assert minimum == 5001
    field = FormFieldSpec(
        name="answers",
        field_id="answers",
        label="Answers",
        input="json",
        required=True,
        min_json_length=minimum,
    )
    form = _form(db).model_copy(update={"fields": [field]})
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


def test_required_json_emoji_array_uses_telegram_utf16_units(db):
    from garmin_ai.tracker_forms import _minimum_json_length

    schema = {"type": "array", "minItems": 1000, "items": {"const": "🚴"}}
    minimum = _minimum_json_length(schema, {})
    assert minimum == 5001
    field = FormFieldSpec(
        name="answers",
        field_id="answers",
        label="Answers",
        input="json",
        required=True,
        min_json_length=minimum,
    )
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(
            AppState(key="unused:pending", value={}),
            _form(db).model_copy(update={"fields": [field]}),
            timezone="UTC",
            locale="en",
        )


def test_required_json_numeric_bound_allows_compact_exponent(db):
    from garmin_ai.tracker_forms import _minimum_json_length

    number = 10**300
    schema = {"type": "array", "minItems": 20, "items": {"type": "integer", "minimum": number}}
    minimum = _minimum_json_length(schema, {})
    assert minimum < 4096
    field = FormFieldSpec(
        name="answers",
        field_id="answers",
        label="Answers",
        input="json",
        required=True,
        min_json_length=minimum,
    )
    begin_chat_form(
        AppState(key="unused:pending", value={}),
        _form(db).model_copy(update={"fields": [field]}),
        timezone="UTC",
        locale="en",
    )


def test_required_json_large_exact_integer_array_uses_numeric_width(db):
    from garmin_ai.tracker_forms import _minimum_json_length

    number = 10**300 + 1
    schema = {
        "type": "array",
        "minItems": 20,
        "items": {"type": "integer", "minimum": number, "maximum": number},
    }
    minimum = _minimum_json_length(schema, {})
    assert minimum >= 6021
    field = FormFieldSpec(
        name="answers",
        field_id="answers",
        label="Answers",
        input="json",
        required=True,
        min_json_length=minimum,
    )
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(
            AppState(key="unused:pending", value={}),
            _form(db).model_copy(update={"fields": [field]}),
            timezone="UTC",
            locale="en",
        )


def test_required_json_large_integer_const_rejected(db):
    from garmin_ai.tracker_forms import _minimum_json_length

    schema = {"type": "array", "minItems": 20, "items": {"const": 10**300 + 1}}
    minimum = _minimum_json_length(schema, {})
    assert minimum >= 6021
    field = FormFieldSpec(
        name="answers",
        field_id="answers",
        label="Answers",
        input="json",
        required=True,
        min_json_length=minimum,
    )
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(
            AppState(key="unused:pending", value={}),
            _form(db).model_copy(update={"fields": [field]}),
            timezone="UTC",
            locale="en",
        )


def test_required_json_exact_and_negative_numeric_ranges_have_sufficient_width():
    from garmin_ai.tracker_forms import _minimum_json_length

    exact = 10**300 + 1
    schemas = (
        {
            "type": "array",
            "minItems": 20,
            "items": {"type": "integer", "minimum": exact, "maximum": exact},
        },
        {"type": "array", "minItems": 1000, "items": {"type": "integer", "maximum": -100}},
    )
    assert all(_minimum_json_length(schema, {}) > 4096 for schema in schemas)


def test_affirmative_not_only_emergency_wording_is_not_negated():
    from garmin_ai.diary_forms import obvious_urgent_symptoms

    assert obvious_urgent_symptoms("I have not only sudden severe pain")
    assert obvious_urgent_symptoms("I am not without sudden severe pain")
    assert not obvious_urgent_symptoms("I have not had sudden severe pain")
    assert not obvious_urgent_symptoms("I don't have sudden severe pain")
    assert not obvious_urgent_symptoms("I haven't had sudden severe pain")
    assert not obvious_urgent_symptoms("sudden severe pain is not present")
    assert not obvious_urgent_symptoms("I didn't pass out")


def test_json_answer_rejects_unstorable_strings_and_keys():
    field = FormFieldSpec(name="data", field_id="data", label="Data", input="json", required=True)
    for answer in (r'"\u0000"', r'{"\u0000": 1}', r'{"note": "\ud800"}'):
        with pytest.raises(FormAnswerError, match="unsupported characters"):
            _value(answer, field, "en")


def test_text_answer_rejects_unstorable_characters():
    field = FormFieldSpec(name="note", field_id="note", label="Note", input="text", required=True)
    for answer in ("a\x00b", "a\ud800b"):
        with pytest.raises(FormAnswerError, match="unsupported characters"):
            _value(answer, field, "en")


def test_number_answers_reject_huge_exponents_and_lossy_json_decimals():
    number = FormFieldSpec(
        name="score", field_id="score", label="Score", input="number", required=True
    )
    with pytest.raises(FormAnswerError, match="too long"):
        _value("1e999999999", number, "en")
    structured = FormFieldSpec(
        name="data", field_id="data", label="Data", input="json", required=True
    )
    with pytest.raises(FormAnswerError, match="exactly"):
        _value("[0.1234567890123456789]", structured, "en")
    assert _value('[0.5, {"dose": 5}]', structured, "en") == [0.5, {"dose": 5}]


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
    assert pending.value["created_at"] == NOW.isoformat()
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


def test_delayed_answer_refreshes_form_with_processing_time(db):
    form = _form(db)
    pending = AppState(key="conversation:pending", value={"created_at": NOW.isoformat()})
    db.add(pending)
    begin_chat_form(pending, form, timezone="UTC", locale="en")
    processed_at = NOW + timedelta(hours=3)

    advance_chat_form(
        db,
        pending,
        "now",
        actor="test",
        now=NOW,
        processed_at=processed_at,
        source="telegram_text",
    )

    assert pending.value["created_at"] == processed_at.isoformat()


def test_delayed_caption_advances_the_guided_form_from_message_time(db, db_engine):
    form = _form(db)
    created = datetime.now(UTC) - timedelta(hours=3)
    pending = AppState(
        key="conversation:pending",
        value={
            "button": "tracker_form",
            "definition_version_id": str(form.action.definition_version_id),
            "channel_instance_id": "telegram:primary",
            "created_at": created.isoformat(),
        },
    )
    db.add(pending)
    begin_chat_form(pending, form, timezone="UTC", locale="en")
    update = {
        "update_id": 5968,
        "message": {
            "message_id": 5968,
            "date": int((created + timedelta(hours=1)).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "voice": {"file_id": "synthetic"},
            "caption": "now",
        },
    }
    assert save_update(db, update, 42)
    db.commit()

    response = process_message(
        db_engine, None, Settings(telegram_user_id=42, locale="en"), 5968, transcript=""
    )
    db.expire_all()
    pending = db.get(AppState, "conversation:pending")
    assert pending.value["chat_form"]["step"] == 1
    assert datetime.fromisoformat(pending.value["created_at"]) > created + timedelta(hours=2)
    assert response


def test_paused_provider_defers_callback_behind_queued_guided_answer(db, db_engine):
    from garmin_ai.models import Job, TelegramUpdate
    from garmin_ai.provider_gate import KEY, configuration_key
    from garmin_ai.telegram import DiaryDeferred

    settings = Settings(telegram_user_id=42, locale="en")
    form = _form(db)
    pending = AppState(
        key="conversation:pending",
        value={
            "button": "tracker_form",
            "definition_version_id": str(form.action.definition_version_id),
            "channel_instance_id": "telegram:primary",
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    db.add(pending)
    begin_chat_form(pending, form, timezone="UTC", locale="en")
    message = {
        "message_id": 5969,
        "date": int(datetime.now(UTC).timestamp()),
        "from": {"id": 42},
        "chat": {"id": 42, "type": "private"},
        "text": "now",
    }
    assert save_update(db, {"update_id": 5969, "message": message}, 42)
    assert save_update(
        db,
        {
            "update_id": 5970,
            "callback_query": {
                "id": "synthetic-guided-callback",
                "from": {"id": 42},
                "data": "note",
                "message": {**message, "message_id": 5970, "text": "Choose"},
            },
        },
        42,
    )
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
        process_message(db_engine, None, settings, 5970)
    db.expire_all()
    assert db.get(AppState, "conversation:pending").value["chat_form"]["step"] == 0
    assert db.get(TelegramUpdate, 5970).status == "pending"
    assert db.scalar(select(Job).where(Job.dedup_key == "telegram:5969")) is not None


def test_guided_form_uses_regional_english_locale(db):
    form = _form(db)
    pending = AppState(
        key="conversation:pending",
        value={"definition_version_id": str(form.action.definition_version_id)},
    )
    db.add(pending)
    prompt = begin_chat_form(pending, form, timezone="UTC", locale="en-US")

    assert prompt.startswith("When did the entry start?")


@pytest.mark.parametrize(
    "value",
    [
        "2026-03-29 02:30",
        "2026-10-25 02:30",
        "2026-01-01 12:00+09:00",
        "2026-09-20",
    ],
)
def test_generated_form_rejects_ambiguous_or_incomplete_local_times(value):
    with pytest.raises(FormAnswerError):
        _time(value, "Europe/Bratislava", NOW)


def test_generated_form_accepts_explicit_valid_dst_offsets():
    after_transition = datetime(2026, 10, 26, tzinfo=UTC)
    first = _time("2026-10-25 02:30+02:00", "Europe/Bratislava", after_transition)
    second = _time("2026-10-25 02:30+01:00", "Europe/Bratislava", after_transition)
    assert first.astimezone(UTC) != second.astimezone(UTC)


def test_generated_form_rejects_future_fact_time():
    with pytest.raises(FormAnswerError, match="future"):
        _time("2062-09-20 12:00", "UTC", NOW, "en")


def test_invalid_time_does_not_echo_parser_input(db):
    form = _form(db)
    pending = AppState(
        key="conversation:pending",
        value={"definition_version_id": str(form.action.definition_version_id)},
    )
    db.add(pending)
    begin_chat_form(pending, form, timezone="UTC", locale="ru")

    response = advance_chat_form(
        db,
        pending,
        "2026-02-99 12:00",
        actor="test",
        now=NOW,
        source="telegram_text",
    )["response"]

    assert "2026-02-99" not in response
    assert "Некорректная дата" in response
    assert pending.value["created_at"] == NOW.isoformat()


def test_choice_prefers_exact_case_and_keeps_literal_skip_value():
    field = FormFieldSpec(
        name="choice",
        field_id="choice",
        label="Choice",
        input="choice",
        required=False,
        options=["Yes", "yes", "-"],
    )
    assert _value("yes", field, "en") == "yes"
    assert _value("-", field, "en") == "-"
    assert _value("/skip", field, "en") is None
    with pytest.raises(FormAnswerError):
        _value("YES", field, "en")
    nullable = field.model_copy(update={"options": [None, "known"]})
    assert _value("None", nullable, "en") is None
    slash = field.model_copy(update={"options": ["/skip", "/cancel", "=value"]})
    from garmin_ai.tracker_chat_form import _choice_labels

    assert _choice_labels(slash.options) == ["=/skip", "=/cancel", "==value"]
    assert _value("=/skip", slash, "en") == "/skip"
    assert _value("=/cancel", slash, "en") == "/cancel"
    assert _value("==value", slash, "en") == "=value"
    assert _value("/skip", slash, "en") is None
    spaced = field.model_copy(update={"options": [" /cancel"]})
    assert _choice_labels(spaced.options) == ["= /cancel"]
    assert _value("= /cancel", spaced, "en") == " /cancel"


def test_choice_labels_distinguish_json_types():
    from garmin_ai.tracker_chat_form import _choice_labels

    field = FormFieldSpec(
        name="choice",
        field_id="choice",
        label="Choice",
        input="choice",
        required=True,
        options=[1, "1", True, "True"],
    )
    labels = _choice_labels(field.options)
    assert len(set(labels)) == 4
    assert [_value(label, field, "en") for label in labels] == field.options

    colliding = field.model_copy(
        update={"options": [1, "1", '"1"', None, "None", "null", "", "/empty"]}
    )
    labels = _choice_labels(colliding.options)
    assert len(set(labels)) == len(labels)
    assert [_value(label, colliding, "en") for label in labels] == colliding.options

    cyclic = field.model_copy(update={"options": [None, "None", "1: null", '3: "1: null"']})
    labels = _choice_labels(cyclic.options)
    assert len(set(labels)) == len(labels)
    assert [_value(label, cyclic, "en") for label in labels] == cyclic.options
    equals_empty = field.model_copy(update={"options": ["=/empty", "==/empty"]})
    labels = _choice_labels(equals_empty.options)
    assert labels == ["==/empty", "===/empty"]
    assert [_value(label, equals_empty, "en") for label in labels] == equals_empty.options


def test_constant_schema_field_is_injected_without_chat_question(db, monkeypatch):
    from garmin_ai import tracker_chat_form
    from garmin_ai.tracker_forms import _form_fields

    constant = _form_fields(
        {"properties": {"origin": {"type": "string", "const": "chat"}}, "required": ["origin"]},
        {"origin": {"id": "origin", "labels": {"en": "Origin"}}},
        "en",
    )[0]
    form = _form(db)
    form = form.model_copy(update={"fields": [*form.fields, constant]})
    pending = AppState(key="conversation:pending", value={})
    db.add(pending)
    monkeypatch.setattr(tracker_chat_form, "form_for_action", lambda *_args, **_kwargs: form)
    saved = []
    monkeypatch.setattr(
        tracker_chat_form,
        "submit_form",
        lambda _session, _action, body, **_kwargs: saved.append(body),
    )

    begin_chat_form(pending, form, timezone="UTC", locale="en")
    state = pending.value["chat_form"]
    assert "origin" not in state["field_order"]
    assert state["values"]["origin"] == "chat"
    answers = {"rating": "4", "count": "2", "note": "Fine"}
    advance_chat_form(db, pending, "now", actor="test", now=NOW, source="telegram_text")
    for name in state["field_order"]:
        advance_chat_form(db, pending, answers[name], actor="test", now=NOW, source="telegram_text")
    assert len(saved) == 1 and saved[0].values["origin"] == "chat"


def test_invalid_constant_only_form_rejected_before_opening(db):
    from garmin_ai.tracker_forms import _form_fields

    constant = _form_fields(
        {
            "properties": {"origin": {"type": "string", "const": "x", "minLength": 2}},
            "required": ["origin"],
        },
        {"origin": {"id": "origin", "labels": {"en": "Origin"}}},
        "en",
    )[0]
    form = _form(db).model_copy(update={"fields": [constant]})
    pending = AppState(key="conversation:pending", value={})
    db.add(pending)
    with pytest.raises(FormAnswerError, match="fixed value does not match"):
        begin_chat_form(pending, form, timezone="UTC", locale="en")
    assert "chat_form" not in pending.value


@pytest.mark.parametrize("answer, included", [("/skip", False), ("manual", True)])
def test_optional_constant_can_be_omitted_or_supplied(db, monkeypatch, answer, included):
    from garmin_ai import tracker_chat_form
    from garmin_ai.tracker_forms import _form_fields

    constant = _form_fields(
        {"properties": {"origin": {"type": "string", "const": "manual"}}},
        {"origin": {"id": "origin", "labels": {"en": "Origin"}}},
        "en",
    )[0]
    form = _form(db)
    form = form.model_copy(update={"fields": [*form.fields, constant]})
    pending = AppState(key="conversation:pending", value={})
    db.add(pending)
    monkeypatch.setattr(tracker_chat_form, "form_for_action", lambda *_args, **_kwargs: form)
    saved = []
    monkeypatch.setattr(
        tracker_chat_form,
        "submit_form",
        lambda _session, _action, body, **_kwargs: saved.append(body),
    )

    begin_chat_form(pending, form, timezone="UTC", locale="en")
    assert pending.value["chat_form"]["field_order"][-1] == "origin"
    assert "origin" not in pending.value["chat_form"]["values"]
    for value in ("now", "4", "2", "Fine"):
        advance_chat_form(db, pending, value, actor="test", now=NOW, source="telegram_text")
    invalid = advance_chat_form(db, pending, "other", actor="test", now=NOW, source="telegram_text")
    assert "fixed value" in invalid["response"]
    assert pending.value["chat_form"]["step"] == 4
    result = advance_chat_form(db, pending, answer, actor="test", now=NOW, source="telegram_text")
    assert result["written"] and len(saved) == 1
    assert ("origin" in saved[0].values) is included


def test_required_text_rejects_bare_skip_but_accepts_explicit_literal():
    field = FormFieldSpec(name="note", field_id="note", label="Note", input="text", required=True)
    with pytest.raises(FormAnswerError, match="cannot be skipped"):
        _value("/skip", field, "en")
    assert _value("=/skip", field, "en") == "/skip"


def test_json_and_text_fields_reject_values_that_cannot_be_persisted():
    json_field = FormFieldSpec(
        name="data", field_id="data", label="Data", input="json", required=True
    )
    assert _value("null", json_field, "en") is None
    with pytest.raises(ValueError):
        _value("NaN", json_field, "en")
    with pytest.raises(ValueError, match="Duplicate"):
        _value('{"dose": 5, "dose": 50}', json_field, "en")
    for overflow in ("1e100000", "[1e100000]", '{"dose": [1e100000]}'):
        with pytest.raises(ValueError, match="Non-finite"):
            _value(overflow, json_field, "en")
    text_field = FormFieldSpec(
        name="note",
        field_id="note",
        label="Note",
        input="text",
        required=True,
        min_length=3,
    )
    with pytest.raises(FormAnswerError):
        _value("ab", text_field, "en")
    assert _value("  ab  ", text_field, "en") == "  ab  "
    empty_allowed = text_field.model_copy(update={"min_length": 0})
    assert _value("=/empty", empty_allowed, "en") == ""
    assert _value("==/empty", empty_allowed, "en") == "/empty"
    assert _value("===/empty", empty_allowed, "en") == "=/empty"
    choice = FormFieldSpec(
        name="choice",
        field_id="choice",
        label="Choice",
        input="choice",
        required=True,
        options=["/empty", "=/empty"],
    )
    assert _value("=/empty", choice, "en") == "/empty"
    assert _value("==/empty", choice, "en") == "=/empty"
    with pytest.raises(FormAnswerError, match="Prefix"):
        _value("/skp", empty_allowed, "en")
    assert _value("=/skp", empty_allowed, "en") == "/skp"


def test_deep_json_answer_reasks_without_crashing(db, monkeypatch):
    from garmin_ai import tracker_chat_form

    form = _form(db).model_copy(
        update={
            "fields": [
                FormFieldSpec(
                    name="data", field_id="data", label="Data", input="json", required=True
                )
            ]
        }
    )
    monkeypatch.setattr(tracker_chat_form, "form_for_action", lambda *_args, **_kwargs: form)
    pending = AppState(key="conversation:pending", value={})
    db.add(pending)
    begin_chat_form(pending, form, timezone="UTC", locale="en")
    advance_chat_form(db, pending, "now", actor="test", now=NOW, source="telegram_text")
    response = advance_chat_form(
        db,
        pending,
        "[" * 1200 + "]" * 1200,
        actor="test",
        now=NOW,
        source="telegram_text",
    )
    assert not response["written"]
    assert pending.value["chat_form"]["step"] == 1


def test_guided_form_rejects_required_answer_exceeding_telegram_limit(db, monkeypatch):
    form = _form(db)
    oversized = form.model_copy(
        update={
            "fields": [
                field.model_copy(update={"min_length": 4097}) if field.input == "text" else field
                for field in form.fields
            ]
        }
    )
    pending = AppState(key="unused:pending", value={})
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(pending, oversized, timezone="UTC", locale="en")
    assert "chat_form" not in pending.value
    oversized_choice = form.model_copy(
        update={
            "fields": [
                FormFieldSpec(
                    name="choice",
                    field_id="choice",
                    label="Choice",
                    input="choice",
                    required=True,
                    options=["x" * 5000],
                )
            ]
        }
    )
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(pending, oversized_choice, timezone="UTC", locale="en")
    assert "chat_form" not in pending.value
    db.info["channel_destination_instance_id"] = "telegram:primary"
    monkeypatch.setattr(
        "garmin_ai.tracker_forms.form_for_action", lambda *_args, **_kwargs: oversized
    )
    response = handle_button(db, form.id, Settings(locale="en"), "telegram:42", 9100, NOW)
    assert "Telegram" in response
    assert db.get(AppState, "conversation:pending") is None


@pytest.mark.parametrize(
    "field",
    [
        FormFieldSpec(
            name="choice",
            field_id="choice",
            label="Choice",
            input="choice",
            required=True,
            options=["x"],
            min_length=2,
            validation_schema={"type": "string", "enum": ["x"], "minLength": 2},
        ),
        FormFieldSpec(
            name="choice",
            field_id="choice",
            label="Choice",
            input="choice",
            required=True,
            options=[{str(index): index for index in range(33)}],
        ),
        FormFieldSpec(
            name="choice",
            field_id="choice",
            label="Choice",
            input="choice",
            required=True,
            has_const=True,
            const_value="a",
            options=["b"],
            validation_schema={"type": "string", "const": "a", "enum": ["b"]},
        ),
    ],
)
def test_guided_form_rejects_unreachable_choice_or_constant(db, field):
    form = _form(db).model_copy(update={"fields": [field]})
    with pytest.raises(FormAnswerError):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


def test_choice_schema_constraints_reach_chat_preflight(db):
    from garmin_ai.tracker_forms import _form_fields

    fields = _form_fields(
        {
            "type": "object",
            "properties": {"choice": {"type": "string", "enum": ["x"], "minLength": 2}},
            "required": ["choice"],
        },
        {"choice": {"id": "choice", "labels": {"en": "Choice"}}},
        "en",
    )
    form = _form(db).model_copy(update={"fields": fields})
    with pytest.raises(FormAnswerError):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


def test_guided_form_accepts_reachable_choice_and_long_split_prompt(db):
    form = _form(db)
    pending = AppState(key="unused:pending", value={})
    choices = ["x" * 5000, "short"]
    choice_form = form.model_copy(
        update={
            "fields": [
                FormFieldSpec(
                    name="choice",
                    field_id="choice",
                    label="Choice",
                    input="choice",
                    required=True,
                    options=choices,
                )
            ]
        }
    )
    assert begin_chat_form(pending, choice_form, timezone="UTC", locale="en")
    many_choices = [f"{index}:" + "x" * 120 for index in range(50)]
    long_prompt = choice_form.model_copy(
        update={"fields": [choice_form.fields[0].model_copy(update={"options": many_choices})]}
    )
    begin_chat_form(pending, long_prompt, timezone="UTC", locale="en")
    assert len(_prompt(long_prompt, 1, locale="en")) > 4096


def test_guided_form_rejects_required_json_over_input_limit(db):
    form = _form(db)
    pending = AppState(key="unused:pending", value={})
    oversized = form.model_copy(
        update={
            "fields": [
                FormFieldSpec(
                    name="data",
                    field_id="data",
                    label="Data",
                    input="json",
                    required=True,
                    min_json_length=4097,
                )
            ]
        }
    )
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(pending, oversized, timezone="UTC", locale="en")


def test_guided_form_validates_json_field_before_advancing(db, monkeypatch):
    from copy import deepcopy

    from garmin_ai import tracker_chat_form
    from garmin_ai.models import EventDefinitionVersion
    from garmin_ai.tracker_forms import _form_fields

    form = _form(db)
    version = db.get(EventDefinitionVersion, form.action.definition_version_id)
    schema = deepcopy(version.schema)
    schema["properties"]["rating"] = {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "object",
            "required": ["score"],
            "properties": {"score": {"type": "integer", "minimum": 1}},
        },
    }
    version.schema = schema
    form = form.model_copy(update={"fields": _form_fields(schema, version.field_metadata, "en")})
    monkeypatch.setattr(tracker_chat_form, "form_for_action", lambda *_args, **_kwargs: form)
    pending = AppState(key="conversation:pending", value={})
    db.add(pending)
    begin_chat_form(pending, form, timezone="UTC", locale="en")
    with db.no_autoflush:
        advance_chat_form(db, pending, "now", actor="test", now=NOW, source="telegram_text")
        for invalid in ("{}", "[{}]", '[{"score": 0}]'):
            result = advance_chat_form(
                db, pending, invalid, actor="test", now=NOW, source="telegram_text"
            )
            assert "JSON does not match the field schema" in result["response"]
            assert pending.value["chat_form"]["step"] == 1
        result = advance_chat_form(
            db, pending, '[{"score": 1}]', actor="test", now=NOW, source="telegram_text"
        )
        assert result["written"] is False
        assert pending.value["chat_form"]["step"] == 2


def test_guided_form_rejects_conditional_required_fields(db):
    form = _form(db).model_copy(update={"conditional_requirements": True})
    with pytest.raises(FormAnswerError, match="conditional required fields"):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


@pytest.mark.parametrize(
    "root_constraint",
    [
        {"$ref": "#/$defs/root", "$defs": {"root": {"type": "object", "required": ["rating"]}}},
        {
            "oneOf": [
                {"properties": {"rating": {"maximum": 2}}},
                {"properties": {"rating": {"minimum": 3}}},
            ]
        },
        {"anyOf": [{"$ref": "#/$defs/branch"}], "$defs": {"branch": {"required": ["rating"]}}},
    ],
)
def test_guided_form_rejects_unrendered_root_constraints(db, monkeypatch, root_constraint):
    from types import SimpleNamespace

    from garmin_ai import tracker_forms
    from garmin_ai.models import EventDefinition, EventDefinitionVersion

    form = _form(db)
    version = db.get(EventDefinitionVersion, form.action.definition_version_id)
    definition = db.get(EventDefinition, version.definition_id)
    shadow = SimpleNamespace(
        id=version.id,
        labels=version.labels,
        topology=version.topology,
        schema_hash=version.schema_hash,
        schema={**version.schema, **root_constraint},
        field_metadata=version.field_metadata,
    )
    monkeypatch.setattr(tracker_forms, "_resolve_action", lambda *_args: (definition, shadow, None))
    generated = form_for_action(db, form.id, locale="en")
    assert generated.conditional_requirements
    with pytest.raises(FormAnswerError, match="conditional required fields"):
        begin_chat_form(
            AppState(key="unused:pending", value={}), generated, timezone="UTC", locale="en"
        )


def test_guided_form_sizes_choice_answers_in_telegram_utf16_units(db):
    form = _form(db)
    field = FormFieldSpec(
        name="symbol",
        field_id="symbol",
        label="Symbol",
        input="choice",
        required=True,
        options=["🚴" * 3000],
    )
    choice_form = form.model_copy(update={"fields": [field]})
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(
            AppState(key="unused:pending", value={}), choice_form, timezone="UTC", locale="en"
        )


def test_guided_form_rejects_aggregate_required_payload_over_storage_limit(db):
    form = _form(db)
    fields = [
        FormFieldSpec(
            name=f"field_{index}",
            field_id=f"field_{index}",
            label=f"Field {index}",
            input="text",
            required=True,
            min_length=4096,
        )
        for index in range(17)
    ]
    with pytest.raises(FormAnswerError, match="64 KiB"):
        begin_chat_form(
            AppState(key="unused:pending", value={}),
            form.model_copy(update={"fields": fields}),
            timezone="UTC",
            locale="en",
        )


def test_guided_form_counts_ascii_escaped_constants_in_storage(db):
    from garmin_ai.tracker_forms import _form_fields

    names = [f"field_{index}" for index in range(32)]
    schema = {
        "$defs": {"text": {"type": "string", "const": "я" * 400}},
        "type": "object",
        "properties": {name: {"$ref": "#/$defs/text"} for name in names},
        "required": names,
    }
    metadata = {name: {"id": name, "labels": {"en": name}} for name in names}
    fields = _form_fields(schema, metadata, "en")
    assert all(field.has_const for field in fields)
    with pytest.raises(FormAnswerError, match="64 KiB"):
        begin_chat_form(
            AppState(key="unused:pending", value={}),
            _form(db).model_copy(update={"fields": fields}),
            timezone="UTC",
            locale="en",
        )


def test_guided_form_counts_nested_json_storage_bytes(db):
    from garmin_ai.tracker_forms import _form_fields

    names = [f"field_{index}" for index in range(32)]
    schema = {
        "$defs": {"text": {"type": "string", "const": "я" * 400}},
        "type": "object",
        "properties": {
            name: {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/text"}}
            for name in names
        },
        "required": names,
    }
    metadata = {name: {"id": name, "labels": {"en": name}} for name in names}
    fields = _form_fields(schema, metadata, "en")
    assert all(field.min_json_length < 4096 for field in fields)
    assert all(field.min_json_storage_length > 2400 for field in fields)
    with pytest.raises(FormAnswerError, match="64 KiB"):
        begin_chat_form(
            AppState(key="unused:pending", value={}),
            _form(db).model_copy(update={"fields": fields}),
            timezone="UTC",
            locale="en",
        )


def test_guided_form_counts_object_reference_storage_bytes(db):
    from garmin_ai.tracker_forms import _form_fields

    names = [f"field_{index}" for index in range(32)]
    schema = {
        "$defs": {
            "detail": {
                "type": "object",
                "properties": {"text": {"type": "string", "const": "я" * 400}},
                "required": ["text"],
            }
        },
        "type": "object",
        "properties": {name: {"$ref": "#/$defs/detail"} for name in names},
        "required": names,
    }
    metadata = {name: {"id": name, "labels": {"en": name}} for name in names}
    fields = _form_fields(schema, metadata, "en")
    assert all(field.min_json_length < 4096 for field in fields)
    assert all(field.min_json_storage_length > 2400 for field in fields)
    with pytest.raises(FormAnswerError, match="64 KiB"):
        begin_chat_form(
            AppState(key="unused:pending", value={}),
            _form(db).model_copy(update={"fields": fields}),
            timezone="UTC",
            locale="en",
        )


def test_whitespace_only_choice_has_sendable_label():
    from garmin_ai.tracker_chat_form import _choice_labels

    field = FormFieldSpec(
        name="choice",
        field_id="choice",
        label="Choice",
        input="choice",
        required=True,
        options=[" "],
    )
    label = _choice_labels(field.options)[0]
    assert label.strip()
    assert _value(label, field, "en") == " "


def test_narrow_number_interval_is_rejected_before_chat_starts(db):
    field = FormFieldSpec(
        name="score",
        field_id="score",
        label="Score",
        input="number",
        required=True,
        minimum=0.1,
        maximum=0.10000000000000002,
        exclusive_minimum=True,
        exclusive_maximum=True,
    )
    with pytest.raises(FormAnswerError, match="numeric field"):
        begin_chat_form(
            AppState(key="unused:pending", value={}),
            _form(db).model_copy(update={"fields": [field]}),
            timezone="UTC",
            locale="en",
        )


def test_mixed_numeric_bounds_without_representable_value_are_rejected(db):
    field = FormFieldSpec(
        name="score",
        field_id="score",
        label="Score",
        input="number",
        required=True,
        minimum=0,
        maximum=5e-324,
        exclusive_minimum=True,
        exclusive_maximum=True,
    )
    with pytest.raises(FormAnswerError, match="numeric field"):
        begin_chat_form(
            AppState(key="unused:pending", value={}),
            _form(db).model_copy(update={"fields": [field]}),
            timezone="UTC",
            locale="en",
        )


def test_exact_integer_candidate_keeps_number_form_available(db):
    lower = 9_007_199_254_740_992
    field = FormFieldSpec(
        name="score",
        field_id="score",
        label="Score",
        input="number",
        required=True,
        minimum=lower,
        maximum=lower + 1,
        exclusive_maximum=True,
    )
    prompt = begin_chat_form(
        AppState(key="unused:pending", value={}),
        _form(db).model_copy(update={"fields": [field]}),
        timezone="UTC",
        locale="en",
    )
    assert "When" in prompt
    assert _value(str(lower), field, "en") == lower


def test_required_reference_with_sibling_constraints_is_complex(db):
    from garmin_ai.tracker_forms import _form_fields

    schema = {
        "$defs": {
            "base": {
                "type": "object",
                "required": ["a"],
                "properties": {"a": {"type": "string", "minLength": 2500}},
            }
        },
        "properties": {
            "data": {
                "$ref": "#/$defs/base",
                "type": "object",
                "required": ["b"],
                "properties": {"b": {"type": "string", "minLength": 2500}},
            }
        },
        "required": ["data"],
    }
    field = _form_fields(schema, {"data": {"id": "data", "labels": {"en": "Data"}}}, "en")[0]
    assert field.complex_json
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(
            AppState(key="unused:pending", value={}),
            _form(db).model_copy(update={"fields": [field]}),
            timezone="UTC",
            locale="en",
        )


def test_reference_annotations_do_not_block_guided_field(db):
    from garmin_ai.tracker_forms import _form_fields

    schema = {
        "$defs": {"score": {"type": "integer", "minimum": 1, "maximum": 5}},
        "properties": {
            "score": {"$ref": "#/$defs/score", "description": "Daily score", "title": "Score"}
        },
        "required": ["score"],
    }
    field = _form_fields(schema, {"score": {"id": "score", "labels": {"en": "Score"}}}, "en")[0]
    assert not field.complex_json
    assert begin_chat_form(
        AppState(key="unused:pending", value={}),
        _form(db).model_copy(update={"fields": [field]}),
        timezone="UTC",
        locale="en",
    )


def test_optional_property_composition_does_not_block_guided_form(db, monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace

    from garmin_ai import tracker_forms
    from garmin_ai.models import EventDefinition, EventDefinitionVersion

    form = _form(db)
    version = db.get(EventDefinitionVersion, form.action.definition_version_id)
    definition = db.get(EventDefinition, version.definition_id)
    schema = deepcopy(version.schema)
    schema["properties"]["note"]["anyOf"] = [{"type": "string"}]
    schema["required"].remove("note")
    shadow = SimpleNamespace(
        id=version.id,
        labels=version.labels,
        topology=version.topology,
        schema_hash=version.schema_hash,
        schema=schema,
        field_metadata=version.field_metadata,
    )
    monkeypatch.setattr(tracker_forms, "_resolve_action", lambda *_args: (definition, shadow, None))
    generated = form_for_action(db, form.id, locale="en")
    assert not generated.complex_schema
    assert begin_chat_form(
        AppState(key="unused:pending", value={}), generated, timezone="UTC", locale="en"
    )


def test_guided_form_rejects_overlapping_oneof_json(db):
    from garmin_ai.tracker_forms import _form_fields

    schema = {
        "required": ["data"],
        "properties": {
            "data": {
                "oneOf": [
                    {"type": "string", "maxLength": 16000},
                    {"type": "string", "maxLength": 4096},
                ]
            }
        },
    }
    field = _form_fields(schema, {"data": {"id": "data", "labels": {"en": "Data"}}}, "en")[0]
    assert field.complex_json
    form = _form(db).model_copy(update={"fields": [field]})
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


def test_guided_edit_can_preserve_an_existing_complex_json_field(db):
    form = _form(db)
    field = FormFieldSpec(
        name="data", field_id="data", label="Data", input="json", required=True, complex_json=True
    )
    edit = form.model_copy(
        update={
            "action": form.action.model_copy(update={"kind": "edit_entry"}),
            "fields": [field],
            "initial_values": {"data": {"note": "existing"}},
        }
    )
    pending = AppState(key="unused:pending", value={})
    assert begin_chat_form(pending, edit, timezone="UTC", locale="en")
    assert pending.value["chat_form"]["values"]["data"] == {"note": "existing"}


def test_guided_form_refreshes_expiry_using_processing_clock(db):
    form = _form(db)
    pending = AppState(key="conversation:pending", value={})
    db.add(pending)
    begin_chat_form(pending, form, timezone="UTC", locale="en")
    processing_now = NOW + timedelta(hours=3)
    db.info["conversation_now"] = processing_now
    try:
        advance_chat_form(
            db, pending, "invalid time", actor="test", now=NOW, source="telegram_text"
        )
        assert pending.value["created_at"] == processing_now.isoformat()
    finally:
        db.info.pop("conversation_now", None)


@pytest.mark.parametrize(
    "failure, message",
    [
        ("schema", "Check the values"),
        ("size", "Shorten the values"),
        ("array_size", "Shorten the values"),
        ("string_size", "Shorten the values"),
        ("depth", "Shorten the values"),
        ("aggregate_size", "Shorten the values"),
    ],
)
def test_final_validation_retry_uses_processing_clock(db, monkeypatch, failure, message):
    from garmin_ai import tracker_chat_form
    from garmin_ai.tracker_forms import FormValidationError

    form = _form(db)
    pending = AppState(key="conversation:pending", value={})
    db.add(pending)
    begin_chat_form(pending, form, timezone="UTC", locale="en")
    processed_at = NOW + timedelta(hours=3)
    db.info["conversation_now"] = processed_at
    error = (
        FormValidationError([{"field": "note", "code": "minLength"}])
        if failure == "schema"
        else ValueError(
            {
                "size": "Entry object is too large",
                "array_size": "Entry array is too large",
                "string_size": "Entry string is too long",
                "depth": "Entry value depth exceeds the supported profile",
                "aggregate_size": "Entry values exceed 64 KiB",
            }[failure]
        )
    )

    def reject(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(tracker_chat_form, "submit_form", reject)
    try:
        result = None
        answers = {"rating": "4", "count": "2", "note": "Fine"}
        for answer in (
            "now",
            *(answers[name] for name in pending.value["chat_form"]["field_order"]),
        ):
            result = advance_chat_form(
                db, pending, answer, actor="test", now=NOW, source="telegram_text"
            )
        assert message in result["response"]
        assert pending.value["created_at"] == processed_at.isoformat()
        assert pending.value["chat_form"]["step"] == 1
    finally:
        db.info.pop("conversation_now", None)


def test_huge_setup_integer_bound_is_validation_error():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="finite bounds"):
        TrackerFieldDraft(
            key="count",
            label="Count",
            kind="integer",
            minimum=0,
            maximum=10**400,
        )


def test_integer_schema_bounds_keep_exact_precision(db):
    from garmin_ai.tracker_forms import _form_fields

    exact = 9_007_199_254_740_993
    field = _form_fields(
        {
            "properties": {"count": {"type": "integer", "minimum": exact, "maximum": exact}},
            "required": ["count"],
        },
        {"count": {"id": "count", "labels": {"en": "Count"}}},
        "en",
    )[0]
    assert field.minimum == exact and field.maximum == exact
    form = _form(db).model_copy(update={"fields": [field]})
    assert str(exact) in _prompt(form, 1, locale="en")
    assert _value(str(exact), field, "en") == exact
    with pytest.raises(FormAnswerError):
        _value(str(exact - 1), field, "en")


def test_number_field_preserves_large_integer_and_rejects_lossy_decimal():
    exact = 9_007_199_254_740_993
    field = FormFieldSpec(
        name="amount",
        field_id="amount",
        label="Amount",
        input="number",
        required=True,
        minimum=exact,
        maximum=exact + 2,
    )
    assert _value(str(exact), field, "en") == exact
    decimal_field = field.model_copy(update={"minimum": None, "maximum": None})
    assert _value("0.1", decimal_field, "en") == 0.1
    with pytest.raises(FormAnswerError, match="Too many digits"):
        _value("0.1234567890123456789", decimal_field, "en")


def test_guided_prompt_displays_exact_large_bounds(db):
    exact = 9_007_199_254_740_993
    form = _form(db)
    field = FormFieldSpec(
        name="amount",
        field_id="amount",
        label="Amount",
        input="number",
        required=True,
        minimum=exact,
        maximum=exact + 2,
    )
    prompt = _prompt(form.model_copy(update={"fields": [field]}), 1, locale="en")
    assert str(exact) in prompt and str(exact + 2) in prompt
    assert "e+" not in prompt


def test_unsupported_locale_uses_english_guided_prompts(db):
    from garmin_ai.diary_forms import form_safety_notice

    form = _form(db)
    assert "When did" in _prompt(form, 0, locale="de")
    assert form_safety_notice("de").startswith("This form")


def test_guided_numeric_field_respects_exclusive_schema_bounds():
    from garmin_ai.tracker_forms import _form_fields

    fields = _form_fields(
        {
            "properties": {
                "score": {
                    "type": "number",
                    "minimum": 0,
                    "exclusiveMinimum": 1,
                    "maximum": 6,
                    "exclusiveMaximum": 5,
                }
            },
            "required": ["score"],
        },
        {"score": {"id": "score", "labels": {"en": "Score"}}},
        "en",
    )
    field = fields[0]
    assert field.minimum == 1 and field.exclusive_minimum
    assert field.maximum == 5 and field.exclusive_maximum
    with pytest.raises(FormAnswerError, match="minimum"):
        _value("1", field, "en")
    with pytest.raises(FormAnswerError, match="maximum"):
        _value("5", field, "en")
    assert _value("1.5", field, "en") == 1.5
    with pytest.raises(FormAnswerError, match="decimal point"):
        _value("1,000", field, "en")


def test_local_urgent_screen_handles_emergencies_without_negated_choices():
    from garmin_ai.diary_forms import obvious_urgent_symptoms

    for text in (
        "I can't breathe",
        "I can not breathe",
        "I can't  breathe",
        "не   могу дышать",
        "Can you help? He cannot breathe",
        "How to help someone having a heart attack?",
        "My husband is having a heart attack",
        "My child is having a stroke",
        "My wife is having a seizure",
        "My father is bleeding heavily",
        "I am bleeding heavily",
        "Как помочь человеку, у которого инсульт?",
        "I can’t breathe",
        "signs of a stroke",
        "sudden severe chest pain",
        "I think I'm having a heart attack",
        "I may be having a stroke",
        "I am having a seizure",
        "Can you help—sudden crushing chest pressure and cold sweat",
        "severe bleeding",
        "у меня сильное кровотечение",
        "I have severe chest pain",
        "у меня сильная боль",
        "I'm having a stroke",
        "I'm having a heart attack",
        "I had a heart attack",
        "у меня инсульт",
        "What are signs of a stroke? I can't breathe",
        "I had a stroke in 2010 and I cannot breathe",
        "I had a stroke in 2010 and now I'm having a heart attack",
        "I had severe back pain five years ago and now have severe chest pain",
        "У меня инфаркт",
        "потерял сознание",
    ):
        assert obvious_urgent_symptoms(text)
    for text in (
        "No sudden severe pain",
        "нет внезапной сильной боли",
        "Внезапной сильной боли нет",
        "Внезапной сильной боли не было",
        "no signs of a stroke",
        "What are signs of a stroke?",
        "no heart attack",
        "What are the common signs of a stroke?",
        "Can you explain the signs of a stroke?",
        "What causes severe chest pain?",
        "Какие признаки инсульта?",
        "I had a stroke in 2010",
        "I had a stroke in 2010 and now take aspirin",
        "I had a heart attack 10 years ago and take aspirin",
        "I had a heart attack 10 years ago",
        "I had a seizure 10 years ago",
        "Log seizure medication at 8",
        "Record stroke recovery medication",
        "Record tracker Seizure",
        "Записать трекер Инсульт",
        "Log severe knee pain from last week",
        "I had severe knee pain yesterday",
        "I had severe back pain five years ago",
        "I had severe back pain in 2010 and now take aspirin",
        "She has a seizure disorder",
    ):
        assert not obvious_urgent_symptoms(text)


@pytest.mark.parametrize("emergency", ["I can't breathe", "I am having a heart attack"])
def test_emergency_text_is_not_delayed_by_earlier_pending_update(db, db_engine, emergency):
    for update_id, answer in [(5960, "ordinary message"), (5961, emergency)]:
        assert save_update(
            db,
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": int(datetime.now(UTC).timestamp()),
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": answer,
                },
            },
            42,
        )
    db.commit()

    class Provider:
        instance_id = "model:gemini:primary"

        def structured(self, *_args, **_kwargs):
            raise AssertionError("Explicit emergency must be screened locally")

    assert "112" in process_message(db_engine, Provider(), Settings(telegram_user_id=42), 5961)


def test_single_tracker_match_waits_for_earlier_diary_mutation(db, db_engine, monkeypatch):
    from types import SimpleNamespace

    from garmin_ai.telegram import DiaryDeferred

    monkeypatch.setattr(
        "garmin_ai.tracker_chat_selection.select_tracker_actions",
        lambda *_args, **_kwargs: [SimpleNamespace(id="synthetic-action")],
    )
    for update_id, answer in ((5962, "earlier diary answer"), (5963, "log focus")):
        assert save_update(
            db,
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": int(datetime.now(UTC).timestamp()),
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": answer,
                },
            },
            42,
        )
    db.commit()
    with pytest.raises(DiaryDeferred):
        process_message(db_engine, None, Settings(telegram_user_id=42), 5963)
    db.expire_all()
    assert db.get(AppState, "conversation:pending") is None


def test_queued_guided_answer_does_not_reach_model_safety_screen(db, db_engine):
    from garmin_ai.telegram import DiaryDeferred

    form = _form(db)
    pending = AppState(
        key="conversation:pending",
        value={
            "button": "tracker_form",
            "definition_version_id": str(form.action.definition_version_id),
            "channel_instance_id": "telegram:primary",
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    db.add(pending)
    begin_chat_form(pending, form, timezone="UTC", locale="en")
    for update_id, answer in ((5965, "earlier answer"), (5966, "private answer")):
        assert save_update(
            db,
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": int(datetime.now(UTC).timestamp()),
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": answer,
                },
            },
            42,
        )
    db.commit()

    class NoSafetyModel:
        instance_id = "model:gemini:primary"

        def structured(self, *_args, **_kwargs):
            raise AssertionError("Queued private answer must remain local")

    with pytest.raises(DiaryDeferred):
        process_message(db_engine, NoSafetyModel(), Settings(telegram_user_id=42), 5966)
    db.expire_all()
    assert db.get(AppState, "conversation:pending").value["chat_form"]["step"] == 0


def test_captioned_control_is_queued_as_control_work(db):
    from garmin_ai.models import Job

    assert save_update(
        db,
        {
            "update_id": 5964,
            "message": {
                "message_id": 5964,
                "date": int(datetime.now(UTC).timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "voice": {"file_id": "synthetic"},
                "caption": "/forget_conversation",
            },
        },
        42,
    )
    assert db.scalar(select(Job).where(Job.dedup_key == "telegram:5964")).kind == "telegram_control"


def test_guided_submission_conflict_cancels_pending_form(db, monkeypatch):
    from garmin_ai import tracker_chat_form
    from garmin_ai.events import Conflict

    form = _form(db)
    pending = AppState(
        key="conversation:pending",
        value={"definition_version_id": str(form.action.definition_version_id)},
    )
    db.add(pending)
    begin_chat_form(pending, form, timezone="UTC", locale="en")
    advance_chat_form(db, pending, "now", actor="test", now=NOW, source="telegram_text")
    answers = {"rating": "4", "count": "2", "note": "Fine"}
    for field in form.fields[:-1]:
        advance_chat_form(
            db, pending, answers[field.name], actor="test", now=NOW, source="telegram_text"
        )

    def changed(*_args, **_kwargs):
        raise Conflict("Revision changed during submit")

    monkeypatch.setattr(tracker_chat_form, "submit_form", changed)
    result = advance_chat_form(
        db, pending, answers[form.fields[-1].name], actor="test", now=NOW, source="telegram_text"
    )

    assert result["cancelled"]
    assert "Tracker changed" in result["response"]


@pytest.mark.parametrize(
    "note, expected",
    [("/skip", None), (" /skip ", None), ("=/foo", "/foo"), ("=/skip", "/skip")],
)
def test_optional_field_skip_command_reaches_guided_form(db, db_engine, note, expected):
    draft = TrackerSetupDraft(
        key="optional_chat",
        name="Optional chat",
        locale="ru",
        fields=[
            TrackerFieldDraft(key="rating", label="Оценка", kind="scale", minimum=1, maximum=5),
            TrackerFieldDraft(key="note", label="Заметка", kind="text", required=False),
        ],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    db.info["channel_destination_instance_id"] = "telegram:primary"
    handle_button(
        db,
        created["action"]["id"],
        Settings(telegram_user_id=42),
        "telegram:42",
        6000,
        datetime.now(UTC),
    )
    db.commit()

    order = db.get(AppState, "conversation:pending").value["chat_form"]["field_order"]
    answers = ["сейчас", *[{"rating": "4", "note": note}[name] for name in order]]
    for update_id, answer in enumerate(answers, 6101):
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

    assert "Запись сохранена" in response
    db.expire_all()
    event = db.scalar(select(Event).where(Event.kind == "user.optional_chat"))
    assert event is not None and event.payload["rating"] == 4
    if expected is None:
        assert "note" not in event.payload
    else:
        assert event.payload["note"] == expected


def test_choice_prompt_keeps_markdown_characters_visible(db):
    from garmin_ai.telegram_format import message_parts

    draft = TrackerSetupDraft(
        key="literal_choice",
        name="Literal choice",
        locale="en",
        fields=[
            TrackerFieldDraft(key="choice", label="*Choice*", kind="choice", options=["*unknown*"])
        ],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    form = form_for_action(db, created["action"]["id"], locale="en")
    rendered = "".join(part for part, _ in message_parts(_prompt(form, 1, locale="en")))
    assert "*Choice*" in rendered and "*unknown*" in rendered


def test_bounded_form_reasks_end_when_equal_to_start(db):
    draft = TrackerSetupDraft(
        key="bounded_chat",
        name="Bounded chat",
        locale="en",
        topology="bounded_interval",
        fields=[TrackerFieldDraft(key="score", label="Score", kind="scale", minimum=1, maximum=5)],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    form = form_for_action(db, created["action"]["id"], locale="en")
    pending = AppState(
        key="conversation:pending",
        value={"definition_version_id": str(form.action.definition_version_id)},
    )
    db.add(pending)
    begin_chat_form(pending, form, timezone="UTC", locale="en")
    advance_chat_form(
        db, pending, "2026-09-20 12:00", actor="test", now=NOW, source="telegram_text"
    )
    rejected = advance_chat_form(
        db, pending, "2026-09-20 12:00", actor="test", now=NOW, source="telegram_text"
    )

    assert "End time" in rejected["response"]
    assert pending.value["chat_form"]["step"] == 1


def test_guided_voice_caption_uses_local_form_instead_of_audio(db, db_engine, monkeypatch):
    from garmin_ai.runtime import _guided_caption_answers_form

    started = datetime.now(UTC) - timedelta(minutes=2)
    db.add(
        AppState(
            key="conversation:pending",
            value={
                "created_at": started.isoformat(),
                "channel_instance_id": "telegram:primary",
                "chat_form": {"step": 1},
            },
        )
    )
    db.commit()

    assert _guided_caption_answers_form(db_engine, {"caption": "4"}, "telegram:primary")
    assert not _guided_caption_answers_form(db_engine, {"caption": " "}, "telegram:primary")
    assert not _guided_caption_answers_form(db_engine, {"caption": "4"}, "telegram:secondary")
    monkeypatch.setattr("garmin_ai.conversation.is_analytic_reply", lambda *_args: True)
    assert not _guided_caption_answers_form(db_engine, {"caption": "4"}, "telegram:primary")
    monkeypatch.setattr("garmin_ai.conversation.is_analytic_reply", lambda *_args: False)
    pending = db.get(AppState, "conversation:pending")
    pending.value = {**pending.value, "created_at": datetime.now(UTC).isoformat()}
    db.commit()
    assert _guided_caption_answers_form(
        db_engine,
        {"caption": "4", "date": int((started + timedelta(minutes=1)).timestamp())},
        "telegram:primary",
    )
    created = datetime.now(UTC) - timedelta(hours=3)
    pending.value = {**pending.value, "created_at": created.isoformat()}
    db.commit()
    assert _guided_caption_answers_form(
        db_engine,
        {"caption": "4", "date": int((created + timedelta(hours=1)).timestamp())},
        "telegram:primary",
    )
    assert not _guided_caption_answers_form(
        db_engine,
        {"caption": "4", "date": int((created - timedelta(minutes=1)).timestamp())},
        "telegram:primary",
    )
    pending.value = {
        **pending.value,
        "created_at": datetime.now(UTC).isoformat(),
        "button": "tracker_select",
        "chat_form": None,
    }
    db.commit()
    assert _guided_caption_answers_form(db_engine, {"caption": "1"}, "telegram:primary")
    db.delete(pending)
    db.commit()


def test_tracker_voice_caption_is_recognized_before_transcription():
    from garmin_ai.tracker_chat_selection import tracker_selection_cue

    assert tracker_selection_cue("Record Focus")
    assert tracker_selection_cue("Record BP")
    assert not tracker_selection_cue("Record Water")
    assert not tracker_selection_cue("Record Headache")


def test_voice_caption_only_skips_audio_for_a_matching_nonanalytic_tracker(
    db, db_engine, monkeypatch
):
    from garmin_ai.runtime import _caption_selects_tracker

    _form(db)
    db.commit()
    message = {"caption": "Record Focus chat"}
    assert _caption_selects_tracker(db_engine, message, "telegram:primary", "en")
    assert not _caption_selects_tracker(
        db_engine, {"caption": "Record Missing"}, "telegram:primary", "en"
    )
    monkeypatch.setattr("garmin_ai.conversation.is_analytic_reply", lambda *_args: True)
    assert not _caption_selects_tracker(db_engine, message, "telegram:primary", "en")


@pytest.mark.anyio
async def test_voice_transcription_holds_model_consent_fence(db_engine, monkeypatch):
    from sqlalchemy import text

    from garmin_ai.runtime import cached_transcription

    async def synthetic_transcription(*_args):
        with db_engine.begin() as other:
            assert not other.scalar(text("SELECT pg_try_advisory_xact_lock(72104632)"))
        return "synthetic voice"

    monkeypatch.setattr("garmin_ai.runtime.transcribe_voice", synthetic_transcription)
    assert (
        await cached_transcription(db_engine, object(), object(), {"file_id": "synthetic"}, 5990)
        == "synthetic voice"
    )


@pytest.mark.anyio
async def test_urgent_voice_caption_stays_local_before_transcription(db_engine, monkeypatch):
    from garmin_ai.llm import ProviderConsentRequired
    from garmin_ai.runtime import cached_transcription

    async def unexpected_transcription(*_args):
        raise AssertionError("Urgent caption must not reach audio provider")

    monkeypatch.setattr("garmin_ai.runtime.transcribe_voice", unexpected_transcription)
    with pytest.raises(ProviderConsentRequired, match="Emergency caption"):
        await cached_transcription(
            db_engine,
            object(),
            object(),
            {"file_id": "synthetic"},
            5992,
            caption="I am having a heart attack",
        )


def test_urgent_voice_caption_returns_emergency_guidance_without_a_transcript(db, db_engine):
    update = {
        "update_id": 5993,
        "message": {
            "message_id": 5993,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "voice": {"file_id": "synthetic"},
            "caption": "I can't breathe",
        },
    }
    assert save_update(db, update, 42)
    db.commit()

    response = process_message(
        db_engine, None, Settings(telegram_user_id=42, locale="en"), 5993, transcript=""
    )
    assert "112" in response
    assert "unavailable" not in response.lower()


@pytest.mark.anyio
async def test_model_consent_revoke_waits_for_sensitive_voice_transcription(
    db, db_engine, monkeypatch
):
    import asyncio
    from threading import Event as ThreadEvent

    from garmin_ai.db import transaction
    from garmin_ai.runtime import cached_transcription
    from garmin_ai.share_policy import (
        TrackerShareConsent,
        grant_tracker_share,
        revoke_tracker_share,
    )

    draft = TrackerSetupDraft(
        key="sensitive_revoke_voice",
        name="Sensitive voice",
        locale="en",
        privacy="sensitive",
        fields=[TrackerFieldDraft(key="note", label="Note", kind="text")],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    version_id = created["action"]["definition_version_id"]
    definition_id = created["tracker"]["definition_id"]
    grant_tracker_share(
        db,
        TrackerShareConsent(
            definition_id=definition_id,
            destination_kind="model",
            destination_instance_id="model:gemini:primary",
            categories={"schema", "facts", "original_text"},
            granted_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
        authorized=True,
    )
    db.add(
        AppState(
            key="conversation:pending",
            value={
                "button": "tracker_form",
                "definition_version_id": version_id,
                "channel_instance_id": "telegram:primary",
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
    )
    db.commit()

    started = ThreadEvent()
    revoke_task = None

    def revoke():
        with transaction(db_engine) as session:
            started.set()
            revoke_tracker_share(
                session, definition_id, "model", "model:gemini:primary", authorized=True
            )

    async def synthetic_transcription(*_args):
        nonlocal revoke_task
        revoke_task = asyncio.create_task(asyncio.to_thread(revoke))
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0.1)
        assert not revoke_task.done()
        return "synthetic voice"

    monkeypatch.setattr("garmin_ai.runtime.transcribe_voice", synthetic_transcription)
    assert (
        await cached_transcription(db_engine, object(), object(), {"file_id": "synthetic"}, 5991)
        == "synthetic voice"
    )
    await asyncio.wait_for(revoke_task, 2)


@pytest.mark.anyio
@pytest.mark.parametrize("prompt_age_hours, voice_delta_hours", [(0, -3), (3, 1)])
async def test_sensitive_guided_voice_is_rejected_before_transcription(
    db, db_engine, monkeypatch, prompt_age_hours, voice_delta_hours
):
    from garmin_ai.llm import ProviderConsentRequired
    from garmin_ai.runtime import cached_transcription

    draft = TrackerSetupDraft(
        key="sensitive_voice",
        name="Sensitive voice",
        locale="en",
        privacy="sensitive",
        fields=[TrackerFieldDraft(key="note", label="Note", kind="text")],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    original_prompt_at = datetime.now(UTC) - timedelta(hours=prompt_age_hours)
    db.add(
        AppState(
            key="conversation:pending",
            value={
                "button": "tracker_form",
                "definition_version_id": created["action"]["definition_version_id"],
                "channel_instance_id": "telegram:primary",
                "created_at": original_prompt_at.isoformat(),
            },
        )
    )
    assert save_update(
        db,
        {
            "update_id": 5970,
            "message": {
                "message_id": 5970,
                "date": int((original_prompt_at + timedelta(hours=voice_delta_hours)).timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "voice": {"file_id": "synthetic"},
            },
        },
        42,
    )
    db.add(AppState(key="telegram:transcript:5970", value={"text": "synthetic cached voice"}))
    db.commit()

    class Provider:
        instance_id = "model:gemini:primary"

        def transcribe(self, *_args):
            raise AssertionError("Audio must not reach the provider")

    with pytest.raises(ProviderConsentRequired):
        await cached_transcription(db_engine, object(), Provider(), {"file_id": "synthetic"}, 5970)
    monkeypatch.setattr("garmin_ai.conversation.is_analytic_reply", lambda *_args: True)
    assert (
        await cached_transcription(
            db_engine,
            object(),
            Provider(),
            {"file_id": "synthetic"},
            5970,
            reply_to_message_id=123,
        )
        == "synthetic cached voice"
    )


@pytest.mark.anyio
async def test_voice_waits_for_earlier_pending_mutation_before_transcription(db, db_engine):
    from garmin_ai.runtime import DiaryDeferred, cached_transcription

    for update_id, text, voice in (
        (5975, "/privacy sensitive", None),
        (5976, None, {"file_id": "synthetic"}),
    ):
        assert save_update(
            db,
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": int(datetime.now(UTC).timestamp()),
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    **({"voice": voice} if voice else {"text": text}),
                },
            },
            42,
        )
    db.commit()

    class Provider:
        def transcribe(self, *_args):
            raise AssertionError("Audio must not reach the provider")

    with pytest.raises(DiaryDeferred, match="Earlier Telegram mutation"):
        await cached_transcription(db_engine, object(), Provider(), {"file_id": "synthetic"}, 5976)


@pytest.mark.anyio
async def test_voice_order_uses_provider_id_after_cross_instance_collision(db, db_engine):
    from garmin_ai.runtime import DiaryDeferred, cached_transcription
    from garmin_ai.telegram import _storage_update_id

    secondary = ChannelInstanceRef(channel="telegram", instance_id="secondary")
    for update_id in (5981, 5982):
        payload = {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": int(datetime.now(UTC).timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "other instance",
            },
        }
        assert save_update(db, payload, 42)
        payload["message"].pop("text")
        payload["message"].update(
            {"text": "/privacy sensitive"}
            if update_id == 5981
            else {"voice": {"file_id": "synthetic"}}
        )
        assert save_update(db, payload, 42, channel_instance=secondary)
    voice_storage_id = _storage_update_id(db, 5982, secondary)
    assert voice_storage_id < 0
    db.commit()

    class Provider:
        def transcribe(self, *_args):
            raise AssertionError("Audio must wait for the earlier mutation")

    with pytest.raises(DiaryDeferred, match="Earlier Telegram mutation"):
        await cached_transcription(
            db_engine,
            object(),
            Provider(),
            {"file_id": "synthetic"},
            voice_storage_id,
            destination_instance_id="telegram:secondary",
        )


@pytest.mark.parametrize("delayed", [False, True])
def test_sensitive_caption_advances_english_form_without_audio_model_access(db, db_engine, delayed):
    from garmin_ai.accounts import owner
    from garmin_ai.share_policy import TrackerShareConsent, grant_tracker_share

    owner(db).locale = "en"
    draft = TrackerSetupDraft(
        key="caption_voice",
        name="Caption voice",
        locale="en",
        privacy="sensitive",
        fields=[TrackerFieldDraft(key="note", label="Note", kind="text")],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    grant_tracker_share(
        db,
        TrackerShareConsent(
            definition_id=created["tracker"]["definition_id"],
            destination_kind="channel",
            destination_instance_id="telegram:primary",
            categories={"schema", "facts"},
            granted_at=datetime.now(UTC),
        ),
        authorized=True,
    )
    db.info["channel_destination_instance_id"] = "telegram:primary"
    opened = handle_button(
        db,
        created["action"]["id"],
        Settings(telegram_user_id=42, locale="en"),
        "telegram:test",
        5971,
        datetime.now(UTC),
    )
    assert "When did" in opened
    assert "This form does not assess" in opened
    assert "Форма не оценивает" not in opened
    sent_at = datetime.now(UTC)
    if delayed:
        created_at = sent_at - timedelta(hours=3)
        pending = db.get(AppState, "conversation:pending")
        pending.value = {**pending.value, "created_at": created_at.isoformat()}
        sent_at = created_at + timedelta(hours=1)
    db.commit()
    incoming = {
        "update_id": 5972,
        "message": {
            "message_id": 5972,
            "date": int(sent_at.timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "voice": {"file_id": "synthetic-audio-not-transcribed"},
            "caption": "now",
        },
    }
    assert save_update(db, incoming, 42)
    db.commit()
    if delayed:
        from garmin_ai.runtime import _guided_caption_answers_form

        assert _guided_caption_answers_form(db_engine, incoming["message"], "telegram:primary")

    response = process_message(
        db_engine,
        None,
        Settings(telegram_user_id=42, locale="en"),
        5972,
        transcript="ignored transcript",
    )

    assert "Note" in response
    assert "This form does not assess" in response
    assert "Форма не оценивает" not in response
    db.expire_all()
    assert db.get(AppState, "conversation:pending").value["chat_form"]["step"] == 1

    incoming["update_id"] = 5973
    incoming["message"]["message_id"] = 5973
    incoming["message"]["caption"] = "typed note"
    assert save_update(db, incoming, 42)
    db.commit()
    process_message(
        db_engine,
        None,
        Settings(telegram_user_id=42, locale="en"),
        5973,
        transcript="",
    )
    db.expire_all()
    event = db.scalar(select(Event))
    assert event is not None
    assert event.source == "telegram_text"
    assert event.payload["note"] == "typed note"


def test_blank_voice_caption_uses_transcript_for_public_form(db, db_engine):
    from garmin_ai.models import EventDefinitionVersion
    from garmin_ai.share_policy import TrackerShareConsent, grant_tracker_share

    form = _form(db)
    version = db.get(EventDefinitionVersion, form.action.definition_version_id)
    grant_tracker_share(
        db,
        TrackerShareConsent(
            definition_id=version.definition_id,
            destination_kind="channel",
            destination_instance_id="telegram:primary",
            categories={"schema", "facts"},
            granted_at=datetime.now(UTC),
        ),
        authorized=True,
    )
    db.info["channel_destination_instance_id"] = "telegram:primary"
    opened = handle_button(
        db, form.id, Settings(telegram_user_id=42), "telegram:42", 5974, datetime.now(UTC)
    )
    assert "Когда" in opened, opened.encode("unicode_escape").decode()
    db.commit()
    incoming = {
        "update_id": 5975,
        "message": {
            "message_id": 5975,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "voice": {"file_id": "synthetic"},
            "caption": " ",
        },
    }
    assert save_update(db, incoming, 42)
    db.commit()

    response = process_message(
        db_engine, None, Settings(telegram_user_id=42), 5975, transcript="сейчас"
    )

    assert "Оценка" in response or "Заметка" in response, response.encode("unicode_escape").decode()
    db.expire_all()
    assert db.get(AppState, "conversation:pending").value["chat_form"]["step"] == 1


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


def test_consent_permitted_selected_tracker_keeps_model_urgent_screen(db, db_engine):
    form = _form(db)
    db.add(
        AppState(
            key="conversation:pending",
            value={
                "button": "tracker_form",
                "definition_version_id": str(form.action.definition_version_id),
                "channel_instance_id": "telegram:primary",
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
    )
    incoming = {
        "update_id": 5980,
        "message": {
            "message_id": 5980,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": "мне очень плохо сегодня",
        },
    }
    assert save_update(db, incoming, 42)
    db.commit()

    class UrgentProvider:
        instance_id = "model:gemini:primary"

        def structured(self, _instruction, _prompt, schema):
            assert schema is SafetyScreen
            return SafetyScreen(urgent=True)

    response = process_message(db_engine, UrgentProvider(), Settings(telegram_user_id=42), 5980)

    assert "112" in response
    assert (
        db.scalar(select(func.count()).select_from(Event).where(Event.kind == "user.focus_chat"))
        == 0
    )


def test_telegram_generated_form_survives_messages_without_model(db, db_engine):
    form = _form(db)
    db.info["channel_destination_instance_id"] = "telegram:primary"
    opened = handle_button(
        db, form.id, Settings(telegram_user_id=42), "telegram:42", 6000, datetime.now(UTC)
    )
    assert "Когда" in opened
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

    order = db.get(AppState, "conversation:pending").value["chat_form"]["field_order"]
    values = {"rating": "4", "count": "2", "note": "Нормально"}
    answers = ["сейчас", *[values[name] for name in order]]
    for update_id, answer in enumerate(answers, 6001):
        send(update_id, answer, voice=update_id == 6000 + len(answers))

    assert "Оценка" in replies[0] or "Количество" in replies[0] or "Заметка" in replies[0]
    assert replies[-1].startswith("Запись сохранена."), replies
    assert "112" in replies[-1]
    db.expire_all()
    assert (
        db.scalar(select(func.count()).select_from(Event).where(Event.kind == "user.focus_chat"))
        == 1
    )
    assert process_message(db_engine, None, Settings(telegram_user_id=42), 6004) == replies[-1]
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
    assert "Форма не оценивает" in prompt
    pending = db.get(AppState, "conversation:pending")
    assert pending.value["action"] == "update"
    assert pending.value["event_ids"] == [str(original.id)]
    order = pending.value["chat_form"]["field_order"]
    answers = {"rating": "5", "count": "=", "note": "=="}
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

    assert response.startswith("Запись исправлена.")
    db.expire_all()
    db.refresh(original)
    assert original.revision == 2
    assert (original.payload["rating"], original.payload["count"], original.payload["note"]) == (
        5,
        2,
        "=",
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


def test_open_custom_entry_closes_from_history_and_undo_restores_it(db, db_engine):
    now = datetime.now(UTC)
    draft = TrackerSetupDraft(
        key="open_focus_chat",
        name="Open focus",
        locale="ru",
        topology="open_interval",
        fields=[
            TrackerFieldDraft(key="rating", label="Оценка", kind="scale", minimum=1, maximum=5)
        ],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    form = form_for_action(db, created["action"]["id"])
    original = submit_form(
        db,
        form.id,
        FormSubmission(
            action_id=form.id,
            schema_hash=form.schema_hash,
            submission_id=form.submission_id,
            start=now.replace(microsecond=0) - timedelta(hours=1),
            timezone="UTC",
            values={"rating": 4},
        ),
        actor="telegram:test",
    )
    original.evidence_refs = [{"field_id": "user.open_focus_chat.rating", "start": 0, "end": 1}]
    db.flush()
    point_start = now.replace(microsecond=0) - timedelta(hours=2)
    completed_point = submit_form(
        db,
        form.id,
        FormSubmission(
            action_id=form.id,
            schema_hash=form.schema_hash,
            submission_id=uuid4().hex,
            start=point_start,
            end=point_start,
            timezone="UTC",
            values={"rating": 2},
        ),
        actor="telegram:test",
    )
    assert completed_point.topology == "point" and completed_point.end is None
    db.info["channel_instance"] = ChannelInstanceRef(channel="telegram", instance_id="primary")
    db.info["channel_destination_instance_id"] = "telegram:primary"
    db.info["conversation_now"] = now
    db.info["locale"] = "ru"
    history_page(db, now)
    assert not any(
        row.value["action"] == "close" and row.value.get("event_id") == str(completed_point.id)
        for row in db.scalars(
            select(AppState).where(AppState.key.startswith("telegram:selection:"))
        )
    )
    selector = next(
        row.key.removeprefix("telegram:selection:")
        for row in db.scalars(
            select(AppState).where(AppState.key.startswith("telegram:selection:"))
        )
        if row.value["action"] == "close" and row.value["event_id"] == str(original.id)
    )
    assert "Когда завершилась" in selected_action(db, "h:" + selector, now, "telegram:test")
    pending = db.get(AppState, "conversation:pending")
    rejected = advance_close_chat_form(
        db, pending, "bad time", actor="test", now=NOW, source="telegram_text"
    )
    assert not rejected["written"]
    assert pending.value["created_at"] == now.isoformat()
    db.commit()

    def send(update_id, answer):
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

        class NoModel:
            def structured(self, *_args):
                raise AssertionError("Close form answers must remain local")

        return process_message(
            db_engine,
            NoModel() if update_id in {6200, 6201} else None,
            Settings(telegram_user_id=42),
            update_id,
        )

    assert "позже начала" in send(6200, "2020-01-01 00:00")
    db.refresh(original)
    assert original.end is None and original.revision == 1
    assert send(6201, "сейчас").startswith("Запись завершена.")
    db.refresh(original)
    assert original.end is not None and original.revision == 2
    assert original.payload["rating"] == 4
    assert original.evidence_refs == [
        {"field_id": "user.open_focus_chat.rating", "start": 0, "end": 1}
    ]
    assert "отменено" in send(6202, "/undo")
    db.refresh(original)
    assert original.end is None and original.revision == 3


def test_editing_point_in_open_tracker_keeps_point_topology(db):
    draft = TrackerSetupDraft(
        key="point_in_open_chat",
        name="Point in open",
        locale="en",
        topology="open_interval",
        fields=[
            TrackerFieldDraft(key="rating", label="Rating", kind="scale", minimum=1, maximum=5)
        ],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    form = form_for_action(db, created["action"]["id"])
    point = submit_form(
        db,
        form.id,
        FormSubmission(
            action_id=form.id,
            schema_hash=form.schema_hash,
            submission_id=form.submission_id,
            start=NOW,
            end=NOW,
            timezone="UTC",
            values={"rating": 3},
        ),
        actor="test",
    )
    assert point.topology == "point" and point.end is None
    point.evidence_refs = [{"source": "synthetic-test"}]
    db.flush()
    edit = form_for_action(db, f"edit:{point.id}:{point.revision}")
    pending = AppState(key="conversation:pending", value={})
    db.add(pending)
    begin_chat_form(pending, edit, timezone="UTC", locale="en")
    for answer in ("=", "=", "="):
        result = advance_chat_form(
            db, pending, answer, actor="test", now=NOW, source="telegram_text"
        )
    assert result["written"]
    db.refresh(point)
    assert point.topology == "point" and point.end is None
    assert point.evidence_refs == [{"source": "synthetic-test"}]


def test_edit_does_not_insert_absent_optional_constant(db):
    form = _form(db)
    optional_const = FormFieldSpec(
        name="origin",
        field_id="origin",
        label="Origin",
        input="text",
        required=False,
        has_const=True,
        const_value="chat",
    )
    edit = form.model_copy(
        update={
            "action": form.action.model_copy(update={"kind": "edit_entry"}),
            "submission_id": None,
            "fields": [*form.fields, optional_const],
            "initial_values": {"rating": 3},
        }
    )
    pending = AppState(key="conversation:pending", value={})
    begin_chat_form(pending, edit, timezone="UTC", locale="en")
    assert "origin" not in pending.value["chat_form"]["values"]


def test_guided_form_combines_reference_and_sibling_json_requirements(db):
    from garmin_ai.tracker_forms import _form_fields

    schema = {
        "$defs": {
            "base": {
                "type": "object",
                "required": ["a"],
                "properties": {"a": {"type": "string", "minLength": 2500}},
            }
        },
        "required": ["data"],
        "properties": {
            "data": {
                "$ref": "#/$defs/base",
                "required": ["b"],
                "properties": {"b": {"type": "string", "minLength": 2500}},
            }
        },
    }
    field = _form_fields(schema, {"data": {"id": "data", "labels": {"en": "Data"}}}, "en")[0]
    assert field.min_json_length > 4096
    form = _form(db).model_copy(update={"fields": [field]})
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


def test_guided_form_rejects_array_reference_with_sibling_constraints(db):
    from garmin_ai.tracker_forms import _form_fields

    schema = {
        "$defs": {"base": {"type": "array", "minItems": 300, "items": {"type": "string"}}},
        "required": ["data"],
        "properties": {
            "data": {"$ref": "#/$defs/base", "items": {"type": "string", "minLength": 20}}
        },
    }
    field = _form_fields(schema, {"data": {"id": "data", "labels": {"en": "Data"}}}, "en")[0]
    assert field.complex_json
    form = _form(db).model_copy(update={"fields": [field]})
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


def test_guided_form_counts_numeric_json_width_before_opening(db):
    from garmin_ai.tracker_forms import _form_fields, _minimum_json_length

    item = {"type": "integer", "minimum": 9007199254740993, "maximum": 9007199254740993}
    schema = {
        "required": ["data"],
        "properties": {"data": {"type": "array", "minItems": 1000, "items": item}},
    }
    assert _minimum_json_length(schema["properties"]["data"], {}) == 17001
    field = _form_fields(schema, {"data": {"id": "data", "labels": {"en": "Data"}}}, "en")[0]
    form = _form(db).model_copy(update={"fields": [field]})
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


def test_guided_form_rejects_contradictory_numeric_reference_bounds(db):
    from garmin_ai.tracker_forms import _form_fields

    schema = {
        "$defs": {"count": {"type": "integer", "minimum": 10, "maximum": 20}},
        "required": ["count"],
        "properties": {"count": {"$ref": "#/$defs/count", "minimum": 1, "maximum": 5}},
    }
    field = _form_fields(schema, {"count": {"id": "count", "labels": {"en": "Count"}}}, "en")[0]
    assert field.minimum == 10 and field.maximum == 5
    form = _form(db).model_copy(update={"fields": [field]})
    with pytest.raises(FormAnswerError, match="no valid value"):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


def test_optional_contradictory_numeric_field_can_be_skipped(db):
    field = FormFieldSpec(
        name="count",
        field_id="count",
        label="Count",
        input="integer",
        required=False,
        minimum=10,
        maximum=5,
    )
    form = _form(db).model_copy(update={"fields": [field]})
    assert begin_chat_form(
        AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en"
    )


def test_guided_form_rejects_contradictory_text_reference_bounds(db):
    from garmin_ai.tracker_forms import _form_fields

    schema = {
        "$defs": {"note": {"type": "string", "minLength": 10}},
        "required": ["note"],
        "properties": {"note": {"$ref": "#/$defs/note", "maxLength": 5}},
    }
    field = _form_fields(schema, {"note": {"id": "note", "labels": {"en": "Note"}}}, "en")[0]
    form = _form(db).model_copy(update={"fields": [field]})
    with pytest.raises(FormAnswerError, match="Telegram"):
        begin_chat_form(AppState(key="unused:pending", value={}), form, timezone="UTC", locale="en")


def test_flexible_end_prompt_explains_point_entry(db):
    form = _form(db).model_copy(update={"topology": "flexible"})
    prompt = _prompt(form, 1, locale="en")
    assert "point entry" in prompt
    assert "still open" not in prompt


@pytest.mark.anyio
async def test_ambiguous_tracker_voice_stays_local_before_selection(db, db_engine):
    from garmin_ai.llm import ProviderConsentRequired
    from garmin_ai.runtime import cached_transcription

    db.add(
        AppState(
            key="conversation:pending",
            value={
                "button": "tracker_select",
                "options": [{"definition_version_id": "synthetic"}],
                "channel_instance_id": "telegram:primary",
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
    )
    db.add(AppState(key="telegram:transcript:5974", value={"text": "cached"}))
    db.commit()

    class Provider:
        instance_id = "model:gemini:primary"

        def transcribe(self, *_args):
            raise AssertionError("Ambiguous tracker audio must stay local")

    with pytest.raises(ProviderConsentRequired):
        await cached_transcription(db_engine, object(), Provider(), {"file_id": "synthetic"}, 5974)


@pytest.mark.anyio
@pytest.mark.parametrize("button", ["tracker_select", "tracker_form"])
async def test_expired_tracker_pending_does_not_block_voice(db, db_engine, button):
    from garmin_ai.runtime import cached_transcription

    db.add(
        AppState(
            key="conversation:pending",
            value={
                "button": button,
                "definition_version_id": "synthetic",
                "channel_instance_id": "telegram:primary",
                "created_at": (datetime.now(UTC) - timedelta(hours=3)).isoformat(),
            },
        )
    )
    db.add(AppState(key="telegram:transcript:5976", value={"text": "cached"}))
    db.commit()

    class Provider:
        instance_id = "model:gemini:primary"

        def transcribe(self, *_args):
            raise AssertionError("Cached transcription should be reused")

    assert (
        await cached_transcription(db_engine, object(), Provider(), {"file_id": "synthetic"}, 5976)
        == "cached"
    )


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


def test_tracker_selection_escapes_markdown_labels(db, db_engine):
    for key, url in (("focus_a", "https://a"), ("focus_b", "https://b")):
        draft = TrackerSetupDraft(
            key=key,
            name=f"[Focus]({url})",
            locale="en",
            fields=[TrackerFieldDraft(key="note", label="Note", kind="text")],
        )
        preview = preview_tracker(db, draft)
        confirm_tracker(
            db,
            TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
            actor="test",
        )
    incoming = {
        "update_id": 5959,
        "message": {
            "message_id": 5959,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": "Record Focus",
        },
    }
    assert save_update(db, incoming, 42)
    db.commit()

    response = process_message(db_engine, None, Settings(telegram_user_id=42, locale="en"), 5959)

    assert r"\[Focus\]\(https://a\)" in response
    assert r"\[Focus\]\(https://b\)" in response


def test_ambiguous_tracker_text_requires_numbered_choice(db, db_engine, monkeypatch):
    _form(db)
    draft = TrackerSetupDraft(
        key="focus_other",
        name="Focus chat",
        locale="ru",
        fields=[TrackerFieldDraft(key="score", label="Оценка", kind="scale", minimum=1, maximum=5)],
    )
    preview = preview_tracker(db, draft)
    second = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )

    def fail_diary_parse(*_args, **_kwargs):
        raise AssertionError("Tracker selection must not run the diary parser")

    monkeypatch.setattr("garmin_ai.diary_forms.interpret_form", fail_diary_parse)

    def send(update_id, text, *, message_time=None, locale="ru"):
        incoming = {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": int((message_time or datetime.now(UTC)).timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": text,
            },
        }
        assert save_update(db, incoming, 42)
        db.commit()
        return process_message(
            db_engine, None, Settings(telegram_user_id=42, locale=locale), update_id
        )

    response = send(
        5960, "Записать Focus", message_time=datetime.now(UTC) - timedelta(hours=3), locale="de"
    )
    assert response.startswith("Choose a tracker:")
    assert "1." in response and "2." in response
    assert "focus_chat" in response and "focus_other" in response
    db.expire_all()
    pending = db.get(AppState, "conversation:pending")
    assert pending.value["button"] == "tracker_select"
    assert datetime.fromisoformat(pending.value["created_at"]) > datetime.now(UTC) - timedelta(
        minutes=1
    )

    captioned = {
        "update_id": 5964,
        "message": {
            "message_id": 5964,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "voice": {"file_id": "synthetic"},
            "caption": "not a number",
        },
    }
    assert save_update(db, captioned, 42)
    db.commit()
    assert (
        process_message(db_engine, None, Settings(telegram_user_id=42), 5964, transcript="")
        == response
    )

    assert "112" in send(5961, "I can't breathe")
    db.expire_all()
    assert db.get(AppState, "conversation:pending").value["button"] == "tracker_select"
    assert not db.get(AppState, "telegram:reply:5961").value["share_requirements"]

    acute = {
        "update_id": 5968,
        "message": {
            "message_id": 5968,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": "my face is drooping and one arm is weak",
        },
    }
    assert save_update(db, acute, 42)
    db.commit()

    class UrgentProvider:
        def structured(self, *_args):
            return SafetyScreen(urgent=True)

    assert "112" in process_message(
        db_engine, UrgentProvider(), Settings(telegram_user_id=42), 5968
    )
    db.expire_all()
    assert not db.get(AppState, "telegram:reply:5968").value["share_requirements"]

    with monkeypatch.context() as patch:
        patch.setattr("garmin_ai.conversation.is_analytic_reply", lambda *_args: True)
        assert "недоступна" in send(5963, "1")
    db.expire_all()
    assert db.get(AppState, "conversation:pending").value["button"] == "tracker_select"

    pending = db.get(AppState, "conversation:pending")
    pending.value = {
        **pending.value,
        "created_at": (datetime.now(UTC) - timedelta(hours=1, minutes=59)).isoformat(),
    }
    db.commit()
    assert "Choose a tracker" in send(5965, "99")
    db.expire_all()
    assert datetime.fromisoformat(db.get(AppState, "conversation:pending").value["created_at"]) > (
        datetime.now(UTC) - timedelta(minutes=1)
    )

    response = send(5962, "2")
    assert "Когда" in response
    db.expire_all()
    pending = db.get(AppState, "conversation:pending")
    assert pending.value["definition_version_id"] == second["action"]["definition_version_id"]
    assert pending.value["button"] == "tracker_form"
    assert pending.value["chat_form"]["step"] == 0

    db.delete(pending)
    db.commit()
    captioned = {
        "update_id": 5966,
        "message": {
            "message_id": 5966,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "voice": {"file_id": "synthetic"},
            "caption": "Записать Focus",
        },
    }
    assert save_update(db, captioned, 42)
    db.commit()
    response = process_message(db_engine, None, Settings(telegram_user_id=42), 5966, transcript="")
    assert response.startswith("Выберите трекер:")
    db.expire_all()
    assert db.get(AppState, "conversation:pending").value["button"] == "tracker_select"

    db.delete(db.get(AppState, "conversation:pending"))
    db.commit()
    captioned["update_id"] = 5967
    captioned["message"]["message_id"] = 5967
    assert save_update(db, captioned, 42)
    db.commit()
    response = process_message(
        db_engine, None, Settings(telegram_user_id=42), 5967, transcript="score is five"
    )
    assert response.startswith("Выберите трекер:")


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
        db, "Записать Private Focus", locale="ru", destination="telegram:primary"
    )


def test_tracker_selection_requires_entry_cue_and_leaves_questions_to_analysis(db):
    _form(db)
    assert not select_tracker_actions(
        db, "How did Focus chat affect sleep?", locale="en", destination="telegram:primary"
    )
    assert not select_tracker_actions(db, "Focus chat", locale="en", destination="telegram:primary")
    assert select_tracker_actions(
        db, "Record Focus chat", locale="en", destination="telegram:primary"
    )
    assert select_tracker_actions(
        db, "I recorded Focus chat", locale="en", destination="telegram:primary"
    )

    short = TrackerSetupDraft(
        key="bp",
        name="BP",
        locale="en",
        fields=[TrackerFieldDraft(key="score", label="Score", kind="scale", minimum=1, maximum=5)],
    )
    short_preview = preview_tracker(db, short)
    confirm_tracker(
        db,
        TrackerConfirmation(draft=short, confirmation_token=short_preview["confirmation_token"]),
        actor="test",
    )
    assert select_tracker_actions(db, "Record BP", locale="en", destination="telegram:primary")
    assert select_tracker_actions(
        db, "Record tracker BP", locale="en", destination="telegram:primary"
    )
    assert select_tracker_actions(db, "Record BP 120", locale="en", destination="telegram:primary")
    draft = TrackerSetupDraft(
        key="coffee_tracker",
        name="Coffee",
        locale="en",
        fields=[TrackerFieldDraft(key="score", label="Score", kind="scale", minimum=1, maximum=5)],
    )
    preview = preview_tracker(db, draft)
    confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    assert not select_tracker_actions(db, "Log Coffee", locale="en", destination="telegram:primary")
    assert select_tracker_actions(
        db, "Log tracker Coffee", locale="en", destination="telegram:primary"
    )
    for key, label in (
        ("migraine_custom", "Migraine"),
        ("note_custom", "Note"),
        ("water_custom", "Water"),
        ("headache_custom", "Headache"),
        ("pain_custom", "Pain"),
        ("energy_custom", "Energy"),
    ):
        draft = TrackerSetupDraft(
            key=key,
            name=label,
            locale="en",
            fields=[
                TrackerFieldDraft(key="score", label="Score", kind="scale", minimum=1, maximum=5)
            ],
        )
        preview = preview_tracker(db, draft)
        confirm_tracker(
            db,
            TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
            actor="test",
        )
        assert not select_tracker_actions(
            db, f"Record {label}", locale="en", destination="telegram:primary"
        )
        assert select_tracker_actions(
            db, f"Record tracker {label}", locale="en", destination="telegram:primary"
        )


def test_tracker_selection_rejects_conflicting_multiword_labels(db):
    draft = TrackerSetupDraft(
        key="blood_pressure",
        name="Blood pressure",
        locale="en",
        fields=[TrackerFieldDraft(key="score", label="Score", kind="scale", minimum=1, maximum=5)],
    )
    preview = preview_tracker(db, draft)
    confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    assert not select_tracker_actions(
        db, "Record blood sugar", locale="en", destination="telegram:primary"
    )
    assert select_tracker_actions(
        db, "Record blood pressure", locale="en", destination="telegram:primary"
    )


def test_tracker_selection_does_not_match_only_the_inflected_cue(db):
    draft = TrackerSetupDraft(
        key="recorded_symptoms",
        name="Recorded symptoms",
        locale="en",
        fields=[TrackerFieldDraft(key="score", label="Score", kind="scale", minimum=1, maximum=5)],
    )
    preview = preview_tracker(db, draft)
    confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    assert not select_tracker_actions(
        db, "I recorded a walk", locale="en", destination="telegram:primary"
    )
    assert select_tracker_actions(
        db, "I recorded Recorded symptoms", locale="en", destination="telegram:primary"
    )


def test_selected_tracker_fallback_starts_a_fresh_form_lifetime(db, db_engine):
    from garmin_ai.agent import pending_clarification

    form = _form(db)
    created = datetime.now(UTC) - timedelta(hours=2) + timedelta(seconds=30)
    db.add(
        AppState(
            key="conversation:pending",
            value={
                "button": "tracker_form",
                "definition_version_id": str(form.action.definition_version_id),
                "channel_instance_id": "telegram:primary",
                "created_at": created.isoformat(),
            },
        )
    )
    incoming = {
        "update_id": 6010,
        "message": {
            "message_id": 6010,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": "I want to log a value",
        },
    }
    assert save_update(db, incoming, 42)
    db.commit()

    process_message(db_engine, None, Settings(telegram_user_id=42), 6010)
    db.expire_all()
    pending = db.get(AppState, "conversation:pending")
    assert pending.value.get("chat_form")
    db.info["channel_destination_instance_id"] = "telegram:primary"
    assert pending_clarification(db, datetime.now(UTC) + timedelta(minutes=1)) is not None


def test_json_exponent_is_stored_as_an_integer_and_sized_after_normalization():
    from garmin_ai.tracker_forms import _minimum_json_length

    field = FormFieldSpec(name="data", field_id="data", label="Data", input="json", required=True)
    assert _value("[1e3]", field, "en") == [1000]
    schema = {
        "type": "array",
        "minItems": 1000,
        "items": {"type": "integer", "minimum": 1000, "maximum": 1000},
    }
    assert _minimum_json_length(schema, {}) == 4001
    assert _minimum_json_length(schema, {}, storage=True) == 5001


def test_normalized_json_numbers_reject_an_oversized_aggregate(db):
    from garmin_ai.tracker_forms import _form_fields

    names = [f"field_{index}" for index in range(14)]
    schema = {
        "type": "object",
        "properties": {
            name: {
                "type": "array",
                "minItems": 1000,
                "items": {"type": "integer", "minimum": 1000, "maximum": 1000},
            }
            for name in names
        },
        "required": names,
    }
    metadata = {name: {"id": name, "labels": {"en": name}} for name in names}
    fields = _form_fields(schema, metadata, "en")
    assert all(field.min_json_length < 4096 for field in fields)
    with pytest.raises(FormAnswerError, match="64 KiB"):
        begin_chat_form(
            AppState(key="unused:pending", value={}),
            _form(db).model_copy(update={"fields": fields}),
            timezone="UTC",
            locale="en",
        )


def test_adjacent_exclusive_float_bounds_are_unreachable_in_json():
    from garmin_ai.tracker_forms import _minimum_json_length

    schema = {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "number",
            "exclusiveMinimum": 0.1,
            "exclusiveMaximum": 0.10000000000000002,
        },
    }
    assert _minimum_json_length(schema, {}) > 4096
