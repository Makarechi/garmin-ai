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
Offline rebuilds, authoritative interval replacement and provider scheduling
are separate steps.
