# Explicit entry time relation

`AnalysisSpec.time_relation` now selects `starts_within` (the existing default) or `overlap` for `query_entries`. The overlap mode applies half-open `[start, end)` boundaries to reconstructed Event/Audit snapshots. An open interval started before the window remains visible; a point that occurred before the window does not. A correction to an event end is evaluated at `knowledge_cutoff` rather than from the current row.

Candidate SQL includes both current event times and before/after audit times, then the bounded snapshot reconstruction applies the final relation. The existing 10,000 candidate limit remains a deliberate guard for unusually large histories.
