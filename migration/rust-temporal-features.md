# Native pre-event evidence

The batched read selects an observed feature strictly before an event and
within the preceding 24 hours. It uses the latest eligible source revision
available by the knowledge cutoff. Retrospective analysis may use a later
cutoff. An as-known calculation rejects a cutoff after its earliest event.
Earlier revisions and their original source dates remain available. Outputs
keep the evidence ID, source, time, and timezone.

A single PostgreSQL query enforces the reference limit of 100,000 relevant
versions and returns only the selected evidence. The reader shares the
verified-projection guard with other compound health analyses.

Eleven synthetic batches matched pinned Python on TimescaleDB. The cases
cover corrections, same-time precedence, interval boundaries, unobserved
and unknown values, an empty request, and the 100,001-version rejection. The
largest accepted batch selected 6,000 features for 2,000 event times.
The native ARM64 run took 0.35 seconds and peaked at 25,919,488 bytes with
a 32 MiB memory limit and no swap, including writing its result as JSON.
