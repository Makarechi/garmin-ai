# Resource discovery

`resource_catalog` is a read tool over existing event and metric definitions. It
does not create a second registry. It supports `events`, `system_metrics`,
`tracker_metrics`, and `raw_sources`; results are bounded to 1–50 resources and
`next_cursor` can be supplied as `after_key` for the next page. Set
`resource_key` and `detail=true` for the current version's field or metric contract.
Event version history remains available through `event_definitions`.

Event resources require diary read permission; system metrics and raw-source
status require health read permission; tracker metrics require diary read permission.
Model access also respects onboarding, scenario-pack selection and existing
per-tracker sharing consent. A sensitive tracker or its mapped metric is hidden
until both the model and any external channel may receive its schema. Availability
for such a tracker or metric is withheld until those destinations may receive facts.

Availability counts refer to the currently selected immutable version and do not
establish complete coverage. Canonical observations and measurements are counted
separately; an empty current-version count does not exclude legacy data under other
versions. Source labels are generic and do not expose account or device identifiers.
Event resources advertise read operations only. Raw plugin records are represented
by a generic `source.raw_only` resource with no query operations: they are not yet
normalized into the agent's canonical data.
