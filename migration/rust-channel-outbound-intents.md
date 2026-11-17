# Native outbound intent contract

Rust normalizes the existing channel-neutral intent, including text, forms, signed action references, attachments, timing and scheduling metadata. The owner and channel identity stay bound to the intent. Reply and edit references must use the same channel instance. Large text blocks move into the normalized document without being copied a second time.

Twenty-nine synthetic cases matched the pinned Python contract on Linux ARM64, including optional and legacy forms, timezone offsets, content-free intents, out-of-range priorities, cross-channel references and Unicode text limits. The run used 7,766,016 bytes in a 32 MiB container with no swap.
