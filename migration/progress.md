# Garmin AI Rust migration progress

The compatibility baseline is `ee3a14a48a6ad30c1085951bac2f53c9088c2450`. It contains the Gemini
diagnostics change that merged after the SHA cited in the handoff. Follow the dependency
order in the approved migration plan. Recheck the current main SHA before extending the port.

| Task | State | Evidence and next action |
| --- | --- | --- |
| R00 | Merged in #215 | 4,369 collected tests. Local container run passed 4,288, skipped 81, and separately passed 54 extension cases. Baseline CI recorded one timing failure in the Telegram check-in test; #215 passed all checks and clean review. |
| R01 | Inventory captured | Review #216. The checker assigns 459 surfaces to 25 areas and checks both acceptance manifests. Update the mapping as Rust test evidence lands. |
| R02 | In progress | #217 covers cold startup and idle. Working branches capture two scales of FIT projection and the Garmin day and history paths. Complete the remaining workloads, representative host measurements and budgets. |
| R03 | In progress | #218 supplies a differential comparator and Python CLI, HTTP, MCP, archive and SQL observations. A follow-up has two matching captures of offline replay after interrupted ingest. Extend supporting scenarios and compare with the Rust implementation as its entrypoints land. |
| R04 | In progress | Storage/boundary and initial digest decisions are recorded. Finalize dependency direction, target platforms, resource goals, evidence gates and any changes to baseline behavior before R05. |
| R05–R40 | Pending | Start the workspace and first native primitives when their prerequisites are reviewed. Pull Garmin authentication and FIT feasibility forward once their stated dependencies allow it. |

A local pass, an automated check, a clean review and production acceptance are different
observations. The evidence follows the precise application revision, data, platform and
check that produced it. Record an approved change to observable behavior in the deviation
register before moving a contract out of parity.
