# Python resource baseline (R02)

The [raw run](evidence/python-startup-linux-arm64.json) measures a release image built from
`ee3a14a48a6ad30c1085951bac2f53c9088c2450`. The database starts empty. The regular migration,
HTTP server and worker run in separate containers. The worker has no configured external
providers. The HTTP readiness check and worker heartbeat probe run from a separate observer.

The Linux arm64 Docker Desktop VM had 10 CPUs and about 7.75 GiB of memory. The measured
containers shared one CPU and used the PostgreSQL settings, memory limits and swap ceilings
from the GCP Compose profile. These results apply to the stated container setup. The host's
operating-system reserve, optional gateway, production storage and live provider latency are
outside this run.

| Service | Ready after start of run | Median idle cgroup memory | Peak startup or idle cgroup memory |
| --- | ---: | ---: | ---: |
| PostgreSQL / TimescaleDB | 6.20 s | 127.58 MiB | 133.90 MiB |
| Schema migration | 7.30 s | — | 79.02 MiB |
| HTTP API | 10.76 s | 126.60 MiB | 128.03 MiB |
| Worker heartbeat | 11.09 s | 127.35 MiB | 131.16 MiB |

The eight observed API liveness requests took 18.59–19.23 ms from the observer. No sampled
container reported an out-of-memory event. These are measurements of one cold deployment and
its initial idle window. Do not use them to set the Rust admission or release limits.

## Repeat

Use a clean worktree at the pinned SHA and a Linux Docker daemon with cgroup v2. Build from
that worktree and run the measurement script from the same root. The runner verifies the image
revision label, creates a disposable database ending in `_test`, starts and measures each service
on its own internal network, and cleans up its containers and volumes. It uses no host personal
storage or environment file.

```sh
git_sha=$(git rev-parse HEAD)
docker build --label "org.opencontainers.image.revision=$git_sha" -t garmin-ai:benchmark .
python /path/to/migration/measure_startup.py --image garmin-ai:benchmark --sha "$git_sha" > startup.json
```

`memory.current` and `memory.peak` are measured at the cgroup level and include charged file
cache. Process RSS, high-water RSS and PSS are recorded independently where procfs permits.
Process RSS can count shared pages more than once. Component peaks may occur at different
times. The sampling shell and its short-lived commands contribute to the cgroup counters.
The evidence also preserves CPU, throttling, I/O, swap, memory pressure and task counters.

## Paired Python and Rust startup

Build the native image from a clean candidate worktree and label it with that revision.
Pass the two local images to the same runner.

```sh
candidate_sha=$(git rev-parse HEAD)
docker build -f Dockerfile.rust --label "org.opencontainers.image.revision=$candidate_sha" \
  -t garmin-ai-rust:benchmark .
python migration/measure_startup.py \
  --image garmin-ai:benchmark --sha ee3a14a48a6ad30c1085951bac2f53c9088c2450 \
  --candidate-image garmin-ai-rust:benchmark --candidate-sha "$candidate_sha" > startup-pair.json
```

The runner uses fresh databases, identical PostgreSQL settings and the same CPU, memory and
swap limits. Both deployments must reach HTTP readiness and commit a worker heartbeat. The
result records both image IDs and the shared schema revision. The same Python observer probes
HTTP in both runs. It runs outside the measured application containers, as does the native
worker readiness check.

The [Linux ARM64 trial](evidence/python-rust-startup-linux-arm64.json.gz) used the pinned
Python image and a local native runtime image containing the release binary for
`b838708c4286bacea256ff94565fa0a0a333b559`. Each run started empty, reached schema
`c54f8a72d130`, and recorded eight idle samples. No sample showed an OOM kill or swap use.

| Observed startup and idle | Python | Rust |
| --- | ---: | ---: |
| API ready from deployment start | 10.77 s | 8.52 s |
| Worker heartbeat from deployment start | 11.10 s | 8.69 s |
| Peak migration cgroup charge | 92.58 MiB | 10.03 MiB |
| Highest sampled combined API and worker charge | 261.73 MiB | 22.66 MiB |
| Median idle PostgreSQL cgroup charge | 127.52 MiB | 117.83 MiB |

The combined value sums the API and worker `memory.current` readings within each sample.
The readings are sequential and include charged cache.

## Paired web chat under tenfold message volume

Add `--web-chat-messages 80` or `--web-chat-messages 800` to the paired command above.
Each run writes a fresh synthetic conversation through HTTP. Seven messages in every
eight write a 4,000-character note. Each eighth message is 16,000 characters, the
web chat limit. The runner retries a note with the same request ID, checks the 100-message
history limit, and compares all committed note content, counts and audit records. A second
client probes liveness and the authenticated operations endpoint during the writes.

The [80-message](evidence/python-rust-web-chat-80-linux-arm64.json.gz) and
[800-message](evidence/python-rust-web-chat-800-linux-arm64.json.gz) trials used the same
Linux ARM64 images, one shared CPU, PostgreSQL settings and memory limits as the startup
comparison. Both implementations stored the same input text and one response per request.
At 800 messages, both stored 700 notes, 100 maximum-length messages, and 700 create audit
records. The final HTTP page contained 100 messages and their replies.

| Observed workload | Python, 80 | Rust, 80 | Python, 800 | Rust, 800 |
| --- | ---: | ---: | ---: | ---: |
| Highest sampled API and worker charge | 356.74 MiB | 29.81 MiB | 262.25 MiB | 30.47 MiB |
| Highest sampled charge including database | 532.76 MiB | 155.80 MiB | 416.49 MiB | 166.35 MiB |
| Message write latency, p95 | 21.95 ms | 12.52 ms | 29.88 ms | 14.58 ms |
| Liveness latency during writes, p95 | 27.69 ms | 53.27 ms | 5.32 ms | 8.15 ms |
| Operations latency during writes, p95 | 27.98 ms | 8.01 ms | 27.60 ms | 9.76 ms |

No sample showed swap use, a memory-limit event, or an OOM kill. The charges include
cache, and the containers are sampled sequentially. The two message counts ran
against four independent fresh databases, so their startup and cache charges can vary.
The 80-message runs yielded only 10 and 6 liveness probes and 4 and 3 memory samples.
Treat their peaks and percentiles as individual observations.

The first trials use ASCII text in the core-only configuration.

### Four-byte UTF-8

Add `--web-chat-multibyte` to the same workload to fill the request text with
four-byte characters. A 16,000-character message then carries 64,000 UTF-8 bytes of
text. PostgreSQL verifies the full stored value of every maximum-length message,
the UTF-8 digest of the notes, and the same reply and audit counts.

The [80-message](evidence/python-rust-web-chat-utf8-80-linux-arm64.json.gz) and
[800-message](evidence/python-rust-web-chat-utf8-800-linux-arm64.json.gz) trials
used the pinned Python runtime and the same rebuilt native ARM64 package image
for both message counts. The image carries the release executable built from tree
`3bda127f1f3f0fb2ad972568dbea05dac8f7c999`. Both runtimes reached schema
`c54f8a72d130` and stored matching results.

| Observed UTF-8 workload | Python, 80 | Rust, 80 | Python, 800 | Rust, 800 |
| --- | ---: | ---: | ---: | ---: |
| Highest sampled API and worker charge | 275.54 MiB | 26.58 MiB | 292.42 MiB | 26.93 MiB |
| Highest sampled charge including database | 414.73 MiB | 158.06 MiB | 444.03 MiB | 161.83 MiB |
| Message write latency, p95 | 33.06 ms | 66.36 ms | 42.60 ms | 20.96 ms |
| Liveness latency during writes, p95 | 29.49 ms | 39.72 ms | 7.31 ms | 14.16 ms |
| Operations latency during writes, p95 | 27.92 ms | 33.48 ms | 38.05 ms | 14.37 ms |

No sample showed swap use, a memory-limit event, or an OOM kill. The 80-message
trials have 13 and 14 overlapping probes, and four memory samples per runtime.
These results describe the stated local previews. The release comparison also
needs external integrations, background workloads, and a sustained run on the
representative small host.

## Verification boundary

These Linux arm64 Docker runs are the local baseline with the documented service limits.
The final resource budget still needs operating-system reserve, storage saturation and
a sustained run on a representative small host before release.

## FIT projection under concurrent service activity

Run the same command with `--fit-records 5000` or `--fit-records 50000` to add the synthetic FIT
workload. Each size runs against a fresh database, with the API and normal worker still running.
A dedicated application container takes the same 384 MiB memory ceiling as the worker. Its
fixture generates two valid FIT members with timestamps, heart rate and a deliberately unknown
field. It writes the ZIP through the real baseline `store_fit` path, commits the projection and
checks its row count, boundaries, field provenance, archive digest and stored bytes. A stable
digest over every committed projection row enables comparison with later implementations.

| Records | Uncompressed FIT bytes | ZIP bytes | Time to parse and commit | Workload peak |
| ---: | ---: | ---: | ---: | ---: |
| [5,000](evidence/python-fit-5000-linux-arm64.json) | 40,062 | 24,359 | 0.369 s | 83.10 MiB |
| [50,000](evidence/python-fit-50000-linux-arm64.json) | 400,062 | 239,839 | 3.727 s | 161.57 MiB |

The API liveness probe stayed available during the sampled overlap (19.28–20.26 ms for the
50,000-record case). The workload peak includes generation, normalization, commit, projection
verification and its charged file cache. This comparison uses the baseline writer directly.
Separate checks below exercise developer data, a leased FIT job, and the 100 MiB admission limit.


### Paired FIT archive replay

Use `--fit-replay developer`, `--fit-replay ceiling` or `--fit-replay over-limit` with the
paired image command. The observer prepares one archived synthetic export under the previous
parser revision. The ordinary worker claims the resulting replay job. Both runtimes use the
same TimescaleDB settings, one shared CPU and the normal 384 MiB worker limit. The observer
checks the retained ZIP digest and streams the stored projection to compare its digest.

| Input | Python result and sampled application peak | Rust result and sampled application peak |
| --- | --- | --- |
| [50,000 FIT records plus developer metadata](evidence/python-rust-fit-replay-developer-linux-arm64.json.gz) | 50,006 rows, 6.146 s, 379.25 MiB | Same row and archive digests, 2.813 s, 24.29 MiB |
| [Exactly 100 MiB of FIT data](evidence/python-rust-fit-replay-ceiling-linux-arm64.json.gz) | Worker OOM kill, zero rows, 563.52 MiB | 409,600 rows, 127.875 s, 28.23 MiB |
| [One byte above the limit](evidence/python-rust-fit-replay-over-limit-linux-arm64.json.gz) | Rejected with no rows or projection pointer | Same result and original archive digest |

The application peak is the largest sampled sum of API and worker cgroup memory. At the
ceiling, the Rust worker's peak cgroup memory was 18.81 MiB, and sampled worker swap and
recorded memory-limit and OOM events were zero. The API's slowest liveness response in that
trial was 25.292 ms across 149 samples. Queue time includes waiting for the worker and observing
the outcome. The two-minute Rust staging window previously expired during database COPY, so
the candidate uses a finite window that allows the maximum-size projection to finish on one CPU.
A [separate correctness run](evidence/python-rust-fit-replay-ceiling-projection.json) gave the
frozen Python worker a 4 GiB limit and replayed the same file. It committed 409,600 rows with
the same complete projection and archive digests as Rust. The tables above use only the
matched resource limits. The mixed-workload and release soak checks remain separate.

#### Owner controls during maximum-size replay

Add `--controls-during-fit` to `--fit-replay ceiling` for a simultaneous authenticated
web pause, operations read and private Telegram webhook delivery while the worker holds
its active replay lease. Only retry a recognized temporary 503 within two seconds, keeping
the original request identity. The fixture verifies one persisted control and one persisted
Telegram ingress.

In the [Linux ARM64 trial](evidence/python-rust-fit-control-overlap-linux-arm64.json.gz), Rust
replied to the pause in 105.651 ms, to the operations read in 79.975 ms, and accepted the
webhook in 198.047 ms including one retry. The worker renewed the same lease three times and
committed all 409,600 rows. The Python client timed out waiting for all three responses. The
pause and Telegram ingress were recorded later, and that worker hit its memory limit during
replay. These are observed latencies for one concurrent burst.

## Daily source sync and historical jobs

Use `--sync-days 1` to pass synthetic responses through the actual Garmin job dispatcher for
all 29 read paths. The test archives 29 inputs, commits 144 measurements for the day, and
preserves one activity with 125 detail and FIT rows. This run took 0.870 s across the source
jobs, with the transport replaced by a local fixture.

Use `--sync-days 7 --preload-history` to reserve and process seven days of windows.
The 140 windows committed 84 normalized and 56 archive-only payloads, including 1,008
intraday measurement rows. The observed stage took 4.825 s. The default historical workload
now drains the queue between scheduler passes to represent simultaneous scheduling and
ingestion.

The [preloaded year case](evidence/python-year-backlog-partial.json) was sampled and stopped
after 707 of 7,300 jobs. In the [interleaved year run](evidence/python-sync-365-linux-arm64.json.gz),
all 7,300 jobs completed in 297.779 s. TimescaleDB reached 320 MiB charged memory and about
7.77 MiB sampled swap. The workload peaked at 160.38 MiB and committed 52,560 intraday
measurements. All 55 sampled HTTP liveness probes succeeded while the work ran.


### Paired weekly and yearly history imports

Add \`--sync-days 7\` or \`--sync-days 365\` and \`--native-history-test\` to the paired
image command. Point \`--native-history-test\` at the Linux \`endpoint_worker\` test
executable from the same candidate tree as the runtime image. For example,
\`cargo test --locked --release -p ga-app --test endpoint_worker --no-run\` prints
the executable path.

Both runs use the normal history scheduler and endpoint writer. A local fixture
provides the same synthetic input for each of the 20 endpoints. The fixture uses
one shared CPU with 320 MiB for PostgreSQL, 192 MiB for the API, and 384 MiB each
for the worker and the history workload. Both runtimes used schema \`c54f8a72d130\`.
The Rust runtime and endpoint executable were verified against revision
`3688ea3bdf3c356fdf1d7868a71803b6a55643f4`.

| Synthetic history | Identical results | Python time | Rust time | Workload peak, Python / Rust |
| --- | --- | ---: | ---: | ---: |
| [7 days](evidence/python-rust-history-7-linux-arm64.json.gz) | 140 jobs and archive objects, 7 daily rows, 1,008 measurement rows and their revisions | 6.481 s | 1.014 s | 156.77 / 15.93 MiB |
| [365 days](evidence/python-rust-history-365-linux-arm64.json.gz) | 7,300 jobs and archive objects, 365 daily rows, 52,560 measurement rows and their revisions | 425.412 s | 66.538 s | 167.88 / 78.08 MiB |

The comparison checked every archived object's bytes, source links, endpoint
fetch counts and final job outcomes. The saved daily schedule is compared after
normalizing its timestamp representation. Both reports include all plan fields,
including the next history date, the recent date reached, and completion status.
The highest sampled API liveness times during the year were 52.13 ms for Python and
28.34 ms for Rust. The timings include endpoint scheduling and processing.
The workload peak includes charged file cache. These trials used generated
data and durable test storage.

### Paired nightly and weekly reconciliation

Add \`--reconcile-days 7\` or \`--reconcile-days 30\` and point
\`--native-history-test\` at the Linux \`endpoint_worker\` test executable. Each
run schedules the same slot twice and leases the endpoint jobs normally.
The next slot repeats part of the date window so the result also verifies
reuse of identical input. The independent activity scans stay queued while
the test measures the endpoint workload.

The [nightly](evidence/python-rust-reconcile-7-linux-arm64.json.gz) and
[weekly](evidence/python-rust-reconcile-30-linux-arm64.json.gz) trials used
the same PostgreSQL and application limits as the paired history tests, on
one shared Linux ARM64 CPU. The native executable and image came from tree
\`9f0b6e071afbab797949fd02fb87ff058fc42957\`.

| Scheduled period | Jobs in each pass | Unique and reused archive objects | Python passes | Rust passes |
| --- | ---: | ---: | ---: | ---: |
| Nightly, 7 days | 141 | 161 unique, 121 reused | 8.332 s and 4.061 s | 1.049 s and 0.615 s |
| Weekly, 30 days | 601 | 741 unique, 461 reused | 35.139 s and 24.657 s | 6.082 s and 4.925 s |

Both runtimes fetched the same configured endpoints, saved the same archive
bytes and retained matching job outcomes, measurements, revisions, and source
links. The paired jobs completed all 1,152 measurement rows in the nightly
trial and all 5,328 in the weekly trial. Peak workload charges were 118.41
and 127.29 MiB in Python and 16.93 and 25.46 MiB in Rust. Final workload swap
usage was zero. The observer captured successful liveness checks during both
synthetic trials.

## Analytical queries, MCP and backup

| Mode | Workload | Observation |
| --- | --- | --- |
| `--analytics-rounds 10` | 730 daily health rows, 365 headache days and 122 custom observations. Each round runs period bootstrap, 15 lags, migraine controls and custom distribution. | [1.390 s total, 125.05 MiB peak](evidence/python-analytics-linux-arm64.json). Results preserve 23 matched controls and all 122 custom observations. |
| `--mcp-idle-seconds 5` | Open a real stdio MCP client/server, list tools, idle for five seconds and list tools ten more times. | [0.865 s to initial list, 180.10 MiB combined client/server peak](evidence/python-mcp-linux-arm64.json). The 23 reads stay available. |
| `--backup-mib 128` | Authenticated backup and restore of 128 MiB of synthetic bytes and a diary note into a second database. Check an independently capped 8 MiB destination. | [3.176 s to write, 0.393 s to authenticate, unpack and restore](evidence/python-backup-128-linux-arm64.json). The work cgroup reached 384 MiB charged memory. The restricted destination refused the backup, and the normal restore preserved the note ID and exact bytes. |

The workload cgroups include fixture preparation, validation and charged file cache. Compare
the named pipeline timings separately from the peak counters.

### Paired analytical HTTP requests

With both images supplied, `--analytics-rounds 1` and `--analytics-rounds 10`
send the same four authenticated requests to each running API. A separate
client writes the synthetic fixture to each fresh database. The measured query
time starts after seeding. The API and worker retain their normal service limits,
and liveness and operations probes run during the queries.

The [first round](evidence/python-rust-http-analytics-1-linux-arm64.json.gz) and
[ten rounds](evidence/python-rust-http-analytics-10-linux-arm64.json.gz) used the
pinned Python runtime and the same native ARM64 installer preview built from
tree `ac7683415de57a12c66b7a3e45e6e6829802dda7`. Both reached schema `c54f8a72d130`.
Every round returned the same period estimate and confidence interval, all fifteen
lag-pair counts, 23 matched migraine controls, and the same distribution across
122 entries.

| Observed workload | Python, 1 | Rust, 1 | Python, 10 | Rust, 10 |
| --- | ---: | ---: | ---: | ---: |
| Elapsed HTTP query phase | 0.245 s | 0.071 s | 1.837 s | 0.278 s |
| Highest sampled API and worker charge | 376.11 MiB | 23.55 MiB | 275.52 MiB | 23.77 MiB |
| Highest sampled charge including database | 541.80 MiB | 148.36 MiB | 414.06 MiB | 150.20 MiB |
| Successful overlapping probe pairs | 2 | 1 | 14 | 3 |

Each measurement ran on one shared Docker CPU. Memory samples cover fixture
seeding, the query phase and the final reading. No sample showed swap use,
a memory-limit event or an OOM kill. The Rust query phases yielded few overlapping
probes. Sustained query concurrency and small-host operation still need measurement.

### Paired stdio MCP sessions

With both images supplied, `--mcp-idle-seconds 5` and `--mcp-idle-seconds 10`
start the normal read-only MCP command in a separate 384 MiB container. Run the
measurement with the locked Python environment to supply the MCP client. The
client uses Docker stdio and runs outside the measured containers. The API and
worker remain running under their usual limits on the shared Docker CPU.

The [five-second](evidence/python-rust-mcp-idle-5-linux-arm64.json.gz) and
[ten-second](evidence/python-rust-mcp-idle-10-linux-arm64.json.gz) trials used
MCP SDK 1.30.0, the pinned Python runtime and the same native ARM64 preview from
tree `7c541990c34458de4a1ddba5188300555f776512`. Each session listed the same
23 read-only tools before and after idle. Ten warm listings and a catalog request
also completed. On each fresh database the catalog returned an empty page for
the absent raw-only source.

| Observed MCP workload | Python, 5 s | Rust, 5 s | Python, 10 s | Rust, 10 s |
| --- | ---: | ---: | ---: | ---: |
| Initialization and first list | 1.013 s | 0.041 s | 0.992 s | 0.044 s |
| Ten warm listings | 0.105 s | 0.029 s | 0.104 s | 0.031 s |
| Catalog read after idle | 0.052 s | 0.039 s | 0.051 s | 0.032 s |
| Highest sampled MCP container charge | 130.14 MiB | 11.00 MiB | 127.12 MiB | 11.12 MiB |

The startup timings include the Docker stdio connection. Memory includes
the service, its container's idle process and the sampling commands. The
observations showed no swap use, memory-limit event or out-of-memory kill.


### Paired encrypted backup, export and recovery

Add `--backup-mib 32` or `--backup-mib 128` to the paired image command. The fixture
stops the service worker before running the ordinary standalone commands. It seeds a
private source database and archive, and restores to a separate empty database. The API
remains available on its own test store. The API and backup each have a 192 MiB limit,
with one shared CPU and identical TimescaleDB settings for both runtimes.

An 8 MiB mount with a 6 MiB filler leaves exactly 2 MiB of free space. The capacity check
reports `insufficient` and the backup leaves no destination. The normal-volume backup,
standalone export and restore report the same 30 table counts on both runtimes, and the
restored archive digest and seeded diary identity match.

| Synthetic raw file | Python backup / recovery | Rust backup / recovery | Sampled API and backup charge, Python / Rust |
| --- | ---: | ---: | ---: |
| [32 MiB](evidence/python-rust-backup-32-linux-arm64.json.gz) | 1.195 / 1.174 s | 0.597 / 0.115 s | 221.07 / 85.28 MiB |
| [128 MiB](evidence/python-rust-backup-128-linux-arm64.json.gz) | 3.675 / 1.912 s | 3.181 / 1.256 s | 316.11 / 202.46 MiB |

Recovery time covers archive authentication, unpack and database restore. The 128 MiB
runs recorded memory-limit events as the containers reclaimed cache. Python used up to
11.22 MiB of sampled swap, and no sampled Rust backup used swap. Both completed without
an OOM kill. In a preliminary 384 MiB-per-backup trial, the 128 MiB Rust case reached
392.55 MiB including the API, with up to 364.80 MiB of file charge in the backup cgroup.
The tighter profile keeps the full workload within the combined budget.

### Paired offline archive replay

Add `--replay-days 7` or `--replay-days 30` to the paired image command.
The fixture prepares independent archives and databases, then starts each ordinary
worker. It checks every saved archive key, content hash, parser version and status,
and compares the retained daily and intraday projection. One deliberately damaged
object leaves a single error and keeps replay readiness false in both runtimes.

| Window | Matched outcomes | Python / Rust completion time | Python / Rust sampled API and worker peak |
| --- | --- | --- | --- |
| [7 days](evidence/python-rust-offline-replay-7-linux-arm64.json.gz) | 84 normalized, 55 archived, 1 error | 423.099 s / 181.687 s | 267.97 MiB / 25.62 MiB |
| [30 days](evidence/python-rust-offline-replay-30-linux-arm64.json.gz) | 360 normalized, 239 archived, 1 error | 1565.521 s / 699.088 s | 270.45 MiB / 45.10 MiB |

Both runtimes used the same CPU affinity and PostgreSQL configuration within each
comparison. Completion includes ordinary worker startup and queue pacing. The
recorded application samples reported zero swap and zero OOM kills. Each pair
matched both the archive and projection SHA-256 digests.

## Recovery, provider timeout and controls

| Case | Result |
| --- | --- |
| [Nightly reconciliation](evidence/python-reconcile-7-linux-arm64.json) | Two scans of seven days processed 141 jobs each, including the global device fetch. The second scan reused 121 existing raw objects. |
| [Weekly reconciliation](evidence/python-reconcile-30-linux-arm64.json.gz) | Two thirty-day scans processed 601 jobs each. The second scan reused 461 existing raw objects. |
| [Offline replay](evidence/python-replay-linux-arm64.json.gz) | Replayed 599 of 600 archive objects across thirty synthetic days. One corrupted object was refused twice and kept a single error outcome. The observed daily and intraday projection digest stayed equal. |
| [Voice and fallback](evidence/python-voice-linux-arm64.json) | Twice accepted a 20 MiB synthetic clip, simulated a primary model timeout and returned the authorized fallback result. A clip one byte larger was rejected before its download. |
| [FIT developer fields and control overlap](evidence/python-fit-overlap-linux-arm64.json) | Committed 50,000 records and six metadata messages across two ZIP members. Kept the developer name, units and raw value, and kept the raw respiration field. Completed 22 lease renewals while the store ran. |
| [FIT admission ceiling](evidence/python-fit-ceiling-linux-arm64.json.gz) | Rejected the input one byte above 100 MiB. The 100 MiB case entered the decoder, hit the 384 MiB cgroup limit and was killed. The workload exited 137 with one recorded out-of-memory kill. |

The pause request, operations read and webhook started together during the synthetic FIT
write. Each took about 5.4 s and finished after the projection completed. The CLI inventory
took about 0.45 s. The Python write and the pause path use the same PostgreSQL advisory lock.
Use this result to compare the maintenance of policy fences and interactive responsiveness
when moving the FIT write to the proposed bounded pipeline.

The developer-field fixture uses a file ID, developer data ID and field description before its
data messages. [Garmin FIT protocol](https://developer.garmin.com/fit/protocol/)
