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

Bedtime and wake-time questions use `analysis_sleep` for the requested wake date,
rather than loading the general timeline or intraday measurements. “Today” selects
today's Garmin summary; its main interval may start yesterday evening or after
midnight today. An explicitly named evening/night selects the following wake date.
Missing timestamps do not authorize substituting an older night or widening the
query to the full history.

The assistant returns `timestamp_claims` citing the exact successful evidence field
and original ISO timestamp. The application checks that field and converts the
timestamp to the owner's timezone, rendering the date, time and UTC offset (including
DST). Sleep start/end labels come from the program's known `main_interval` paths.
The answer identifies this as Garmin's recorded start, not a proven physiological
instant of falling asleep. Synthetic tests cover either side of midnight, DST,
wrong paths, uncited/failed evidence and completion with one daily sleep read.

Simple Russian questions about today's bedtime or wake time use one direct
`analysis_sleep` read with `timing_only=true` after ordinary intent/safety
classification. They do not make additional model requests or scan unrelated
freshness/history. The recorded start may still fall on yesterday's date.

Timing-only reads require one wake date and the separate nap policy. They acquire
a nonblocking exclusive replay fence up front because the answer may retain
conversation state, avoiding shared-to-exclusive lock upgrades. They enforce the
same sharing/category permissions. If another reader or writer holds the fence,
they return the replay notice immediately instead of waiting behind archive work.
They may read an already current night while unrelated history is being replayed:
the interval must reference a normalized Garmin sleep payload at the current parser
version, match the current ingestion pointer/hash and match the payload's exact
start/end and wake date when supplied. An older, failed, superseded or inconsistent source supplies no time.
The general sleep analysis retains its global replay guard. Missing requested
nights never fall back to an older night. Goal, conversation-forgetting and replay
generation checks also apply before delivering or retaining a direct answer.
Queued Telegram answers retain a hash of this bounded timing evidence. Delivery
rechecks the same night's current source under the shared replay fence rather
than the global archive backlog. Changed evidence or generation suppresses the
queued answer; ordinary analysis keeps its existing delivery replay guard.
