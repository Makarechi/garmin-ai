# Native offline replay dispatcher

The replay worker claims only Garmin archive references for the installed parser and an endpoint it can handle. It routes daily and intraday measurements, activity summaries and pages, activity parts, and original FIT or ZIP exports through the existing replay paths. The earlier health-only entry point still leaves activity and FIT work for a compatible consumer. No provider connection is used.

While the worker verifies and stages the original payload, it renews the job lease. A lost lease cancels further archive work. FIT publishes its replacement projection, records the replay result, and completes the job in one transaction. A stale worker cannot overwrite an existing projection. An eligible retry can publish after the prior lease expires.

`crates/ga-db/tests/replay_dispatch.rs` uses an isolated TimescaleDB at the pinned Python schema. It exercises all five routing families, a FIT export that outlives a one-second initial lease, a lost lease with the old projection still intact, and a successful second claim. It also checks that unsupported endpoints, invalid archive references, and other parser targets do not use up attempts. The older health-only lease regression runs separately. The fixtures use synthetic data and run on Linux ARM64 with 32 MiB of memory and swap disabled.
