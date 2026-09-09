"""Render inline CommonMark as Telegram entities without interpreting HTML."""

from markdown_it import MarkdownIt
from telegram import MessageEntity

_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("strikethrough")
_TYPES = {"strong": "bold", "em": "italic", "s": "strikethrough", "link": "text_link"}


def message_parts(source: str) -> list[tuple[str, list[MessageEntity]]]:
    # Inline parsing preserves the model's paragraphs and list numbers.
    tokens = _MARKDOWN.parseInline(source)[0].children or []
    pieces = []
    entities = []
    stack = []
    position = 0
    for token in tokens:
        kind = token.type.removesuffix("_open").removesuffix("_close")
        if kind in _TYPES and token.nesting == 1:
            stack.append((kind, position, token.attrGet("href")))
            continue
        if kind in _TYPES and token.nesting == -1:
            name, start, url = stack.pop()
            if position > start:
                entities.append(MessageEntity(_TYPES[name], start, position - start, url=url))
            continue
        content = "\n" if token.type in {"softbreak", "hardbreak"} else token.content
        if token.type == "code_inline" and content:
            entities.append(MessageEntity("code", position, len(content)))
        pieces.append(content)
        position += len(content)
    # Telegram forbids code entities inside other entities. Split enclosing styles.
    code_ranges = [(e.offset, e.offset + e.length) for e in entities if e.type == "code"]
    safe_entities = []
    for entity in entities:
        ranges = [(entity.offset, entity.offset + entity.length)]
        if entity.type != "code":
            for code_start, code_end in code_ranges:
                ranges = [
                    (left, right)
                    for start, end in ranges
                    for left, right in (
                        [(start, end)]
                        if end <= code_start or start >= code_end
                        else [(start, min(end, code_start)), (max(start, code_end), end)]
                    )
                    if left < right
                ]
        safe_entities.extend(
            MessageEntity(entity.type, start, end - start, url=entity.url) for start, end in ranges
        )
    entities = safe_entities
    rendered = "".join(pieces)
    # Whitespace-only input is kept for compatibility with the previous sender.
    rendered = rendered or source
    parts = []
    start = 0
    while start < len(rendered):
        end = start
        units = 0
        while end < len(rendered):
            width = 2 if ord(rendered[end]) > 0xFFFF else 1
            if units + width > 3500:
                break
            units += width
            end += 1
        part = rendered[start:end]
        clipped = []
        for entity in entities:
            left = max(start, entity.offset)
            right = min(end, entity.offset + entity.length)
            if left < right:
                clipped.append(
                    MessageEntity(entity.type, left - start, right - left, url=entity.url)
                )
        parts.append((part, MessageEntity.adjust_message_entities_to_utf_16(part, clipped)))
        start = end
    return parts
