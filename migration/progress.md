# Garmin AI Rust migration progress

The compatibility baseline is `ee3a14a48a6ad30c1085951bac2f53c9088c2450`. It contains the Gemini
diagnostics change that merged after the SHA cited in the handoff. Follow the dependency
order in the approved migration plan. Recheck the current main SHA before extending the port.

| Task | State | Evidence and next action |
| --- | --- | --- |
| R00 | Merged in #215 | 4,369 collected tests. Local container run passed 4,288, skipped 81, and separately passed 54 extension cases. Baseline CI recorded one timing failure in the Telegram check-in test; #215 passed all checks and clean review. |
| R01 | Merged in #216 | The checker assigns 459 surfaces to 25 areas and checks both acceptance manifests. Update the mapping as Rust test evidence lands. |
| R02 | Container baseline merged in #217 and #222 | Startup, sync, replay, FIT, analytics, voice, backup and concurrent controls are measured. Confirm the operating-system reserve and full process mix on the representative host in R37. |
| R03 | Harness merged in #218 and #220 | Differential CLI, HTTP, MCP, archive and SQL observations include replay after interruption and A→B→A. Extend the cases and candidate coverage with each ported path. |
| R04 | Initial decisions merged in #219 | Storage, dependency directions, rollout gates and legacy digests are recorded. Working constrained-host budgets appear in ADR 0002. Revisit them with the representative-host evidence. |
| R05 | Workspace and liveness merged in #221 and #223 | Native Linux CI, advisory checks and HTTP liveness are available. The domain boundary guard is in review with the first domain crate. |
| R06–R08 | In review | JSON compatibility, identity/time/text offsets, database readiness and filesystem archive changes are in #224–#227. |
| R09–R40 | Pending | Continue durable runtime, Garmin authentication and the FIT comparison as prerequisites land. |

A local pass, an automated check, a clean review and production acceptance are different
observations. The evidence follows the precise application revision, data, platform and
check that produced it. Record an approved change to observable behavior in the deviation
register before moving a contract out of parity.
