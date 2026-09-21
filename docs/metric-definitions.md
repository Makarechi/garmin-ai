# Versioned metric contracts

Metric definitions describe how a value may be interpreted. A version fixes its value kind,
unit and dimension, optional ordinal scale or nominal category domain, time semantics, bounds,
coverage policy and allowed aggregations. Active versions are immutable; a scale, unit, or
category-domain change creates a new version.

Supported value kinds are physical numbers, increments, interval totals, cumulative counters,
ordinal scales, nominal categories and booleans. Categories cannot be averaged, ordinal scales
do not imply that higher is better, and values from different scale versions are queried
separately. Unit conversions are limited to an internal dimension-checked registry.

`event_metric_mappings` link a stable field identity from one exact event-definition version to
one exact metric-definition version. Projected observations retain the source event, field,
projection generation, effective interval, record/upload/ingest clocks, precision and coverage.
Editing or deleting the source invalidates the active projection without deleting its lineage.

Built-in Garmin contracts remain compatible with the existing metric catalog and are registered
as system definitions during migration/startup. Existing measurements and temporal observations
are backfilled to those versions. `HealthDay` remains a legacy projection; custom metrics never
add columns to it.

Generic aggregation is bounded to 366 days and 10,000 observations, uses only methods allowed by
the exact metric version, keeps source references, and accepts an explicit knowledge cutoff.
Both projected observations and measurement-backed system metrics are filtered by their durable
ingestion time, never by observation time as a substitute for when the value became known.
Time-weighted contracts fail closed below their required coverage; sparse subjective observations
do not inherit that policy.
