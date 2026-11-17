# Rust migration – attested Garmin intervals

The existing health publisher accepts an explicit replacement proof for intraday
channels. It validates the interval and channels, records the normalized proof,
and replaces observations only within the half-open interval. Other sources,
calendar keys and newer overlapping observations retain their current values.
Removed observations retain their revision history and tombstones. Raw outcome,
observations and analytical invalidation commit together. An identical final
projection leaves analytical results intact even when the new proof differs.

With Rust 1.99.1 on Linux ARM64, the repository's PostgreSQL/TimescaleDB schema
and the pinned CPython reference, `check_authoritative_replacement.py` matched
nine sequential attempts on two isolated databases. It compared the raw label,
current measurements and observation revisions after partial input, overlapping
requests, repeat and amended proof, a late SQL failure, an authoritative empty
result and a stale request. The failure retained the successful projection and
the untouched input could be retried.

This covers publication of a captured payload. The caller supplies the adapter
proof. The adapter and the background replay dispatcher are separate migration
steps.
