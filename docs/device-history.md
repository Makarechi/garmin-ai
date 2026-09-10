# Device evidence (GA-12, first phase)

`device_history` exposes activity-scoped device and zone/settings snapshots from stored FIT messages, using the pinned FIT profile field names. It preserves message sequence and activity identity instead of collapsing sensors sharing a timestamp. It does not infer optical/chest-strap attribution from device presence.

Only an explicit field allowlist is returned: serial numbers, ANT identifiers, descriptors, product names, and raw/developer fields are excluded. This applies to the new tool; existing raw activity detail/export surfaces retain their separately authorized behavior. Missing metadata is unavailable, not zero. A mismatching current/parsed FIT archive marks evidence stale. Stored zone settings are never retroactively applied to other activities.

Bounded to 366 days, 200 activities, 200 metadata messages per activity, and 40 KB output. Truncation is explicit. This is a first evidence surface: durable cross-activity inventory identity, change-point annotations in comparisons, confirmed sensor-to-sample attribution, body composition, and separate respiration/SpO2 context remain later phases.
