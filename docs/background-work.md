# Background work and idle queue waiting

The service needs to collect new data, publish derived facts, send requested
notifications and maintain its storage even when the owner is not chatting.
An idle queue does not itself require repeated short transactions.

## Execution and scheduling

Jobs and outbox rows remain the durable source of work. A single PostgreSQL
`LISTEN` connection per worker now forwards transactional queue notifications
to the existing independent consumers. This connection has its own one-slot
pool; it does not occupy either slot in the control or work pools.

Consumers subscribe before their first claim, drain available work, then wait
for a retained signal or a deadline. New messages, processing-stage handoffs
and ready replies wake the relevant lanes. Notifications contain fixed topic
names, never messages, health values or credentials.

`NOTIFY` is a hint, not a replacement for the queue. The listener reconciles
durable state at startup, after reconnecting and every 30 seconds. Its deadline
query schedules future `run_at`, `next_attempt_at` and lease expiry times in
the next recovery window. A row whose deadline has already passed can be
blocked by another eligibility condition; it must not produce a `sleep(0)`
loop. Near-future deadlines crossed during notification processing also need
a bounded second check.

The triggers run for changed rows, not every executed SQL statement. Empty
claim updates and pure lease renewals do not signal consumers. Claims register
new lease deadlines without waking every processing lane. Stage handoffs and
terminal transitions signal work that may have become eligible through
ordering or resource fences.

Advisory-lock contention is distinct from an empty queue. In particular,
Telegram lanes sharing the ingress lock retain their short retry path when
another lane holds that lock. Otherwise a lane could consume a wakeup, lose
the lock race and sleep despite a pending message.

## What runs, and why

| Work | Useful trigger | Current behavior after the queue-wait change |
| --- | --- | --- |
| Telegram receive | Incoming Telegram update | Existing 15-second long polling, or webhook ingestion; long polling returns when an update arrives. |
| Telegram acknowledgment, control, local and model lanes | Committed inbound job or stage change | Event/deadline waiting with a 30-second recovery bound, replacing 250-ms empty polls. Fast controls and slow model work remain independent. |
| Ordinary reply delivery | Committed outbox row or retry deadline | Event/deadline waiting; separate notice checks every 2 seconds and delivery recovery every 10 seconds remain. A notice-only tick does not cause an outbox claim. |
| Garmin collection | Scheduled slot or an explicit refresh request | The existing 15-minute collection slots remain. Empty executors wait for work or the next planner pass, replacing one-second queue polls. The planner still reconciles every 30 seconds. |
| Conversation-triggered refresh | First ordinary Telegram message after a pause | A new inbound job also wakes the Garmin planner. Existing 15-minute conversation-gap, 3-minute freshness and deduplication checks remain. It requests current-day `sleep` and `daily`, not an unrestricted full download. |
| Archive replay | Existing repair job, retry or parser-version reconciliation | Empty claims wait for events/deadlines. Planning remains every 30 seconds while an unfinished scan needs progress, or every 15 minutes after completion. |
| Backup and storage check | Daily backup or six-hour capacity-check schedule | Empty executors wait for work or the next 30-second planner pass. Real maintenance work retains its previous pacing and lease renewal. |
| Optional source plugins | Durable scheduled source-poll job | Empty claims wait for events or the existing planner deadline; real external work keeps its previous pacing. |
| Tracker, diary and trend notifications | Enabled reminder schedule or eligible insight | Existing bounded scans remain until their APIs expose the next safe due time. |
| Trend preparation | New usable health history and the six-hour generation slot | Deterministic statistics, not a continuous LLM task. Existing minute checks and busy retry remain. |
| Worker heartbeat | Runtime ownership and liveness | Existing heartbeat and lease rules remain independent of queue waiting. |

The smartwatch's continuous measurements do not imply a continuous stream to
this server. Garmin Connect exposes uploaded data after device synchronization.
The current integration uses `LegacyReader`; adopting a different Garmin push
API is a separate integration change. Official Personal API access is currently
pull-only, while business offerings have push options.

New Garmin data is already normalized and published during successful sync.
Archive replay repairs missing, failed or outdated projections. It is not a
necessary second full pass over every newly downloaded record.

## Next scheduling changes

The queue-wait change removes the most frequent empty claims. It deliberately
does not infer reminder deadlines from incomplete information or silently
delay notifications. The remaining work is to expose scheduling decisions
from the component that already owns them:

1. Return `next_due_at` from Garmin and maintenance planning. Sleep until that
   time, an explicit settings change or a recovery reconciliation, instead of
   reconsidering the whole schedule every 30 seconds.
2. Return the next reminder deadline with its page cursor. This must include
   timezone and DST, quiet hours, snooze, daily budget, pending-question
   expiry and configuration changes. A timer based only on a rule's nominal
   clock time is insufficient.
3. Distinguish completed trend slots, missing data and a busy replay fence.
   The current boolean combines these outcomes; treating every `true` as
   permission to sleep six hours would delay analysis of the first arriving
   health data.
4. Wake service notices on selected storage/integration state changes and
   their own retry/day-boundary deadlines. Broad triggers on every `app_state`
   update would also wake on heartbeat and planner metadata, recreating the
   idle work being removed.
5. Prepare compact day/interval facts only for changed source revisions:
   sleep intervals, stress intervals, daily aggregates and baselines. Generate
   conversational text when asked or under an enabled notification schedule.
   A question about a known date should not wait for unrelated historical
   repair when that date's projection is current.

An external cron process is unnecessary for these changes. The existing job
store already supplies persistence, retries, leases and deduplication. A
single deadline-aware planner can keep these guarantees while spending most
idle time asleep.

## Verification and rollout

The completion checks cover transaction commit versus rollback, no signal from
empty claims or lease renewal, stage and ordering handoffs, delayed retries,
lease expiry, listener reconnection and the empty-check/wait race. A synthetic
Telegram integration test runs multiple idle consumers, checks their database
transaction count and injects a webhook through a separate `Database` to
verify prompt control processing and reply delivery.

### Synthetic comparison, 2026-11-24

The same seven-consumer probe used a three-second warmup and a five-second
measurement window on fresh migrated databases with PostgreSQL 17.12 and
TimescaleDB 2.23.0. The baseline was commit
`78af9d316e137667169b6c992ffcbb85c5f2ddf1`; it used the same probe without the
new listener or Busy-specific assertion.

| Measurement | Baseline | Event-driven consumers |
| --- | ---: | ---: |
| Observed idle interval | 5.021 s | 5.018 s; fresh-cluster repeat 5.072 s |
| Database transactions in that interval | 286 | 10; repeat 13 |
| Approximate transactions per second | 56.96 | 1.99; repeat 2.56 |
| One webhook `/pause` command to synthetic `sendMessage` | 0.971 s | 0.120 s; repeat 1.151 s |

The observed idle transaction rate was about 22–29 times lower in these probes.
Both new-version commands were delivered before the three-second test limit;
these samples do not establish an improvement in command-latency percentiles.
Statistics include the fixture's monitoring queries and are published
asynchronously by PostgreSQL. Startup scans and their short lock-contention
retries are intentionally outside the measured interval. These are short
local measurements, not production CPU savings, latency percentiles or LLM
response times. The regression test uses a generous upper bound, not these
exact timings or counts.

Queue triggers require schema revision `d1f7a9306e42`. Apply the normal stopped
worker migration before starting this runtime. The preceding revision
`c54f8a72d130` has the same exported row layout; its snapshots remain accepted
by the new restore path. Historical evidence files retain their original
revision labels.

After deployment, compare equal idle windows and windows with the same archive
backlog. Record transactions and statements per second, CPU, disk reads,
database timeout count, receive-to-claim latency and outbox-ready-to-send
latency. Synthetic transaction counts demonstrate queue behavior; they are
not a prediction of production CPU or model response time. The 750-ms SQL
statement limit is not a deadline for an entire model-generated answer.

## References

- [PostgreSQL LISTEN: registration and the initial scan race](https://www.postgresql.org/docs/current/sql-listen.html)
- [PostgreSQL NOTIFY: commit delivery and transaction coalescing](https://www.postgresql.org/docs/current/sql-notify.html)
- [Garmin Connect program and delivery options](https://developer.garmin.com/gc-developer-program/overview/)
- [Garmin Health API and device synchronization](https://developer.garmin.com/gc-developer-program/enterprise/health-api/)
