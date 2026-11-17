# Durable Garmin health projections

A previously captured daily or intraday response is applied under the same
owner, consent and source locks. The fields retained on each calendar date
keep their source and fetch time. Current samples are updated in batches
while earlier sample revisions remain available for historical queries.
Sleep retains both endpoints and readiness keeps the distinction between an
observed instant and an unknown observation time.

Partial and empty responses preserve earlier samples. Reapplying the same
response does not create duplicate revisions. A failed transaction keeps the
previous daily fields, current samples and journal. The original response
remains available for an ordinary retry.

The comparison uses two freshly migrated disposable TimescaleDB databases.
The ARM64 Linux build and pinned Python produce the same daily fields, current
and historical sample values, observations, timeline and journal. The fixture
checks duplicate samples, a late response and an injected constraint failure.
Offline rebuilds, authoritative interval replacement and provider scheduling
are separate steps.
