# Approved deviations from the Python baseline

Approval remains pending for each candidate below.

For each proposed change, identify the parity entry, baseline behavior, intended observable
behavior, impacted data and callers, regression case, approval and recovery implications.
Record evidence for the original and candidate behaviors. Preserve the baseline until the
change has a documented decision.

## Candidate differences to resolve before release

- File imports use platform-specific nesting budgets based on measurements of
  pinned CPython 3.15.2. The accepted and rejected synthetic cases match on Linux
  ARM64 and emulated AMD64. Values immediately around the reference stack
  limit can still differ by shape and environment. See [the file JSON comparison](rust-file-json.md).
- Native intraday publication keeps a newer current measurement when an
  older overlapping response is applied while retaining both revisions.
  Verify that ordering through replay, analytics and migration of legacy
  histories before declaring the temporal contract complete.
- Submitting an incomplete coverage interval to owner diary HTTP returns
  status `422` in both runtimes and leaves the store unchanged. The pinned
  Python response includes a specific validation message and the submitted
  fields. Native HTTP returns a short invalid-arguments message.
  [The paired system diary fixture](check_system_diary_types.py) records this
  response difference. Approval is pending for PAR-09.
- The established native onboarding and tracker settings routes accept valid JSON
  objects from authenticated clients that omit the content type. Frozen FastAPI
  requires the header. Both runtimes reject explicit non-JSON media types and
  preserve the same malformed-request errors in the comparison.
  See the five settings and export requests in `check_http_json_requests.py`.
- Installation rejects overlapping storage paths even when their capitalization
  differs. In three case-insensitive filesystem checks (token, backup, and lock
  directories under an existing data directory), both runtimes rejected setup
  and preserved the settings file and directory permissions. The pinned Python
  setup created the requested nested directory before rejecting the operation.
  Native setup rejects the overlap before creating that directory. The check
  is reproducible with [the native setup comparison](check_native_setup.py).
  This affects failed installation attempts only. The existing data and settings
  can be used when reverting to Python. Approval is pending for PAR-01.
