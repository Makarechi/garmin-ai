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

The Garmin worker also checks for the first Telegram text or voice message
after 15 minutes of inactivity. On its next scheduling pass, it can request
today's sleep and daily summary. A check within the last three minutes or an
existing job for the same data satisfies the request. Telegram can prepare its
reply while the update runs. Commands and delayed Telegram deliveries leave
the regular schedule in place.

Synthetic PostgreSQL tests cover morning, nightly and evening overlaps, same-slot
restart scheduling, completed slots and retained thirty-day reconciliation.
