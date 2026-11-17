# Durable Garmin connection state

The native store records Garmin reachability, expired login, account mismatch,
rate limiting and connection failures in the same state document that existing
workers read. The queue and state writes share the coordination lock. Rate
limits honor the provider delay within the supported range and retain the legacy
15-minute minimum. Transient connection errors use the existing bounded backoff.
Saved state contains only a fixed failure class and timings.

An older transport result cannot clear a newer state. Ordinary reads also cannot
resume the queue after a login failure. A verified completed login can resume it.

The isolated TimescaleDB test verifies that a separately opened connection honors
the saved cooldown, waits through an authentication failure and claims the
original job after a verified login. It also checks stale replies and a different
account. No requests were made to Garmin.
