# Legacy Garmin health normalization

The native parser produces daily fields, device samples and observations with
source time kept separate from interpretation. It covers daily summaries,
sleep, HRV, readiness, body battery, heart rate, stress, respiration, oxygen
saturation, steps, hydration and maximum metrics.

Missing and invalid values are distinct from valid zero values. The parser
keeps sleep interval bounds, readiness observation order and whether its
timestamp had an explicit offset. Stress identifies the battery value through
the upstream descriptor. Older oxygen-saturation responses use the alternate
series only when the preferred key is absent.

The synthetic comparison executes the pinned Python normalizer with captured
normalization effects. The Rust check matches all 106 cases, including UTC
offsets, ambiguous local times, fractional epoch timestamps, sentinels,
invalid ranges, optional fields and empty responses. Durable publication and
replay are separate migration steps.
