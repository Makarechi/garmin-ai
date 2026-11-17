# Durable job leases in PostgreSQL

This step uses the existing Python job table for the local channel-initiative job type. Enqueue retains the unique deduplication key. A claim locks one eligible row and writes its new lease and attempt in the same transaction. Renewal keeps a later deadline if the clock moves backward. Finish records a result only while the matching lease is valid. A previous owner cannot complete a reclaimed job. Unfinished jobs can be reclaimed after expiry.

Queue writes use the existing storage and owner fences and refuse a mismatched schema. Two lazy pools give readiness, renewal and completion connection capacity separate from enqueue and claim. Individual database statements have a 750 ms timeout. Retry preserves terminal invalid requests, the eight-attempt limit, explicit retry deadlines and the default capped exponential delay with jitter. Deferrals and provider cooldowns refund the attempt.

The real database test ran with a native Linux arm64 test executable and disposable PostgreSQL 17.11 and TimescaleDB 2.29.2 bootstrapped at the Python baseline revision. It checks concurrent enqueue and claim, renewal with a backward clock, exact expiration boundaries, reopening the database client, stale completion, retry, the attempt limit, storage fences and readiness while both work connections are blocked. CI runs the same checks against a fresh fixture.

This provides the storage operations for one local job type. Handler publication, worker supervision, scheduler heartbeat, process shutdown and the specialized claim policies for other job types are separate steps. This code does not enable a Rust background worker.

## Publishing local database effects

The publication method locks the job row and checks the lease with PostgreSQL's clock before entering the callback and again before commit. Local changes and completion use one transaction. A missing lease, callback failure, deadline, or process exit leaves the local changes uncommitted. The callback uses the work pool, preserving readiness capacity, and has a two-second limit. Use this method for transactional database effects. Remote actions will need their own durable delivery records.

A native Linux process was killed after staging a synthetic write. PostgreSQL kept the marker invisible and released the transaction, then another process reclaimed the job after the lease expired. An injected callback failure also rolled back. The surviving claimant subsequently committed one marker and a completed job. A separate test let the lease expire during a live callback and checked that its changes rolled back.
