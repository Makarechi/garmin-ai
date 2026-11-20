# Approved deviations from the Python baseline

No observed product behavior changes are recorded at this stage.

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
- The established native onboarding and tracker settings routes accept valid JSON
  objects from authenticated clients that omit the content type. Frozen FastAPI
  requires the header. Both runtimes reject explicit non-JSON media types and
  preserve the same malformed-request errors in the comparison.
  See the five settings and export requests in `check_http_json_requests.py`.
