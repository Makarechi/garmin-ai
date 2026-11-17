# R32 current logical database recovery

The Rust importer accepts the current PostgreSQL revision `c54f8a72d130` and streams its `garmin-ai-jsonl-v1` records into a single transaction. It requires an empty migrated destination or its untouched bootstrap rows, obtains the runtime, storage and enrollment locks, validates the ordered table inventory and footer, and keeps identity, event provenance and revision data. The two sequences are updated transactionally. The header is limited to 64 KiB and each row to 32 MiB.

## Verification

The native Linux ARM64 importer recovered all 30 tables and all nine rows written by pinned Python `ee3a14a48a6ad30c1085951bac2f53c9088c2450`. The fixture compared the typed database rows and verified that the next audit ID advanced. The synthetic values included timestamps with microseconds, Unicode and an escaped quote, negative signed zero, and Python's legacy floating-point infinity encoding.

The destination initially contained its system registries and default scenario packs. Each simulated failure preserved the original bootstrap data. The fixture exercised an incompatible schema, incomplete footer, incorrect row count, trailing data, an invalid non-finite token in a JSON document, the three advisory locks, and a destination with existing user data. A completed restore also refused a second attempt.

Run `migration/check_database_restore.py` against two distinct empty PostgreSQL test databases using `GA_TEST_DATABASE_URL` and `GA_TEST_RESTORE_DATABASE_URL`, and pass the native test executable built with `cargo test --locked -p ga-db --test database_restore --no-run`.

This verifies the current logical format. Older schema revisions, filesystem activation and the public restore command use separate compatibility work.
