# Approved deviations from the Python baseline

No observed product behavior changes are recorded at this stage.

For each proposed change, identify the parity entry, baseline behavior, intended observable
behavior, impacted data and callers, regression case, approval and recovery implications.
Record evidence for the original and candidate behaviors. Preserve the baseline until the
change has a documented decision.

## Candidate differences to resolve before release

- The JSON import candidate stops at 1,000 nested containers. Pinned
  CPython 3.15.2 accepted a 1,200-level synthetic input. The native
  validation, serialization and disposal paths need a compatible verified
  bound, or an explicit decision about the observable limit, before
  replacing the Python file importer.
- Native intraday publication keeps a newer current measurement when an
  older overlapping response is applied while retaining both revisions.
  Verify that ordering through replay, analytics and migration of legacy
  histories before declaring the temporal contract complete.
