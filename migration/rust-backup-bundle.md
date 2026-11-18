# R32 compatible encrypted backup bundle

The native bundle writer packages a staged compressed database export, the existing coverage report, raw archive files and token files. The encrypted output uses the existing authenticated format and compressed tar layout. The caller supplies database-referenced raw keys so missing files or hash mismatches prevent publication. Source reads use directory handles and reject redirects. The writer detects file and directory changes during capture and preserves an existing backup on failure.

## Verification

The native Linux ARM64 fixture ran in a 32 MiB cgroup with no swap or OOM events. The cgroup reached its limit while processing the files. Pinned Python `ee3a14a48a6ad30c1085951bac2f53c9088c2450` unpacked the generated backup with its existing reader and recovered all six members and 54,526,139 bytes. The fixture includes a long Unicode filename, incompressible raw data, a required content-addressed file, and an independent destination.

The native unit test verified missing and corrupted required raw files, a redirected token file, an output inside the archived source, and preservation of a previous authenticated backup. Reproduce with `migration/check_backup_bundle.py record DIRECTORY` in pinned Python, then `GA_TEST_BACKUP_BUNDLE=DIRECTORY cargo test --locked -p ga-archive --test backup_bundle -- --include-ignored`, followed by Python `migration/check_backup_bundle.py verify DIRECTORY`.

The file writer consumes an export and a raw inventory supplied by its caller. The public backup command will acquire the maintenance locks and capture both from the same database snapshot.
