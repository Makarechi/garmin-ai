# Garmin endpoint investigation

Inspected [python-garminconnect commit 5511c729](https://github.com/cyberjunky/python-garminconnect/tree/5511c729b4f92713c6b6bfdd5f8d1ebb4363f4cd), reported version 0.3.12.
The exact commit is recorded in standard package metadata and `uv.lock`; the configured package
index did not provide that version during investigation.

The account was authenticated and 14 dates requested on 2026-09-07. The table records **response
availability**, not complete health coverage: many nonempty responses contain only metadata or
null values. Only three dates produced usable daily summaries in the initial import. Actual scalar
health values, identifying keys, raw reports and FIT files are not committed.

| Domain | Method | Scope | Fields/granularity inspected | Current handling | Account response evidence |
|---|---|---|---|---|---|
| daily | `get_stats` | day | totalSteps, restingHeartRate, stress and Body Battery summaries | archive + canonical values | 14/14 nonempty; 0 errors |
| steps | `get_steps_data` | day | startGMT, steps; intraday buckets | archive + canonical values | 3/14 nonempty; 0 errors |
| heart_rate | `get_heart_rates` | day | heartRateValues [timestamp, bpm]; restingHeartRate | archive + canonical values | 14/14 nonempty; 0 errors |
| sleep | `get_sleep_data` | day | dailySleepDTO stages, timestamps, sleepScores | archive + canonical values | 14/14 nonempty; 0 errors |
| hrv | `get_hrv_data` | day | hrvSummary averages/baseline/status; hrvReadings | archive + canonical values | 2/14 nonempty; 0 errors |
| stress | `get_stress_data` | day | stressValuesArray; descriptor-indexed bodyBatteryValuesArray | archive + canonical values | 14/14 nonempty; 0 errors |
| body_battery | `get_body_battery` | day | charged, drained; sparse level values archived | archive + canonical values | 14/14 nonempty; 0 errors |
| body_battery_events | `get_body_battery_events` | day | Source-specific document; retained without an unverified numeric parser | archive only | 2/14 nonempty; 0 errors |
| respiration | `get_respiration_data` | day | respirationValuesArray | archive + canonical values | 14/14 nonempty; 0 errors |
| spo2 | `get_spo2_data` | day | spO2HourlyAverages on this account; spO2ValuesArray fallback | archive + canonical values | 14/14 nonempty; 0 errors |
| readiness | `get_training_readiness` | day | calendarDate, score, recoveryTime and REACHED_ZERO marker | archive + canonical values | 3/14 nonempty; 0 errors |
| training_status | `get_training_status` | day | Source-specific document; retained without an unverified numeric parser | archive only | 14/14 nonempty; 0 errors |
| max_metrics | `get_max_metrics` | day | generic.vo2MaxPreciseValue | archive + canonical values | 1/14 nonempty; 0 errors |
| endurance | `get_endurance_score` | day | Source-specific document; retained without an unverified numeric parser | archive only | 3/14 nonempty; 0 errors |
| hill | `get_hill_score` | day | Source-specific document; retained without an unverified numeric parser | archive only | 3/14 nonempty; 0 errors |
| hydration | `get_hydration_data` | day | valueInML; daily aggregate | archive + canonical values | 14/14 nonempty; 0 errors |
| body_composition | `get_body_composition` | day | Source-specific document; retained without an unverified numeric parser | archive only | 14/14 nonempty; 0 errors |
| intensity | `get_intensity_minutes_data` | day | Source-specific document; retained without an unverified numeric parser | archive only | 14/14 nonempty; 0 errors |
| resting_hr | `get_rhr_day` | day | Source-specific document; retained without an unverified numeric parser | archive only | 14/14 nonempty; 0 errors |
| all_day_events | `get_all_day_events` | day | Source-specific document; retained without an unverified numeric parser | archive only | 2/14 nonempty; 0 errors |
| devices | `get_devices` | global | Source-specific document; retained without an unverified numeric parser | archive only | 1/1 nonempty; 0 errors |
| activity | `get_activity` | activity | activityId, GMT start, duration, HR, distance, effects, load | archive + canonical values | 2/2 nonempty; 0 errors |
| activity_details | `get_activity_details` | activity | structured detail document and samples | archive + structured activity parts | 2/2 nonempty; 0 errors |
| activity_splits | `get_activity_splits` | activity | structured lap/split documents | archive + structured activity parts | 2/2 nonempty; 0 errors |
| activity_typed_splits | `get_activity_typed_splits` | activity | structured typed split documents | archive + structured activity parts | 2/2 nonempty; 0 errors |
| activity_zones | `get_activity_hr_in_timezones` | activity | structured HR zone documents | archive + structured activity parts | 2/2 nonempty; 0 errors |
| activity_weather | `get_activity_weather` | activity | structured recorded activity weather | archive + structured activity parts | 2/2 nonempty; 0 errors |
| Activities | `get_activities` | paginated | Activity summaries | archive + canonical activities | Two activities returned |
| Original FIT | `download_activity(..., ORIGINAL)` | activity | records, laps, sessions, events | archive ZIP/FIT + parsed parts | Two activity exports parsed |

## Findings and boundaries

The stress response supplies the authoritative dense Body Battery stream. Its descriptor places
level at index 2 on this account; the separate Body Battery endpoint is sparse and is used for
charged/drained summaries. SpO2 is hourly on this account. Missing and negative sentinel values
are filtered, not replaced with zero. An empty upstream response does not erase retained history.

Training-status documents were present but their current status/load objects were null. Endurance
and hill documents similarly contained metadata without score observations. Body composition
contained no weight observations. These documents are archived for future parser expansion; no
unsupported historical score is claimed. Intensity and resting-HR summaries are normalized from
the daily response; their secondary endpoint documents remain archived.

Date-scoped endpoints were probed over 14 dates; this does not prove longer historical support.
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
