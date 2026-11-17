# Compatible storage and application boundaries

Status — accepted as the implementation direction for the pinned Python baseline. Release budgets
remain open until R02 covers the required workloads.

## Application and data

PostgreSQL/TimescaleDB and the existing migration chain are the storage contract. Begin with
read compatibility at Alembic revision `c54f8a72d130`. Port live operations as transaction-sized
units including audit, projections and durable receipts. Keep persistent UUIDs, definition
versions, source provenance and operation states intact. Require a compatible revision before
readiness. Record every additional schema change and demonstrate both upgrade and recovery.

Keep one application with a composition root and bounded background work. The preferred small
host profile combines HTTP and work dispatch in one Rust process. Preserve the independent
`serve`, `worker`, `mcp` and administrative command entrypoints. Measure stdio MCP and maintenance
processes within the system budget. Keep PostgreSQL separate.

| Layer | Allowed responsibility |
| --- | --- |
| Domain | Typed identities, time, values and validation, without SQL or transport |
| Analytics | Deterministic, bounded calculations over domain data |
| Application | Operations, explicit permissions, deadlines, transaction orchestration and ports |
| Adapters | PostgreSQL, archive, Garmin, FIT, channels, providers and extension boundaries |
| Composition | CLI, runtime, HTTP and MCP integration, startup, shutdown and readiness |

Use Rust tooling to check dependency direction. Start with the boundaries that the first testable
slice uses. Add the other crates as their responsibilities land. Decide exact toolchain and
dependency features in R05 build and test evidence. Preserve stable public schemas separately
from Rust domain structures. Reuse coarse-grained ports for deterministic clocks, IDs,
providers and transport fixtures.

## Execution and safety

Move owner, actor, consent, projection generation and budget into explicit operation context.
Keep event time, app knowledge time and source fetch time independent. Preserve permission and
revision checks at the point of commit, dispatch and acknowledgement. Maintain the delivery
and storage fences through their existing protection intervals.

Bound work by bytes, samples, concurrency and time. Heavy jobs leave light owner controls,
heartbeat and lease renewal available. A transaction can claim only the work it owns, and an
uncertain connection with a held session lock cannot return to the pool. Record lock order and
dry-run transfer of writer ownership before switching deployment.

## Evidence for release

The pinned Python code and persisted contracts drive characterization. When they conflict with
documentation or correctness, reproduce the difference and record the decision and acceptance
test in the deviation register. Test the resulting HTTP, CLI, MCP, query, diary, archive and
operational behavior with independently restored synthetic state.

M1 requires storage, core, integration, recovery, resource and cutover evidence. An explicitly
declared Python bridge may temporarily implement a missing native integration. Its owner,
memory cost, failure behavior and removal milestone belong in the release manifest. M2 adds
a standard runtime and lifecycle that operate without Python, including Garmin login, FIT,
migrations, healthchecks and backup/restore. An optional extension compatibility runner is
counted in the installation that enables it.

Verify Linux amd64 and arm64 builds first, and test the actual constrained deployment before
promoting a release. Installation support on other platforms and the resource budgets are
decisions for measured evidence. Applicable live integration checks and production
authorization remain tied to the actual release and its target.
