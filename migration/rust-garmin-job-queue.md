# Garmin queue leases

The native store claims the existing Garmin endpoint, activity-list and FIT
jobs. It uses the same storage-queue lock as Python. The stored Garmin
reauthorization and cooldown states pause claims. Running and overdue backup
jobs take priority when backups are enabled.

The selection order uses the owner's local date. It preserves current heart
rate and stress work, recent activities, and the distinct priorities for
stress and heart rate history. A recovered job receives a fresh lease token.
Completion, renewal and cooldown retries share the existing queue rules.

The disposable TimescaleDB test verified the storage lock, an overdue backup,
a reauthorization gate, a timed cooldown, ordered work selection, renewal,
expiry and recovery from a separate database handle. It also verified that a
stale worker cannot finish the job, and that a cooldown refunds the attempt.

This store exposes claims and status changes. Garmin handlers and atomic
publication under the active lease are integrated separately.
