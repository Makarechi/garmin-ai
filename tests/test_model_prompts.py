"""Model instructions stay packaged and independent of the owner's language."""

import json
import re
from datetime import UTC, datetime

import pytest

from garmin_ai.agent import SafetyScreen, interpret, screen_reply_safety
from garmin_ai.config import Settings
from garmin_ai.prompts import load_prompt

PROMPTS = (
    "analysis_answer",
    "diary_extract",
    "extension_probe",
    "form_safety",
    "mcp_server",
    "safety_fragment",
    "safety_reply",
    "tracker_extract",
    "voice_transcribe",
)


@pytest.mark.parametrize("name", PROMPTS)
def test_model_instructions_are_bundled_and_not_russian_only(name):
    instruction = load_prompt(name)
    assert instruction and not re.search(r"[\u0400-\u04ff]", instruction)


def test_diary_and_analysis_prompts_preserve_language_and_evidence_contracts():
    diary = load_prompt("diary_extract")
    analysis = load_prompt("analysis_answer")
    assert all(
        term in diary
        for term in (
            "current owner message",
            "response_language",
            "changed_fields",
            "target_question_id",
        )
    )
    assert all(
        term in analysis
        for term in (
            "current owner message",
            "response_language",
            "numeric_claims",
            "timestamp_claims",
        )
    )


@pytest.mark.parametrize(
    ("text", "message_language", "interface_language"),
    [
        ("I feel well today.", "en", "ru"),
        ("Сегодня чувствую себя нормально.", "ru", "en"),
    ],
)
def test_safety_screen_uses_message_language_when_interface_differs(
    text, message_language, interface_language
):
    class Provider:
        def structured(self, instruction, prompt, schema):
            assert instruction == load_prompt("safety_reply")
            assert prompt == text
            assert schema is SafetyScreen
            return schema(urgent=False, response_language=message_language)

    result = screen_reply_safety(Provider(), text, locale=interface_language)
    assert result.response_language == message_language


@pytest.mark.parametrize(
    ("text", "message_language", "interface_language", "clarification"),
    [
        ("I felt tired after lunch.", "en", "ru", "At what time?"),
        ("После обеда чувствовал усталость.", "ru", "en", "Во сколько это было?"),
    ],
)
def test_diary_prompt_keeps_message_language_independent_of_interface(
    db, text, message_language, interface_language, clarification
):
    class Provider:
        def structured(self, instruction, prompt, schema):
            assert instruction == load_prompt("diary_extract")
            assert json.loads(prompt)["text"] == text
            return schema(
                intent="clarify",
                confidence=0.9,
                response_language=message_language,
                clarification=clarification,
            )

    result = interpret(
        db,
        Provider(),
        text,
        Settings(locale=interface_language),
        datetime(2026, 10, 21, 12, tzinfo=UTC),
    )
    assert result.response_language == message_language
    assert result.clarification == clarification


def test_prompt_loader_rejects_unlisted_files():
    with pytest.raises(ValueError, match="Unknown model prompt"):
        load_prompt("../../LICENSE")
