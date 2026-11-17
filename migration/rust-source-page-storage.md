# Durable source pages and metric revisions

Source polling retains the established seven-day window, private source
identity, retry time and remembered cursor digests across restarts. It rechecks
owner selection and saved progress in the transaction. Stale responses leave
records and cursor state unchanged.

Each accepted record is retained as a content-addressed private raw and a
revision journal. Corrections and deletion records invalidate only their
previous metric observation. Projection checks the configured source hash,
the confirmed mapping, the currently active metric and linked tracker, and
the declared unit. An absent or wrong unit keeps the record private without
creating a measurement. Existing observations remain available for as-of
queries. Analysis generated from a changed projection is invalidated in the
same transaction.

`check_source_page_storage.py` drives a synthetic adapter through pinned
Python and the native Linux ARM64 client on separate disposable TimescaleDB
databases with Python-created tracker and mapping records. Nine steps cover a
saved cursor and backoff, repeated input, corrections, missing and wrong units,
exact integer raw data, deletions, response races, and a forced failure after
one record of a page. Raw hashes, journal state, metric history and cursor
state match. The runtime adapter dispatch and new mapping confirmation have
separate integration work.
