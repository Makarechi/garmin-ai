# Replay readiness and health reads

Replay status uses the scheduler's canonical source predicate across the full
stored history. Only a retained projection interpreted by a different parser
version blocks the ordinary health read. Jobs and completion counts reflect the
same committed snapshot. A busy or timed-out read reports unavailability and
does not return an empty health analysis.

A native Linux ARM64 test compared decisions and progress counts with pinned
Python in a disposable TimescaleDB. The fixture contains 7,300 old raw
revisions, 365 current source streams and 52,560 measurements. The test
reintroduced a retained revision beyond the first scheduler page, verified
that the ordinary daily analysis was withheld, then verified it recovered once
that revision matched the active parser. It also tested a rollback target and
a concurrent mutation lock. The native application test completed in 0.21 s
at a 32 MiB limit with zero swap and zero OOM events.
