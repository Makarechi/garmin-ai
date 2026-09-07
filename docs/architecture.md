# Runtime architecture

The application is a single-owner Python service suite backed by PostgreSQL 17
with TimescaleDB. It keeps immutable raw payload versions and typed records for
daily health, measurements, activities, events, questions, and evidence.
The Garmin-specific schema is provisional until account probes verify actual
responses. Unknown fields remain archived for later reprocessing.

`LocalArchive` provides content-addressed, private files through `put_json`,
`put_bytes`, and `read`; a future object-store implementation can implement the
same boundary. Personal values never belong in version-control fixtures.

## Durable work

For the initial single-host deployment, jobs and schedules live in PostgreSQL.
Rows are claimed with `FOR UPDATE SKIP LOCKED`; expiring leases permit crash
recovery, and lease tokens reject stale acknowledgements. Handlers must be
idempotent because a crashed job may run again. Retries are bounded and retain
failure state. The domain layer is independent of the scheduler so Temporal
can replace execution later without moving canonical data.

This chooses the persistent-queue alternative from the handoff for one-host
operation; it does not implement Temporal. It avoids running a second durable
workflow control plane before workloads require it, while retaining persistent
jobs, backoff, duplicate suppression, and recovery after worker termination.

## Event writes

Every write validates a discriminated payload schema, requires timezone-aware
timestamps, and rejects nonfinite numbers and invalid intervals. Caffeine
estimates retain their bounds; medication name and dose are required. Repeated
requests use idempotency keys. Reusing a key for different data is an error.
Updates require the current revision. Mutation and audit entry share a database
transaction. Deletion is reversible; an undo cannot overwrite a newer edit.

## Isolation and validation

The database binds to localhost only. Credentials come from a private ignored
`.env` file. Tests use a separate database whose name must end in `_test`;
they migrate real PostgreSQL/TimescaleDB, never substitute SQLite.
