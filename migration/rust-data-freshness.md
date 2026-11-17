# Rust data freshness report

The native read combines the current and historical endpoint results, source
metadata, the connection state, history jobs, and replay progress. It uses
one read-only snapshot. Replay progress remains available while projections
are being rebuilt, and observation channels resume when replay is ready.

The differential check compares six complete reports with the pinned Python
application on one disposable TimescaleDB. It covers an empty database,
7,300 history windows, fetch-clock ties across UTC offsets, owner-local
calendar changes, source references, future and positive fetch lag, and
reports before and after replay. History scan and plan arrays are compared
without assuming an order from Python. The populated report took 0.07 s in
an ARM64 Linux test and peaked at 8,011,776 bytes in a 32 MiB container with
no swap.

The report is a database read. HTTP tool registration and access policy
checks are separate.
