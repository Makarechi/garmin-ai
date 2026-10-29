# Source-to-metric mapping preview

An owner can preview an explicit mapping from one configured source instance to
one current, owner-controlled tracker metric. The mapping names the source
payload keys holding the value and its unit; no source values are sent to the
preview endpoint. The first supported contract is a point-in-time numeric metric
whose source unit exactly matches the metric's unit.

`POST /source-metric-mappings/preview` accepts a source instance ID, metric
version ID, value path, unit path, and expected unit. It returns the selected
metric contract and a short-lived confirmation token. `POST
/source-metric-mappings` accepts the same draft and the token. Both endpoints
require owner-level API access. Confirmation checks the source selection and
the current metric contract again; an expired token, changed draft, changed
metric, or previously consumed token cannot confirm the mapping. A confirmed
mapping is bound to one source instance and metric version.

`GET /source-metric-mappings` lists confirmed mappings, with `limit` and
`after_id` for pagination. `DELETE /source-metric-mappings/{id}` removes a
confirmed mapping only while it has no normalized data. Confirming a different
mapping for the same source and metric requires removing the old one first.

On the next source poll, a confirmed mapping can project point-in-time numeric
records whose configured fields exist and whose unit matches exactly. The raw
record and its revision remain available; repeated pages do not add a second
observation. A correction invalidates the previous projection, and an explicit
deletion invalidates its current projection. Unsupported times, missing fields,
incorrect units, nonnumeric or out-of-range values stay in private raw storage
without a current metric value. This does not infer missing fields or units.
Reading a tracker metric that contains source observations requires both diary
and health access, including for historical reads. The model also needs both
categories enabled, in addition to any tracker-specific sharing consent.

Only records returned by a subsequent poll are processed. Previously stored raw
records are not automatically replayed, and source windows remain bounded by the
existing polling policy. Calendar-day and interval sources are not normalized by
this mapping. If the source configuration or metric contract changes, no
additional records are projected using the old confirmation. A changed source
configuration may be confirmed again for the same mapping and metric. Its
preview warns that old and new records with the same source ID are treated as
the same identity; the owner must explicitly set `reconfirm_source_identity`
to `true` only when that continuity is correct. Use a new source instance for
an unrelated source. The preview expires after 15 minutes and becomes invalid
if another confirmation changes the mapping first. The previous confirmation
is retained in private history. A changed metric contract or mapping fields
that already have projections cannot yet be replaced through this API.

The synthetic restore test exports a disposable database and restores it into
an empty migrated database on the same host. It checks that a repeated record
does not duplicate its observation and that subsequent corrections and
deletions still apply. This is not independent-host restore acceptance.
