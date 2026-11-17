# Python compatibility inventory (R01)

[parity.yaml](parity.yaml) records who owns each compatibility area and which Python tests
exercise it. Its JSON syntax is valid YAML 1.2 and lets the checker run with the Python standard
library. The [surface capture](contracts/baseline-surfaces.json) lists the individual routes,
commands, tools, and other contracts that each area owns.

From the repository root, run `python migration/check_parity.py` to compare the mapping with
the inventory and pinned test collection. The report fails on missing, unknown or duplicate
assignments and on references to tests outside the baseline collection. Universal acceptance IDs
retain their exact Python test references. Agent acceptance IDs are requirements from
`tests/evals/acceptance_cases.json`; the IDs do not establish that a model run passed. References
to test files in the parity entries describe existing area coverage. Add scenario-specific Rust
evidence before marking an area verified.

## Reproduce the capture

Install the locked full Python environment from the SHA in `baseline.json`. Run the collector
from the root of that pinned checkout using the copy of `collect_inventory.py` from this
directory. Direct output to a separate file and compare it with the checked-in capture.

```sh
uv run --no-sync python /path/to/migration/collect_inventory.py > test-results/baseline-surfaces.json
```

The collector reads code registrations and static declarations in a clean environment. It
intercepts API bootstrap before the first database operation and executes only local CLI help,
argument validation and inventory. Its output contains no environment values or stored data.

## Registration and policy details

- The API registers the browser asset routes and web chat from separate modules. Static routes
  are absent from the internal OpenAPI schema. The application does not publish a schema URL.
  The Garmin auth routes are registered even when the auth flow is disabled. The Docker
  deployment may separately publish them through Caddy.
- Route dependencies are recorded as declared. Additional checks live inside the handlers.
  The Telegram webhook verifies its own secret, Garmin auth checks Telegram init data on flow
  requests, and per-token scopes for `/tools/{name}` depend on the requested tool and arguments.
  `resource_catalog` and `generic_analysis` can narrow permissions after validating input.
- The CLI parser registers four file-taking operational commands in a loop. It also accepts the
  hidden `pair-telegram --container-runtime` flag called by the installer. The inventory includes
  both. Test the result shapes and side effects with isolated state in R03 and the owning tasks.
- MCP exposes the read registry by default. Four writes require explicit opt-in. The HTTP scope
  list beside the matching read tool names describes HTTP access. For MCP, the local process
  boundary and per-tool diary/health access rules still apply.
- Telegram supports callback data generated at runtime, including stored action tokens,
  tracker actions, history navigation and caffeine presets. Literal commands and prefixes alone
  cannot establish the stale-action, revision, owner or delivery behavior.
- Provider discovery combines five built-in registrations with explicitly configured
  `garmin_ai.integrations` entry points. Three synthetic examples cover source, channel and model
  extensions. Arbitrary user-installed providers and user-created tracker definitions are
  instance data, with compatibility tracked under their versioned contracts. The
  `GA_PLUGIN_` entry describes the allowed credential-reference prefix.
- The database table list was checked against the public schema of a disposable TimescaleDB
  database upgraded to the baseline Alembic head. Extension-internal catalogs are outside this
  application table list. R07 will capture the schema and row-level contracts.
