# PostgreSQL readiness

The Rust server reads the existing `GA_DATABASE_URL` setting. The driver scheme
`postgresql+psycopg://` is mapped to the native database client. The database pool
uses zero initial connections, at most two simultaneous connections and a 750 ms
acquisition timeout. Each probe allows at most 750 ms per database statement and
1,500 ms end to end. Liveness remains independent of the database.

Readiness runs in a read-only transaction. It acquires the same shared storage and
owner fences as the Python runtime, checks the erased-storage marker and requires a
single Alembic revision matching `c54f8a72d130`. The probe releases both locks by
rolling back the read transaction. Lock contention, database failure and missing
configuration leave readiness closed. The Alembic command remains the schema migrator.
Application bootstrap and owner validation will be covered with the relevant workflows.
[SQLx connection pool](https://docs.rs/sqlx/0.9.0/sqlx/pool/struct.PoolOptions.html)
[PostgreSQL client defaults](https://www.postgresql.org/docs/17/runtime-config-client.html)

## Representative verification

The check ran with a native Linux arm64 release binary built offline, CPython 3.15.2
at `ee3a14a48a6ad30c1085951bac2f53c9088c2450`, PostgreSQL 17.11 and TimescaleDB 2.29.2.
Python and Rust returned the same statuses and JSON for ready, an unknown revision,
and erased storage. Rust also rejected two recorded heads and an empty database, kept
liveness responsive, recovered after each exclusive application lock and a concurrent
table lock, and left both advisory locks available after rollback.

On a synthetic copy with existing data, eight parallel clients completed 32 readiness
requests and their liveness checks. The before and after `pg_dump` output for the
public schema matched byte for byte after removing per-run restriction tokens.

| Snapshot | SHA-256 before and after |
| --- | --- |
| Schema | `7b1041d33cbf8d27bebcb3c051ecbca82656f0ef03067e22506c7c1ae061b678` |
| Data | `4efa0d99cd8ae51280cbaa85ca031c685b71275bb892cdaa682b5aca755995f1` |

Run the check with the release binary and a disposable service-owned PostgreSQL
database ending in `_test`. The optional `--bootstrap` argument applies the Python
baseline schema only to an empty fixture, as the CI job does. The check temporarily
injects and removes synthetic invalid schema and lock states in the fixture.

```sh
cargo build --locked --release -p ga-app
uv run python migration/check_postgres_readiness.py target/release/garmin-ai-rust --bootstrap
```
