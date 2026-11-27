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

## Telegram model request timing

A worker writes one JSON log event, `telegram_request_latency`, when it finishes
processing a model job. Use its internal `inbound_message_id` to match it to the
inbox and delivery receipt. The `phase` distinguishes text processing from voice
transcription. `queue_wait_ms` starts at the current eligibility
time, including the end of a previously expired claim. `inbox_age_ms` starts at
ingress, including earlier screening or retries. Both end when model processing
starts. `processing_ms` ends when the worker finishes. Telegram delivery is
recorded separately by the receipt.

Stage offsets are relative to the start of processing. The initial
`postgres_analysis_read` includes connection checkout and verification.
`postgres_sleep_read` covers each scoped sleep read, including revalidation.
Each `postgres_sleep_query` records its attempt number and outcome. PostgreSQL
reports server-side statement timeouts as `query_canceled`. Nested stage
durations overlap, so summing them double-counts time. `gemini_http` times each
provider call through receipt and decoding of the response. It includes
network time. A dropped operation is marked `cancelled`.

These events contain stage names, durations, outcomes and the internal
message identifier. Message text, health data, provider output and credentials
are excluded.
