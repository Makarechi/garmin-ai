# Erasing and re-enabling the database

The native database maintenance session owns the shared runtime, storage, and enrollment locks. It checks the supported schema before a change. Erasure clears the 30 application tables and restarts their sequences in the same transaction that records the erased marker. A foreign-key dependency outside the known schema fails the transaction. Re-enabling storage clears the database marker while the same locks protect the caller's file activation.

On disposable Linux ARM64 PostgreSQL 17 and TimescaleDB, the native result matched pinned Python's erasure and resume across every application table and both sequences. The tests exercised busy locks, late writes, a blocked truncation with full rollback, repeated erasure, repeated resume, and a table outside the application schema that remained intact. Filesystem removal and the native CLI are separate work.
