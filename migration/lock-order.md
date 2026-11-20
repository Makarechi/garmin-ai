# Lock order at the storage and worker boundaries

The release uses the existing PostgreSQL advisory keys across Python and Rust.
Keep the database session and the filesystem lock bound to the operation that
owns them. A worker restart may release its process lease before a job lease
expires. The stored job lease and publication fence determine whether the new
worker can finish that job.

| Path | Acquisition and release |
| --- | --- |
| Runtime worker | Check storage readiness, then hold the runtime key on a dedicated PostgreSQL session and publish a heartbeat tied to that session. Connection loss stops ownership. |
| Migrate and logical restore | Acquire transaction locks for runtime, storage lifecycle and owner enrollment in that order. Commit the schema or restored rows with the corresponding ledger and activation step. |
| Erasure and storage resume | Hold the local recovery file lock before acquiring the dedicated database session. That session acquires runtime, storage lifecycle and owner enrollment in the same order. Durable markers cover the boundaries between filesystem and database work. |
| Manual backup | Hold the local active storage lock, acquire a dedicated runtime lease, then acquire the token publication lock on that session. Stage the backup in a private workspace before committing its destination. |
| Scheduled backup | The worker already holds runtime ownership. The backup holds the local active storage lock and obtains the token fence on its own session, then rechecks the worker and job leases. |
| Export snapshot | Hold shared storage lifecycle and owner enrollment locks on a dedicated session through a repeatable-read snapshot. Close the session after completing the snapshot to release both. |
| Read and publish canonical source state | Use the existing transaction-scoped replay mutation key. Readers take it in shared mode where the read requires a current projection. Publication validates its lease and commits data and provenance together. |

The exact key and command definitions live in
[worker ownership](../crates/ga-db/src/runtime_lease.rs),
[storage maintenance](../crates/ga-db/src/storage_maintenance.rs),
[backup and export](../crates/ga-db/src/database_export.rs),
[schema migration](../crates/ga-db/src/schema_migration.rs) and
[logical restore](../crates/ga-db/src/database_restore.rs).
The [local storage guard](../crates/ga-archive/src/storage_files.rs)
checks durable maintenance markers when acquiring an active lock.

A busy owner or a failed fence leaves the operation pending or reports a
bounded refusal. End failed transactions before returning their result so the
next request can acquire the same transaction-scoped lock immediately. Release
a session-owning lease by closing its dedicated connection. On delivery, keep
the relevant consent and destination checks through the provider call and
persist its actual receipt before allowing a retry. Recover an unknown
provider response as an uncertain delivery.
