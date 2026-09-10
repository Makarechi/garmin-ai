# Metric semantics (GA-05)

The shared catalog defines canonical units, kind, safe range, aggregation,
interval semantics, reset policy, quality and provenance. `metric_series`
returns its contract to HTTP, MCP and agent callers. `health_snapshot` also
exposes contracts and explicitly separates Garmin hydration from manual water.

Steps are source-interval increments: 10 + 20 yields 30, and mean is null.
We select increments by interval start and do not invent proportional step
counts for a partially selected source interval. No supported input is declared
a cumulative counter; a future cumulative adapter must define reset/delta
semantics before registration.

Gauges use time-weighted left-hold values between adjacent valid samples from
the same source, capped at each metric's maximum gap. Intervals are clipped to
the requested range and split at bucket boundaries. No fill after the last point
or across gaps. Mean/value is null below 80% bucket coverage; covered duration,
coverage, extrema and sample count remain visible. These are engineering quality
policies. Different providers are separate rows, never summed or interleaved.
Buckets align to UTC Unix-epoch multiples of the requested duration. Requests
over 100000 source samples fail explicitly and require a shorter range.

Garmin `valueInML` is a daily hydration summary in HealthDay. It is never a
midnight drink. Migration adds the daily field, preserving archived provenance;
old fabricated midnight rows remain stored but are excluded from all supported
intraday metric queries by the catalog. Replaying hydration with parser version 7
populates daily totals. Manual diary water is not added to Garmin totals without
an explicit link/dedup contract. Sources may therefore disagree visibly.

Synthetic contracts cover milliseconds/seconds, minutes/hours, meters/kilometers,
speed/pace, sentinels, unknown units, sparse gauges and bucket boundaries. This
slice does not infer timestamped drinks from undocumented hydration payloads.
