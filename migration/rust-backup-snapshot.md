# R32 backup source snapshot coordination

The backup source lease shares the Python runtime and Garmin token publication locks. The database rows and distinct archive references come from the same repeatable-read snapshot. Invalid references refuse the backup before rows are emitted. The caller holds the source lease through archive verification and can recheck it before publication.

The native Linux ARM64 fixture verified both existing lock identities, distinct required raw and activity references, refusal of an invalid reference, lookup from the same database snapshot, and release after completion. It ran on a disposable PostgreSQL database bootstrapped with the pinned Python migration and peaked at 7,778,304 bytes in a 32 MiB cgroup.
