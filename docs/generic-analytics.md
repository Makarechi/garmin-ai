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
