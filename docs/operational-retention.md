# GA-27: explicit retention of completed Telegram transport text

`garmin-ai prune-telegram-text` previews one batch; `--apply` redacts it. The default
horizon is 90 days, configurable with `--older-than-days` from 30 to 3650. `--limit`
is 1–1000. Continue a bounded scan with the returned `next_cursor` via `--cursor`,
including when older ineligible rows fill a page. Start later sweeps without a cursor.
This command uses the same standalone lock as other maintenance commands, so stop
the worker first; no recurring cleanup is enabled or run automatically.

Only processed updates received before the cutoff qualify, and only if their primary
job and every related Telegram job are done and completed before that cutoff. Pending,
running, failed, recently completed, missing-reply and missing-primary records stay.
A preview locks and checks the same records but changes no content. Output contains
counts, cutoff and cursor, never messages, transcripts or answers.

Apply replaces the transport update payload with an random redaction receipt,
removes transcripts from completed job payloads, and replaces the cached answer and
keyboard with an expiry notice. Telegram update IDs/received times, job identities and
dedup keys, and outbox delivery receipts remain. Re-delivery cannot enqueue the same
update or repeat its event; direct processing returns the retained reply tombstone,
and already-sent outbox parts cannot send again. Transactions and existing erase fences
apply to the batch. This is text redaction, not deletion of queue/idempotency history.

Canonical diary records, their original text, mutation audits, health/raw archives,
analytic conversation retention policy and backups are outside this command. Old backup files
can still contain prior transport text until their independent retention expires.
PostgreSQL may retain old physical row versions until its normal storage maintenance;
the report makes no immediate disk-space or secure-erasure guarantee. Existing database
backup/restore carries redaction receipts alongside the retained identities.

Tests use a disposable database and fake Telegram delivery. They cover preview/apply,
unfinished/recent jobs, cursor progress, minimum horizon, CLI wiring, and replay without
new events or sends. This command has not been run against the owner's database.

The separate `telegram:transcript:<update_id>` cache is included in the same transaction. Preview counts these transcript records; apply leaves an empty redacted cache tombstone, preventing a direct transcription retry from downloading or resending old voice content.

Expired conversation:pending clarification text older than the selected retention horizon is removed even when no update candidate remains. Recent or undated clarifications are preserved. Related jobs are loaded in one batch query. Random receipts cannot be used to test candidate message contents.
