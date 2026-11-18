# R35 native PostgreSQL schema transitions

The Rust database migrator uses the existing Alembic revision ledger. It installs the current public schema on an empty database and upgrades revisions `b7c4e1a92d60` and `c8f51d3a7e20`. The former includes the historical measurement revision backfill. The database schema and its ledger commit together under the shared runtime, storage and owner locks. An unknown ledger or unversioned database with unrelated objects is refused.

The bootstrap schema was recorded from pinned Alembic `ee3a14a48a6ad30c1085951bac2f53c9088c2450` on PostgreSQL 17.11 and TimescaleDB 2.29.2. Retention predicates use the original `IN` expressions so that PostgreSQL compiles the same partial indexes. Native bootstrap creates the same seven-day hypertable and single owner. It leaves pre-existing application rows in place when updating.

`migration/check_native_schema.py` compared tables, column definitions, constraints, indexes, triggers, hypertable dimensions and historical measurement values against pinned Alembic. It exercised empty and both historical inputs, repeat runs, runtime exclusion, malformed revisions and an unrelated table. A deliberately conflicting final DDL step rolled back the measurement revision table and left the earlier ledger intact, then passed after the fault was removed.

The database library covers these starting revisions. The application migration command, registry initialization and earlier upgrade steps are tracked separately.
