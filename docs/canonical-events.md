# Canonical event envelope and migration

Every diary entry keeps its existing event UUID, payload, relation fields, idempotency key,
audit history and legacy `source`. The additive canonical envelope supplies the semantics that
the legacy source field could not express independently:

- exact immutable event-definition version and time topology;
- observed interval and precision, separately from recording and ingestion clocks;
- assertion kind, producer/connector, transport and author;
- evidence references, confidence and server-assigned validation status.

The legacy API fields remain present. Event API responses additionally include
`canonical.version = 1`, so clients can adopt the new envelope without a flag-day change.
All old and custom event writes use the same authoritative event row and audit path; there is
no second writable event store.

## Backfill contract

Migration `c71a5e4d290b` maps only existing trusted state. It aborts on an unknown legacy source
instead of guessing. Open ends and unknown payload values remain null. `recorded_at` and
`ingested_at` use the original row creation time because no more precise historical clock is
available. The migration changes neither events nor audit identities and runs transactionally.

After system definitions are installed, `backfill_canonical_events` verifies that every event
has an exact definition version. It is repeatable and has no audit or projection side effects.
An unresolved legacy kind stops startup or migration rather than leaving a partially canonical
diary.

## Rollback and recovery

Before deployment, create and verify an encrypted export against a separate migrated database.
The current restore path accepts earlier additive revisions and derives the same canonical
metadata from their retained trusted fields.

Schema downgrade is allowed for system-only diaries. If a custom entry exists, downgrade stops
before dropping canonical-envelope columns. Recovery is then either roll-forward with the same
database or restore the verified export into a current-schema database. This prevents a release
rollback from silently discarding or making custom entries unreadable.
