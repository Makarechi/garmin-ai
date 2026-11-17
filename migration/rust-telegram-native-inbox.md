# Telegram inbox ownership during the migration

A newly accepted native Telegram delivery creates a processing job in the same transaction as its inbox row, button consumption, acknowledgement and polling offset. The job is the persistent ownership record for that delivery. Replays return the saved event and operation. The ownership record must stay available for the lifetime of the corresponding inbox row.

The transition Python build recognizes native ownership under the existing Telegram ingestion lock before creating legacy work. Native ingress checks both storage keys that Python used for legacy-only updates and preserves the existing dispatcher. The active ordering position advances past legacy-owned poll deliveries without recording a second inbound event. Distinct bot namespaces can reuse the same provider update ID independently.

The tests used disposable TimescaleDB databases. Pinned Python seeded legacy updates for two bot namespaces, Rust recognized both and accepted a third namespace, and the transition Python code replayed an actual Rust-created event without assigning it to the legacy worker. A forced failure to insert the native job rolled back its inbox write and offset together. The native ARM64 test peaked at 7,233,536 bytes in a 32 MiB container with no swap. The Python Telegram adapter suite passed all 25 tests, including the generic worker leaving native messages and acknowledgements eligible for Rust.

This change creates native processing work and leaves it queued until a native handler can claim it. For crossover testing and deployment, run a Python build that recognizes native ownership before accepting native traffic. Existing legacy work is still handled by the Python dispatcher.
