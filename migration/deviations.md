# Approved deviations from the Python baseline

Approval remains pending for the changes in the candidate section.

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

## Candidate differences to resolve before release

- File imports use platform-specific nesting budgets based on measurements of
  pinned CPython 3.15.2. The accepted and rejected synthetic cases match on Linux
  ARM64 and emulated AMD64. Values immediately around the reference stack
  limit can still differ by shape and environment. See [the file JSON comparison](rust-file-json.md).
- Native intraday publication keeps a newer current measurement when an
  older overlapping response is applied while retaining both revisions.
  [The paired recovery fixture](check_intraday_recovery.py) checks the
  published value through the measurement-series API, encrypted backup and
  restore, the Python reader on the restored native data, and replay of the
  older response. Approval for the current-value ordering is pending for PAR-06.
- Installation rejects overlapping storage paths even when their capitalization
  differs. In three case-insensitive filesystem checks (token, backup, and lock
  directories under an existing data directory), both runtimes rejected setup
  and preserved the settings file and directory permissions. The pinned Python
  setup created the requested nested directory before rejecting the operation.
  Native setup rejects the overlap before creating that directory. The check
  is reproducible with [the native setup comparison](check_native_setup.py).
  This affects failed installation attempts only. The existing data and settings
  can be used when reverting to Python. Approval is pending for PAR-01.
- Both HTTP runtimes wait for the shared diary write lock for admitted requests.
  Native HTTP admits up to 16 concurrent diary mutations and 2 MiB of request
  bodies across those writes. A request exceeding either limit receives `503`
  with `Retry-After: 1` before any mutation. The pinned Python application
  continues admitting requests under contention.
  [The paired HTTP fixture](check_diary_http.py) verifies the queue limit,
  read access during contention, and a single canonical event and audit
  after the queued requests complete. The overload behavior still needs a
  release decision for PAR-10.
