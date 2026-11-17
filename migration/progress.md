# Garmin AI Rust migration progress

The compatibility baseline is `ee3a14a48a6ad30c1085951bac2f53c9088c2450`. It
contains the Gemini diagnostics change that merged after the handoff's SHA.
The implementation snapshot below follows `18324600025ff7dd96ad2e500ce8a65f51904ebb`
on main. Test evidence is scoped to the revision, environment and workload
that produced it.

| Task | Evidence on main | Remaining work |
| --- | --- | --- |
| R00 | The frozen Python baseline collected 4,369 tests. The local run passed 4,288, skipped 81, and passed 54 extension cases separately. #215 | Reuse the baseline for the full release comparison. |
| R01 | The acceptance checker inventories 459 surfaces in 25 areas. #216 | Map each ported surface to its differential evidence. |
| R02–R04 | Captured representative Python workloads and established resource and storage decisions. The differential harness covers CLI, HTTP, MCP, archive and SQL observations. #217 #218 #219 #220 #222 #228 | Measure the whole candidate process mix and OS reserve on the representative host. |
| R05–R08 | Linux native workspace and health checks, domain identity and precise JSON, schema-aware database readiness, immutable archive, verified reads and a read-only mount. #221 #223 #224 #225 #226 #227 #267 #273 | Integrate these boundaries across the application. Verify storage lifecycle with restored databases and backups. |
| R09 | Durable priority jobs, independent leases and checked publication transactions. #232 #236 #245 | Compose the worker and scheduler, and exercise service restarts. |
| R10–R12 | Garmin capability matrix, Personal API credentials and bounded responses, legacy owner verification, native login and MFA, all 29 legacy read requests, and cooldown state. #229 #233 #237 #239 #242 #251 | Exercise the authorized account and device matrix. Wire session ownership and the worker to the matching deployment. |
| R13 | Immutable raw JSON, activity documents and summaries, daily and intraday publication, and attested intervals. Nine interval cases matched Python with rollback and revision history. #241 #248 #253 #255 #262 #281 | Connect the actual adapter and finish freshness, endpoint coverage and health history reconstruction. |
| R14–R15 | FIT decoder comparison, raw fields, ZIP and bounded staging. Native projection matched 50,047 records from 15 synthetic inputs and two known errors. A 100 MiB staged run fit a 32 MiB container. Publication and job acknowledgement share a transaction. #230 #234 #243 #247 #249 #257 #266 #272 #277 | Run the complete export pipeline through the worker. Confirm all supported archive encodings and verify the full release workload. |
| R16 | Canonical replay selection, bounded queue and history readiness, verified offline replay inputs, persisted history windows and activity-page transitions. Native scheduling matched 210 jobs in eleven passes. #254 #260 #271 #275 #276 #279 | Complete calendar replay review, reconstruction for intraday samples, activity and FIT replay, and leased dispatch. Verify daily and weekly scheduling and restart behavior. |
| R17 | System diary payloads, exact input, provenance, idempotent create, corrections and atomic audited undo/delete. #235 #240 #244 #250 #258 #265 | Wire native command and API entry points. Test migration against existing diary histories. |
| R18 | Versioned system definitions and metrics, user numeric metrics, and bounded custom schema validation with exact numeric bounds. #246 #252 #256 #268 | Finish custom definition and entry review, then wire registration, activation, retirement and custom metric reads. |
| R19 | Scenario-pack and preference contracts matched the pinned reference. The existing Python setup route reports database commit failure correctly. #261 #264 | Port generated forms, setup preview and confirmation. |
| R20 | Source record and page contracts, atomic cursor and record storage, CSV input and file mapping digest. #263 #269 #274 #280 | Finish JSON reader review and port row mapping, preview, apply and confirmed source-to-metric mappings. |
| R21–R22 | Descriptive and numerical parity checks against NumPy, then archived sleep timing and history-read readiness. #259 #270 #271 | Complete advertised tool coverage, temporal evidence and source freshness. |
| R23–R33 | Native access policy and application entry points are under development. | Complete consent and destination gating, provider and conversation paths, HTTP/MCP/CLI and channels, operational lifecycle and extension compatibility. |
| R34–R40 | The release milestones are open. | Validate native packaging and dependency chain, run full parity and the seven-day soak, rehearse rollback, then perform the authorized cutover. |

Local tests, CI, review and release acceptance are separate evidence. See
[open verification conditions](blockers.md) and the
[compatibility register](deviations.md).
