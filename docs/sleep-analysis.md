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
timestamp to the owner's timezone, rendering the date, time through seconds
(`HH:MM:SS`) and UTC offset (including DST). Sleep start/end labels come from the
program's known `main_interval` paths.
The answer identifies this as Garmin's recorded start, not a proven physiological
instant of falling asleep. Synthetic tests cover either side of midnight, DST,
wrong paths, uncited/failed evidence and completion with one daily sleep read.

Natural-language questions use the configured model to interpret the user's language
and plan reads from tool descriptions and typed schemas. No question wording or
language activates an application branch. The first analytical step receives the
question, bounded conversational topics and tool contracts before global freshness
or historical measurements are loaded. A validated one-day timing read uses the
independently verified point-read path below. The model then receives that small
result and chooses the timestamp fields to cite; the application verifies them.
Other plans continue
through the ordinary analytical loop without repeating the first model request.
Dates, including previous nights and explicit dates, come from the validated plan;
they are not replaced with today's date by a phrase recognizer.

Telegram and web chat follow the same semantic model path. If the model is unavailable,
free text is unavailable consistently across languages; explicit commands and forms
remain available. Answers follow the model's response language rather than a Russian-only
instruction. The timestamp renderer retains source-checked values and seconds, with
Russian labels for Russian output and English labels as the fallback. This does not
claim complete localization of every existing diary button or service notice.

Timing-only reads require one wake date and the separate nap policy. A primary-key
probe obtains the candidate source identity; one joined statement then reads the
interval, raw source, ingestion watermark and failed-parser metadata from the same
committed database snapshot. All joined records use primary-key filters. An identity
change between the probe and joined read supplies no timestamp; revisions cannot
be combined across independent queries. Unrelated replay writers do not have to
finish before this read.

The interval must reference a normalized Garmin sleep payload at the current parser
version, match the current ingestion pointer/hash and match the payload's exact
start/end and wake date when supplied. An older, failed, superseded or inconsistent
source supplies no time. Missing requested nights never fall back to an older night.
The normal sharing/category checks still apply; general analysis keeps its global
replay guard.

Question planning reads the current conversation epoch and goal revision without
requiring history promotion. Delivered pending context can be read without a write;
prior answers are excluded from model prompts and source facts are re-queried.
Interpretation reads its bounded diary context without background reconciliation
or history promotion, so unrelated replay does not block semantic understanding. Retaining
the answer in conversation memory is best effort: it happens only when the normal
write fence is immediately available. Busy replay does not prevent a fact reply or
force a shared-to-exclusive lock upgrade.

Queued Telegram timing answers recheck current permissions and the exact timing
evidence hash under the existing channel-consent and delivery-policy fences.
The current owner timezone must still match the prepared timing projection.
Forgetting also takes an exclusive context-delivery fence, while timing sends
hold its shared side through publication; goal edits keep their existing revision
fence. Changed timing evidence, revoked access, forgetting and changed
goals suppress the old answer. Unrelated replay-generation changes with identical
verified timing do not. Timing values describe the committed snapshot checked for
delivery; later source updates are read by subsequent requests. General analysis
retains its existing replay-generation/backlog delivery guard.

Timing publication in Telegram never queues on the channel-consent, settings,
conversation or goal fences: a busy fence defers delivery for a retry, keeping the
async runtime responsive. Other reply kinds retain their existing fence behavior.
Web timing replies persist a reference to their projection, conversation epoch and
goal revision in the existing outbox evidence references. The first response,
duplicate request, queued/read history and read receipt recheck this proof and
current health-sharing access. Web response streaming holds the consent, policy,
context and goal fences through body publication. Missing proof, corrected data,
changed timezone or revoked access hides the old reply.
Timing history and read receipts also avoid waiting for the replay write fence.
Mixed history defers initiatives when that fence is busy, while exposing verified
timing replies; receipt promotion into conversation memory waits for a later turn
if the fence is unavailable. Consent, policy, context and goal checks still apply.

Background check-ins also skip busy replay/policy fences rather than waiting on
the async loop. Delivery bookkeeping runs in a worker after publication fences
are released, so another diary writer cannot stall polling or job leases.

Unavailable and unverified timing results also return to the model before completion.
A missing night does not implicitly finish a compound question; the model may request
other tools. Only a completed point answer receives the trusted unavailable template.
Response language tags accept script and numeric-region subtags, and model-detected
safety responses retain the detected language through Telegram and web rendering.

Gemini transport retries are disabled so the application's provider gate controls
fallback, cooldown and deadlines. The pinned Interactions adapter maps the public
attempt count to retries, so its generated retry counter is also set to zero.
Actual-SDK tests use a synthetic HTTP transport to verify one attempt and no SDK
sleep for quota and temporary server errors, including long Retry-After values.
