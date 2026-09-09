from garmin_ai.telegram_format import message_parts


def test_russian_report():
    parts = message_parts(
        "1\\. **Вечер (20:00–01:00):**\n   - **Пик:** 51.0\n\nОграничения: n < 5."
    )
    assert len(parts) == 1
    text, entities = parts[0]
    assert text == "1. Вечер (20:00–01:00):\n- Пик: 51.0\n\nОграничения: n < 5."
    assert [text[e.offset : e.offset + e.length] for e in entities] == [
        "Вечер (20:00–01:00):",
        "Пик:",
    ]
    assert all(e.type == "bold" for e in entities)


def test_unicode_and_formatting_across_chunks():
    source = "☕😀 **" + "я😀" * 2400 + "**"
    parts = message_parts(source)
    assert "".join(text for text, _ in parts) == "☕😀 " + "я😀" * 2400
    assert len(parts) == 3
    for text, entities in parts:
        encoded = text.encode("utf-16-le")
        assert len(encoded) // 2 <= 3500
        assert len(entities) == 1
        entity = entities[0]
        selected = encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode(
            "utf-16-le"
        )
        assert selected
        assert "☕" not in selected
    assert parts[0][1][0].offset == 4


def test_links_code_html_and_unmatched_markers():
    text, entities = message_parts("[ссылка](https://example.com) `a_b < c` <b>текст</b> **")[0]
    assert text == "ссылка a_b < c <b>текст</b> **"
    assert {e.type for e in entities} == {"text_link", "code"}
    assert next(e for e in entities if e.type == "text_link").url == "https://example.com"


def test_plain_and_empty():
    assert message_parts("") == []
    assert message_parts("Обычный текст 42.7 > 40") == [("Обычный текст 42.7 > 40", [])]


def test_code_does_not_overlap_bold():
    text, entities = message_parts("**до `code` после**")[0]
    assert text == "до code после"
    code = next(e for e in entities if e.type == "code")
    for entity in entities:
        if entity.type != "code":
            assert (
                entity.offset + entity.length <= code.offset
                or entity.offset >= code.offset + code.length
            )
