# R32 logical database export

The Rust writer streams the current `garmin-ai-jsonl-v1` logical database from one read-only PostgreSQL snapshot. It selects the schema and enrolled owner after obtaining shared storage and owner locks. Those locks remain held while writing the ordered rows and footer. The caller is responsible for staging, compression and atomic publication of the completed output.

## Verification

`migration/check_database_export.py` uses the pinned Python reference at `ee3a14a48a6ad30c1085951bac2f53c9088c2450` and two empty disposable PostgreSQL databases. It compares the native inventory against the Python export and restores the Rust output through the existing Python loader. All 30 tables and nine synthetic rows matched, including owner identity, the canonical diary event and its audit, a measurement, a source payload, Unicode, a signed zero and timestamps through microsecond precision. A subsequent audit insert used a new sequence ID.

The Linux ARM64 integration test also required a matching owner, rejected erased storage and held locks, exercised a simulated full writer, and committed a real row from another connection immediately after the export header. The later row remained visible in the database and absent from the exported snapshot. The initial signed-zero regression was reproduced against the existing reader and passed after the Rust writer preserved the floating-point spelling.

For reproduction, run the Python fixture against distinct empty databases ending in `_test`, using `GA_TEST_DATABASE_URL` and `GA_TEST_RESTORE_DATABASE_URL`, and pass the test executable built with `cargo test --locked -p ga-db --test database_export --no-run`.

The proof covers export from the current schema and restore through the current Python reader. Native restore and the full encrypted backup flow are separate R32 work.
