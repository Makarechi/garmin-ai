# Durable Garmin health projections

A previously captured daily or intraday response is applied under the same
owner, consent and source locks. The fields retained on each calendar date
keep their source and fetch time. Current samples are updated in batches
while earlier sample revisions remain available for historical queries.
Overlapping calendar streams retain the newest sample by its fetch clock.
Sleep retains both endpoints and readiness keeps the distinction between an
observed instant and an unknown observation time.

Partial and empty responses preserve earlier samples. Reapplying the same
response does not create duplicate revisions. A failed transaction keeps the
previous daily fields, current samples and journal. The original response
remains available for an ordinary retry.

The comparison uses two freshly migrated disposable TimescaleDB databases.
The ARM64 Linux build and pinned Python produce the same daily fields,
historical values, observations, timeline and journal. The fixture covers
duplicates, a late response and an injected constraint failure. The explicit
overlapping-calendar case records both sample revisions and retains the newer
one as current in Rust. Pinned Python published the older value as current in
that case.

The [recovery check](check_intraday_recovery.py) follows this overlap through the
measurement-series API and the native encrypted backup and restore commands.
It checks every restored table and the hash of every original archive file.
It then reads the retained native sample through the frozen Python API.
The restored fixture places a legacy replay job ahead of the older archive
record, marks that record with a prior parser version, and runs the native
replay command for each job. The current measurement, both revision identities
and the native measurement-series response remain the same.

Run it in the frozen Python environment with three distinct, empty PostgreSQL
databases on the same isolated server. Pass their addresses in
`GA_TEST_DATABASE_URL`, `GA_TEST_RUST_DATABASE_URL`, and
`GA_TEST_RESTORE_DATABASE_URL`. The database names end in `_test`.
Use the native publication test and application executables from the same source.

```sh
python migration/check_intraday_recovery.py "$GARMIN_HEALTH_PUBLICATION_TEST" "$GARMIN_NATIVE_CLI"
```

Offline rebuilds, authoritative interval replacement and provider scheduling
are separate steps.
