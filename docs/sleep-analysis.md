# Sleep analysis

The shared `analysis_sleep` HTTP/MCP/assistant tool exposes already normalized main
sleep duration, deep/REM/light/awake durations and Sleep Score with per-field raw
references and explicit unavailable fields. Sleep Score is a device estimate; no
subjective score is substituted for it. Stage fractions are returned only when the
reported stage sum agrees with reported sleep duration within 60 seconds.

Main sleep is identified by Garmin's source calendar date. Confirmed diary naps are
separate episodes assigned to their local start date. The default `separate` policy
uses only main sleep for the duration summary. `include_confirmed` adds the union of
complete, nonoverlapping reported naps. Incomplete naps or overlap with main sleep
make that day's combined value unavailable. No nap report means unknown, not zero.

Timing regularity is circular standard deviation of main-session local bed/wake times
in minutes, with at least three timed sessions. It describes wall-clock consistency,
not recovery. Duration values are seconds; score values are scores; stage fractions
are dimensionless. No migraine prediction or recovery claim is made.

The tool accepts 31 days and 200 naps at most. Summary statistics use the full window.
Day details stay within the model result budget and expose `rows_truncated`/`next_day`;
continue with that day as the next request's start. Each day's nap details show at most
10 records with total/truncation metadata. Both read:health and read:diary are required.

| Source fields | Canonical | Queryable | Used in analysis |
| --- | --- | --- | --- |
| dailySleepDTO sleepTimeSeconds | HealthDay.sleep_seconds | analysis_sleep values + source_refs | documented duration mean |
| deep/rem/lightSleepSeconds | HealthDay stage durations | analysis_sleep values + source_refs | coherent stage fractions |
| awakeSleepSeconds | HealthDay.awake_seconds | analysis_sleep values + source_refs | available duration alongside sleep; not counted as sleep |
| sleepScores.overall.value | HealthDay.sleep_score | analysis_sleep values + source_refs | explicitly separate device score |
| sleepStart/EndTimestampGMT | TimelineInterval sleep session | analysis_sleep main_interval | circular timing regularity |
| confirmed diary nap interval | Event | analysis_sleep naps | selected nap-policy duration |

This GA-11 slice uses existing confirmed contracts. Raw-only Garmin nap/stage-interval,
Body Battery event, training-status and load-component parsers still require validated
payload contracts; this tool does not invent those fields or mark the entire GA-11
backlog item complete. Existing source revisions/corrections are read on each request.
