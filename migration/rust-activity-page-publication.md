# Rust migration – activity page publication

One leased Garmin page publishes its summaries, endpoint freshness,
activity detail and FIT jobs, and the saved scan cursor in one database
transaction. The final queue update checks the live token against the
database clock. The original captured response remains available for
recovery independently of publication. Legacy offset jobs continue in
100-row steps. Tracked scans use their recorded overlap and restart rules.

A synthetic 100-activity page on native Linux ARM64 with TimescaleDB
created 1,200 detail jobs, 100 FIT jobs and one continuation. A shifted
page restarted the tracked offset without repeating those jobs. An
injected failure during the final cursor write rolled back all projections
and jobs, and a forced lost lease after that write did the same. A
malformed page and a failed fetch left the cursor and previous normalized
freshness available. The test ran in a 32 MiB cgroup without swap or OOM.
