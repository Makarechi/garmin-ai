# Garmin endpoint investigation

Upstream inspected: [python-garminconnect commit 5511c729](https://github.com/cyberjunky/python-garminconnect/tree/5511c729b4f92713c6b6bfdd5f8d1ebb4363f4cd), version 0.3.12, on 2026-09-07.
This version was not available on the configured package index, so the dependency
is pinned to the exact inspected commit and recorded in `uv.lock`.

**Account evidence is pending authentication.** Every row below is supported by
upstream source/signature inspection, not a claim that this account returns data.
Fields, sampling intervals, historical depth, and endpoint stability remain
unverified until `garmin-ai probe` runs against the account. The probe stores raw
payloads and a structural report locally; neither is automatically committed.

| Domain | Method | Request scope | Planned handling | Account evidence |
|---|---|---|---|---|
| daily | `get_stats` | day | archive + normalize | Pending |
| steps | `get_steps_data` | day | archive + normalize | Pending |
| heart_rate | `get_heart_rates` | day | archive + normalize | Pending |
| sleep | `get_sleep_data` | day | archive + normalize | Pending |
| hrv | `get_hrv_data` | day | archive + normalize | Pending |
| stress | `get_stress_data` | day | archive + normalize | Pending |
| body_battery | `get_body_battery` | day | archive + normalize | Pending |
| body_battery_events | `get_body_battery_events` | day | archive + normalize | Pending |
| respiration | `get_respiration_data` | day | archive + normalize | Pending |
| spo2 | `get_spo2_data` | day | archive + normalize | Pending |
| readiness | `get_training_readiness` | day | archive + normalize | Pending |
| training_status | `get_training_status` | day | archive + normalize | Pending |
| max_metrics | `get_max_metrics` | day | archive + normalize | Pending |
| endurance | `get_endurance_score` | day | archive + normalize | Pending |
| hill | `get_hill_score` | day | archive + normalize | Pending |
| hydration | `get_hydration_data` | day | archive + normalize | Pending |
| body_composition | `get_body_composition` | day | archive + normalize | Pending |
| intensity | `get_intensity_minutes_data` | day | archive + normalize | Pending |
| resting_hr | `get_rhr_day` | day | archive + normalize | Pending |
| all_day_events | `get_all_day_events` | day | archive; inspect schema | Pending |
| devices | `get_devices` | global | archive | Pending |
| activity | `get_activity` | activity | archive + normalize | Pending |
| activity_details | `get_activity_details` | activity | archive + normalize | Pending |
| activity_splits | `get_activity_splits` | activity | archive + normalize | Pending |
| activity_typed_splits | `get_activity_typed_splits` | activity | archive + normalize | Pending |
| activity_zones | `get_activity_hr_in_timezones` | activity | archive + normalize | Pending |
| activity_weather | `get_activity_weather` | activity | archive + normalize | Pending |
| Activity list | `get_activities` | Paginated | Archive + normalize | Pending |
| Original FIT | `download_activity(..., ORIGINAL)` | Activity | Archive ZIP; extract and parse FIT | Pending |

## Reconnaissance limits

The probe requests 14 days by default (maximum 31), the 100 most recent activity
summaries, and details/original exports for at most five recent activities.
Its bounded activity sample is not a historical import. Empty data is recorded
separately from endpoint errors. Authentication or repeated connection failures
stop the probe to avoid repeatedly hitting Garmin. Error messages and response
values are excluded from console output. Structural reports remain private:
unknown upstream dictionary keys may themselves contain identifiers.

Garmin Connect does not provide a continuous raw accelerometer/gyroscope stream
through this integration. No such data is assumed by this project.

## Comparison with existing MCP projects

[Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) documents health,
activity detail/file retrieval and analysis, along with upstream mutation tools
for workouts, nutrition, and other account features. Its read-side categories
help identify useful ingestion coverage. Garmin mutations are outside our
read-only ingestion boundary; our diary writes target our own database.

[tamcore/garmin-mcp](https://github.com/tamcore/garmin-mcp) separates read, write,
and destructive tools and documents a pinned compatibility manifest. Its
coverage is a useful audit reference, not evidence of availability on this
account. Both projects expose upstream operations; our final MCP must query
locally retained canonical data so that historical analysis survives outages.

## Verify locally

```sh
uv sync --locked
uv run garmin-ai inventory
uv run garmin-ai login
uv run garmin-ai probe --start 2026-08-25 --end 2026-09-07
```

Run login yourself in a local terminal: email, password, and MFA code are
interactive and never arguments stored in shell history. Tokens remain under
`tokens/garmin` with restricted permissions. Probe output stays under `data/`.
