# Confirmation gate for projected custom facts

Custom entries can remain `needs_confirmation` in the diary while their numeric projections exist. An observation's `quality=observed` describes technical quality and does not assert owner confirmation. Aggregate and generic observation queries now resolve the owning Event's status and deletion state at `knowledge_cutoff` from the audit history, and only use confirmed, undeleted facts. This keeps earlier cutoffs unchanged after confirmation, correction, undo, or deletion.

Generic entry queries remain the diagnostic surface for pending facts. Their rows include status, validation status, assertion kind, source, and topology. Generic observation rows expose the same provenance from the event snapshot, with technical quality kept separate.

New event mutations assign one explicit transition timestamp to their audit record and metric projection. PostgreSQL's transaction-start `now()` previously gave multiple mutations in one transaction the same audit timestamp, so it could not represent an intermediate cutoff accurately.

The existing observation rows are preserved. The query gate uses Event/Audit history directly, so no destructive backfill is required for this correctness fix. A separate additive migration and dry-run report are still needed if projection metadata must be materialized in each historical observation row. Old audit rows whose transitions share the same timestamp cannot recover an ordering within that timestamp.
