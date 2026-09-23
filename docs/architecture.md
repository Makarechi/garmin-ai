# Runtime architecture

## Universal application boundary

The application core now identifies an installation-local owner independently of Garmin accounts
or channel IDs. Event and metric meaning lives in immutable versioned registries. Scenario packs
select capabilities, while generated tracker forms and generic analytics consume registry
contracts instead of adding a new Python class, SQL column, menu branch or prompt branch for each
user tracker.

Inbound messages, conversations, outbound intents and delivery receipts are transport-neutral.
Telegram remains a supported adapter; a restricted text-only adapter proves that buttons, edits,
reply relationships and receipts degrade explicitly. Provider and channel SDKs are optional and
are loaded only by their configured adapters.

User-authored schemas are bounded data: remote references, executable hooks and excessive shape
are rejected. Definition management, integration management and fact writes use separate access
scopes. Sensitive tracker schemas and facts require destination-specific consent before reaching a
model or channel. Shareable packs contain definitions and display metadata, never facts, owner or
channel bindings, original messages, credentials or action tokens.

Database changes remain additive. Portable exports retain definition versions and lineage. A
release rollback restores a verified pre-release backup; an old binary is never treated as a safe
reader for new custom facts. See [universal acceptance](universal-acceptance.md) for the automated
release boundary.

The verified starting point and dependency rules for the incremental universal tracker/channel
work are recorded in [Universal core foundation](universal-foundation.md). UNI-01 deliberately
adds guardrails and characterization only; it does not claim custom trackers or a second channel.

The application is a single-owner Python service suite backed by PostgreSQL 17
with TimescaleDB. It keeps immutable raw payload versions and typed records for
daily health, measurements, activities, events, questions, and evidence.
The Garmin-specific schema is provisional until account probes verify actual
responses. Unknown fields remain archived for later reprocessing.

The owner is an installation-local `Person` with an opaque UUID and explicit
locale, timezone and unit preferences. Garmin is a `SourceConnection`; Telegram
is a `ChannelBinding`. External IDs stay as opaque strings inside their provider
namespace and cannot create or replace a person. The database constraint permits
exactly one owner for now; additional channel bindings require an explicit
confirmation flow.

Event meaning is held in a versioned definition registry. System definitions adapt
the existing trusted Pydantic validators; user definitions use a bounded JSON Schema
profile and never execute code or retrieve remote references. Every new event records
the exact immutable definition version and its actual point/interval topology. See
[Event definitions](event-definitions.md).

Metric meaning is versioned separately from event shape: units, dimensions, scales, time
semantics, coverage and allowed aggregations are fixed in immutable contracts. Event-field
projections keep exact definition versions and source lineage; `HealthDay` remains a compatibility
projection. See [Metric definitions](metric-definitions.md).

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
estimates retain their bounds; explicit medication intake may retain unknown name, dose and unit as null; known values remain validated. Repeated
requests use idempotency keys. Reusing a key for different data is an error.
Updates require the current revision. Mutation and audit entry share a database
transaction. Deletion is reversible; an undo cannot overwrite a newer edit.

## Isolation and validation

The database binds to localhost only. Credentials come from a private ignored
`.env` file. Tests use a separate database whose name must end in `_test`;
they migrate real PostgreSQL/TimescaleDB, never substitute SQLite.
