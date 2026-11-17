# Rust agent diary batches

The native diary store applies a validated log command in one owner-scoped transaction. It reuses the regular event writer for validation, deduplication, derived observations, intervals and audit entries. The authenticated caller provides the exact source message, transport and stable operation identifiers. Text supplied by the model cannot replace the source message.

A confirmed migraine is stored before any medication in the same command linked to it. Returned entries and deduplication keys retain their original draft positions. If any entry fails, earlier diary rows, observations, intervals and audit entries in the same operation roll back.

`migration/check_agent_diary_batch.py` captures results and persistent state from the pinned Python baseline. The ignored `agent_diary_batch` Rust integration test runs against a second disposable TimescaleDB database. The four scenarios cover the linked write, replay, rollback after an invalid relation and success after that rollback. On Linux ARM64 the comparison passed under a 32 MiB limit and peaked at 13,586,432 bytes with no swap or OOM events. This verifies the storage operation before it is connected to Telegram processing.
