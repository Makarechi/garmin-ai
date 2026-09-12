# GA-06: overlapping daily schedules

Within one scheduler pass, each endpoint/source-day pair is enqueued once across
frequent, morning, nightly reconciliation and daily schedules. The earliest
matching scheduling class retains its existing durable deduplication key, so a
repeated pass in the same time slot does not create another request. Frequent
refreshes continue in later slots even after earlier work has completed.

Historical reconciliation dates remain scheduled independently. This change does
not merge activity pagination, previously queued legacy jobs, or account-history
windows into periodic jobs: those carry separate progress and ownership contracts.
Full account-export import and broader history-window coalescing remain GA-06 work.

Synthetic PostgreSQL tests cover morning, nightly and evening overlaps, same-slot
restart scheduling, completed slots and retained thirty-day reconciliation.
