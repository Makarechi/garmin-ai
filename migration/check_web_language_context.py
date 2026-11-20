"""Check the saved language context with the mock model on ordinary chat routes."""

from unittest.mock import patch

import check_web_chat_model as chat
from sqlalchemy import text

from garmin_ai.agent import Interpretation

ENGLISH_REQUEST = "Какая нужна запись? Ответьте по-английски, пример."
RUSSIAN_REQUEST = "Please clarify the fixture in Russian."
CONTINUE = "Immediately."
ENGLISH_REPLY = "When did the synthetic event happen?"
RUSSIAN_REPLY = "Когда была синтетическая запись?"
FOLLOWUP = "What value should the synthetic record contain?"


def output(prompt):
    sentence = prompt["text"]
    if sentence == ENGLISH_REQUEST:
        return Interpretation(
            intent="clarify",
            confidence=0.4,
            clarification=ENGLISH_REPLY,
            response_language="EN-us",
        )
    if sentence == RUSSIAN_REQUEST:
        return Interpretation(
            intent="clarify",
            confidence=0.4,
            clarification=RUSSIAN_REPLY,
            response_language="RU-ru",
        )
    assert sentence == CONTINUE
    assert prompt["context"]["pending_clarification"]["question"] == ENGLISH_REPLY
    return Interpretation(
        intent="clarify",
        confidence=0.4,
        clarification=FOLLOWUP,
        response_language="en-US",
    )


def exercise(engine, python, native, python_calls, native_calls, _active, _release):
    chat.set_model_categories(engine, ["health", "diary"])
    for client, calls in ((python, python_calls), (native, native_calls)):
        admin = chat.HEADERS
        help_text = chat.submit(client, "/help")["reply"]["text"]
        chat.submit(client, "/cancel")
        assert chat.submit(client, ENGLISH_REQUEST)["reply"]["text"] == ENGLISH_REPLY
        continued = chat.submit(client, CONTINUE)["reply"]["text"]
        assert continued == FOLLOWUP, (
            continued,
            [(value.get("text"), sorted(value)) for value in calls],
        )
        assert [value["text"] for value in calls] == [ENGLISH_REQUEST, CONTINUE]
        with engine.connect() as connection:
            original = connection.scalar(
                text("""
                SELECT normalized_text FROM inbound_messages
                 WHERE normalized_text=:sentence AND channel='web'
                 ORDER BY received_at DESC LIMIT 1
            """),
                {"sentence": ENGLISH_REQUEST},
            )
            assert original == ENGLISH_REQUEST
        chat.submit(client, "/cancel")
        assert chat.submit(client, RUSSIAN_REQUEST)["reply"]["text"] == RUSSIAN_REPLY
        chat.submit(client, "/cancel")
        with engine.begin() as connection:
            connection.execute(text("UPDATE people SET locale='pl-PL' WHERE singleton"))
        actual = client.get("/tracker-profile", headers=admin)
        assert actual.status_code == 200, actual.text
        assert actual.json()["locale"].lower() == "pl-pl", actual.json()
        assert chat.submit(client, "/help")["reply"]["text"] == help_text
        with engine.begin() as connection:
            connection.execute(text("UPDATE people SET locale='en' WHERE singleton"))
    print(
        "Both runtimes preserved Russian source text, explicit English/Russian replies and continuation context."
    )
    print(
        "Both runtimes retained the unsupported saved locale and displayed the same English command text."
    )


if __name__ == "__main__":
    with patch.object(chat, "output", output), patch.object(chat, "exercise", exercise):
        chat.main()
