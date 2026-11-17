# Python startup and idle resource baseline (R02, part 1)

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

## Remaining resource scenarios

R02 also needs MCP startup, ingestion and reconciliation, scaled real FIT and ZIP, offline
replay, long analytics, simulated voice and provider latency, backup/restore and low disk, and
overlap with interactive work. Capture comparable output and final state for each scenario
alongside timing and resource metrics. Validate operating-system reserve and storage saturation
on a representative small host before assigning release limits.

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
It leaves out Garmin transport and the worker queue, and does not cover the 100 MiB input
ceiling, developer-field data, or a long-running deployment.
