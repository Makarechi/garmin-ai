# Generic metric query contract: version and completeness

All generic metric entrypoints now use one resolver for definition and version. A missing positive version raises `LookupError("Metric version not found")` before any contract fields are read. The API maps that exception to 404; the tool path receives the same controlled error.

`query_completeness` distinguishes whether an aggregate can be calculated from whether diary reporting is complete. `aggregate_available` reports the former. `reporting_completeness` is `unknown` and legacy `complete` is null until an explicit reporting contract exists. `coverage_ratio` remains an observation coverage measure where the metric contract defines one. Sparse tracker observations do not invent a denominator.

Overlap semantics and source selection remain follow-on parts of HF-05.
