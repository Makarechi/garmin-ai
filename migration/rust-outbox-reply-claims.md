# Native reply outbox claims

The native database layer claims ordinary replies for one verified owner and channel instance. It uses the Python write fence and skips rows assigned to a legacy Telegram update, a legacy alias, or initiative delivery. It checks the persisted intent binding against the target conversation before claiming. The claim contains the outbox identifier and expiring lease, with content held back until delivery authorization.

Before claiming a generated reply, Rust verifies the target memory epoch, all opted-in source epochs, and the latest revision of the authenticated inbound operation. Changed memory or stale input cancels that candidate. A batch examines at most 64 candidates before leaving additional work for a later pass.

Six synthetic claims and queue snapshots matched pinned Python. Native tests also verified rollback after an injected claim write failure, contention with the Python lock, and separation from legacy, initiative and foreign intent rows. Linux ARM64 used 6,672,384 bytes in a 32 MiB container with no swap. A sender has to recheck the lease and delivery policy before exposing content to the channel.
