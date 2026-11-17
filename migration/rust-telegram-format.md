# R27 Telegram text formatting

The native formatter consumes inline CommonMark, including strikethrough, with HTML parsing disabled. It keeps paragraph and list markers as text and emits Telegram entities for bold, italic, strikethrough, links and inline code. It subtracts code spans from other entities. Text parts and their entity offsets are measured in UTF-16. Each part has at most 3,500 UTF-16 units and keeps supplementary characters intact. URL formatting uses the same allowed schemes and escaping conventions as the pinned Python sender. A malformed punycode hostname stays visible as typed if it cannot be decoded.

`migration/check_telegram_format.py` records 34 synthetic cases from the pinned Python formatter. The native test compares complete text and entity documents, including image alt text, escape sequences, nested styles, invalid percent-encoded text and entity clipping around emoji. The Python formatter's five existing tests also pass. The optimized Linux arm64 tests passed offline with a 32 MiB memory cap. Peak memory was 7,802,880 bytes with zero swap and no OOM events.

Telegram sending and the delivery records for partial sends are separate integration work.

[Telegram MessageEntity fields](https://core.telegram.org/bots/api#messageentity)
