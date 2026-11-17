# Source adapter page contracts

The native page reader preserves source record IDs, operation, observed and
effective time, supplied UTC offset, source timezone and reference. It keeps
exact integers and the existing compact UTF-8 representation for record
hashes. Payload shape, nesting, item count and UTF-8 byte limits are checked
before private raw storage.

An admitted page matches the selected instance, the adapter's capabilities
and its requested half-open window. Validation covers declared deletions,
bounded intervals, calendar days using the recorded timezone, duplicate
record IDs, cursor reuse, cursor history capacity and complete snapshots.

`check_source_contracts.py` produces 78 synthetic decisions and record
representations from the pinned Python pre-migration checkout. The Rust
fixture was run on native Linux ARM64 and matched all cases with a 32 MiB
container limit. This covers page admission. Database publication and
confirmed field mappings are subsequent R20 work.
