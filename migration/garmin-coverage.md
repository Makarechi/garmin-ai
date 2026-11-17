# Garmin Personal API and legacy capability coverage (R10)

The Python reference is `ee3a14a48a6ad30c1085951bac2f53c9088c2450` with
`python-garminconnect` at `5511c729b4f92713c6b6bfdd5f8d1ebb4363f4cd`. The baseline reader
allows 27 registered requests plus activity search and original download. Its stable
account binding comes from a separate authenticated legacy profile request.
[Baseline reader](../src/garmin_ai/garmin.py)
[Capability registry](../src/garmin_ai/garmin_contract.py)
[Pinned SDK](https://github.com/cyberjunky/python-garminconnect/tree/5511c729b4f92713c6b6bfdd5f8d1ebb4363f4cd)

Garmin's public Personal OpenAPI v1.0.0 contained 26 read routes and 109 schemas on
2026-11-17. The OpenAPI document was embedded in the Swagger page's script. The
script SHA-256 was `4ba5d8fcafdc877b78228285048cb67c43c5af40a4efb66f91f8032ca5861eea`.
The official Python example was inspected and tested at
`979e6e9fc58bcbf968611509b634ba734d6f8cdf`. Orbit discovery returned third-party
integration endpoints. Garmin's published specification and official example supplied
the contract below.
[Garmin Personal Swagger](https://developer.garmin.com/gc-developer-program/personal/swagger-docs/)
[Garmin's example client](https://github.com/garmin/connect-personal-api-python/tree/979e6e9fc58bcbf968611509b634ba734d6f8cdf)

## Field comparison

The Personal column gives the published route or field where available. R11 can archive
those responses under Personal API provenance. R12 retains coverage for the full legacy
table. R13 will test field equivalence, raw source ownership and calendar/time semantics
before enabling overlapping projections.

| Legacy capability | Personal route and data | Baseline fields and remaining work |
| --- | --- | --- |
| `daily` | `/usersummary-service/usersummary/daily` exposes the flat daily summary. The v2 range form nests the same families under `data`. | Flat fields include `totalSteps`, `activeKilocalories`, `restingHeartRate`, `averageStressLevel`, `maxStressLevel`, four daily Body Battery values, and moderate/vigorous intensity minutes. Prefer the flat form for the first comparison. Apply the existing weighted intensity rule. |
| `steps` | `/usersummary-service/stats/steps/daily/{startDate}/{endDate}` gives `totalSteps`, `stepGoal` and `totalDistance` for each day. | Legacy uses intraday `startGMT` and `steps` buckets. Retain that request for the time series. |
| `heart_rate` | `/wellness-service/wellness/dailyHeartRate?date=…` exposes `heartRateValues`, descriptors, `restingHeartRate` and date bounds. | Compare the timestamp/value array, sampling gaps, resting fallback, and device-dependent coverage. |
| `sleep` | `/sleep-service/sleep/dailySleep?date=…` returns `DailySleepDTO` directly. | Legacy projects the nested `dailySleepDTO`. Map seconds by stage, `sleepScores.overall.value`, and UTC interval edges. Verify the timestamp representation because the schema marks a date-time string and its description references milliseconds. Keep the complete legacy response for fields outside that DTO. |
| `hrv` | `/hrv-service/hrv/{calendarDate}` exposes `hrvSummary` and `hrvReadings`. | Compare `lastNightAvg`, `weeklyAvg`, `balancedLow`, `balancedUpper`, `status`, and the `readingTimeGMT` plus `hrvValue` points. |
| `stress` | `/usersummary-service/stats/stress/daily/{startDate}/{endDate}` gives a calendar day and aggregate values. | Legacy supplies `stressValuesArray` and the dense Body Battery points with descriptor offsets. The detailed stress series still uses the legacy request. |
| `body_battery` | `/wellness-service/wellness/bodyBattery/reports/daily` documents `date`, `charged`, `drained`, value descriptors and raw series. | Compare charge/drain and validate descriptor indices, timestamp units and series cadence. The legacy stress response still owns the established dense projection. |
| `body_battery_events` | Body Battery reports include `bodyBatteryActivityEvent`. The v2 daily summary also exposes activity events. | Verify identities, bounds, event types and coverage against the separate legacy event payload. Keep both response families distinct. |
| `respiration` | Daily summaries and sleep expose respiration statistics. | Keep `respirationValuesArray` timestamps and values from the legacy endpoint. |
| `spo2` | `/wellness-service/wellness/daily/spo2acclimation/{calendarDate}` exposes `spO2HourlyAverages`, `spO2SingleValues`, descriptors and sleep summaries. | Compare hourly points and validate the existing fallback from `spO2ValuesArray` before any substitution. |
| `readiness` | A matching training-readiness route was absent from the published specification. | Keep dated score, recovery duration, change phrase and source timestamp in the legacy path. |
| `training_status` | A matching training-status route was absent from the published specification. | Preserve the legacy raw response, including unrecognized fields. |
| `max_metrics` | The official Python client contains a daily VO₂ max wrapper outside this Swagger snapshot. | Keep the legacy sport/date max-metric payload and projected values. Validate any additional Personal route as a separate capability. |
| `endurance` | A matching endurance-score route was absent from the published specification. | Preserve the legacy payload and its reporting window. |
| `hill` | A matching hill-score route was absent from the published specification. | Preserve the legacy payload and its reporting window. |
| `hydration` | `/usersummary-service/stats/hydration/daily/{startDate}/{endDate}` exposes date and `valueInML`, plus goal, intake and sweat loss. | Map the day's `valueInML` explicitly and retain the exact archive body. |
| `body_composition` | `/weight-service/weight/all/{startDate}/{endDate}` and the `range` variant expose time, weight, fat, water, muscle and bone fields. | Check units, paging/range semantics and inclusion of individual measurements versus daily summaries. |
| `intensity` | The flat daily DTO and activity summaries expose moderate and vigorous minutes. | Keep the standalone legacy payload and use equivalent daily totals only where the effective day and overlap are established. |
| `resting_hr` | Flat daily summary and daily heart-rate DTO expose `restingHeartRate` and the seven-day average. | Keep the dedicated legacy value history pending comparison of statistics and calendar-day semantics. |
| `all_day_events` | `/wellnessactivity-service/activity/summary/range/{fromDate}/{untilDate}` documents identified wellness events and UTC edges. | The legacy all-day response is archived for schema inspection. Keep the two event sets distinct. |
| `devices` | A matching owned-device inventory route was absent from the published specification. Some activity summaries include device metadata. | Keep the legacy inventory for device history and ownership context. |
| `activities` | `/activitylist-service/activities/search/activities` documents start/limit, dates and activity types. | Compare activity IDs, paging and lookback boundaries, start time, type and names. |
| `activity` | `/activity-service/activity/{activityId}` provides `summaryDTO`, `activityTypeDTO`, `timeZoneUnitDTO`, identifiers and `splitSummaries`. | Compare summary metrics, optional fields, timezone handling and projection provenance. |
| `activity_details` | The activity summary and original FIT expose separate partial data. | Preserve the legacy detailed metrics and route independently of the FIT projection. |
| `activity_splits` | The activity summary includes `splitSummaries`. | Compare lap boundaries, timestamps and units. Keep the legacy splits payload and its identity. |
| `activity_typed_splits` | A matching typed-splits route was absent from the published specification. | Preserve the legacy typed splits and their fields. |
| `activity_zones` | `/biometric-service/heartRateZones/v2` gives account-level thresholds; search activity rows include time-in-zone summaries. | Retain the per-activity legacy zone breakdown and compare any overlap by activity and sport. |
| `activity_weather` | A matching activity-weather route was absent from the published specification. | Preserve the legacy weather payload. |
| `activity_fit` | `/download-service/files/activity/{activityId}` documents original ZIP bytes. | Stream the body to the archive, preserve byte identity and member order, and verify the original download against the legacy account/device fixture. |

## Authentication and account continuity

Personal API access starts with an owner-created key. Exchange it at the published token
endpoint with the documented `ApiKey` authentication scheme. Use the returned bearer
token for data reads, tracking its expiry and re-exchanging the key as needed. The
official example treats the response as a JWT string and reads its expiry. Garmin gives
the access token a maximum lifetime of 24 hours and documents re-exchange when it
expires. Bind the native flow to an explicit credential source and use one concurrent
token exchange per source instance.
[Garmin authentication guide](https://developer.garmin.com/gc-developer-program/personal/personal-api/)
[Garmin FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq/)
[Official token implementation](https://github.com/garmin/connect-personal-api-python/blob/979e6e9fc58bcbf968611509b634ba734d6f8cdf/src/garmin_personal_api/auth.py)

The baseline binds to the positive integer from authenticated legacy
`/userprofile-service/socialProfile` under the `socialProfile.profileId:v1` namespace.
Personal profile settings expose presentation settings such as `displayName` and
`timeZone`. Other documented responses contain `userProfileId`, `userProfilePk` or
`userProfilePK`. Validate namespace equivalence on an authorized paired account
before attaching these Personal data to retained legacy history. A display name,
credential value or token subject alone is insufficient evidence. Preserve the
stored legacy fingerprint and owner UUID. Ambiguous or mismatched identities leave
ingestion blocked for that source instance.
[Archived identity contract](hash-contracts.md)

R12 needs native equivalents of the legacy credential login, optional MFA
continuation, restart/session restore, refresh and authenticated identity check.
The web restore workflow binds an expiring MFA continuation to the authorized
owner session and confirms the existing fingerprint before replacing token files.
A temporary bridge can run the currently pinned Python flow as a named M1 dependency.
M2 requires the equivalent built-in native flow.
[Baseline web restore](../src/garmin_ai/garmin_web_auth.py)

## Transport choice and evidence

Use a small native client for R11 along the published path. Treat
`garmin-rs` 0.1.0 as an endpoint and typed-model reference. Its crate-internal read
helper buffers complete success and error bodies, its token exchange buffers the
whole response and simultaneous cache misses can exchange independently. These details
affect the archive, input bounds and request coordination. The native client needs
streaming ZIP bytes, bounded status/error processing, strict redirect and authorization
handling, one exchange at a time, explicit stale-token retry, pacing, and distinct
empty, unsupported and failed results.
[garmin-rs 0.1.0 client source](https://docs.rs/crate/garmin-rs/0.1.0/source/src/client.rs)

Ninety-one offline tests from Garmin's official example passed against its local HTTP
doubles, including authentication and data shapes. Schema inventory and those tests
establish documented and synthetic coverage. Live date range, device availability,
account continuity, native legacy login/MFA and FIT retrieval still need the named,
authorized account and scoped checks in their respective stages.
