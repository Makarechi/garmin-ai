# R35 native PostgreSQL schema transitions

The Rust database migrator uses the existing Alembic revision ledger. The application command `garmin-ai-rust migrate --status` reads the starting revision. The command `garmin-ai-rust migrate` applies the following transitions to reach `c54f8a72d130`.

| Starting revision | First action |
| --- | --- |
| Empty public schema | Create the current schema and owner |
| `f103aa712b44` | Recover the original Telegram conversation links and voice metadata |
| `a42d9e18c701` | Rebind historical Telegram conversations to their stable identities |
| `a72d9f4c8e31` | Populate receipt times in historical measurements |
| `b7c4e1a92d60` | Install and populate the measurement revision history |
| `c8f51d3a7e20` | Install the delivery fence |
| `c54f8a72d130` | Read the current ledger and initialize the application registry |

The database schema, data transformations, registry and ledger commit together under the runtime, storage and owner locks. A revision outside the table, a ledger with zero or multiple rows, or unrelated objects in an unversioned public schema stops the migration.

The bootstrap schema was recorded from pinned Alembic `ee3a14a48a6ad30c1085951bac2f53c9088c2450` on PostgreSQL 17.11 and TimescaleDB 2.29.2. Retention predicates use the original `IN` expressions so that PostgreSQL compiles the same partial indexes. Native bootstrap creates the same seven-day hypertable and single owner. It leaves pre-existing application rows in place when updating.

`migration/check_native_schema.py` compared tables, column definitions, constraints, indexes, triggers, hypertable dimensions and historical measurement values against pinned Alembic. It exercised empty and both historical inputs, repeat runs, runtime exclusion, malformed revisions and an unrelated table. A deliberately conflicting final DDL step rolled back the measurement revision table and left the earlier ledger intact, then passed after the fault was removed.

The historical fixtures separately compare Telegram identities and voice metadata, measurement receipt times, and application registry initialization against the same pinned Python revision. The original Telegram recovery fixture covers a confirmed binding, a saved raw message, a callback and the unbound fallback. Recovery around deliberately interrupted DDL preserves the starting revision until the entire transition succeeds.

`migration/check_native_migrate_command.py` runs the public migrator from every
starting state in the table. On disposable PostgreSQL, it compares the resulting
schema and synthetic records against the pinned Python command, repeats the
upgrade and checks the Rust HTTP readiness endpoint. An interruption test kills
the migrator while its final schema change is blocked, verifies that the earlier
ledger and measurement survived, then reruns the migration to a ready service.
Rust CI also verifies that the public status and migration commands refuse
unversioned occupied storage and unknown, empty or split ledgers, preserving
the existing rows.
