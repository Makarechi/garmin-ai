# Constrained-host implementation budgets

Status — working limits for design and measurement. Confirm the final deployment budget on
a representative Linux host before R37 acceptance.

## Observations

The pinned Python baseline completed 7,300 history windows with 52,560 intraday measurements
when scheduling and ingestion were interleaved. TimescaleDB reached its 320 MiB container
limit and used about 7.77 MiB swap. The preloaded queue case was sampled at 707 completed jobs.
The 100 MiB FIT case hit the 384 MiB application limit and recorded an out-of-memory kill.
During a smaller concurrent FIT/database write, an operations read, pause and webhook took
about 5.4 seconds. [R02 measurements](../resources.md)

## Working limits

| Component | Implementation target |
| --- | --- |
| Native M2 application | Peak charged memory at most 256 MiB while completing each workload |
| Combined M1 application | Peak charged memory at most 384 MiB including bridge, CLI, MCP and maintenance processes active in the scenario |
| FIT pipeline | At most 64 MiB incremental application memory within the overall cap |
| Small host reserve | Begin with 15–20% of physical RAM pending measurements of the OS and neighboring services |
| Bulk admission | One memory-heavy pipeline in the constrained profile, with up to 500 records and a separate bounded byte allocation per batch |
| Interactive operations | p95 at most 2 seconds for local controls during heavy work, excluding external provider response time |

Use an observed database budget, system reserve and concurrent neighbors to size the final
application cgroup. Treat file cache and swap as separate measurements and capture
`memory.current`, `memory.peak`, `memory.events`, swap, pressure and queue delay. Compare
equivalent workloads through durable writes and complete read results.

Reserve capacity for lease renewal and light operations. An in-flight provider wait occupies
its own admission lane. If safe staging or an operation's byte budget is exhausted,
defer or reject explicitly. Keep the complete accepted history and provenance set in
the release comparison.

## Acceptance

Verify Linux amd64 and arm64 release builds. On a representative small host, repeat the
baseline workloads and report database and application peaks, 10× input scaling, concurrent
control timing, and operation outcomes after interruption. Include the complete process set
that the release installation starts. Record the resulting host budget and soak run in R37.
A budget adjustment requires measurements and a decision before evaluating the release gate.
