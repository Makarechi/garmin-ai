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
