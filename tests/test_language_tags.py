import pytest
from pydantic import ValidationError

from garmin_ai.agent import AgentStep, Interpretation
from garmin_ai.config import Settings
from garmin_ai.definitions import FieldSpec
from garmin_ai.i18n import translate
from garmin_ai.tracker_forms import TrackerSetupDraft


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("zh-Hans", "zh-Hans"),
        ("SR-latn-rs", "sr-Latn-RS"),
        ("pt-br", "pt-BR"),
        ("es-419", "es-419"),
        ("en-u-ca-gregory", "en-u-ca-gregory"),
        ("x-PRIVATE", "x-private"),
        ("en-x-x", "en-x-x"),
        ("sl-rozaj-biske", "sl-rozaj-biske"),
        ("I-KLINGON", "i-klingon"),
    ],
)
def test_language_tags_are_consistent_across_settings_responses_and_tracker_labels(tag, expected):
    assert Settings(_env_file=None, locale=tag).locale == expected
    assert AgentStep(response_language=tag).response_language == expected
    assert (
        Interpretation(intent="question", confidence=1, response_language=tag).response_language
        == expected
    )
    draft = TrackerSetupDraft(
        key="focus",
        name="Focus",
        locale=tag,
        fields=[{"key": "value", "label": "Value", "kind": "boolean"}],
    )
    assert draft.locale == expected
    assert FieldSpec(id="value", labels={tag: "Value"}, semantic="boolean").labels == {
        expected: "Value"
    }


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "en_US",
        "en--US",
        "a",
        "en-a",
        "en-a-foo-a-bar",
        "en-x",
        "sl-rozaj-rozaj",
        "en-abcdefghi",
        "x",
        "a" * 256,
    ],
)
def test_invalid_tags_are_rejected_by_settings_and_model_response(tag):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, locale=tag)
    with pytest.raises(ValidationError):
        AgentStep(response_language=tag)


def test_canonical_label_collisions_are_rejected():
    with pytest.raises(ValidationError):
        FieldSpec(id="value", labels={"en-us": "One", "EN-US": "Two"}, semantic="boolean")


def test_interface_fallback_is_independent_of_valid_requested_language():
    assert translate("agent.record_fallback", "zh-Hans") == translate("agent.record_fallback", "en")
    assert translate("agent.record_fallback", "RU-ru") == translate("agent.record_fallback", "ru")


@pytest.mark.parametrize("module_name", ["garmin_ai.tracker_forms", "garmin_ai.natural_language"])
def test_label_selection_tries_script_before_generic_language(module_name):
    from importlib import import_module

    select_label = import_module(module_name)._label
    labels = {"zh-Hant": "繁體", "zh": "中文", "en": "Chinese"}
    assert select_label(labels, "zh-Hant-TW") == "繁體"
    assert select_label(labels, "zh-Hant-TW-u-ca-gregory") == "繁體"
    assert select_label(labels, "zh-Hans-CN") == "中文"
    assert select_label(labels, "fr") == "Chinese"
