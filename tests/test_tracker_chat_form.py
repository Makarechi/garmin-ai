from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from garmin_ai.agent import SafetyScreen
from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.config import Settings
from garmin_ai.models import AppState, Event, TelegramUpdate
from garmin_ai.telegram import handle_button, process_message, save_update
from garmin_ai.tracker_chat_form import (
    FormAnswerError,
    _prompt,
    _time,
    _value,
    advance_chat_form,
    begin_chat_form,
)
from garmin_ai.tracker_forms import (
    FormFieldSpec,
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    form_for_action,
    preview_tracker,
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


def test_affirmative_not_only_emergency_wording_is_not_negated():
    from garmin_ai.diary_forms import obvious_urgent_symptoms

    assert obvious_urgent_symptoms("I have not only sudden severe pain")
    assert obvious_urgent_symptoms("I am not without sudden severe pain")
    assert not obvious_urgent_symptoms("I have not had sudden severe pain")


def test_json_answer_rejects_unstorable_strings_and_keys():
    field = FormFieldSpec(name="data", field_id="data", label="Data", input="json", required=True)
    for answer in (r'"\u0000"', r'{"\u0000": 1}', r'{"note": "\ud800"}'):
        with pytest.raises(FormAnswerError, match="unsupported characters"):
            _value(answer, field, "en")


def test_number_answers_reject_huge_exponents_and_lossy_json_decimals():
    number = FormFieldSpec(
        name="score", field_id="score", label="Score", input="number", required=True
    )
    with pytest.raises(FormAnswerError, match="too large"):
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


def test_json_and_text_fields_reject_values_that_cannot_be_persisted():
    json_field = FormFieldSpec(
        name="data", field_id="data", label="Data", input="json", required=True
    )
    assert _value("null", json_field, "en") is None
    with pytest.raises(ValueError):
        _value("NaN", json_field, "en")
    with pytest.raises(ValueError, match="Duplicate"):
        _value('{"dose": 5, "dose": 50}', json_field, "en")
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
    assert _value("==/empty", empty_allowed, "en") == "=/empty"


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
            "Entry values exceed 64 KiB"
            if failure == "aggregate_size"
            else "Entry object is too large"
        )
    )

    def reject(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(tracker_chat_form, "submit_form", reject)
    try:
        result = None
        for answer in ("now", "4", "2", "Fine"):
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


def test_integer_schema_bounds_keep_exact_precision():
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

    for text in ("I can't breathe", "I can’t breathe", "signs of a stroke", "потерял сознание"):
        assert obvious_urgent_symptoms(text)
    for text in (
        "No sudden severe pain",
        "нет внезапной сильной боли",
        "Внезапной сильной боли нет",
        "Внезапной сильной боли не было",
        "no signs of a stroke",
    ):
        assert not obvious_urgent_symptoms(text)


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


@pytest.mark.parametrize("note, expected", [("/skip", None), ("/foo", "/foo"), ("=/skip", "/skip")])
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


@pytest.mark.anyio
async def test_sensitive_guided_voice_is_rejected_before_transcription(db, db_engine, monkeypatch):
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
    original_prompt_at = datetime.now(UTC)
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
                "date": int((original_prompt_at - timedelta(hours=3)).timestamp()),
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
async def test_secondary_voice_order_uses_provider_id_after_storage_collision(db, db_engine):
    from garmin_ai.runtime import DiaryDeferred, cached_transcription

    primary = ChannelInstanceRef(channel="telegram", instance_id="primary")
    secondary = ChannelInstanceRef(channel="telegram", instance_id="secondary")
    for channel, update_id, payload in (
        (primary, 5980, {"text": "/status"}),
        (primary, 5981, {"text": "/status"}),
        (secondary, 5980, {"text": "/privacy sensitive"}),
        (secondary, 5981, {"voice": {"file_id": "synthetic"}}),
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
                    **payload,
                },
            },
            42,
            channel_instance=channel,
        )
    db.commit()
    stored = [
        row
        for row in db.scalars(select(TelegramUpdate))
        if row.payload["_channel_instance"]["instance_id"] == "secondary"
    ]
    voice_id = next(row.id for row in stored if row.payload["update_id"] == 5981)
    assert all(row.id < 0 for row in stored)

    class Provider:
        def transcribe(self, *_args):
            raise AssertionError("Earlier mutation must complete before provider access")

    with pytest.raises(DiaryDeferred, match="Earlier Telegram mutation"):
        await cached_transcription(
            db_engine,
            object(),
            Provider(),
            {"file_id": "synthetic"},
            voice_id,
            destination_instance_id="telegram:secondary",
        )


def test_sensitive_caption_advances_english_form_without_audio_model_access(db, db_engine):
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
    db.commit()
    incoming = {
        "update_id": 5972,
        "message": {
            "message_id": 5972,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "voice": {"file_id": "synthetic-audio-not-transcribed"},
            "caption": "now",
        },
    }
    assert save_update(db, incoming, 42)
    db.commit()

    response = process_message(
        db_engine, None, Settings(telegram_user_id=42, locale="en"), 5972, transcript="now"
    )

    assert "Note" in response
    assert "This form does not assess" in response
    assert "Форма не оценивает" not in response
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
