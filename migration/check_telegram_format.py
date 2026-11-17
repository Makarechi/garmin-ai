"""Record Telegram text and entities from the pinned sender, using synthetic Markdown."""

import argparse
import json
from pathlib import Path

from garmin_ai.telegram_format import message_parts


def fixtures():
    return {
        "empty": "",
        "plain": "Обычный текст 42.7 > 40",
        "report": "1\\. **Вечер (20:00–01:00):**\n   - **Пик:** 51.0\n\nОграничения: n < 5.",
        "whitespace": " \t  ",
        "linebreaks": "a  \r\n   b\\\nc\r\n\t d\r\n\0",
        "delimiters": "__lead ~~one *two*~~ end__ and *unclosed ** with \\*escaped",
        "legacy_literals": "[ссылка](https://example.com) `a_b < c` <b>текст</b> **",
        "emoji_units": "☕😀 **" + "я😀" * 2400 + "**",
        "astral_boundary": "**" + "a" * 3499 + "😀b" + "**",
        "exact_boundary": "x" * 3500 + "_y_",
        "code_boundary": "**a `" + "🧪" * 1752 + "` b**",
        "code_subtraction": "**до `code` и `ещё` после**",
        "nested_code_subtraction": "[a ~~b `不` c~~ and `💬` d](https://example.test)",
        "tight_code": "**`only code`** and `` one ` two ``",
        "images": '![**alt** and `code`](https://example.test/image.png "caption")',
        "image_unsafe": "![text](javascript:alert(1))",
        "empty_alt_fallback": "![](https://example.test/image.png)",
        "empty_link_fallback": "[](https://example.test)",
        "unsafe_links": "[s](JaVaScRiPt:alert(1)) [v](vbscript:bad) [f](file:///synthetic)",
        "image_data_url": "[png](data:image/png;base64,AA) [html](data:text/plain,AA)",
        "unicode_links": "[cafè](https://пример.рф/ñ?x=🧪#тест) [b](//例.jp) [q](<a b>)",
        "unicode_aliases": "[a](https://bücher.example:8080/) [b](mailto:synthetic@例.jp)",
        "autolinks": "<https://xn--bcher-kva.example/%CF%80%23%25> <https://例.jp/%7b>",
        "encoded_autolinks": "<https://example.test/%E2%82%AC%2E%2F%f0%9f%a7%aa>",
        "invalid_utf8": "<https://example.test/%FF%FE%E0%A0>",
        "named_entities": "&amp; &NotEqualTilde; &#x1f9ea; &#0; &not_a_real_entity;",
        "plain_links": "https://example.test/a www.example.test [a][ref]\n[ref]: /elsewhere",
        "email_autolink": "<synthetic@example.test>",
        "empty_destination": "[link]()",
        "literal_markup": "# T\n> quote\n- x\n1. y\n$3 + 2$",
        "punycode_highercase": " [a](HTTPS://bücher.example/) ",
        "escape_preservation": "[a](https://example.test/a%2fb%XX) [b](/path?q=a%25)",
        "deep_brackets": "[" * 45 + "a" + "]" * 45 + "(https://example.test)",
        "links_around_styles": "[left *inside* right](https://example.test) _a_",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    results = [
        {
            "name": name,
            "source": source,
            "parts": [
                {"text": text, "entities": [entity.to_dict() for entity in entities]}
                for text, entities in message_parts(source)
            ],
        }
        for name, source in fixtures().items()
    ]
    args.output.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Captured {len(results)} synthetic Telegram formatting cases")


if __name__ == "__main__":
    main()
