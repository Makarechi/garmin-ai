# R27 Telegram neutral reply rendering

The renderer prepares neutral outbox messages for the configured owner's private Telegram chat. It keeps markup literal, splits each text block at the same 3,500-character boundary as Python and keeps buttons on the first message. It emits the existing text fallbacks for references, replacements, forms and action-only replies. Attachments return an explicit unsupported result. Wrong bindings, expired actions, and missing, duplicate or oversized callback tokens are rejected before sending.

The pinned Python adapter generated 14 synthetic request and state documents. The native test matched all of them. A separate native test checks the owner and signed action requirements. Both tests passed offline on Linux arm64 with a 32 MiB memory limit. Peak memory was 7,786,496 bytes with zero swap and no OOM events.
