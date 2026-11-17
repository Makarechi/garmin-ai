# Durable archive storage

The archive library opens directories by file descriptor and validates the legacy
`<first two digest characters>/<SHA-256>.<bin|fit|json|zip>` key before opening an
object. It reads bytes at their existing keys. JSON writes use the Python-compatible
archive representation from R06. File and stream writes use a 64 KiB buffer.

The writer checks the byte limit and available disk space as it goes. It syncs the
private staging file before creating the final directory entry and syncs the directory
before returning the key. Concurrent writers converge on one file. A matching key
with different bytes returns an error and preserves the existing object for diagnosis.
Replay callers can open a verified reader. It checks the byte limit, streams the
hash from the existing object and rewinds the same open file for processing. A
size change, hash mismatch or cancellation interrupts verification.
Stage cleanup holds an exclusive lock on the archive directory and removes regular
private entries in the Rust staging namespace. Writers hold a shared lock from
stage creation through publication and cleanup.
[Rustix relative filesystem operations](https://docs.rs/rustix/1.1.6/rustix/fs/index.html)

## Verification

- Read an object produced by the pinned Python archive representation and reused its
  exact hash and name on a Rust write.
- On native Linux arm64, eleven checks passed in a container capped at 32 MiB with swap
  disabled. A 100 MiB write followed by a verified read passed and matched Python's
  SHA-256 `2279306afc5fd86754d2e0f92e8af4adecc15bc1aea93c17945569a973a502e9`.
  The container reached its memory ceiling while reclaiming filesystem cache and
  recorded zero out-of-memory events and zero killed processes.
- A changed object of the same length was rejected. Verification stopped on
  cancellation and rejected oversized input before reading it.
- On an 8 MiB writable filesystem, a 12 MiB stream was refused. The original archive
  object stayed readable and the next small write succeeded.
- An abrupt process exit left one unpublished stage. Cleanup removed that stage,
  preserved unrelated entries and refused to run while another writer was active.
  Concurrent duplicate writes, redirected paths and corrupt collisions were checked.

The filesystem tests run on Linux amd64 and arm64 in Rust CI. The dedicated small
filesystem case can be run separately using an empty 8 MiB test mount.

```sh
cargo test --locked -p ga-archive
cargo test --locked -p ga-archive --test archive disk_full_aborts_and_preserves_existing_archive -- --ignored --exact
```

For the second command, set `GA_TEST_ARCHIVE_FULL_DIR` to the dedicated test mount.
FIT decoding and archive reference updates belong to the later ingestion slice.
