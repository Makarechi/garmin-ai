# Atomic operation undo (GA-16, second phase)

New Telegram log batches share a deterministic UUID operation identity in the audit
log. Undo reverses all records in the latest operation, children before parents,
inside one savepoint. A newer edit through any channel or an external reference
rejects the entire undo without partial deletion. Repeating an already completed
undo fails the existing revision check. Telegram confirms the number of records.

The additive nullable audit column preserves legacy rows: their undo remains an
individual mutation. Existing API/MCP calls retain their response shape (the latest
undone event); the atomic effect covers the operation. Redo, selecting an older
operation and edited-message reconciliation remain later phases. Downgrading drops
operation grouping metadata, so a production downgrade would lose batch-level undo
history; no production database is modified by these verification steps.

Readiness and backup schema versions advance together to b91d02a4c703. Compatible older exports, including a637902bf114, restore into the migrated destination with null operation identity on legacy audit rows. Synthetic verification covers migration upgrade/downgrade/upgrade and backup roundtrip with the new schema.
