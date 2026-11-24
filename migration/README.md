# Rust migration records and fixtures

The Rust build and tests use the files in `contracts/` and `fixtures/`.
The PostgreSQL migration implementation and its SQL schemas live in
`crates/ga-db/src/schema_migration.rs` and `crates/ga-db/src/schema_migration/`.
`check_postgres_readiness.py` tests the current Rust server against a disposable
TimescaleDB database. The release bundle includes the cutover and rollback
runbooks.

## Reproduce a historical check

Use the [frozen Python checkout][archive] at
`9376d081620740799362282ac9faf2f1a1baaffa`. It contains the Python application,
the locked dependencies, and the comparison scripts and FIT probe that lived
here during the migration. It also contains the
[original migration instructions][historical-readme]. Use the revision named
by an individual evidence record when it differs from this checkout.

```sh
git worktree add --detach /path/to/garmin-ai-migration-reference \
  9376d081620740799362282ac9faf2f1a1baaffa
cd /path/to/garmin-ai-migration-reference
uv sync --locked
```

Run the archived commands from that worktree with synthetic data and isolated
databases. When a current Rust test names one of the archived scripts for
fixture generation, use the fixture environment variable documented by the test.
Provide the current Rust executable to historical helpers that accept a binary
path when testing it against the frozen reference.

CI runs the current Rust checks from `main`. The off-host recovery and historical
schema tests obtain their fixtures from the frozen checkout and verify recovery
or upgrade with the Rust executable under test.

[archive]: https://github.com/Makarechi/garmin-ai/tree/9376d081620740799362282ac9faf2f1a1baaffa/migration
[historical-readme]: https://github.com/Makarechi/garmin-ai/blob/9376d081620740799362282ac9faf2f1a1baaffa/migration/README.md
