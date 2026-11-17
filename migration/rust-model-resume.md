# Durable model fallback resume

A version-two fallback record identifies a model integration configuration, the ordered model list, and the exact request digest. It stores which models already returned invalid replies. The record contains no prompt or response text. A changed request, model order, or integration identity cannot reuse an earlier result. Untyped or damaged records fail closed.

Only the active job lease may read, write or clear the record. A renewed worker can recover an existing record, while an expired worker cannot clear it. A failed write rolls back and releases the job row immediately. The bookkeeping path runs under the same provider session lock as other provider operations.

Four persisted records and cleanup results matched the pinned Python gate. The tests also covered key rotation, changed order and digest, invalid and oversized saved records, an expired claim, and a failed write. A blocked update reproduced an unclosed transaction during testing. After the fix, all checks passed on disposable TimescaleDB on Linux ARM64 with a peak of 7,839,744 bytes under 32 MiB. The gateway still supplies the digest and decides when a prepared request can be sent.
