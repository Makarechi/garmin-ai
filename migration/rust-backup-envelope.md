# R32 authenticated backup envelope

The file layer uses the legacy `GARMINAI1` marker, a 12-byte random nonce, AES-256-GCM, and a 16-byte trailing tag. OpenSSL processes the entire frame with one cipher context and 64 KiB I/O buffers. The completed ciphertext is reread and authenticated before publication, and its plaintext digest must match the bytes supplied by the caller. Decryption keeps the plaintext in a private staging directory until authentication succeeds. Publication preserves an existing destination.

## Verification

The pinned Python reference is `ee3a14a48a6ad30c1085951bac2f53c9088c2450`. Its original encryption and decryption commands exchanged a 69,271,573-byte synthetic file with the native Linux ARM64 tests. Both implementations recovered the same bytes. The native tests also covered an empty Python frame, incorrect recovery key, altered tag, truncated header, a simulated full disk, a competing publisher, symlink rejection, and cleanup of staging files.

The native tests completed in a 32 MiB Linux cgroup with no swap and no OOM events. The cgroup reached the 32 MiB limit while processing the files. The test executable linked to the operating system C libraries without requiring a separate runtime OpenSSL library.

Reproduce by running `migration/check_backup_envelope.py record DIRECTORY` with the pinned Python environment, followed by `GA_TEST_BACKUP_ENVELOPE=DIRECTORY cargo test --locked -p ga-archive --test backup_envelope -- --include-ignored`, then `migration/check_backup_envelope.py verify DIRECTORY` with pinned Python. Use a new fixture directory for each test.

This verifies the encrypted file format. Building and restoring the complete database and archive bundle are separate R32 work.
