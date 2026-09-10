# Event topology (GA-01)

Review baseline: `99b746419cf71b652cdcbc30b5f8176618e3e374`.

Event history uses half-open query intervals `[start, end)`. A bounded interval
overlaps when its start precedes the query end and its end follows the query start.
Zero-length events are points, included only when their timestamp is in the query.

For both existing and newly written rows, an absent end means an open interval only
for `migraine` and `illness`. Other kinds with no end remain points. History and
timeline event projections expose `topology`, `ongoing`, and `missing_end`.
`ongoing` describes an unclosed record, not a confirmed current symptom; consumers
must also respect the unchanged event `status` and `confidence`.

The shared SQL overlap predicate does not filter status or deletion itself. History
excludes deleted records and retains inferred/unconfirmed records with their status.
API, MCP and Telegram use the same history tool, also embedded in timeline results.
Limited results prioritize events starting inside the requested window before
carry-over intervals; each group orders by start and ID. Old unclosed episodes
therefore cannot consume the limit before newly logged events. `truncated` remains
explicit when not all matching records fit.
Onset-based analytical windows remain onset-based; interval-aware migraine control
selection is tracked separately as GA-02.

No migration or backfill is necessary: topology is derived from existing facts.
No calculated query boundary is written to `end`, and no old episode is automatically
closed. Reverting this code restores previous read behavior without data loss.
Audit/export serialization stays unchanged for backup compatibility.

Acceptance coverage: synthetic PostgreSQL regressions in `test_event_topology.py`
cover previous-day migraine/illness versus coffee/note, both Budapest DST changes,
half-open boundaries, zero-length points, deletion, retained status and stable limits.
