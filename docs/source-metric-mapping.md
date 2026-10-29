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
mapping for the same source and metric requires removing the old one first;
changes to source configuration likewise require a fresh preview.

This step records the owner's selection only. It does not import, normalize, or
make source records queryable. Unmapped and confirmed source records remain
private raw data until normalization and its correction, deletion, and consent
rules are implemented separately.
