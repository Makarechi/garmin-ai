# Generic metric source selection

`AnalysisSpec.source` accepts one bounded source identity for metric operations: `event`, `measurement:<source>`, or `observation:[<account>,<device>]`. The latter is a JSON pair and can contain null values. The selector applies to aggregate, period comparison, completeness, and observation queries. Entry queries have no metric source selector.

Aggregates continue to reject multiple source identities when no selector is given. A selected source is returned in the aggregate and in each observation row, so callers can inspect provenance. Period comparisons require the same source identity in both aggregates, along with the existing version, unit, scale, and method checks.

This change exposes the source filtering already present in the lower-level metric aggregate. It does not change the meaning of a metric, combine incompatible streams, or infer reporting completeness.
