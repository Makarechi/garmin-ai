# GA-27: periodic backup-capacity notices

When backups are configured, the worker schedules one capacity check per six-hour
UTC bucket, including startup. This runs separately from model analysis and backup
creation. A persisted shortage enqueues at most one technical Telegram notice per
UTC date; outbox receipts protect delivery retries. The notice includes no paths,
credentials or diary content. Without Telegram credentials the capacity state is
still retained locally in `storage:backup-capacity`.

Queued notices are skipped if a later check found capacity restored. The check is
an estimate, not a reservation or guarantee against writes between checks. It does
not delete snapshots or source data. Rapid disk consumption can still exhaust space
before a check; the synchronous creation preflight remains in effect.

The authenticated operational snapshot and Prometheus endpoint also expose the age
of the last valid check, capacity availability, sufficiency and per-role free/required
bytes. Roles are a fixed allowlist; paths and extra stored fields are omitted. Missing,
malformed, future-dated or inconsistent reports are unknown, not healthy. Monitoring
should alert separately on insufficient capacity and a stale/missing check.
