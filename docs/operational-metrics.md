# Queue and connection diagnostics (GA-27, first phase)

The existing admin-only `/operations` and `/metrics` now distinguish the process
heartbeat from the Garmin connection gate. A healthy process may be paused for
reauthentication or backoff. The state and effective paused flag are separate.

Due pending jobs expose count and oldest `run_at` age by fixed lane: Garmin,
Telegram, analysis, backup and other. Future retries and completed jobs do not count
as overdue. This measures time since current eligibility, not original enqueue time
or total time across retries. A lane with no due jobs is absent.

Job kinds/statuses and source endpoints/statuses use finite allowlists before SQL
aggregation. Unknown stored values collapse into `other`; connection states collapse
into `unknown`. Payloads, source keys, event names and upstream error strings are
never metric labels. Existing counts stay available without unbounded label cardinality.

Tests use synthetic jobs and private-text markers in a disposable PostgreSQL database.
Retention policy, disk budget alerts, off-host backups, restore drills, observation SLOs
and production load profiling remain later GA-27 phases.
