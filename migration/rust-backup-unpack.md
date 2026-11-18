# R32 authenticated backup extraction

Rust authenticates the encrypted input before extracting it to a private workspace. The reader accepts the gzip-compressed and older uncompressed tar packages produced by Garmin AI. It checks member paths, file types, checksums and archive terminators before publishing an exclusive recovery directory.

Pinned Python `ee3a14a48a6ad30c1085951bac2f53c9088c2450` generated both formats. The Linux ARM64 tests recovered the same six files and 54,526,139 bytes from each package in a 32 MiB cgroup without swap or OOM events. Python verified every recovered file and its private permissions. Native tests rejected path traversal, absolute paths, symbolic links, unknown members, duplicated files, damaged tar and gzip checksums, the wrong key and an existing destination.

Reproduce by running `migration/check_backup_unpack.py record DIRECTORY` in the pinned Python environment, then `GA_TEST_BACKUP_UNPACK=DIRECTORY cargo test --locked -p ga-archive --test backup_unpack -- --include-ignored`, then the Python fixture with `verify DIRECTORY`.
