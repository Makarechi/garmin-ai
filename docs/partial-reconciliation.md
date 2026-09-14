# Partial responses and authoritative replacement (GA-09, sample slice)

Live intraday responses have unverified completeness by default. They update
observed timestamps and retain omitted points; ten returned samples cannot erase
a previously collected day. An API response being nonempty is not snapshot evidence.
Parser version 9 records this normalization policy.

A trusted adapter can supply `Replacement(start, end, metrics, evidence)` to ingest.
It must attest a positive aware interval of at most 31 days and explicitly name
channels belonging to that endpoint. Only prior points from the same source,
endpoint and source key inside the half-open interval are removed. An attested
empty snapshot can clear that interval. Live Garmin polling supplies no attestation
because completeness has not been established for its response contract.

The attestation is persisted with the request-order watermark, participates in
idempotence, and is restored during offline replay. Partial and authoritative
corrections supersede old insights; stale requests cannot restore removed samples.
Parser errors roll back the replacement together with failed normalization.

Upstream activity tombstones, inventory confirmation, user-facing interval recheck,
cross-endpoint revision arbitration and reconstruction of superseded partial raw
versions remain separate GA-09/GA-08 work. Retaining an omitted historical point
does not prove that the upstream provider still considers it valid.
Authoritative HRV replacement is supported. Dense Body Battery belongs to the stress
adapter contract; the summary-only body_battery endpoint rejects sample replacement.
Normalized samples preserve the source of their raw reference. Contract channel order
and duplicates are canonicalized for replay/idempotence.

Parser replay explicitly rebuilds measurements owned by the current immutable raw revision inside the normalization savepoint. Readings omitted by a new parser disappear; older partial revisions retain their own observations. A parser failure rolls back the projection deletion. Reprocessing all superseded partial versions remains a separate archive replay extension.

Parser rebuilds recover overwritten observations from prior partial applications under the current parser. An ordered per-source-key application journal retains timezone and interval attestations, so earlier authoritative deletions remain effective. Reconstruction uses verified archive bytes and savepoints, then restores only measurements previously owned by the rebuilt revision. Missing historical provenance/archive data or more than 1,000 applications fails atomically instead of dropping observations; this bound is a deliberate operational limit. Legacy revisions without an application journal are treated as partial patches from before adapter attestation existed.

Physiological context questions use the Garmin source consistently for HR baselines,
stress runs and interval HR evidence; alternate imported sources are not interleaved.
Pre-journal raw creation dates cannot reconstruct repeated A → B → A application order.
New observations can start a journal with an explicit unknown legacy boundary, but
rebuilding an existing projection across that boundary fails atomically and preserves
its measurements. Verified application history is required for that reconstruction;
no guessed fallback value is published.
