# Legacy Garmin raw JSON capture

The Rust capture path verifies the established Garmin owner and every collection
permission required by the endpoint before touching the archive. It uses the
same global mutation fence as Python pack configuration and the same per-source
scope. An unconfigured legacy installation retains the baseline default. When
configuration rows exist, missing or disabled required packs block collection.

The immutable archive key still comes from Python-compatible sorted UTF-8 JSON.
The existing `source_payloads` row preserves unknown values, keeps the first
fetch timestamp, and starts at `pending` with parser version zero. Repeated
responses or key-order changes reuse the recorded source identity. A later
reappearance of an earlier payload also reuses that archived source; recording
a new application and its clocks belongs to normalization.

The native Linux arm64 test ran on disposable PostgreSQL 17.11 and TimescaleDB
2.29.2. It checked exact archive bytes and digest against Python, ordered the
synthetic A, B, A source identities, rejected a mismatched owner and a partial
collection grant before writing files, and left measurements and freshness
unchanged. It also checked disk reserve protection and mutation contention.

This path covers complete legacy JSON responses. It never assigns Personal API
account ownership, publishes a health observation, or reports a successful fetch
as normalized freshness.
