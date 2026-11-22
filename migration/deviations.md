# Approved deviations from the Python baseline

For each proposed change, identify the parity entry, baseline behavior, intended observable
behavior, impacted data and callers, regression case, approval and recovery implications.
Record evidence for the original and candidate behaviors. Preserve the baseline until the
change has a documented decision.

## Corrected acknowledgement timing

The frozen Python API can acknowledge a generated tracker form before its
database transaction commits. An immediate read can miss the record, and a
failed commit can leave a success response without a saved entry. Both the
current Python fallback and the native API complete the transaction before
acknowledging the submission. The Python regression test forces a commit failure,
checks the retryable response and verifies that a retry saves one entry.

## Owner decisions for the native release

The owner approved the following four differences on 2026-11-22.

### PAR-01 Overlapping storage paths

Both runtimes reject overlapping storage paths on a case-insensitive filesystem.
The pinned Python installer created the requested nested directory before it
rejected setup. Rust rejects the overlap before creating that directory. The
three checks cover the token, backup and lock directories under an existing data
directory. They verify preservation of the settings file and directory
permissions. Existing data and settings can be used when reverting to Python.
[Native setup comparison](check_native_setup.py)

### PAR-06 Current intraday measurement

Applying an older overlapping Garmin response could make its measurement current
in Python. Rust keeps the newer current measurement and retains both revisions.
The comparison checks the measurement-series API, encrypted backup and restore,
the Python reader on restored native data, and replay of the older response.
[Paired recovery fixture](check_intraday_recovery.py)

### PAR-10 Diary writes under load

Both runtimes wait for the shared diary write lock for admitted requests. The
pinned Python application continued admitting requests under contention. Rust
admits up to 16 concurrent diary mutations and 2 MiB of request bodies across
those writes. An excess request receives `503` with `Retry-After: 1` before any
mutation. Clients can retry the rejected operation after the indicated delay.
The comparison verifies read access under contention and a single canonical
event and audit after the admitted requests finish. The limit affects admission
only, so it requires no transformation of saved diary entries.
[Paired HTTP fixture](check_diary_http.py)

### PAR-14 Deep JSON imports

Python's extreme nesting limit depends on the shape of the JSON and its runtime
environment. Rust uses fixed platform-specific nesting budgets based on the
pinned CPython 3.15.2 measurements. The synthetic cases cover accepted and
rejected inputs on Linux ARM64 and emulated AMD64. Near the reference stack limit
the same file can still have a different outcome. A file rejected at this
boundary fails before rows are written, and the import comparison covers
replaying accepted rows through Python.
[JSON import comparison](rust-file-json.md)
