# Garmin endpoint investigation

Inspected [python-garminconnect commit 5511c729](https://github.com/cyberjunky/python-garminconnect/tree/5511c729b4f92713c6b6bfdd5f8d1ebb4363f4cd), reported version 0.3.12.
The exact commit is recorded in standard package metadata and `uv.lock`; the configured package
index did not provide that version during investigation.

The table describes adapter capabilities, not availability for any particular account. Response
availability, counts, identifying keys, raw reports and FIT files belong only in private local reports.
A nonempty response can contain metadata or null values without observed health measurements.

| Domain | Method | Scope | Fields/granularity inspected | Current handling |
|---|---|---|---|---|
| daily | `get_stats` | day | totalSteps, restingHeartRate, stress and Body Battery summaries | archive + canonical values |
| steps | `get_steps_data` | day | startGMT, steps; intraday buckets | archive + canonical values |
| heart_rate | `get_heart_rates` | day | heartRateValues [timestamp, bpm]; restingHeartRate | archive + canonical values |
| sleep | `get_sleep_data` | day | dailySleepDTO stages, timestamps, sleepScores | archive + canonical values |
| hrv | `get_hrv_data` | day | hrvSummary averages/baseline/status; hrvReadings | archive + canonical values |
| stress | `get_stress_data` | day | stressValuesArray; descriptor-indexed bodyBatteryValuesArray | archive + canonical values |
| body_battery | `get_body_battery` | day | charged, drained; sparse level values archived | archive + canonical values |
| body_battery_events | `get_body_battery_events` | day | Source-specific document; retained without an unverified numeric parser | archive only |
| respiration | `get_respiration_data` | day | respirationValuesArray | archive + canonical values |
| spo2 | `get_spo2_data` | day | spO2HourlyAverages; spO2ValuesArray fallback | archive + canonical values |
| readiness | `get_training_readiness` | day | calendarDate, score, recoveryTime and REACHED_ZERO marker | archive + canonical values |
| training_status | `get_training_status` | day | Source-specific document; retained without an unverified numeric parser | archive only |
| max_metrics | `get_max_metrics` | day | generic.vo2MaxPreciseValue | archive + canonical values |
| endurance | `get_endurance_score` | day | Source-specific document; retained without an unverified numeric parser | archive only |
| hill | `get_hill_score` | day | Source-specific document; retained without an unverified numeric parser | archive only |
| hydration | `get_hydration_data` | day | valueInML; daily aggregate | archive + canonical values |
| body_composition | `get_body_composition` | day | Source-specific document; retained without an unverified numeric parser | archive only |
| intensity | `get_intensity_minutes_data` | day | Source-specific document; retained without an unverified numeric parser | archive only |
| resting_hr | `get_rhr_day` | day | Source-specific document; retained without an unverified numeric parser | archive only |
| all_day_events | `get_all_day_events` | day | Source-specific document; retained without an unverified numeric parser | archive only |
| devices | `get_devices` | global | Source-specific document; retained without an unverified numeric parser | archive only |
| activity | `get_activity` | activity | activityId, GMT start, duration, HR, distance, effects, load | archive + canonical values |
| activity_details | `get_activity_details` | activity | structured detail document and samples | archive + structured activity parts |
| activity_splits | `get_activity_splits` | activity | structured lap/split documents | archive + structured activity parts |
| activity_typed_splits | `get_activity_typed_splits` | activity | structured typed split documents | archive + structured activity parts |
| activity_zones | `get_activity_hr_in_timezones` | activity | structured HR zone documents | archive + structured activity parts |
| activity_weather | `get_activity_weather` | activity | structured recorded activity weather | archive + structured activity parts |
| Activities | `get_activities` | paginated | Activity summaries | archive + canonical activities |
| Original FIT | `download_activity(..., ORIGINAL)` | activity | records, laps, sessions, events | archive ZIP/FIT + parsed parts |

## Findings and boundaries

The stress parser reads descriptor-indexed Body Battery levels; the separate Body Battery
endpoint provides charged/drained summaries. The SpO2 parser supports hourly averages and a
values-array fallback. Missing and negative sentinel values are filtered, not replaced with zero.
An empty upstream response does not erase retained history.

Archive-only documents are preserved for future parser expansion without claiming unsupported
measurements. Intensity and resting-HR summaries are normalized from the daily response; their
secondary endpoint documents remain archived. A bounded probe does not prove historical coverage.
The manual probe is bounded to 31 dates, 100 activity summaries and five detail/FIT exports.
Scheduled synchronization paginates activity summaries and refreshes registered global endpoints.
Nightly reconciliation covers seven days, or 30 days on Mondays. Garmin can revise older responses;
canonical writes retain request ordering and raw source references.

No continuous raw accelerometer/gyroscope feed is exposed by this adapter. No such stream is
assumed for activity recognition. Garmin labels, confirmed diary intervals and unknown gaps stay
distinct. Stored activity weather is available as activity context; there is no external weather
or calendar connector in the current project.

## Comparison with MCP projects

[Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) documents health, activity details/files
and upstream mutation tools. [tamcore/garmin-mcp](https://github.com/tamcore/garmin-mcp) separates
read/write/destructive tools and documents compatibility. Their coverage helped audit this adapter;
it is not account-availability evidence. This project's Garmin adapter is read-only, diary mutations
target its own database, and its MCP queries locally retained canonical history during Garmin outages.

Run `uv run garmin-ai inventory`, `uv run garmin-ai probe --start YYYY-MM-DD --end YYYY-MM-DD`,
and `uv run garmin-ai import-probe` locally. Structural reports can still contain identifying keys;
they are not safe to publish automatically.
