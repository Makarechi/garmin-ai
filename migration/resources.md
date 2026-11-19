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

Use the paired result for cold migration, startup and idle memory. The existing Python
workload options run independently. The release still needs paired workload measurements,
the small-host reserve and the sustained run described below.

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


## Analytical queries, MCP and backup

| Mode | Workload | Observation |
| --- | --- | --- |
| `--analytics-rounds 10` | 730 daily health rows, 365 headache days and 122 custom observations. Each round runs period bootstrap, 15 lags, migraine controls and custom distribution. | [1.390 s total, 125.05 MiB peak](evidence/python-analytics-linux-arm64.json). Results preserve 23 matched controls and all 122 custom observations. |
| `--mcp-idle-seconds 5` | Open a real stdio MCP client/server, list tools, idle for five seconds and list tools ten more times. | [0.865 s to initial list, 180.10 MiB combined client/server peak](evidence/python-mcp-linux-arm64.json). The 23 reads stay available. |
| `--backup-mib 128` | Authenticated backup and restore of 128 MiB of synthetic bytes and a diary note into a second database. Check an independently capped 8 MiB destination. | [3.176 s to write, 0.393 s to authenticate, unpack and restore](evidence/python-backup-128-linux-arm64.json). The work cgroup reached 384 MiB charged memory. The restricted destination refused the backup, and the normal restore preserved the note ID and exact bytes. |

The workload cgroups include fixture preparation, validation and charged file cache. Compare
the named pipeline timings separately from the peak counters.


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
