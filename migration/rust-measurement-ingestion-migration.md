# Historical measurement ingestion time

The Rust migrator accepts Alembic revision `a72d9f4c8e31`. It adds the category domain and records when each measurement was ingested before producing measurement history. The recorded fetch time of the source payload takes precedence. A measurement without a source uses the time of the migration transaction.

`check_native_schema.py` compared the native Linux ARM64 migration and the pinned Alembic chain on disposable PostgreSQL 17 with TimescaleDB. The public catalog and the measurement with a synthetic source matched, including its microsecond timestamp. The measurement without a source and its first historical revision shared the same timestamp within their respective migration windows. An injected conflict in the final migration rolled back both added columns, the revision history table, and the ledger before a successful retry.
