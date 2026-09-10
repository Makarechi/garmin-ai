# Durable daily backfill (GA-06, first slice)

After Garmin owner enrollment, the scheduler plans the last `GA_BACKFILL_DAYS`
completed source days (365 by default, 0 disables new planning, maximum 3660).
It schedules at most two days per pass, newest historical days first. Subsequent
missed days are recovered independently of the 02:00–05:00 reconciliation window.
Current-day polling remains the source of live observations.

Plans, cursors and endpoint/day windows are persisted in AppState and exported
with the normal backup. Enqueue and cursor advancement share a transaction;
window keys include the bound account fingerprint. Repeating scheduling or
restarting cannot duplicate a window. A window records completion and source
reference in the same transaction as canonical ingestion. Jobs for a different
owner fail before a health fetch. Empty results do not establish the first day
of the owner's history.

Live jobs precede queued backfill, and historical HR/stress jobs do not hold up
current-context questions. Normal leased-job retry and backup coordination still
apply. An already-running request cannot be preempted by a newer request.

`data_freshness.history_sync` reports counts by window/job status, including
terminal failures needing attention, and the requested horizon. Scheduling
completion is not data completeness. No percentage of all account history is
invented. Turning planning off preserves queued work and completed provenance.

Plan generations include the daily endpoint set so newly supported channels get
the selected historical horizon. Existing windows are checked in a batch and do
not consume the two-day creation budget when a horizon grows. Stale completions
retain the newer authoritative source reference instead of leaving a pending window.
Terminal backfill failures do not suppress current-context questions.

`earliest_nonempty_window_date` reports the oldest completed nonempty requested
source day. `account_first_day` remains unknown: neither a bounded horizon nor an
empty response proves where the entire account history begins.

Validation uses synthetic PostgreSQL jobs for a full 365-day plan, cursor loss,
repeat scheduling, a five-day daytime outage, live-job priority, empty responses,
failure status and disabled/unbound setup.

This focused slice covers daily endpoints. Generation-aware activity pagination,
Garmin account-export ZIP import, coalescing the existing polling schedules,
capability-aware suppression and resumable parser replay are subsequent backlog
slices; they are not claimed as implemented here.
