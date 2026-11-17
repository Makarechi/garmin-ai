# Garmin AI Rust migration progress

The compatibility baseline is `ee3a14a48a6ad30c1085951bac2f53c9088c2450`. It
contains the Gemini diagnostics change that merged after the handoff's SHA.
The implementation snapshot below follows `82250bd0d7a4fc898c71fa6628143ba608597e34`
on main. Test evidence is scoped to the revision, environment and workload
that produced it.

| Task | Evidence on main | Remaining work |
| --- | --- | --- |
| R00 | The frozen Python baseline collected 4,369 tests. The local run passed 4,288, skipped 81, and passed 54 extension cases separately. #215 | Reuse the baseline for the full release comparison. |
| R01 | The acceptance checker inventories 459 surfaces in 25 areas. #216 | Map each ported surface to its differential evidence. |
| R02–R04 | Captured representative Python workloads and established resource and storage decisions. The differential harness covers CLI, HTTP, MCP, archive and SQL observations. #217 #218 #219 #220 #222 #228 | Measure the whole candidate process mix and OS reserve on the representative host. |
| R05–R08 | Linux native workspace and health checks, domain identity and precise JSON, schema-aware database readiness, immutable archive, verified reads and a read-only mount. #221 #223 #224 #225 #226 #227 #267 #273 | Integrate these boundaries across the application. Verify storage lifecycle with restored databases and backups. |
| R09 | Durable priority jobs, independent leases and checked publication transactions. Endpoint archival and publication enforce the live job lease separately. #232 #236 #245 #292 #299 | Compose and supervise all worker lanes and exercise service restarts and producer delays. |
| R10–R12 | Garmin capability matrix, Personal API credentials and bounded responses, legacy owner verification, native login and MFA, all 29 legacy read requests, and cooldown state. #229 #233 #237 #239 #242 #251 | Exercise the authorized account and device matrix. Wire session ownership and the worker to the matching deployment. |
| R13 | Immutable raw JSON, activity documents and summaries, daily and intraday publication, and attested intervals. Endpoint capture and publication preserve the owner, accepted source and previous freshness on failure. #241 #248 #253 #255 #262 #281 #292 #299 | Finish worker and transport integration for all endpoint families and verify complete workloads. |
| R14–R15 | FIT decoder comparison, raw fields, ZIP and bounded staging. Native projection matched 50,047 records from 15 synthetic inputs and two known errors. A 100 MiB staged run fit a 32 MiB container. Publication and job acknowledgement share a transaction. #230 #234 #243 #247 #249 #257 #266 #272 #277 | Run the complete export pipeline through the worker. Confirm all supported archive encodings and verify the full release workload. |
| R16 | Canonical replay selection and readiness, persisted history windows and activity scans, and verified calendar and intraday reconstruction. Calendar and intraday fixtures covered tied and retained owners, damaged archives, and late rollback. History status matched 7,300 windows. #254 #260 #271 #275 #276 #279 #282 #284 #291 #294 | Complete scoped replay dispatch, activity and FIT recovery, and the daily and weekly scheduler and restart checks. |
| R17 | System diary payloads, exact input, provenance, idempotent create, corrections and atomic audited undo/delete. The owner diary read applies recorded version permissions before paging. #235 #240 #244 #250 #258 #265 #298 | Wire native command and API entry points. Test migration against existing diary histories. |
| R18 | Versioned definitions and metrics, custom schema validation with exact numeric bounds, definition registration and retirement, and version-aware custom entries with audit and projections. #246 #252 #256 #268 #278 #287 #290 | Wire native list, definition and entry interfaces and verify supported metric reads. |
| R19 | Scenario-pack and preference contracts matched the pinned reference. The existing Python setup route reports database commit failure correctly. #261 #264 | Port generated forms, setup preview and confirmation. |
| R20 | Source record and page contracts, atomic cursor and record storage, CSV and JSON readers, and the file mapping digest. #263 #269 #274 #280 #283 | Finish row mapping, preview, apply and confirmed source-to-metric mappings. Close the JSON nesting difference recorded in the compatibility register. |
| R21–R22 | Descriptive and numerical parity checks, archive-verified sleep timing, daily reads, observed freshness, knowledge-cutoff queries, activity pages and a combined freshness report. A 6,000-result temporal batch ran within a 32 MiB limit. #259 #270 #271 #288 #289 #295 #296 #297 #298 | Complete advertised analytics, timeline queries, and full retrospective coverage. |
| R23 | Scoped bearer checks and capability discovery, exact version and recipient consent, and guarded delivery with cancellation. The consent test matched 30 decisions and verified lock behavior. #286 #301 | Wire model and channel filters, onboarding, consent revocation and all sending paths. |
| R24–R27 | The provider, conversation, neutral inbox/outbox and adapter milestones are open. | Wire native provider contracts and requests, persisted outcomes and fallback, then port the conversation, delivery and Telegram paths. |
| R28–R33 | Two native HTTP health tools use the pinned schemas and require a verified read. Thirty-four responses matched the Python HTTP service, including archive maintenance and scopes. #293 | Port remaining HTTP, dashboard, MCP, wearable, initiative, extension and operational paths. |
| R34–R40 | The release milestones are open. | Validate native packaging and dependency chain, run full parity and the seven-day soak, rehearse rollback, then perform the authorized cutover. |

Local tests, CI, review and release acceptance are separate evidence. See
[open verification conditions](blockers.md) and the
[compatibility register](deviations.md).
