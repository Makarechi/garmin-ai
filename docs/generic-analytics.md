# Generic analytics

`AnalysisSpec` is the only public plan for generic event and metric analysis. It fixes the metric
or definition key, bounded windows, selected version, allowed aggregation, result limit, and
knowledge cutoff before any query runs. Metric contracts decide which aggregations, units, scales,
and coverage policies are valid; labels cannot introduce a model or executable query.

Results record the normalized specification hash, definition version, scale, projection generation,
source references, source event revisions, and cutoff. A later correction marks that evidence stale.
Comparisons use the same version and scale. Derived arithmetic accepts only a small allowlisted AST,
checks dimensions, and has strict size limits. These tools report associations and completeness;
medical or sports models remain trusted scenario-pack code and are never generated from user labels.

`threshold_segments` finds intervals above or below a chosen value for time-weighted
numeric measurement series. Supply `threshold` in the metric's unit and choose
`gt`, `gte`, `lt`, or `lte`. The query accepts at most 31 days and the `limit`
caps input samples, including adjacent samples used to clip window boundaries.
Only adjacent valid measurements no farther apart than the metric contract's
maximum gap establish a covered interval; the first value is held until the next
sample. The response reports covered and matching seconds separately, version,
unit, source, and measurement revisions. An empty covered interval has unknown
matching duration, not zero. Multiple sources require an explicit selection.
Event-derived, sparse, increment, and unsupported interval data are rejected;
this operation does not infer physiology or a psychological cause.

Measurement history selects the latest revision known at the cutoff before
checking its quality and metric version. A deletion, invalid revision, or revision
under a different contract cannot revive an older value; invalid revisions also
break threshold intervals.
