# Versioned custom entries

Creation reads the active definition and records its version, time evidence and
original request. Updates validate against the definition version saved by the
entry. They apply the metric mappings for that version and keep the revisions
and their prior observations. Retried creation returns the original result
after an edit or a definition change. Reuse of the idempotency key with another
input is rejected.

`check_custom_entries.py` seeds two disposable TimescaleDB databases with the
same old and current definitions through pinned Python. One database applies
the scenario in Python and the other through the native Linux ARM64 test. It
compares current events, immutable versions, audit operations, derived
observations and superseded measurements. It also checks the historical
timezone, wrong units, a stale edit, retry of a pre-existing entry, and two
adjacent 170-bit integers that must stay distinct.
